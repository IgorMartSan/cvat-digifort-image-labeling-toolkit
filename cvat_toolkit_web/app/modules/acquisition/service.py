from __future__ import annotations

import re
import uuid
from typing import Any, Dict, List, Optional

from urllib.parse import urlparse

from app.core.acquisition_manager import (
    AcquisitionWorkerConfig,
    acquisition_manager,
    build_digifort_http_url,
    extract_camera_name_from_url,
    save_camera_url,
    sync_pipeline_files,
    test_snapshot_capture,
)
from app.modules.acquisition.schemas import CreateWorkerRequest, TestSnapshotRequest


def sanitize_folder_name(name: str) -> str:
    """Higieniza o nome para garantir que seja uma pasta válida e segura."""
    clean = re.sub(r"[^A-Za-z0-9_-]", "_", name.strip())
    return clean[:80] or "camera_01"


def test_camera_snapshot_service(req: TestSnapshotRequest) -> Dict[str, Any]:
    url = (req.direct_url or req.snapshot_url or "").strip()
    use_rtsp = req.use_rtsp
    if not url:
        if not req.digifort_host or not req.camera_name:
            return {
                "success": False,
                "error": "Informe o link direto da câmera ou os parâmetros de host e câmera.",
            }
        url = build_digifort_http_url(
            host=req.digifort_host,
            camera=req.camera_name,
            port=req.digifort_port,
            username=req.username or "",
            password=req.password or "",
        )
    else:
        if url.lower().startswith("rtsp://"):
            use_rtsp = True

    return test_snapshot_capture(url=url, use_rtsp=use_rtsp, camera_name=req.camera_name)


def create_worker_service(req: CreateWorkerRequest) -> Dict[str, Any]:
    camera_name = (req.camera_name or "").strip()
    direct_url = (req.direct_url or req.snapshot_url or "").strip()
    if not camera_name or camera_name == "camera_01":
        auto = extract_camera_name_from_url(direct_url)
        if auto:
            camera_name = auto
        elif req.cvat_project_name:
            camera_name = req.cvat_project_name
        else:
            camera_name = "camera_01"

    clean_name = sanitize_folder_name(camera_name)
    worker_id = f"worker_{clean_name}_{uuid.uuid4().hex[:6]}"

    snapshot_url = (req.direct_url or req.snapshot_url or "").strip()
    digifort_host = (req.digifort_host or "").strip()
    use_rtsp = req.use_rtsp
    source_type = req.source_type

    if snapshot_url:
        save_camera_url(snapshot_url)
        if snapshot_url.lower().startswith("rtsp://"):
            use_rtsp = True
            source_type = "rtsp"
        if not digifort_host:
            try:
                parsed = urlparse(snapshot_url)
                digifort_host = parsed.hostname or "stream"
            except Exception:
                digifort_host = "stream"
    else:
        snapshot_url = build_digifort_http_url(
            host=digifort_host,
            camera=req.camera_name,
            port=req.digifort_port,
            username=req.username or "",
            password=req.password or "",
        )

    cfg = AcquisitionWorkerConfig(
        id=worker_id,
        camera_name=clean_name,
        source_type=source_type,
        digifort_host=digifort_host,
        digifort_port=req.digifort_port,
        username=req.username or "",
        password=req.password or "",
        snapshot_url=snapshot_url,
        use_rtsp=use_rtsp,
        cvat_project_id=req.cvat_project_id,
        cvat_project_name=req.cvat_project_name or clean_name,
        cvat_organization=req.cvat_organization or "",
        task_prefix=(req.task_prefix or "").strip(),
        interval_seconds=max(req.interval_seconds, 1.0),
        images_per_task=max(req.images_per_task, 1),
        auto_remove=req.auto_remove,
        ptz_pan=req.ptz_pan,
        ptz_tilt=req.ptz_tilt,
        ptz_zoom=req.ptz_zoom,
        ptz_preset=req.ptz_preset,
        ptz_mode=req.ptz_mode,
        selected_presets=req.selected_presets,
        capture_mode=req.capture_mode,
        start_date=req.start_date,
        end_date=req.end_date,
        sync_pipeline=req.sync_pipeline,
    )

    if req.sync_pipeline:
        try:
            sync_pipeline_files(cfg)
        except Exception:
            pass

    worker = acquisition_manager.create_worker(cfg, auto_start=req.auto_start)
    return worker.to_dict()


def list_active_workers_service() -> List[Dict[str, Any]]:
    return acquisition_manager.list_workers()


def control_worker_service(worker_id: str, action: str) -> bool:
    action = action.lower()
    if action == "start":
        return acquisition_manager.start_worker(worker_id)
    elif action == "pause":
        return acquisition_manager.pause_worker(worker_id)
    elif action == "resume":
        return acquisition_manager.resume_worker(worker_id)
    elif action == "stop":
        return acquisition_manager.stop_worker(worker_id)
    return False


def delete_worker_service(worker_id: str, delete_images: bool = True) -> bool:
    return acquisition_manager.delete_worker(worker_id, delete_images=delete_images)
