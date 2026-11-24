from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

APP_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(APP_ROOT / ".env")

# Base de volume unificado (quando montado via Docker em /app/volume ou ./volume)
VOLUME_DIR = Path("/app/volume") if Path("/app/volume").is_dir() else (APP_ROOT / "volume" if (APP_ROOT / "volume").is_dir() else None)

DATA_DIR = (VOLUME_DIR / "data") if VOLUME_DIR else (APP_ROOT / "data")
MODELS_DIR = (VOLUME_DIR / "models") if VOLUME_DIR else (APP_ROOT / "models")
LOGS_DIR = (VOLUME_DIR / "logs") if VOLUME_DIR else (APP_ROOT / "logs")
CACHE_DIR = (VOLUME_DIR / "cache") if VOLUME_DIR else (APP_ROOT / ".cache")

# Ultralytics & Torch cache
os.environ.setdefault("YOLO_CONFIG_DIR", str(CACHE_DIR / "ultralytics"))
os.environ.setdefault("TORCH_HOME", str(CACHE_DIR / "torch"))

MAX_LOG_ENTRIES = int(os.getenv("MAX_LOG_ENTRIES", "20"))


def ensure_system_dirs() -> None:
    for folder in (DATA_DIR, MODELS_DIR, LOGS_DIR, CACHE_DIR):
        folder.mkdir(parents=True, exist_ok=True)


def cleanup_temporary_datasets() -> int:
    """Remove pastas dataset_* e caches de frames temporários em DATA_DIR."""
    import shutil
    count = 0
    if DATA_DIR.is_dir():
        for item in DATA_DIR.glob("dataset_*"):
            if item.is_dir():
                try:
                    shutil.rmtree(item, ignore_errors=True)
                    count += 1
                except Exception:
                    pass
        cache_dir = DATA_DIR / "cache"
        if cache_dir.is_dir():
            try:
                shutil.rmtree(cache_dir, ignore_errors=True)
                count += 1
            except Exception:
                pass
    return count


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "s"}


@dataclass(frozen=True)
class Settings:
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
    def load(cls) -> "Settings":
        load_dotenv(APP_ROOT / ".env", override=True)
        host = (os.getenv("CVAT_HOST") or os.getenv("CVAT_URL") or "http://localhost:8080").rstrip("/")
        return cls(
            host=host,
            username=os.getenv("CVAT_USERNAME", ""),
            password=os.getenv("CVAT_PASSWORD", ""),
            token=os.getenv("CVAT_TOKEN", ""),
            epochs=int(os.getenv("YOLO_EPOCHS", "1000")),
            batch=int(os.getenv("YOLO_BATCH", "16")),
            image_size=int(os.getenv("YOLO_IMAGE_SIZE", "640")),
            device=os.getenv("YOLO_DEVICE", "auto"),
            confidence=float(os.getenv("CONFIDENCE_THRESHOLD", "0.50")),
            overwrite=_bool(os.getenv("OVERWRITE_ANNOTATIONS")),
        )


settings = Settings.load()
ensure_system_dirs()
