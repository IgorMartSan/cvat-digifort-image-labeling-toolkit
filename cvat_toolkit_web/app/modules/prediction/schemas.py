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
    post_stage: str = Field(default="validation", description="Estágio atribuído no CVAT pós-anotação: 'validation', 'annotation' ou 'completed'")
    max_instances_per_frame: int = Field(default=0, ge=0, le=100, description="0 para sem limite, ou N para limitar o número de demarcações por imagem")
    instance_limit_mode: str = Field(default="total", description="'total' (no quadro todo) ou 'per_class' (por classe)")
    iou_threshold: float = Field(default=0.45, ge=0.05, le=0.95, description="Limiar IoU para supressão de sobreposição (NMS)")
