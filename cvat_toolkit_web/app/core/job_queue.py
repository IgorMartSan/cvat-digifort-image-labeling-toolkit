from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from app.core.config import DATA_DIR, MAX_LOG_ENTRIES
from app.core.events import sse_manager

logger = logging.getLogger("cvat_toolkit.queue")

MAX_JOB_HISTORY = int(os.getenv("MAX_JOB_HISTORY", "50"))
HISTORY_FILE = DATA_DIR / "job_history.json"


@dataclass
class Job:
    id: str
    job_type: str  # "training" | "prediction"
    title: str
    status: str  # "queued" | "running" | "completed" | "failed" | "canceled"
    created_at: str
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    progress: float = 0.0
    current_step: str = "Aguardando na fila..."
    payload: Dict[str, Any] = field(default_factory=dict)
    metrics: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None
    logs: List[str] = field(default_factory=list)

    @property
    def status_label(self) -> str:
        """Rótulo legível em português direto para o status da tarefa."""
        if self.status == "completed":
            return "Finalizado com sucesso"
        elif self.status == "failed":
            return "Finalizado com erro"
        elif self.status == "queued":
            return "Em espera"
        elif self.status == "running":
            pct = f" ({round(self.progress, 1)}%)" if self.progress else ""
            return f"Executando{pct}"
        elif self.status == "canceled":
            return "Cancelado"
        return self.status

    @property
    def status_category(self) -> str:
        """Categoria para agrupamento e filtros."""
        if self.status == "completed":
            return "success"
        elif self.status == "failed":
            return "error"
        elif self.status in {"queued", "running"}:
            return "waiting"
        elif self.status == "canceled":
            return "canceled"
        return "neutral"

    @property
    def description(self) -> str:
        """Descrição compreensiva da tarefa e parâmetros."""
        if self.job_type == "training":
            proj = self.payload.get("project_id", "-")
            tasks = self.payload.get("task_ids", [])
            tasks_str = ", ".join(map(str, tasks)) if tasks else "Nenhuma"
            mode = self.payload.get("mode", "detection")
            mode_str = "Detecção" if mode == "detection" else "Segmentação"
            model = self.payload.get("model_name", "YOLO")
            epochs = self.payload.get("epochs", "-")
            batch = self.payload.get("batch", "-")
            imgsz = self.payload.get("image_size", "-")
            return f"Treinamento {model} no Projeto #{proj} ({mode_str}) • Tarefas CVAT: [{tasks_str}] • {epochs} épocas, batch {batch}, imgsz {imgsz}"
        elif self.job_type == "prediction":
            proj = self.payload.get("destination_project_id") or self.payload.get("project_id", "-")
            tasks = self.payload.get("task_ids", [])
            tasks_str = ", ".join(map(str, tasks)) if tasks else "Todas"
            model = self.payload.get("model_path", "-")
            conf = self.payload.get("conf_thresh", "0.25")
            return f"Auto-Anotação no Projeto #{proj} • Modelo: {model} • Tarefas CVAT: [{tasks_str}] • Confiança: {conf}"
        return self.title or f"Tarefa {self.id}"

    @staticmethod
    def format_datetime(dt_str: Optional[str]) -> str:
        if not dt_str:
            return "-"
        try:
            cleaned = dt_str.replace("Z", "+00:00")
            dt = datetime.fromisoformat(cleaned)
            return dt.strftime("%d/%m/%Y %H:%M:%S")
        except Exception:
            return dt_str[:19].replace("T", " ")

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        # Limita logs no payload retornado para não sobrecarregar (buffer circular de no máximo MAX_LOG_ENTRIES)
        d["recent_logs"] = self.logs[-MAX_LOG_ENTRIES:] if self.logs else []
        d["status_label"] = self.status_label
        d["status_category"] = self.status_category
        d["description"] = self.description
        d["created_at_formatted"] = self.format_datetime(self.created_at)
        d["started_at_formatted"] = self.format_datetime(self.started_at)
        d["finished_at_formatted"] = self.format_datetime(self.finished_at)
        return d


