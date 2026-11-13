from __future__ import annotations

import os
from typing import Optional
from fastapi import APIRouter, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app.core.acquisition_manager import (
    delete_camera_url,
    format_saved_camera_item,
    get_saved_camera_urls,
)
from app.core.config import Settings
from app.core.cvat import list_organizations, list_projects
from app.core.templates import templates
from app.modules.acquisition.schemas import CreateWorkerRequest, TestSnapshotRequest
from app.modules.acquisition.service import (
    control_worker_service,
    create_worker_service,
    delete_worker_service,
    list_active_workers_service,
    test_camera_snapshot_service,
)

router = APIRouter(prefix="/acquisition", tags=["Acquisition"])


@router.get("", response_class=HTMLResponse)
async def acquisition_index():
    return RedirectResponse(url="/acquisition/digifort")


@router.get("/digifort", response_class=HTMLResponse)
async def digifort_wizard_page(request: Request):
    """Página do fluxo guiado passo a passo para configuração de câmera Digifort."""
    try:
        projects = list_projects()
        organizations = list_organizations()
        error = None
    except Exception as exc:
        projects = []
        organizations = []
        error = f"Não foi possível carregar projetos do CVAT: {str(exc)}"

    settings = Settings.load()
    default_org = os.getenv("CVAT_ORGANIZATION", "")
    saved_urls = get_saved_camera_urls()
    saved_cameras = [format_saved_camera_item(u) for u in saved_urls]

    return templates.TemplateResponse(
        request=request,
        name="acquisition/templates/digifort.html",
        context={
            "active_tab": "acquisition",
            "active_source": "digifort",
            "active_subtab": "new",
            "projects": projects,
            "organizations": organizations,
            "error": error,
            "default_org": default_org,
            "cvat_host": settings.host,
            "saved_urls": saved_urls,
            "saved_cameras": saved_cameras,
        },
    )


@router.get("/saved-urls")
async def list_saved_urls_endpoint():
    """Retorna lista de URLs de câmeras salvas no histórico com rótulos formatados."""
    urls = get_saved_camera_urls()
    cameras = [format_saved_camera_item(u) for u in urls]
    return JSONResponse({"urls": urls, "cameras": cameras})


@router.delete("/saved-urls")
async def delete_saved_url_endpoint(url: str = Query(...)):
    """Remove uma URL do histórico de câmeras."""
    ok = delete_camera_url(url)
    urls = get_saved_camera_urls()
    cameras = [format_saved_camera_item(u) for u in urls]
    return JSONResponse({"success": ok, "urls": urls, "cameras": cameras})


@router.get("/workers", response_class=HTMLResponse)
async def workers_dashboard_page(request: Request):
    """Painel de monitoramento de todos os workers de aquisição ativos."""
    workers = list_active_workers_service()
    return templates.TemplateResponse(
        request=request,
        name="acquisition/templates/workers.html",
        context={
            "active_tab": "acquisition",
            "active_source": "digifort",
            "active_subtab": "workers",
            "workers": workers,
        },
    )


@router.get("/workers/cards", response_class=HTMLResponse)
async def workers_cards_fragment(request: Request):
    """Fragmento HTMX para auto-refresh da lista de cards de workers a cada 3s."""
    workers = list_active_workers_service()
    return templates.TemplateResponse(
        request=request,
        name="acquisition/templates/_workers_cards.html",
        context={"workers": workers},
    )


@router.post("/test-snapshot")
async def test_snapshot_endpoint(request: Request):
    """Testa a conexão e captura um snapshot para preview em tempo real."""
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        body = await request.json()
        req = TestSnapshotRequest(**body)
    else:
        form = await request.form()
        req = TestSnapshotRequest(
            direct_url=form.get("direct_url") or form.get("snapshot_url"),
            snapshot_url=form.get("snapshot_url", ""),
            digifort_host=form.get("digifort_host"),
            digifort_port=int(form.get("digifort_port") or 8601),
            camera_name=form.get("camera_name"),
            username=form.get("username", ""),
            password=form.get("password", ""),
            use_rtsp=form.get("use_rtsp") in {"true", "1", "on"},
        )

    res = test_camera_snapshot_service(req)
    return JSONResponse(res)


