from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class DatasetStats:
    images: int
    annotated_images: int
    empty_images: int
    rectangles: int
    polygons: int
    labels: Dict[str, int]
    incompatible: Dict[str, int]


def _value(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def shapes_from(annotations: Any) -> List[Any]:
    return list(_value(annotations, "shapes", []) or [])


def normalize_points(points: Any) -> List[float]:
    if isinstance(points, str):
        values = [float(part) for part in points.replace(",", " ").split()]
    else:
        values = [float(part) for part in points]
    if len(values) < 4 or len(values) % 2:
        raise ValueError("A shape precisa de pelo menos dois pares de coordenadas.")
    return values


def polygon_to_bbox(points: Any) -> Tuple[float, float, float, float]:
    values = normalize_points(points)
    xs = values[0::2]
    ys = values[1::2]
    return min(xs), min(ys), max(xs), max(ys)


def rectangle_to_yolo(points: Any, width: int, height: int) -> Tuple[float, float, float, float]:
    x1, y1, x2, y2 = normalize_points(points)[:4]
    left, right = sorted((max(0.0, min(float(width), x1)), max(0.0, min(float(width), x2))))
    top, bottom = sorted((max(0.0, min(float(height), y1)), max(0.0, min(float(height), y2))))
    if right <= left or bottom <= top:
        raise ValueError("Bounding box vazio ou fora da imagem.")
    return ((left + right) / (2 * width), (top + bottom) / (2 * height), (right - left) / width, (bottom - top) / height)


def polygon_to_yolo(points: Any, width: int, height: int) -> List[float]:
    values = normalize_points(points)
    if len(values) < 6:
        raise ValueError("Um polígono precisa de pelo menos três pontos.")
    result: List[float] = []
    for index, value in enumerate(values):
        limit = width if index % 2 == 0 else height
        result.append(max(0.0, min(float(limit), value)) / limit)
    return result


def analyze_annotations(annotations: Any, frame_count: int, label_names: Dict[int, str]) -> DatasetStats:
    shapes = shapes_from(annotations)
    frames = {int(_value(shape, "frame", -1)) for shape in shapes}
    labels: Counter[str] = Counter()
    incompatible: Counter[str] = Counter()
    rectangles = polygons = 0

    for shape in shapes:
        kind = str(_value(shape, "type", "unknown")).lower()
        label_id = int(_value(shape, "label_id", -1))
        labels[label_names.get(label_id, f"ID {label_id}")] += 1
        if kind == "rectangle":
            rectangles += 1
        elif kind == "polygon":
            polygons += 1
        else:
            incompatible[kind] += 1

    for key in ("tracks", "tags"):
        values = list(_value(annotations, key, []) or [])
        if values:
            incompatible[key] += len(values)

    return DatasetStats(
        images=frame_count,
        annotated_images=len(frames),
        empty_images=max(0, frame_count - len(frames)),
        rectangles=rectangles,
        polygons=polygons,
        labels=dict(labels),
        incompatible=dict(incompatible),
    )
