from __future__ import annotations

from typing import Any

from .annotations import _value, shapes_from
from .frames import get_frame_bytes


def collect_task(task: Any, mode: str, derive_polygons: bool = True, include_empty: bool = False) -> list[dict[str, Any]]:
    """Download frames and convert compatible CVAT shapes to simple records."""
    annotations = task.get_annotations()
    by_frame: dict[int, list[dict[str, Any]]] = {}
    for shape in shapes_from(annotations):
        kind = str(_value(shape, "type", "")).lower()
        if kind not in {"rectangle", "polygon"}:
            continue
        if (mode == "detection" and (kind == "rectangle" or derive_polygons)) or (mode == "segmentation" and kind == "polygon"):
            by_frame.setdefault(int(_value(shape, "frame", 0)), []).append({
                "type": kind,
                "label_id": int(_value(shape, "label_id")),
                "points": _value(shape, "points", []),
            })
    meta = task.get_meta()
    frame_count = int(_value(meta, "size", 0))
    deleted_frames = set(_value(meta, "deleted_frames", []) or [])
    result = []
    selected_count = 0
    active_frames = [f for f in range(frame_count) if f not in deleted_frames]
    for frame in active_frames:
        objects = by_frame.get(frame, [])
        if not objects and not include_empty:
            continue
        selected_count += 1
        if selected_count == 1 or selected_count % 10 == 0 or (active_frames and frame == active_frames[-1]):
            print(f"Baixando task {task.id}: {selected_count} imagens selecionadas", flush=True)
        result.append({"task_id": int(task.id), "frame": frame, "objects": objects,
                       "image_bytes": get_frame_bytes(task, frame)})
    return result
