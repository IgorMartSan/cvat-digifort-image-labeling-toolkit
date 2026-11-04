from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any


@dataclass
class DatasetStats:
    images: int
    annotated_images: int
    empty_images: int
    rectangles: int
    polygons: int
    labels: Counter[str]
    incompatible: Counter[str]


def _value(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def shapes_from(annotations: Any) -> list[Any]:
    return list(_value(annotations, "shapes", []) or [])


def analyze_annotations(annotations: Any, frame_count: int, label_names: dict[int, str]) -> DatasetStats:
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
    # Tracks, tags, masks and other CVAT annotation forms cannot safely be
    # interpreted as independent YOLO instances by this converter.
    for key in ("tracks", "tags"):
        values = list(_value(annotations, key, []) or [])
        if values:
            incompatible[key] += len(values)
    return DatasetStats(frame_count, len(frames), max(0, frame_count - len(frames)), rectangles, polygons, labels, incompatible)


def normalize_points(points: Any) -> list[float]:
    if isinstance(points, str):
        values = [float(part) for part in points.replace(",", " ").split()]
    else:
        values = [float(part) for part in points]
    if len(values) < 4 or len(values) % 2:
        raise ValueError("A shape precisa de pelo menos dois pares de coordenadas.")
    return values


def rectangle_to_yolo(points: Any, width: int, height: int) -> tuple[float, float, float, float]:
    x1, y1, x2, y2 = normalize_points(points)[:4]
    left, right = sorted((max(0.0, min(float(width), x1)), max(0.0, min(float(width), x2))))
    top, bottom = sorted((max(0.0, min(float(height), y1)), max(0.0, min(float(height), y2))))
    if right <= left or bottom <= top:
        raise ValueError("Bounding box vazio ou fora da imagem.")
    return ((left + right) / (2 * width), (top + bottom) / (2 * height), (right - left) / width, (bottom - top) / height)


def polygon_to_yolo(points: Any, width: int, height: int) -> list[float]:
    values = normalize_points(points)
    if len(values) < 6:
        raise ValueError("Um polígono precisa de pelo menos três pontos.")
    result: list[float] = []
    for index, value in enumerate(values):
        limit = width if index % 2 == 0 else height
        result.append(max(0.0, min(float(limit), value)) / limit)
    return result


def map_labels(source: dict[int, str], destination: list[Any]) -> dict[int, int]:
    """Resolve source classes to destination label IDs using names only."""
    by_name: dict[str, list[int]] = {}
    for label in destination:
        by_name.setdefault(str(label.name).strip().casefold(), []).append(int(label.id))
    mapping: dict[int, int] = {}
    missing: list[str] = []
    for source_id, name in source.items():
        candidates = by_name.get(name.strip().casefold(), [])
        if len(candidates) == 1:
            mapping[source_id] = candidates[0]
        elif len(candidates) > 1:
            raise ValueError(f"Label destino ambígua: {name}")
        else:
            missing.append(name)
    if missing:
        raise ValueError("Labels ausentes no projeto de destino: " + ", ".join(missing))
    return mapping
