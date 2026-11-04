from __future__ import annotations

from typing import Any

from cvat_sdk import make_client

from .config import Config


def connect(config: Config):
    kwargs: dict[str, Any] = {}
    if config.token:
        kwargs["access_token"] = config.token
    elif config.username and config.password:
        kwargs["credentials"] = (config.username, config.password)
    else:
        raise RuntimeError("Configure CVAT_TOKEN ou CVAT_USERNAME/CVAT_PASSWORD em cvat_yolo/.env.")
    return make_client(config.host, **kwargs)


def project_tasks(project: Any) -> list[Any]:
    return sorted(list(project.get_tasks()), key=lambda task: task.id)


def labels_by_id(items: list[Any]) -> dict[int, str]:
    return {int(item.id): str(item.name) for item in items}


def task_is_completed(task: Any) -> tuple[bool, str]:
    jobs = list(task.get_jobs())
    if not jobs:
        return False, "sem jobs"
    states = [str(getattr(job, "state", "unknown")).lower() for job in jobs]
    complete = all(state == "completed" for state in states)
    return complete, ", ".join(sorted(set(states)))
