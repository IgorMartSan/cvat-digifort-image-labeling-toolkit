from __future__ import annotations

from typing import Any

from .annotations import _value, shapes_from
from .frames import get_frame_bytes


def collect_task(task: Any, mode: str, derive_polygons: bool = True) -> list[dict[str, Any]]:
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
    result = []
    for frame in range(frame_count):
        objects = by_frame.get(frame, [])
        if not objects:
            continue
        result.append({"task_id": int(task.id), "frame": frame, "objects": objects,
                       "image_bytes": get_frame_bytes(task, frame)})
    return result
