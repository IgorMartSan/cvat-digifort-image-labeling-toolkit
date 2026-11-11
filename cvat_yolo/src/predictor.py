from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

from cvat_sdk import models
from cvat_sdk.core.proxies.annotations import AnnotationUpdateAction

from .annotations import _value, shapes_from
from .config import ROOT
from .frames import get_frame_bytes


def resolve_label_mapping(source: dict[int, str], destination: list[Any], choose: Any) -> dict[int, int]:
    by_name: dict[str, list[int]] = {}
    for label in destination:
        by_name.setdefault(str(label.name).strip().casefold(), []).append(int(label.id))
    result: dict[int, int] = {}
    for source_id, name in source.items():
        matches = by_name.get(name.strip().casefold(), [])
        if len(matches) == 1:
            result[source_id] = matches[0]
        else:
            if matches:
                target = choose([label for label in destination if int(label.id) in matches], f"Label destino ambígua para {name}:")
            else:
                target = choose(destination, f"Mapear a classe '{name}' para qual label CVAT?")
            result[source_id] = int(target.id)
    return result


def existing_annotations(task: Any) -> tuple[set[int], dict[int, list[int]], bool]:
    annotations = task.get_annotations()
    protected = set()
    automatic: dict[int, list[int]] = {}
    for shape in shapes_from(annotations):
        frame = int(_value(shape, "frame", -1))
        if str(_value(shape, "source", "manual")).lower() == "auto":
            automatic.setdefault(frame, []).append(int(_value(shape, "id")))
        else:
            protected.add(frame)
    protected.update(int(_value(tag, "frame", -1)) for tag in (_value(annotations, "tags", []) or []))
    tracks = list(_value(annotations, "tracks", []) or [])
    # Track interpolation spans frames beyond explicit keyframes; skip the task
    # rather than risk adding a duplicate where its effective annotation exists.
    return protected, automatic, bool(tracks)


def clear_annotations(task: Any) -> int:
    """Remove all shapes, tags and tracks from a task before a full re-prediction."""
    annotations = task.get_annotations()
    ids = [int(_value(item, "id")) for kind in ("shapes", "tags", "tracks")
           for item in (_value(annotations, kind, []) or []) if _value(item, "id") is not None]
    if ids:
        task.remove_annotations(ids=ids)
    return len(ids)


def predict_task(task: Any, model: Any, mode: str, class_map: dict[int, int], threshold: float,
                 overwrite_auto: bool = False, recreate_all: bool = False) -> dict[str, int]:
    from PIL import Image
    import io

    meta = task.get_meta()
    frame_count = int(_value(meta, "size", 0))
    if recreate_all:
        removed = clear_annotations(task)
        print(f"  Anotações removidas da task {task.id}: {removed}")
    protected_frames, automatic_frames, has_tracks = existing_annotations(task)
    if has_tracks:
        return {"processed": 0, "objects": 0, "sent": 0, "failed": 0, "skipped": frame_count}
    totals = {"processed": 0, "objects": 0, "sent": 0, "failed": 0, "skipped": 0}
    for frame in range(frame_count):
        if frame in protected_frames or (frame in automatic_frames and not overwrite_auto):
            totals["skipped"] += 1
            continue
        try:
            raw = get_frame_bytes(task, frame)
            image = Image.open(io.BytesIO(raw)).convert("RGB")
            width, height = image.size
            results = model.predict(source=image, conf=threshold, verbose=False)
            shapes = []
            detected = 0
            for result in results:
                if result.boxes is None:
                    continue
                for index, box in enumerate(result.boxes):
                    source_id = int(box.cls.item())
                    if source_id not in class_map:
                        raise ValueError(f"Classe do modelo sem mapeamento: {source_id}")
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
                    shapes.append(models.LabeledShapeRequest(
                        frame=frame, label_id=label_id, type=shape_type, points=points,
                        occluded=False, outside=False, z_order=0, rotation=0.0,
                        source="auto", score=float(box.conf.item()), attributes=[]))
                    detected += 1
            totals["processed"] += 1
            totals["objects"] += detected
            if shapes:
                request = models.PatchedLabeledDataRequest(shapes=shapes)
                task.update_annotations(request, action=AnnotationUpdateAction.CREATE)
                totals["sent"] += len(shapes)
                if frame in automatic_frames and overwrite_auto:
                    task.remove_annotations(ids=automatic_frames[frame])
            if totals["processed"] % 50 == 0:
                print(f"  [{totals['processed']}/{frame_count}] Objetos acumulados: {totals['objects']}")
        except Exception as exc:
            totals["failed"] += 1
            print(f"Task {task.id}, frame {frame}: {exc}")
    return totals


def log_prediction(task_ids: list[int], model_name: str, summary: dict[str, Any]) -> None:
    log_dir = ROOT / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / "predictions.jsonl"
    record = {"timestamp": datetime.now(timezone.utc).isoformat(), "task_ids": task_ids,
              "model": model_name, **summary}
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, ensure_ascii=False) + "\n")
