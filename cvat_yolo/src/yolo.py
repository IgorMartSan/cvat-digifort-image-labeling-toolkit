from __future__ import annotations

import json
import random
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .annotations import _value, polygon_to_yolo, rectangle_to_yolo
from .config import ROOT, ensure_dirs


def _split(items: list[dict[str, Any]], seed: int = 42) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split by task; for one source task, leave a temporal gap between splits."""
    if len(items) < 2:
        raise ValueError("São necessárias pelo menos duas imagens anotadas para treino e validação.")
    task_ids = sorted({item["task_id"] for item in items})
    rng = random.Random(seed)
    if len(task_ids) > 1:
        rng.shuffle(task_ids)
        val_tasks = set(task_ids[:max(1, round(len(task_ids) * 0.2))])
        train = [item for item in items if item["task_id"] not in val_tasks]
        val = [item for item in items if item["task_id"] in val_tasks]
        if train and val:
            return train, val
    ordered = sorted(items, key=lambda item: item["frame"])
    split_at = max(1, min(len(ordered) - 1, round(len(ordered) * 0.8)))
    gap = max(1, len(ordered) // 50)
    train, val = ordered[:max(1, split_at - gap)], ordered[min(len(ordered) - 1, split_at + gap):]
    if not train or not val:
        raise ValueError("Uma única tarefa precisa de mais imagens para separar treino e validação com intervalo temporal.")
    return train, val


def select_stratified(items: list[dict[str, Any]], count: int, seed: int = 42) -> list[dict[str, Any]]:
    """Sample by each image's most frequent class to retain class proportions."""
    import random
    from collections import defaultdict

    groups: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for item in items:
        if not item["objects"]:
            groups[-1].append(item)
            continue
        counts: dict[int, int] = defaultdict(int)
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
        chosen.extend(group[:allocations[label]])
    rng.shuffle(chosen)
    return chosen


def build_dataset(items: list[dict[str, Any]], mode: str, label_names: dict[int, str], output: Path, seed: int = 42) -> tuple[Path, dict[str, int]]:
    from PIL import Image
    import io

    compatible = list(items)
    if len(compatible) < 2 or not any(item["objects"] for item in compatible):
        raise ValueError("O dataset precisa de ao menos duas imagens e uma imagem anotada compatível.")
    rng = random.Random(seed)
    rng.shuffle(compatible)
    selected = compatible
    train, val = _split(selected, seed)
    classes = sorted({obj["label_id"] for item in selected for obj in item["objects"]}, key=lambda x: label_names[x].casefold())
    class_index = {label_id: index for index, label_id in enumerate(classes)}
    output.mkdir(parents=True, exist_ok=False)
    for split_name, split_items in (("train", train), ("val", val)):
        for item in split_items:
            image = Image.open(io.BytesIO(item["image_bytes"])).convert("RGB")
            width, height = image.size
            image_path = output / "images" / split_name / f"task_{item['task_id']}_frame_{item['frame']:08d}.jpg"
            label_path = output / "labels" / split_name / f"task_{item['task_id']}_frame_{item['frame']:08d}.txt"
            image_path.parent.mkdir(parents=True, exist_ok=True)
            label_path.parent.mkdir(parents=True, exist_ok=True)
            image.save(image_path, quality=95)
            rows: list[str] = []
            for obj in item["objects"]:
                points = obj["points"]
                if mode == "detection":
                    if obj["type"] == "polygon":
                        xs, ys = points[0::2], points[1::2]
                        coords = rectangle_to_yolo([min(xs), min(ys), max(xs), max(ys)], width, height)
                    else:
                        coords = rectangle_to_yolo(points, width, height)
                else:
                    coords = polygon_to_yolo(points, width, height)
                rows.append(str(class_index[obj["label_id"]]) + " " + " ".join(f"{value:.6f}" for value in coords))
            label_path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    yaml_path = output / "dataset.yaml"
    yaml_path.write_text(
        "path: " + str(output.resolve()).replace("\\", "/") + "\n"
        "train: images/train\nval: images/val\n"
        "names:\n" + "".join(f"  {i}: {json.dumps(label_names[label_id], ensure_ascii=False)}\n" for i, label_id in enumerate(classes)),
        encoding="utf-8",
    )
    return yaml_path, {"selected": len(selected), "train": len(train), "val": len(val)}


def train_model(dataset_yaml: Path, mode: str, config: Any, metadata: dict[str, Any]) -> Path:
    from ultralytics import YOLO

    ensure_dirs()
    mode_dir = ROOT / "models" / mode
    mode_dir.mkdir(parents=True, exist_ok=True)
    runs = [int(p.name[4:]) for p in mode_dir.glob("run_*") if p.name[4:].isdigit()]
    run_dir = mode_dir / f"run_{max(runs, default=0) + 1:03d}"
    run_dir.mkdir()
    weights = "yolo11n.pt" if mode == "detection" else "yolo11n-seg.pt"
    model = YOLO(weights)
    result = model.train(data=str(dataset_yaml), epochs=config.epochs, patience=config.patience, batch=config.batch,
                         imgsz=config.image_size, device=None if config.device == "auto" else config.device,
                         project=str(run_dir), name="training", exist_ok=True, cache="disk")
    best = Path(result.save_dir) / "weights" / "best.pt"
    if not best.exists():
        raise RuntimeError(f"Treinamento terminou sem checkpoint best.pt: {best}")
    destination = run_dir / "best.pt"
    destination.write_bytes(best.read_bytes())
    metrics = {key: float(value) for key, value in getattr(result, "results_dict", {}).items() if isinstance(value, (int, float))}
    metadata.update({"mode": mode, "trained_at": datetime.now(timezone.utc).isoformat(),
                     "epochs": config.epochs, "metrics": metrics,
                     "ultralytics_version": __import__("ultralytics").__version__})
    (run_dir / "metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    return destination


def available_models() -> list[tuple[Path, dict[str, Any]]]:
    found = []
    for metadata_path in (ROOT / "models").glob("*/run_*/metadata.json"):
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            checkpoint = metadata_path.parent / "best.pt"
            if checkpoint.is_file():
                found.append((checkpoint, metadata))
        except (OSError, json.JSONDecodeError):
            continue
    return sorted(found, key=lambda item: (item[0].parent.parent.name, item[0].parent.name))


def yolo_version() -> str:
    from ultralytics import __version__
    return __version__
