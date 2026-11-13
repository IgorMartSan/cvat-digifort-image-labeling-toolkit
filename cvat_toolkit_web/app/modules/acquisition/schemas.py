from __future__ import annotations

from typing import Optional
from pydantic import BaseModel, Field


class TestSnapshotRequest(BaseModel):
    snapshot_url: Optional[str] = None
    direct_url: Optional[str] = None
    digifort_host: Optional[str] = None
    digifort_port: int = 8601
    camera_name: Optional[str] = None
    username: Optional[str] = ""
    password: Optional[str] = ""
    use_rtsp: bool = False


class CreateWorkerRequest(BaseModel):
    camera_name: Optional[str] = Field("camera_01", description="Nome de identificação da câmera/pasta (resolvido automaticamente)")
    source_type: str = Field("digifort", description="Tipo de origem ('digifort' ou 'rtsp')")
    direct_url: Optional[str] = Field(None, description="Link direto (RTSP ou HTTP) da câmera")
    digifort_host: Optional[str] = Field("", description="IP ou Hostname do servidor Digifort")
    digifort_port: int = Field(8601, description="Porta HTTP da API do Digifort")
    username: Optional[str] = Field("", description="Usuário de autenticação")
    password: Optional[str] = Field("", description="Senha de autenticação")
    use_rtsp: bool = Field(False, description="Utilizar stream RTSP em vez de HTTP snapshot")
    snapshot_url: Optional[str] = Field("", description="URL customizada de snapshot HTTP")
    cvat_project_id: Optional[int] = Field(None, description="ID do Projeto CVAT de destino")
    cvat_project_name: Optional[str] = Field("", description="Nome do Projeto CVAT")
    cvat_organization: Optional[str] = Field("", description="Slug da Organização no CVAT")
    task_prefix: Optional[str] = Field("", description="Nome inicial / prefixo de cada task criada no CVAT")
    interval_seconds: float = Field(600.0, description="Intervalo entre capturas em segundos")
    images_per_task: int = Field(200, description="Quantidade de imagens por task no CVAT")
    auto_remove: bool = Field(True, description="Remover imagens locais após confirmação de upload no CVAT")
    auto_start: bool = Field(True, description="Iniciar worker imediatamente após criação")
    ptz_pan: Optional[float] = Field(None, description="Posição Pan atual da câmera")
    ptz_tilt: Optional[float] = Field(None, description="Posição Tilt atual da câmera")
    ptz_zoom: Optional[float] = Field(None, description="Posição Zoom atual da câmera")
    ptz_preset: Optional[str] = Field(None, description="Preset atual ou nome do preset")
    ptz_mode: str = Field("any", description="'any' para filmar em qualquer posição, 'presets' para apenas presets selecionados")
    selected_presets: list[int] = Field(default_factory=list, description="IDs dos presets autorizados para captura")
    capture_mode: str = Field("live", description="'live' para ao vivo, 'history' para gravações passadas")
    start_date: Optional[str] = Field(None, description="Data/hora inicial para histórico (ISO 8601)")
    end_date: Optional[str] = Field(None, description="Data/hora final para histórico")
    sync_pipeline: bool = Field(True, description="Sincronizar configuração com pipeline.env e scripts")
