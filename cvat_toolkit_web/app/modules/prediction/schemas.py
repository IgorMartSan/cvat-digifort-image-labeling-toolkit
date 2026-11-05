from __future__ import annotations

from typing import Dict, List
from pydantic import BaseModel, Field


class LabelMappingRequest(BaseModel):
    model_checkpoint: str
    destination_project_id: int


class StartPredictionRequest(BaseModel):
    model_checkpoint: str
    destination_project_id: int
    task_ids: List[int]
    class_map: Dict[int, int]  # source_class_index -> cvat_label_id
    confidence: float = Field(default=0.50, ge=0.01, le=1.0)
    overwrite_auto: bool = False
