from __future__ import annotations

from pathlib import Path
from typing import Any

from .config import ROOT


def get_frame_bytes(task: Any, frame: int) -> bytes:
    cache = ROOT / "data" / "cache" / f"task_{int(task.id)}" / f"frame_{frame:08d}.bin"
    if cache.is_file() and cache.stat().st_size:
        return cache.read_bytes()
    cache.parent.mkdir(parents=True, exist_ok=True)
    content = task.get_frame(frame, quality="original").read()
    temporary = cache.with_suffix(".part")
    temporary.write_bytes(content)
    temporary.replace(cache)
    return content
