from __future__ import annotations

from typing import List, Optional
from pydantic import BaseModel, Field


class AnalyzeRequest(BaseModel):
    project_id: int
    task_ids: List[int]


class StartTrainRequest(BaseModel):
    project_id: int
    task_ids: List[int]
    mode: str = Field(default="detection", pattern="^(detection|segmentation)$")
    derive_polygons: bool = True
    train_all_images: bool = False
    model_name: str = "yolo11n"
    custom_weights: Optional[str] = None
    sample_limit: Optional[int] = None
    epochs: int = Field(default=100, ge=1, le=1000)
    batch: int = Field(default=16, ge=-1)
    image_size: int = Field(default=640, ge=32)
    device: str = "auto"
    framework: str = "yolo11"
