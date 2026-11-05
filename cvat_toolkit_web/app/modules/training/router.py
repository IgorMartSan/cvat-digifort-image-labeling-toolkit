from __future__ import annotations

import json
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

from app.core.config import APP_ROOT, cleanup_temporary_datasets
from app.core.cvat import get_project_tasks_summary, list_projects
from app.core.events import sse_manager
from app.frameworks.yolo.trainer import list_available_models
from app.modules.training.schemas import StartTrainRequest
from app.modules.training.service import (
    analyze_dataset_service,
    current_train_status,
    delete_trained_model_service,
    get_model_details_service,
    list_trained_models_service,
    start_training_job,
)

router = APIRouter(prefix="/training", tags=["Training"])
templates = Jinja2Templates(directory=str(APP_ROOT / "app" / "modules"))


@router.get("", response_class=HTMLResponse)
async def training_page(request: Request):
    try:
        projects = list_projects()
        error = None
    except Exception as exc:
        projects = []
        error = f"Não foi possível conectar ao CVAT: {str(exc)}"

    return templates.TemplateResponse(
        request=request,
        name="training/templates/index.html",
        context={
            "active_tab": "training",
            "active_subtab": "train",
            "projects": projects,
            "error": error,
            "train_status": current_train_status,
        },
    )


@router.get("/models", response_class=HTMLResponse)
async def trained_models_page(request: Request):
    models = list_trained_models_service()
    frameworks = sorted(list({m["framework"] for m in models if m.get("framework")}))
    modes = sorted(list({m["mode"] for m in models if m.get("mode")}))
    return templates.TemplateResponse(
        request=request,
        name="training/templates/models.html",
        context={
            "active_tab": "training",
            "active_subtab": "models",
            "models": models,
            "frameworks": frameworks,
            "modes": modes,
            "total_count": len(models),
        },
    )


@router.get("/models/{mode}/{run_name}/details", response_class=HTMLResponse)
async def model_details_modal_endpoint(request: Request, mode: str, run_name: str):
    details = get_model_details_service(mode, run_name)
    if not details:
        raise HTTPException(status_code=404, detail="Detalhes do modelo não encontrados.")
    return templates.TemplateResponse(
        request=request,
        name="training/templates/_model_details_modal.html",
        context={
            "d": details,
        },
    )


@router.get("/models/{mode}/{run_name}/json")
async def model_details_json_endpoint(mode: str, run_name: str):
    details = get_model_details_service(mode, run_name)
    if not details:
        raise HTTPException(status_code=404, detail="Modelo não encontrado.")
    return details


@router.delete("/models/{mode}/{run_name}")
async def delete_model_endpoint(mode: str, run_name: str):
    success = delete_trained_model_service(mode, run_name)
    if not success:
        raise HTTPException(status_code=404, detail="Modelo não encontrado ou erro ao excluir.")
    return {
        "status": "success",
        "message": f"Modelo {mode}/{run_name} excluído com sucesso.",
        "id": f"{mode}/{run_name}",
    }


@router.post("/cleanup-temp")
async def cleanup_temp_endpoint():
    cleaned_count = cleanup_temporary_datasets()
    return {
        "status": "success",
        "message": f"{cleaned_count} pastas temporárias/caches foram removidas com sucesso.",
        "cleaned_count": cleaned_count,
    }


@router.get("/tasks", response_class=HTMLResponse)
async def get_project_tasks(request: Request, project_id: int = Query(...)):
    try:
        tasks = get_project_tasks_summary(project_id)
        return templates.TemplateResponse(
            request=request,
            name="training/templates/_tasks_table.html",
            context={
                "project_id": project_id,
                "tasks": tasks,
            },
        )
    except Exception as exc:
        return HTMLResponse(
            f'<div class="alert alert-danger py-2 my-2 small">Erro ao carregar tarefas: {exc}</div>'
        )


@router.post("/analyze", response_class=HTMLResponse)
async def analyze_tasks(
    request: Request,
    project_id: int = Form(...),
    task_ids: List[int] = Form(...),
):
    try:
        stats = analyze_dataset_service(project_id, task_ids)
        available_models = list_available_models()
        return templates.TemplateResponse(
            request=request,
            name="training/templates/_analysis_card.html",
            context={
                "project_id": project_id,
                "task_ids": task_ids,
                "stats": stats,
                "available_models": available_models,
            },
        )
    except Exception as exc:
        return HTMLResponse(
            f'<div class="alert alert-danger py-2 my-2 small">Erro na análise: {exc}</div>'
        )


@router.post("/start")
async def start_training(req: StartTrainRequest):
    try:
        job = start_training_job(req)
        return {
            "status": "queued",
            "job_id": job.id,
            "message": f"Tarefa '{job.title}' inserida na fila com sucesso.",
            "redirect_url": f"/jobs?job_id={job.id}",
        }
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Falha ao enfileirar treino: {exc}")


@router.get("/stream")
async def training_stream():
    return StreamingResponse(
        sse_manager.event_generator("training"),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/status")
async def training_status():
    return current_train_status
