from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.core.config import APP_ROOT, LOGS_DIR
from app.core.cvat import get_cvat_client, get_project_labels
from app.core.events import sse_manager
from app.frameworks.yolo.predictor import (
    YoloPredictor,
    log_prediction_run,
    predict_task,
)
from app.frameworks.yolo.trainer import list_available_models
from app.modules.prediction.schemas import StartPredictionRequest

current_pred_status: Dict[str, Any] = {
    "is_running": False,
    "current_task": None,
    "current_frame": 0,
    "total_frames": 0,
    "percent": 0.0,
    "processed": 0,
    "objects": 0,
    "sent": 0,
    "skipped": 0,
    "failed": 0,
    "error": None,
}


def get_mapping_options(model_checkpoint: str, destination_project_id: int) -> Dict[str, Any]:
    from app.core.config import MODELS_DIR

    ckpt_path = Path(model_checkpoint)
    if not ckpt_path.is_file():
        parts = model_checkpoint.split("/", 1)
        if len(parts) == 2:
            cand = MODELS_DIR / parts[0] / parts[1] / "best.pt"
            if cand.is_file():
                ckpt_path = cand
            else:
                cand_tw = MODELS_DIR / parts[0] / parts[1] / "training" / "weights" / "best.pt"
                if cand_tw.is_file():
                    ckpt_path = cand_tw

    if not ckpt_path.is_file():
        raise FileNotFoundError(f"Checkpoint não encontrado: {model_checkpoint}")

    predictor = YoloPredictor()
    model = predictor.load_model(ckpt_path)
    source_names: Dict[int, str] = {int(k): str(v) for k, v in model.names.items()}

    dest_labels = get_project_labels(destination_project_id)
    # dest_labels é {label_id: "nome"}

    # Tenta mapear automaticamente por nome exato (casefold)
    by_name: Dict[str, List[int]] = {}
    for lid, lname in dest_labels.items():
        by_name.setdefault(lname.strip().casefold(), []).append(lid)

    mapping_suggestions: Dict[int, Optional[int]] = {}
    for src_id, src_name in source_names.items():
        matches = by_name.get(src_name.strip().casefold(), [])
        if len(matches) == 1:
            mapping_suggestions[src_id] = matches[0]
        else:
            mapping_suggestions[src_id] = None

    return {
        "source_classes": source_names,
        "destination_labels": dest_labels,
        "suggestions": mapping_suggestions,
    }


