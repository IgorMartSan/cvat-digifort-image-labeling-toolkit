from __future__ import annotations

import io
import json
import random
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Tuple

from PIL import Image

from app.core.config import DATA_DIR
from app.frameworks.yolo.annotations import (
    _value,
    polygon_to_bbox,
    polygon_to_yolo,
    rectangle_to_yolo,
    shapes_from,
)


def get_cached_frame_bytes(task: Any, frame: int) -> bytes:
    cache = DATA_DIR / "cache" / f"task_{int(task.id)}" / f"frame_{frame:08d}.bin"
    if cache.is_file() and cache.stat().st_size:
        return cache.read_bytes()
    cache.parent.mkdir(parents=True, exist_ok=True)
    content = task.get_frame(frame, quality="original").read()
    temp = cache.with_suffix(".part")
    temp.write_bytes(content)
    temp.replace(cache)
    return content


def collect_task_items(
    task: Any,
    mode: str,
    derive_polygons: bool = True,
    include_empty: bool = False,
    on_progress: Optional[Callable[[int, int, str], None]] = None,
) -> List[Dict[str, Any]]:
    annotations = task.get_annotations()
    by_frame: Dict[int, List[Dict[str, Any]]] = {}

    for shape in shapes_from(annotations):
        kind = str(_value(shape, "type", "")).lower()
        if kind not in {"rectangle", "polygon"}:
            continue
        if (mode == "detection" and (kind == "rectangle" or derive_polygons)) or (
            mode == "segmentation" and kind == "polygon"
        ):
            by_frame.setdefault(int(_value(shape, "frame", 0)), []).append({
                "type": kind,
                "label_id": int(_value(shape, "label_id")),
                "points": _value(shape, "points", []),
            })

    meta = task.get_meta()
    frame_count = int(_value(meta, "size", 0))
    items = []
    for frame in range(frame_count):
        objects = by_frame.get(frame, [])
        if not objects and not include_empty:
            continue

        if on_progress and (frame % 5 == 0 or frame == frame_count - 1):
            on_progress(
                frame + 1,
                frame_count,
                f"📥 Baixando e verificando frame {frame + 1}/{frame_count} (Tarefa #{task.id})...",
            )

        items.append({
            "task_id": int(task.id),
            "frame": frame,
            "objects": objects,
            "image_bytes": get_cached_frame_bytes(task, frame),
        })
    return items


def split_dataset(
    items: List[Dict[str, Any]], seed: int = 42
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Separa treino e validação por tarefa ou com janela temporal se tarefa única."""
    if len(items) < 2:
        raise ValueError("São necessárias pelo menos duas imagens anotadas para treino e validação.")
    task_ids = sorted({item["task_id"] for item in items})
    rng = random.Random(seed)
    if len(task_ids) > 1:
        rng.shuffle(task_ids)
        val_tasks = set(task_ids[: max(1, round(len(task_ids) * 0.2))])
        train = [item for item in items if item["task_id"] not in val_tasks]
        val = [item for item in items if item["task_id"] in val_tasks]
        if train and val:
            return train, val

    ordered = sorted(items, key=lambda item: item["frame"])
    split_at = max(1, min(len(ordered) - 1, round(len(ordered) * 0.8)))
    gap = max(1, len(ordered) // 50)
    train, val = ordered[: max(1, split_at - gap)], ordered[min(len(ordered) - 1, split_at + gap) :]
    if not train or not val:
        raise ValueError("Uma única tarefa precisa de mais imagens para separar treino e validação com intervalo temporal.")
    return train, val


def select_stratified(
    items: List[Dict[str, Any]], count: int, seed: int = 42
) -> List[Dict[str, Any]]:
    groups: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
    for item in items:
        counts: Dict[int, int] = defaultdict(int)
        for obj in item["objects"]:
            counts[int(obj["label_id"])] += 1
        dominant = min(counts, key=lambda label: (-counts[label], label))
        groups[dominant].append(item)

    rng = random.Random(seed)
    chosen = []
    fractions = {label: count * len(group) / len(items) for label, group in groups.items()}
    allocations = {label: min(len(group), int(fractions[label])) for label, group in groups.items()}
    remaining = count - sum(allocations.values())

    for label in sorted(groups, key=lambda key: fractions[key] - allocations[key], reverse=True):
        if remaining <= 0:
            break
        extra = min(remaining, len(groups[label]) - allocations[label])
        allocations[label] += extra
        remaining -= extra

    for label, group in groups.items():
        rng.shuffle(group)
        chosen.extend(group[: allocations[label]])
    rng.shuffle(chosen)
    return chosen


def build_yolo_dataset(
    items: List[Dict[str, Any]],
    mode: str,
    label_names: Dict[int, str],
    output_dir: Path,
    seed: int = 42,
    include_empty: bool = False,
) -> Tuple[Path, Dict[str, int]]:
    annotated = [item for item in items if item["objects"]]
    if len(annotated) < 2:
        raise ValueError("O dataset precisa de ao menos duas imagens compatíveis anotadas.")

    selected = items if include_empty else annotated
    rng = random.Random(seed)
    rng.shuffle(selected)
    train, val = split_dataset(selected, seed)
    classes = sorted(
        {obj["label_id"] for item in annotated for obj in item["objects"]},
        key=lambda x: label_names.get(x, str(x)).casefold(),
    )
    class_index = {label_id: index for index, label_id in enumerate(classes)}

    output_dir.mkdir(parents=True, exist_ok=False)
    for split_name, split_items in (("train", train), ("val", val)):
        for item in split_items:
            image = Image.open(io.BytesIO(item["image_bytes"])).convert("RGB")
            width, height = image.size
            image_path = output_dir / "images" / split_name / f"task_{item['task_id']}_frame_{item['frame']:08d}.jpg"
            label_path = output_dir / "labels" / split_name / f"task_{item['task_id']}_frame_{item['frame']:08d}.txt"
            image_path.parent.mkdir(parents=True, exist_ok=True)
            label_path.parent.mkdir(parents=True, exist_ok=True)
            image.save(image_path, quality=95)

            rows: List[str] = []
            for obj in item["objects"]:
                points = obj["points"]
                if mode == "detection":
                    if obj["type"] == "polygon":
                        min_x, min_y, max_x, max_y = polygon_to_bbox(points)
                        coords = rectangle_to_yolo([min_x, min_y, max_x, max_y], width, height)
                    else:
                        coords = rectangle_to_yolo(points, width, height)
                else:
                    coords = polygon_to_yolo(points, width, height)
                rows.append(f"{class_index[obj['label_id']]} " + " ".join(f"{value:.6f}" for value in coords))
            label_path.write_text("\n".join(rows) + ("\n" if rows else ""), encoding="utf-8")

    yaml_path = output_dir / "dataset.yaml"
    yaml_path.write_text(
        "path: " + str(output_dir.resolve()).replace("\\", "/") + "\n"
        "train: images/train\nval: images/val\n"
        "names:\n"
        + "".join(
            f"  {i}: {json.dumps(label_names.get(label_id, str(label_id)), ensure_ascii=False)}\n"
            for i, label_id in enumerate(classes)
        ),
        encoding="utf-8",
    )
    return yaml_path, {"selected": len(selected), "train": len(train), "val": len(val)}
