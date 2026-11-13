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
    """Lista todos os projetos disponíveis em todas as organizações visíveis pela conta e pessoais."""
    with get_cvat_client() as client:
        projects = []
        seen_ids = set()

        # 1. Projetos pessoais (sem organização)
        try:
            client.organization_slug = ""
            for p in client.projects.list():
                if p.id not in seen_ids:
                    seen_ids.add(p.id)
                    projects.append({
                        "id": int(p.id),
                        "name": str(p.name),
                        "organization": "",
                        "display_name": f"{p.name} (Sem organização)",
                    })
        except Exception:
            pass

        # 2. Projetos em cada organização acessível
        try:
            orgs = list(client.organizations.list())
            for org in orgs:
                slug = getattr(org, "slug", "")
                name = getattr(org, "name", slug)
                client.organization_slug = slug
                for p in client.projects.list():
                    if p.id not in seen_ids:
                        seen_ids.add(p.id)
                        projects.append({
                            "id": int(p.id),
                            "name": str(p.name),
                            "organization": slug,
                            "display_name": f"{p.name} [{slug}]",
                        })
        except Exception:
            pass

        return sorted(projects, key=lambda x: (x.get("organization") or "", x["name"].lower()))


def list_organizations() -> List[Dict[str, str]]:
    """Lista todas as organizações CVAT acessíveis pela conta."""
    with get_cvat_client() as client:
        try:
            return [
                {
                    "slug": str(getattr(org, "slug", "")),
                    "name": str(getattr(org, "name", "") or getattr(org, "slug", "")),
                }
                for org in client.organizations.list()
                if getattr(org, "slug", None)
            ]
        except Exception:
            return []


def inspect_task_workflow(task: Any) -> Dict[str, Any]:
    """
    Analisa os jobs e status da tarefa no CVAT e retorna indicadores estruturados:
    is_completed, stage, state, status, label amigável e classe de estilo.
    """
    try:
        jobs = list(task.get_jobs())
        if not jobs:
            return {
                "completed": False,
                "status": "empty",
                "stage": "annotation",
                "state": "sem jobs",
                "label": "Sem Jobs",
                "badge_class": "badge bg-secondary-subtle text-secondary border",
            }

        stages = [getattr(getattr(j, "stage", None), "value", str(getattr(j, "stage", ""))) for j in jobs]
        states = [getattr(getattr(j, "state", None), "value", str(getattr(j, "state", ""))) for j in jobs]
        raw_status = getattr(getattr(task, "status", None), "value", str(getattr(task, "status", ""))).lower()

        # Se todos os jobs estão 'completed'
        all_completed = all(s == "completed" for s in states)
        has_validation = any(s == "validation" for s in stages) or raw_status == "validation"
        has_in_progress = any(s == "in progress" for s in states)

        if all_completed:
            return {
                "completed": True,
                "status": "completed",
                "stage": stages[0] if stages else "annotation",
                "state": "completed",
                "label": "Validado / Concluído",
                "badge_class": "badge bg-success-subtle text-success border border-success-subtle",
            }

        if has_validation:
            if has_in_progress:
                return {
                    "completed": False,
                    "status": "validation",
                    "stage": "validation",
                    "state": "in progress",
                    "label": "Em Validação",
                    "badge_class": "badge bg-info-subtle text-info-emphasis border border-info-subtle",
                }
            return {
                "completed": False,
                "status": "validation",
                "stage": "validation",
                "state": "new",
                "label": "Pré-Anotado (Validação)",
                "badge_class": "badge bg-warning-subtle text-warning-emphasis border border-warning-subtle",
            }

        if has_in_progress:
            return {
                "completed": False,
                "status": "annotation",
                "stage": "annotation",
                "state": "in progress",
                "label": "Em Anotação",
                "badge_class": "badge bg-primary-subtle text-primary border border-primary-subtle",
            }

        return {
            "completed": False,
            "status": "annotation",
            "stage": "annotation",
            "state": states[0] if states else "new",
            "label": "Novo / Sem Anotação",
            "badge_class": "badge bg-secondary-subtle text-secondary border",
        }
    except Exception as exc:
        return {
            "completed": False,
            "status": "unknown",
            "stage": "unknown",
            "state": "erro",
            "label": f"Erro ({exc})",
            "badge_class": "badge bg-danger-subtle text-danger border",
        }


def task_is_completed(task: Any) -> Tuple[bool, str]:
    """
    Verifica se todos os jobs da tarefa estão com estado 'completed'.
    """
    info = inspect_task_workflow(task)
    return info["completed"], info["state"]


def get_project_tasks_summary(project_id: int) -> List[Dict[str, Any]]:
    """
    Retorna as tarefas de um projeto com status dos jobs e contagem de imagens ativas (descontando deletadas).
    """
    with get_cvat_client() as client:
        project = client.projects.retrieve(project_id)
        tasks_data = []
        for task in project.get_tasks():
            info = inspect_task_workflow(task)
            meta = task.get_meta()
            size = getattr(meta, "size", 0)
            deleted_frames = getattr(meta, "deleted_frames", []) or []
            if isinstance(meta, dict):
                size = meta.get("size", 0)
                deleted_frames = meta.get("deleted_frames", []) or []
            deleted_count = len(deleted_frames)
            active_size = max(0, int(size) - deleted_count)
            tasks_data.append({
                "id": int(task.id),
                "name": str(task.name),
                "completed": info["completed"],
                "status": info["status"],
                "stage": info["stage"],
                "state": info["state"],
                "status_label": info["label"],
                "badge_class": info["badge_class"],
                "images_count": active_size,
                "total_size": int(size),
                "deleted_count": deleted_count,
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