def execute_prediction_job_sync(job: Any, queue_mgr: Any) -> None:
    """Executado pelo worker único da fila de tarefas."""
    global current_pred_status
    req = StartPredictionRequest(**job.payload)

    current_pred_status.update({
        "is_running": True,
        "current_task": None,
        "current_frame": 0,
        "total_frames": 0,
        "percent": 0.0,
        "processed": 0,
        "objects": 0,
        "sent": 0,
        "skipped": 0,
        "failed": 0,
        "error": None,
    })

    sse_manager.clear_history("prediction")
    queue_mgr.update_job_progress(job.id, 5.0, "Carregando modelo e conectando ao CVAT...")
    queue_mgr.append_log(job.id, f"🎯 Iniciando auto-anotação (Modelo: {Path(req.model_checkpoint).name})...")

    ckpt_path = Path(req.model_checkpoint)
    if not ckpt_path.is_file():
        from app.core.config import MODELS_DIR
        parts = req.model_checkpoint.split("/", 1)
        if len(parts) == 2:
            cand = MODELS_DIR / parts[0] / parts[1] / "best.pt"
            if cand.is_file():
                ckpt_path = cand
            else:
                cand_tw = MODELS_DIR / parts[0] / parts[1] / "training" / "weights" / "best.pt"
                if cand_tw.is_file():
                    ckpt_path = cand_tw

    if not ckpt_path.is_file():
        raise FileNotFoundError(f"Checkpoint não encontrado: {req.model_checkpoint}")

    try:
        predictor = YoloPredictor()
        model = predictor.load_model(ckpt_path)

        metadata_file = ckpt_path.parent / "metadata.json"
        mode = "detection"
        if metadata_file.is_file():
            try:
                meta = json.loads(metadata_file.read_text(encoding="utf-8"))
                mode = meta.get("mode", "detection")
            except Exception:
                pass

        total_accum = {"processed": 0, "objects": 0, "sent": 0, "failed": 0, "skipped": 0}

        with get_cvat_client() as client:
            project = client.projects.retrieve(req.destination_project_id)
            tasks = [t for t in project.get_tasks() if int(t.id) in req.task_ids]

            total_tasks = len(tasks)
            for task_idx, task in enumerate(tasks):
                if queue_mgr.is_cancel_requested(job.id):
                    raise InterruptedError("Inferência cancelada pelo usuário.")

                current_pred_status["current_task"] = f"Task #{task.id} - {task.name}"
                queue_mgr.append_log(job.id, f"Processando Tarefa #{task.id} ({task.name})...")

                def on_progress(evt: Dict[str, Any]) -> None:
                    if queue_mgr.is_cancel_requested(job.id):
                        raise InterruptedError("Inferência cancelada pelo usuário.")

                    if evt.get("type") == "frame":
                        frame = evt.get("frame", 0)
                        total_f = max(evt.get("total_frames", 1), 1)
                        task_pct = frame / total_f
                        overall_pct = 5.0 + ((task_idx + task_pct) / max(total_tasks, 1)) * 92.0

                        current_pred_status["current_frame"] = frame
                        current_pred_status["total_frames"] = total_f
                        current_pred_status["percent"] = round(overall_pct, 1)

                        step_str = f"Anotando Tarefa #{task.id}: frame {frame}/{total_f} ({round(task_pct * 100)}%)"
                        queue_mgr.update_job_progress(job.id, overall_pct, step_str)

                    if evt.get("text"):
                        queue_mgr.append_log(job.id, evt["text"])
                    sse_manager.broadcast("prediction", evt)

                counts = predict_task(
                    task=task,
                    model=model,
                    mode=mode,
                    class_map=req.class_map,
                    threshold=req.confidence,
                    overwrite_auto=req.overwrite_auto,
                    progress_callback=on_progress,
                )

                for k in total_accum:
                    total_accum[k] += counts.get(k, 0)
                    current_pred_status[k] = total_accum[k]

                queue_mgr.append_log(
                    job.id,
                    f"✓ Tarefa #{task.id} concluída: {counts['processed']} frames processados, {counts['objects']} objetos anotados.",
                )

            log_prediction_run(
                task_ids=req.task_ids,
                model_name=f"{ckpt_path.parent.name}/{ckpt_path.name}",
                summary=total_accum,
            )

            job.metrics.update(total_accum)
            current_pred_status["is_running"] = False

            queue_mgr.append_log(
                job.id,
                f"🎉 Auto-Anotação finalizada com sucesso! Total de objetos enviados ao CVAT: {total_accum['sent']}.",
                log_type="success",
            )
            sse_manager.broadcast("prediction", {
                "type": "success",
                "text": f"🎉 Processamento concluído com sucesso! Total de objetos enviados: {total_accum['sent']}.",
                "summary": total_accum,
            })
    finally:
        # Limpa cache temporário de frames das tarefas analisadas
        import shutil
        for tid in req.task_ids:
            task_cache = ckpt_path.parents[2] / "data" / "cache" / f"task_{int(tid)}"
            if task_cache.exists():
                try:
                    shutil.rmtree(task_cache, ignore_errors=True)
                except Exception:
                    pass


def start_prediction_job(req: StartPredictionRequest) -> Any:
    """Insere a predição na fila única global e retorna o job criado."""
    from app.core.job_queue import job_queue

    title = f"Auto-Anotação: Projeto #{req.destination_project_id} (Tarefas: {', '.join(map(str, req.task_ids))})"
    job = job_queue.enqueue(
        job_type="prediction",
        title=title,
        payload=req.model_dump(),
    )
    return job