@router.post("/workers")
async def create_worker_endpoint(request: Request):
    """Cria um novo worker de aquisição e inicia a coleta/envio ao CVAT."""
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        body = await request.json()
        req = CreateWorkerRequest(**body)
    else:
        form = await request.form()
        project_id = form.get("cvat_project_id")
        direct_url = form.get("direct_url") or form.get("snapshot_url") or ""
        pan_val = form.get("ptz_pan")
        tilt_val = form.get("ptz_tilt")
        zoom_val = form.get("ptz_zoom")
        preset_val = form.get("ptz_preset")

        presets_raw = form.getlist("selected_presets")
        selected_presets: list[int] = []
        for p in presets_raw:
            try:
                for sub in str(p).split(","):
                    if sub.strip().isdigit():
                        selected_presets.append(int(sub.strip()))
            except Exception:
                pass

        ptz_mode = str(form.get("ptz_mode") or "any")
        capture_mode = str(form.get("capture_mode") or "live")
        start_date = form.get("start_date") or None
        end_date = form.get("end_date") or None
        sync_pipeline = form.get("sync_pipeline") not in {"false", "0", "off"}
        task_prefix = (form.get("task_prefix") or "").strip()

        cam_name = (form.get("camera_name") or "").strip()
        if not cam_name or cam_name == "camera_01":
            auto_cam = extract_camera_name_from_url(direct_url)
            if auto_cam:
                cam_name = auto_cam
            elif form.get("cvat_project_name"):
                cam_name = form.get("cvat_project_name")
            else:
                cam_name = "camera_01"

        req = CreateWorkerRequest(
            camera_name=cam_name,
            source_type=form.get("source_type", "digifort"),
            direct_url=direct_url,
            snapshot_url=form.get("snapshot_url", direct_url),
            digifort_host=form.get("digifort_host", ""),
            digifort_port=int(form.get("digifort_port") or 8601),
            username=form.get("username", ""),
            password=form.get("password", ""),
            use_rtsp=form.get("use_rtsp") in {"true", "1", "on"},
            cvat_project_id=int(project_id) if project_id and project_id.isdigit() else None,
            cvat_project_name=form.get("cvat_project_name", ""),
            cvat_organization=form.get("cvat_organization", ""),
            task_prefix=task_prefix,
            interval_seconds=float(form.get("interval_seconds") or 600.0),
            images_per_task=int(form.get("images_per_task") or 200),
            auto_remove=form.get("auto_remove") in {"true", "1", "on"},
            auto_start=True,
            ptz_pan=float(pan_val) if pan_val not in (None, "") else None,
            ptz_tilt=float(tilt_val) if tilt_val not in (None, "") else None,
            ptz_zoom=float(zoom_val) if zoom_val not in (None, "") else None,
            ptz_preset=str(preset_val) if preset_val not in (None, "") else None,
            ptz_mode=ptz_mode,
            selected_presets=selected_presets,
            capture_mode=capture_mode,
            start_date=start_date,
            end_date=end_date,
            sync_pipeline=sync_pipeline,
        )

    worker = create_worker_service(req)
    # Se chamada via formulário tradicional ou HTMX
    if "text/html" in request.headers.get("accept", ""):
        return RedirectResponse(url="/acquisition/workers", status_code=303)
    return JSONResponse(worker)


@router.post("/workers/{worker_id}/control")
async def control_worker_endpoint(worker_id: str, action: str = Query(...)):
    """Controla o ciclo de vida do worker (start, pause, resume, stop)."""
    ok = control_worker_service(worker_id, action)
    if not ok:
        raise HTTPException(status_code=400, detail="Ação inválida ou worker não encontrado.")
    return JSONResponse({"status": "success", "worker_id": worker_id, "action": action})


@router.delete("/workers/{worker_id}")
async def delete_worker_endpoint(worker_id: str, delete_images: bool = Query(True)):
    """Exclui o worker e remove todos os seus registros e imagens não enviadas do disco."""
    ok = delete_worker_service(worker_id, delete_images=delete_images)
    if not ok:
        raise HTTPException(status_code=404, detail="Worker não encontrado.")
    return JSONResponse({"status": "success", "worker_id": worker_id})
