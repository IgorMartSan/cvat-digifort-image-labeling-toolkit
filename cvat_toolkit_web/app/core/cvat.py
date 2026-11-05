from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple
from cvat_sdk import Client
from cvat_sdk.api_client import Configuration

from app.core.config import Settings


def get_cvat_client() -> Client:
    """Retorna uma instância conectada do CVAT SDK."""
    current_settings = Settings.load()
    if current_settings.token:
        client = Client(url=current_settings.host, check_server_version=False)
        client.api_client.set_default_header("Authorization", f"Token {current_settings.token}")
        return client
    if current_settings.username and current_settings.password:
        client = Client(url=current_settings.host, check_server_version=False)
        client.login((current_settings.username, current_settings.password))
        return client
    return Client(url=current_settings.host, check_server_version=False)


def check_cvat_connection() -> Dict[str, Any]:
    """Testa a conectividade com o CVAT e retorna status e versão."""
    current_settings = Settings.load()
    try:
        with get_cvat_client() as client:
            # Testa consulta listando projetos
            _ = next(iter(client.projects.list()), None)
            return {
                "connected": True,
                "host": current_settings.host,
                "message": "Conectado ao CVAT com sucesso",
            }
    except Exception as exc:
        return {
            "connected": False,
            "host": current_settings.host,
            "message": f"Falha na conexão: {str(exc)}",
        }


def list_projects() -> List[Dict[str, Any]]:
    """Lista todos os projetos disponíveis."""
    with get_cvat_client() as client:
        projects = []
        for p in client.projects.list():
            projects.append({
                "id": int(p.id),
                "name": str(p.name),
            })
        return sorted(projects, key=lambda x: x["name"].lower())


def task_is_completed(task: Any) -> Tuple[bool, str]:
    """
    Verifica se todos os jobs da tarefa estão com estado 'completed'.
    """
    try:
        jobs = list(task.get_jobs())
        if not jobs:
            return False, "sem jobs"
        states = [getattr(getattr(j, "state", None), "value", str(getattr(j, "state", ""))) for j in jobs]
        if all(s == "completed" for s in states):
            return True, "completed"
        return False, ", ".join(sorted(set(states)))
    except Exception:
        return False, "erro ao ler jobs"


def get_project_tasks_summary(project_id: int) -> List[Dict[str, Any]]:
    """
    Retorna as tarefas de um projeto com status dos jobs e contagem de imagens.
    """
    with get_cvat_client() as client:
        project = client.projects.retrieve(project_id)
        tasks_data = []
        for task in project.get_tasks():
            done, state = task_is_completed(task)
            meta = task.get_meta()
            size = getattr(meta, "size", 0)
            if isinstance(meta, dict):
                size = meta.get("size", 0)
            tasks_data.append({
                "id": int(task.id),
                "name": str(task.name),
                "completed": done,
                "state": state,
                "images_count": int(size),
            })
        return sorted(tasks_data, key=lambda x: x["id"])


def get_project_labels(project_id: int) -> Dict[int, str]:
    """Retorna dicionário {label_id: label_name} para o projeto."""
    with get_cvat_client() as client:
        project = client.projects.retrieve(project_id)
        labels = {}
        for lbl in project.get_labels():
            labels[int(lbl.id)] = str(lbl.name)
        return labels
