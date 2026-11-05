from __future__ import annotations

import io
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from cvat_sdk import models
from cvat_sdk.core.proxies.annotations import AnnotationUpdateAction
from PIL import Image

from app.core.base_framework import BasePredictor
from app.core.config import LOGS_DIR
from app.frameworks.yolo.annotations import _value, shapes_from
from app.frameworks.yolo.dataset import get_cached_frame_bytes


def existing_annotations(task: Any) -> Tuple[Set[int], Dict[int, List[int]], bool]:
    annotations = task.get_annotations()
    protected = set()
    automatic: Dict[int, List[int]] = {}

    for shape in shapes_from(annotations):
        frame = int(_value(shape, "frame", -1))
        if str(_value(shape, "source", "manual")).lower() == "auto":
            automatic.setdefault(frame, []).append(int(_value(shape, "id")))
        else:
            protected.add(frame)

    protected.update(int(_value(tag, "frame", -1)) for tag in (_value(annotations, "tags", []) or []))
    tracks = list(_value(annotations, "tracks", []) or [])
    return protected, automatic, bool(tracks)


class YoloPredictor(BasePredictor):
    @property
    def name(self) -> str:
        return "yolo11"

    @property
    def supported_modes(self) -> List[str]:
        return ["detection", "segmentation"]

    def load_model(self, checkpoint_path: Path) -> Any:
        from ultralytics import YOLO

        if not checkpoint_path.is_file():
            raise FileNotFoundError(f"Checkpoint não encontrado: {checkpoint_path}")
        return YOLO(str(checkpoint_path))

    def predict_frame(
        self,
        model: Any,
        image_bytes: bytes,
        mode: str,
        confidence: float,
    ) -> List[Dict[str, Any]]:
        image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        results = model.predict(source=image, conf=confidence, verbose=False)
        detected = []

        for result in results:
            if result.boxes is None:
                continue
            for index, box in enumerate(result.boxes):
                source_id = int(box.cls.item())
                class_name = str(result.names.get(source_id, source_id))
                score = float(box.conf.item())

                if mode == "detection":
                    xyxy = box.xyxy[0].tolist()
                    detected.append({
                        "class_index": source_id,
                        "class_name": class_name,
                        "confidence": score,
                        "type": "rectangle",
                        "points": [float(v) for v in xyxy],
                    })
                else:
                    if result.masks is None or index >= len(result.masks.xy):
                        continue
                    polygon = result.masks.xy[index].reshape(-1).tolist()
                    if len(polygon) < 6:
                        continue
                    detected.append({
                        "class_index": source_id,
                        "class_name": class_name,
                        "confidence": score,
                        "type": "polygon",
                        "points": [float(v) for v in polygon],
                    })
        return detected


def predict_task(
    task: Any,
    model: Any,
    mode: str,
    class_map: Dict[int, int],
    threshold: float,
    overwrite_auto: bool = False,
    progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> Dict[str, int]:
    meta = task.get_meta()
    frame_count = int(_value(meta, "size", 0))
    protected_frames, automatic_frames, has_tracks = existing_annotations(task)

    if has_tracks:
        if progress_callback:
            progress_callback({
                "type": "task_skipped",
                "task_id": int(task.id),
                "reason": "Tarefa possui tracks interpolados e foi ignorada por segurança.",
            })
        return {"processed": 0, "objects": 0, "sent": 0, "failed": 0, "skipped": frame_count}

    totals = {"processed": 0, "objects": 0, "sent": 0, "failed": 0, "skipped": 0}

    for frame in range(frame_count):
        if frame in protected_frames or (frame in automatic_frames and not overwrite_auto):
            totals["skipped"] += 1
            if progress_callback and frame % 10 == 0:
                progress_callback({
                    "type": "frame",
                    "task_id": int(task.id),
                    "frame": frame + 1,
                    "total_frames": frame_count,
                    "totals": totals,
                })
            continue

        try:
            raw = get_cached_frame_bytes(task, frame)
            image = Image.open(io.BytesIO(raw)).convert("RGB")
            results = model.predict(source=image, conf=threshold, verbose=False)
            shapes = []
            detected = 0

            for result in results:
                if result.boxes is None:
                    continue
                for index, box in enumerate(result.boxes):
                    source_id = int(box.cls.item())
                    if source_id not in class_map:
                        continue
                    label_id = class_map[source_id]

                    if mode == "detection":
                        xyxy = box.xyxy[0].tolist()
                        shape_type, points = "rectangle", [float(v) for v in xyxy]
                    else:
                        if result.masks is None or index >= len(result.masks.xy):
                            continue
                        polygon = result.masks.xy[index].reshape(-1).tolist()
                        if len(polygon) < 6:
                            continue
                        shape_type, points = "polygon", [float(v) for v in polygon]

                    shapes.append(
                        models.LabeledShapeRequest(
                            frame=frame,
                            label_id=label_id,
                            type=shape_type,
                            points=points,
                            occluded=False,
                            outside=False,
                            z_order=0,
                            rotation=0.0,
                            source="auto",
                            score=float(box.conf.item()),
                            attributes=[],
                        )
                    )
                    detected += 1

            totals["processed"] += 1
            totals["objects"] += detected

            if shapes:
                request = models.PatchedLabeledDataRequest(shapes=shapes)
                task.update_annotations(request, action=AnnotationUpdateAction.CREATE)
                totals["sent"] += len(shapes)
                if frame in automatic_frames and overwrite_auto:
                    task.remove_annotations(ids=automatic_frames[frame])

            if progress_callback:
                progress_callback({
                    "type": "frame",
                    "task_id": int(task.id),
                    "frame": frame + 1,
                    "total_frames": frame_count,
                    "totals": totals,
                })

        except Exception as exc:
            totals["failed"] += 1
            if progress_callback:
                progress_callback({
                    "type": "error",
                    "task_id": int(task.id),
                    "frame": frame + 1,
                    "error": str(exc),
                })

    return totals


def log_prediction_run(task_ids: List[int], model_name: str, summary: Dict[str, Any]) -> None:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    path = LOGS_DIR / "predictions.jsonl"
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "task_ids": task_ids,
        "model": model_name,
        **summary,
    }
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, ensure_ascii=False) + "\n")
