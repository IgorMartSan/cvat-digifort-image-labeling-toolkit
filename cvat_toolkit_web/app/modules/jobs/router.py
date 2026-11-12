from __future__ import annotations

import asyncio
import json
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

from app.core.config import APP_ROOT, MAX_LOG_ENTRIES
from app.core.events import sse_manager
from app.core.job_queue import MAX_JOB_HISTORY, job_queue
from app.core.templates import templates

router = APIRouter(prefix="/jobs", tags=["Jobs"])


@router.get("", response_class=HTMLResponse)
async def jobs_page(request: Request, job_id: Optional[str] = Query(None)):
    snapshot = job_queue.get_snapshot()
    return templates.TemplateResponse(
        request=request,
        name="jobs/templates/index.html",
        context={
            "active_tab": "jobs",
            "snapshot": snapshot,
            "tasks": snapshot.get("tasks", []),
            "target_job_id": job_id,
            "max_history_limit": MAX_JOB_HISTORY,
            "max_log_entries": MAX_LOG_ENTRIES,
        },
    )


@router.get("/snapshot")
async def get_queue_snapshot():
    return job_queue.get_snapshot()


@router.post("/{job_id}/cancel")
async def cancel_job_endpoint(job_id: str):
    success = job_queue.cancel_job(job_id)
    if not success:
        raise HTTPException(status_code=404, detail="Tarefa não encontrada ou não pode ser cancelada.")
    return {"status": "success", "message": f"Tarefa {job_id} cancelada."}


@router.delete("/logs")
async def clear_logs_endpoint(clear_history: bool = Query(False)):
    job_queue.clear_logs(clear_history=clear_history)
    return {"status": "success", "message": "Logs limpos com sucesso."}


@router.delete("/history")
async def clear_history_endpoint():
    job_queue.clear_history()
    return {"status": "success", "message": "Histórico de tarefas e logs limpo com sucesso."}


@router.get("/stream")
async def jobs_stream():
    """Canal SSE para monitoramento da fila e logs ao vivo de tarefas."""
    queue = sse_manager.subscribe("jobs")

    async def event_generator():
        # Envia snapshot inicial
        initial_event = {
            "type": "queue_update",
            "snapshot": job_queue.get_snapshot(),
        }
        yield f"data: {json.dumps(initial_event)}\n\n"

        try:
            while True:
                data = await queue.get()
                yield f"data: {json.dumps(data)}\n\n"
        except asyncio.CancelledError:
            sse_manager.unsubscribe("jobs", queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
