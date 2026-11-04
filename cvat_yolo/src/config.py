from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")
os.environ.setdefault("YOLO_CONFIG_DIR", str(ROOT / ".cache" / "ultralytics"))
os.environ.setdefault("TORCH_HOME", str(ROOT / ".cache" / "torch"))


def _bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    return default if value is None else value.strip().lower() in {"1", "true", "yes", "s"}


@dataclass(frozen=True)
class Config:
    host: str
    username: str
    password: str
    token: str
    epochs: int
    batch: int
    image_size: int
    device: str
    confidence: float
    overwrite: bool

    @classmethod
    def from_env(cls) -> "Config":
        config = cls(
            host=os.getenv("CVAT_HOST", "http://localhost:8080").rstrip("/"),
            username=os.getenv("CVAT_USERNAME", ""),
            password=os.getenv("CVAT_PASSWORD", ""),
            token=os.getenv("CVAT_TOKEN", ""),
            epochs=int(os.getenv("YOLO_EPOCHS", "100")),
            batch=int(os.getenv("YOLO_BATCH", "16")),
            image_size=int(os.getenv("YOLO_IMAGE_SIZE", "640")),
            device=os.getenv("YOLO_DEVICE", "auto"),
            confidence=float(os.getenv("CONFIDENCE_THRESHOLD", "0.50")),
            overwrite=_bool("OVERWRITE_ANNOTATIONS"),
        )
        if config.epochs < 1 or config.batch == 0 or config.batch < -1 or config.image_size < 1:
            raise ValueError("YOLO_EPOCHS e YOLO_IMAGE_SIZE devem ser positivos; YOLO_BATCH deve ser -1 ou positivo.")
        if not 0 <= config.confidence <= 1:
            raise ValueError("CONFIDENCE_THRESHOLD deve estar entre 0 e 1.")
        return config


def ensure_dirs() -> None:
    for name in ("models", "data", "logs"):
        (ROOT / name).mkdir(parents=True, exist_ok=True)