class JobQueueManager:
    """Fila centralizada de execução única (FIFO) para Treinamento e Predição."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._wake_event = threading.Event()
        self._stop_event = threading.Event()
        self._queued_jobs: List[Job] = []
        self._active_job: Optional[Job] = None
        self._history: List[Job] = []
        self._cancel_requested_for: Optional[str] = None

        self._load_history()

        # Inicia thread única de execução de tarefas
        self._worker_thread = threading.Thread(target=self._worker_loop, daemon=True, name="JobQueueWorker")
        self._worker_thread.start()

    def _load_history(self) -> None:
        if HISTORY_FILE.is_file():
            try:
                data = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
                for item in data[-MAX_JOB_HISTORY:]:
                    # Se algum job ficou em 'running' ou 'queued' ao fechar o servidor, marca como cancelado/interrompido
                    if item.get("status") in {"running", "queued"}:
                        item["status"] = "canceled"
                        item["error"] = "Processo interrompido pelo reinício do servidor."
                        item["finished_at"] = datetime.now(timezone.utc).isoformat()
                    if "logs" in item and isinstance(item["logs"], list):
                        item["logs"] = item["logs"][-MAX_LOG_ENTRIES:]
                    self._history.append(Job(**item))
            except Exception as exc:
                logger.warning(f"Não foi possível carregar histórico de tarefas: {exc}")

    def _save_history(self) -> None:
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            to_save = [asdict(j) for j in self._history[-MAX_JOB_HISTORY:]]
            HISTORY_FILE.write_text(json.dumps(to_save, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:
            logger.error(f"Erro ao salvar histórico de tarefas: {exc}")

    def enqueue(self, job_type: str, title: str, payload: Dict[str, Any]) -> Job:
        with self._lock:
            now_iso = datetime.now(timezone.utc).isoformat()
            short_id = uuid.uuid4().hex[:6]
            job_id = f"job_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{short_id}"

            job = Job(
                id=job_id,
                job_type=job_type,
                title=title,
                status="queued",
                created_at=now_iso,
                payload=payload,
            )
            self._queued_jobs.append(job)

            position = len(self._queued_jobs)
            if self._active_job is not None:
                job.current_step = f"Aguardando na fila (Posição #{position})"
            else:
                job.current_step = "Iniciando processamento..."

            self._broadcast_change()
            self._wake_event.set()
            return job

    def cancel_job(self, job_id: str) -> bool:
        with self._lock:
            # Se estiver na fila de espera:
            for i, j in enumerate(self._queued_jobs):
                if j.id == job_id:
                    j.status = "canceled"
                    j.finished_at = datetime.now(timezone.utc).isoformat()
                    j.current_step = "Cancelado pelo usuário antes do início."
                    self._queued_jobs.pop(i)
                    self._history.insert(0, j)
                    self._trim_and_save_history()
                    self._broadcast_change()
                    return True

            # Se estiver em execução:
            if self._active_job and self._active_job.id == job_id:
                self._cancel_requested_for = job_id
                self._active_job.current_step = "Cancelando execução..."
                self.append_log(job_id, "⚠️ Solicitação de cancelamento enviada pelo usuário...")
                self._broadcast_change()
                return True

        return False

    def is_cancel_requested(self, job_id: str) -> bool:
        with self._lock:
            return self._cancel_requested_for == job_id

    def update_job_progress(
        self,
        job_id: str,
        percent: float,
        step_text: Optional[str] = None,
        metrics: Optional[Dict[str, Any]] = None,
    ) -> None:
        with self._lock:
            job = self._active_job
            if job and job.id == job_id:
                job.progress = round(percent, 1)
                if step_text:
                    job.current_step = step_text
                if metrics:
                    job.metrics.update(metrics)

        # Transmite SSE
        sse_manager.broadcast("jobs", {
            "type": "progress",
            "job_id": job_id,
            "percent": percent,
            "step": step_text or "",
            "metrics": metrics or {},
        })

    def append_log(self, job_id: str, text: str, log_type: str = "log") -> None:
        with self._lock:
            job = None
            if self._active_job and self._active_job.id == job_id:
                job = self._active_job
            else:
                job = self.get_job(job_id)

            if job:
                job.logs.append(text)
                if len(job.logs) > MAX_LOG_ENTRIES:
                    job.logs = job.logs[-MAX_LOG_ENTRIES:]

        sse_manager.broadcast("jobs", {
            "type": log_type,
            "job_id": job_id,
            "text": text,
        })

    def _trim_and_save_history(self) -> None:
        if len(self._history) > MAX_JOB_HISTORY:
            self._history = self._history[:MAX_JOB_HISTORY]
        self._save_history()

    def _broadcast_change(self) -> None:
        snapshot = self.get_snapshot()
        sse_manager.broadcast("jobs", {
            "type": "queue_update",
            "snapshot": snapshot,
        })

    def get_tasks_by_arrival(self) -> List[Dict[str, Any]]:
        """Retorna todas as tarefas organizadas do mais novo para o mais velho (espera -> executando -> finalizado)."""
        with self._lock:
            seen_ids = set()
            all_jobs: List[Job] = []

            for j in self._history:
                if j.id not in seen_ids:
                    seen_ids.add(j.id)
                    all_jobs.append(j)

            if self._active_job and self._active_job.id not in seen_ids:
                seen_ids.add(self._active_job.id)
                all_jobs.append(self._active_job)

            for j in self._queued_jobs:
                if j.id not in seen_ids:
                    seen_ids.add(j.id)
                    all_jobs.append(j)

            def _sort_key(job: Job):
                # Prioridade de status:
                # 0 = queued (Em espera)
                # 1 = running (Executando)
                # 2 = completed / failed (Finalizado com sucesso / erro)
                # 3 = canceled
                rank = 2
                if job.status == "queued":
                    rank = 0
                elif job.status == "running":
                    rank = 1
                elif job.status == "canceled":
                    rank = 3

                # Data decrescente (do mais novo para o mais velho)
                ts = 0.0
                if job.created_at:
                    try:
                        cleaned = job.created_at.replace("Z", "+00:00")
                        ts = datetime.fromisoformat(cleaned).timestamp()
                    except Exception:
                        ts = 0.0

                return (rank, -ts)

            all_jobs.sort(key=_sort_key)

            result = []
            for idx, job in enumerate(all_jobs, start=1):
                item = job.to_dict()
                item["arrival_order"] = idx
                item["arrival_badge"] = f"#{idx}"
                result.append(item)

            return result

    def get_snapshot(self) -> Dict[str, Any]:
        with self._lock:
            tasks_by_arrival = self.get_tasks_by_arrival()
            return {
                "active_job": self._active_job.to_dict() if self._active_job else None,
                "queued_jobs": [j.to_dict() for j in self._queued_jobs],
                "history": [j.to_dict() for j in self._history[:MAX_JOB_HISTORY]],
                "tasks": tasks_by_arrival,
                "counts": {
                    "queued": len(self._queued_jobs),
                    "running": 1 if self._active_job else 0,
                    "history": len(self._history),
                    "limit": MAX_JOB_HISTORY,
                    "total": len(tasks_by_arrival),
                    "waiting": sum(1 for t in tasks_by_arrival if t["status"] in {"queued", "running"}),
                    "success": sum(1 for t in tasks_by_arrival if t["status"] == "completed"),
                    "error": sum(1 for t in tasks_by_arrival if t["status"] == "failed"),
                    "canceled": sum(1 for t in tasks_by_arrival if t["status"] == "canceled"),
                },
            }

    def get_job(self, job_id: str) -> Optional[Job]:
        with self._lock:
            if self._active_job and self._active_job.id == job_id:
                return self._active_job
            for j in self._queued_jobs:
                if j.id == job_id:
                    return j
            for j in self._history:
                if j.id == job_id:
                    return j
        return None

    def clear_logs(self, clear_history: bool = False) -> None:
        with self._lock:
            if self._active_job:
                self._active_job.logs.clear()
            for j in self._queued_jobs:
                j.logs.clear()
            for j in self._history:
                j.logs.clear()
            if clear_history:
                self._history.clear()
            self._save_history()
            self._broadcast_change()
        sse_manager.clear_history("jobs")

    def clear_history(self) -> None:
        self.clear_logs(clear_history=True)

    def _worker_loop(self) -> None:
        """Loop contínuo de processamento único de tarefas (Single Worker)."""
        while not self._stop_event.is_set():
            job_to_run: Optional[Job] = None

            with self._lock:
                if self._queued_jobs and self._active_job is None:
                    job_to_run = self._queued_jobs.pop(0)
                    self._active_job = job_to_run
                    job_to_run.status = "running"
                    job_to_run.started_at = datetime.now(timezone.utc).isoformat()
                    job_to_run.current_step = "Inicializando..."
                    self._cancel_requested_for = None
                    self._broadcast_change()

            if job_to_run is None:
                # Aguarda novo trabalho na fila ou 1 segundo
                self._wake_event.wait(timeout=1.0)
                self._wake_event.clear()
                continue

            # Executa o job fora do lock
            try:
                self.append_log(job_to_run.id, f"🚀 Iniciando execução de '{job_to_run.title}' (ID: {job_to_run.id})...")

                if job_to_run.job_type == "training":
                    from app.modules.training.service import execute_training_job_sync
                    execute_training_job_sync(job_to_run, self)
                elif job_to_run.job_type == "prediction":
                    from app.modules.prediction.service import execute_prediction_job_sync
                    execute_prediction_job_sync(job_to_run, self)
                else:
                    raise ValueError(f"Tipo de job desconhecido: {job_to_run.job_type}")

                with self._lock:
                    if self._cancel_requested_for == job_to_run.id:
                        job_to_run.status = "canceled"
                        job_to_run.current_step = "Cancelado."
                    else:
                        job_to_run.status = "completed"
                        job_to_run.progress = 100.0
                        job_to_run.current_step = "Concluído com sucesso."
                    job_to_run.finished_at = datetime.now(timezone.utc).isoformat()

            except Exception as exc:
                logger.exception(f"Falha na execução do job {job_to_run.id}")
                with self._lock:
                    job_to_run.status = "failed"
                    job_to_run.error = str(exc)
                    job_to_run.current_step = f"Falha: {str(exc)}"
                    job_to_run.finished_at = datetime.now(timezone.utc).isoformat()
                self.append_log(job_to_run.id, f"❌ Erro na execução: {exc}", log_type="error")

            finally:
                with self._lock:
                    # Move o job para o início do histórico
                    self._history.insert(0, job_to_run)
                    self._active_job = None
                    self._cancel_requested_for = None
                    self._trim_and_save_history()
                    self._broadcast_change()


# Instância global singleton do gerenciador de filas
job_queue = JobQueueManager()
