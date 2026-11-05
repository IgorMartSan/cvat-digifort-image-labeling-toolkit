from __future__ import annotations

from pathlib import Path
from typing import List

from fastapi import APIRouter, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

from app.core.config import APP_ROOT
from app.core.cvat import get_project_tasks_summary, list_projects
from app.core.events import sse_manager
from app.frameworks.yolo.trainer import list_available_models
from app.modules.training.service import (
    get_model_details_service,
    list_trained_models_service,
)
from app.modules.prediction.schemas import StartPredictionRequest
from app.modules.prediction.service import (
    current_pred_status,
    get_mapping_options,
    start_prediction_job,
)

router = APIRouter(prefix="/prediction", tags=["Prediction"])
templates = Jinja2Templates(directory=str(APP_ROOT / "app" / "modules"))


@router.get("", response_class=HTMLResponse)
async def prediction_page(request: Request, model_id: Optional[str] = Query(None)):
    try:
        projects = list_projects()
        trained_models = list_trained_models_service()
        models = [m for m in trained_models if m.get("has_weights")]
        if not models:
            models = list_available_models()
        error = None
    except Exception as exc:
        projects = []
        models = []
        error = f"Erro ao inicializar módulo de predição: {str(exc)}"

    return templates.TemplateResponse(
        request=request,
        name="prediction/templates/index.html",
        context={
            "active_tab": "prediction",
            "projects": projects,
            "models": models,
            "preselected_model_id": model_id,
            "error": error,
            "pred_status": current_pred_status,
        },
    )


@router.get("/model-info", response_class=HTMLResponse)
async def get_model_info_endpoint(request: Request, model_id: str = Query(...)):
    """Retorna o card de informações detalhadas do modelo para exibição no Passo 1."""
    parts = model_id.split("/", 1)
    details = None
    if len(parts) == 2:
        mode, run_name = parts
        details = get_model_details_service(mode, run_name)

    trained_models = list_trained_models_service()
    model_obj = next((m for m in trained_models if m["id"] == model_id), details)

    if not model_obj:
        return HTMLResponse('<div class="alert alert-warning py-2 small mb-0">Informações detalhadas do modelo não encontradas.</div>')

    return templates.TemplateResponse(
        request=request,
        name="prediction/templates/_model_info_card.html",
        context={
            "m": model_obj,
            "details": details,
        },
    )


@router.get("/tasks", response_class=HTMLResponse)
async def get_destination_tasks(request: Request, project_id: int = Query(...)):
    try:
        tasks = get_project_tasks_summary(project_id)
        return templates.TemplateResponse(
            request=request,
            name="prediction/templates/_tasks_select.html",
            context={
                "project_id": project_id,
                "tasks": tasks,
            },
        )
    except Exception as exc:
        return HTMLResponse(
            f'<div class="alert alert-danger py-2 my-2 small">Erro ao carregar tarefas: {exc}</div>'
        )


@router.post("/mapping", response_class=HTMLResponse)
async def get_label_mapping_form(
    request: Request,
    model_checkpoint: str = Form(...),
    destination_project_id: int = Form(...),
):
    try:
        options = get_mapping_options(model_checkpoint, destination_project_id)
        return templates.TemplateResponse(
            request=request,
            name="prediction/templates/_label_mapping.html",
            context={
                "model_checkpoint": model_checkpoint,
                "destination_project_id": destination_project_id,
                "source_classes": options["source_classes"],
                "destination_labels": options["destination_labels"],
                "suggestions": options["suggestions"],
            },
        )
    except Exception as exc:
        return HTMLResponse(
            f'<div class="alert alert-danger py-2 my-2 small">Erro ao obter classes do modelo: {exc}</div>'
        )


@router.post("/start")
async def start_prediction(req: StartPredictionRequest):
    try:
        job = start_prediction_job(req)
        return {
            "status": "queued",
            "job_id": job.id,
            "message": f"Tarefa '{job.title}' inserida na fila com sucesso.",
            "redirect_url": f"/jobs?job_id={job.id}",
        }
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Falha ao enfileirar inferência: {exc}")


@router.get("/stream")
async def prediction_stream():
    return StreamingResponse(
        sse_manager.event_generator("prediction"),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/status")
async def prediction_status():
    return current_pred_status
