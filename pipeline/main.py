"""Servico de Pipeline unificada de coleta de imagens e envio resiliente ao CVAT.

Este script opera como um servico continuo (daemon/background service).
Ele gerencia os workers assincronos das cameras e o worker do uploader CVAT.

Execucao:
  uv run python main.py                 : executar o servico continuo.
  uv run python main.py --check         : validar configuracoes e sintaxe sem conectar.
  uv run python main.py --collect-only  : executar exclusivamente a coleta das cameras.
  uv run python main.py --dry-run       : simular envio sem criar tasks no CVAT.
  uv run python main.py --send          : forcar envio real ao CVAT.

Resiliencia:
  - Cada camera tenta reconectar a cada 13 segundos infinitamente se cair.
  - O CVAT tenta reconectar a cada 13 segundos infinitamente se o servidor estiver fora.
  - O servico NUNCA encerra por falha de conexao ou instabilidade temporaria.
  - As imagens acumuladas durante a indisponibilidade do CVAT sao despachadas
    em lotes cronologicos assim que o login for restabelecido.
"""

import argparse
import asyncio
import json
import logging
import math
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
from collections import Counter
from datetime import datetime, timedelta
from io import BytesIO
from itertools import groupby
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from cvat_sdk import make_client
from cvat_sdk.core.proxies.tasks import ResourceType
from dotenv import dotenv_values, load_dotenv
from PIL import Image

# Determina a raiz do repositorio de forma independente de onde o script e invocado
CURRENT_FILE = Path(__file__).resolve()
BASE_DIR = CURRENT_FILE.parent.parent if CURRENT_FILE.parent.name == "pipeline" else CURRENT_FILE.parent
DEFAULT_RETRY_SECONDS = 13.0
STAMP = "%Y-%m-%d_%H-%M-%S"
HISTORY_BEGIN = datetime(1970, 1, 2).timestamp()
PENDING_SUFFIX = " | uploading"
EXTENSIONS = {
    "image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp",
    "image/gif": ".gif", "image/bmp": ".bmp", "image/tiff": ".tiff"
}
CVAT_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff", ".gif"}
DATE_FORMATS = (
    "%Y-%m-%d_%H-%M-%S_%f", "%Y-%m-%d_%H-%M-%S",
    "%Y_%m_%d_%H_%M_%S_%f", "%Y_%m_%d_%H_%M_%S"
)
CONFIG_KEYS = {
    "CAMERAS", "OUTPUT_DIR", "REQUEST_TIMEOUT", "FOLDERS", "CVAT_URL",
    "CVAT_USERNAME", "CVAT_PASSWORD", "CVAT_ORGANIZATION", "CVAT_LABELS",
    "CVAT_PROJECT_ID", "IMAGES_DIR", "IMAGES_PER_TASK", "CAMERA_NAME",
    "DRY_RUN", "LOOP", "AUTO_REMOVE", "POLL_INTERVAL_SECONDS", "CONFIG_ENV_FILE",
    "VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT", "RESTART_SECONDS"
}


# ============================================================================
# Utilitarios e Validacao de Configuracao
# ============================================================================

def positive_number(value):
    if isinstance(value, bool):
        raise ValueError("Intervalos e timeout devem ser numeros positivos.")
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ValueError("Intervalos e timeout devem ser numeros positivos.")
    return number


def resolution(value):
    if (not isinstance(value, list) or len(value) != 2
            or any(type(dimension) is not int or dimension <= 0 for dimension in value)):
        raise ValueError("Resolucao deve ser [largura, altura], com inteiros positivos.")
    return tuple(value)


def parse_date(value, now):
    if value is None or value == "":
        return None
    if type(value) in (int, float):
        try:
            if not math.isfinite(value) or value < 0:
                raise ValueError("Dias atras devem ser um numero finito >= 0.")
            result = now - value * 86400
            datetime.fromtimestamp(result)
        except (OverflowError, OSError) as error:
            raise ValueError("Numero de dias fora da faixa suportada.") from error
        return result
    if not isinstance(value, str):
        raise ValueError("Data deve ser ISO 8601, numero de dias atras ou null.")
    return datetime.fromisoformat(value).timestamp()


def json_list(values, key):
    value = json.loads(values.get(key) or "[]")
    if not isinstance(value, list) or not value or any(not isinstance(item, dict) for item in value):
        raise ValueError(f"{key} deve ser uma lista JSON de objetos nao vazia.")
    return value


def cvat_problem(values):
    for key in ("CVAT_URL", "CVAT_USERNAME", "CVAT_PASSWORD"):
        if not values.get(key):
            return f"falta {key}"
    try:
        url = urlsplit(values["CVAT_URL"])
        if url.scheme not in ("http", "https") or not url.hostname or url.username or url.password:
            return "CVAT_URL invalida; use credenciais nos campos separados"
        folders = json_list(values, "FOLDERS")
        for folder in folders:
            if not isinstance(folder.get("images_dir"), str) or not folder["images_dir"].strip():
                return "falta images_dir em FOLDERS"
            if not folder.get("project_id") and not folder.get("organization", values.get("CVAT_ORGANIZATION")):
                return "falta organizacao para criar/buscar projetos pela pasta"
    except (ValueError, TypeError):
        return "JSON ou campos de configuracao CVAT invalidos"
    return None


def stage(name, folder, env_file):
    environment = {key: value for key, value in os.environ.items() if key not in CONFIG_KEYS}
    environment["CONFIG_ENV_FILE"] = str(env_file)
    return dict(name=name, folder=BASE_DIR / folder, env_file=env_file, env=environment,
                phase="sync", process=None, done=False, restart_at=0)


def load_pipeline(path, collect_only=False):
    path = path.resolve()
    if not path.is_file():
        raise ValueError("Arquivo da pipeline ausente. Copie pipeline.env.example para pipeline.env.")
    values = dotenv_values(path, interpolate=False)
    config = json.loads(values.get("PIPELINE") or "{}")
    if not isinstance(config, dict):
        raise ValueError("PIPELINE deve ser um objeto JSON.")
    restart = values.get("RESTART_SECONDS", "13")
    if not str(restart).isdigit() or int(restart) <= 0:
        raise ValueError("RESTART_SECONDS deve ser inteiro positivo.")
    cameras = config.get("cameras")
    if not isinstance(cameras, dict) or not isinstance(cameras.get("env_file"), str):
        raise ValueError("Informe cameras.env_file no JSON PIPELINE.")
    camera_file = (path.parent / cameras["env_file"]).resolve()
    if not camera_file.is_file():
        raise ValueError("Arquivo de configuracao das cameras nao encontrado.")
    json_list(dotenv_values(camera_file, interpolate=False), "CAMERAS")
    stages = [stage("coleta", "digifort-scripts", camera_file)]
    cvat = config.get("cvat")
    if collect_only or cvat is None:
        logging.info("Etapa CVAT desativada; executando somente coleta.")
        return stages, int(restart)
    if not isinstance(cvat, dict) or type(cvat.get("enabled", True)) is not bool:
        logging.warning("Etapa CVAT ignorada: configuracao de etapa invalida. Coleta preservada.")
        return stages, int(restart)
    if not cvat.get("enabled", True):
        logging.info("Etapa CVAT desativada; executando somente coleta.")
        return stages, int(restart)
    if not isinstance(cvat.get("env_file"), str):
        logging.warning("Etapa CVAT ignorada: falta cvat.env_file. Coleta preservada.")
        return stages, int(restart)
    cvat_file = (path.parent / cvat["env_file"]).resolve()
    if not cvat_file.is_file():
        problem = "arquivo nao encontrado"
    else:
        try:
            problem = cvat_problem(dotenv_values(cvat_file, interpolate=False))
        except OSError:
            problem = "arquivo sem acesso de leitura"
    if problem:
        logging.warning("Etapa CVAT ignorada: %s. Coleta continua; corrija e reinicie a pipeline.", problem)
    else:
        stages.append(stage("cvat", "cvat_scripts", cvat_file))
        if dotenv_values(cvat_file, interpolate=False).get("DRY_RUN", "true").strip().lower() != "false":
            logging.info("Etapa CVAT em simulacao: configure DRY_RUN=false no arquivo referenciado para enviar.")
    return stages, int(restart)


# ============================================================================
# Coletor de Cameras (HTTP / Digifort)
# ============================================================================

def read_progress(camera):
    progress_file = camera["path"] / ".history.json"
    if not progress_file.is_file():
        return {"found": False, "next": None, "done": False, "last_image": None, "last_image_at": None}
    try:
        data = json.loads(progress_file.read_text(encoding="utf-8"))
        if data.get("folder") != camera["folder"]:
            return {"found": False, "next": None, "done": False, "last_image": None, "last_image_at": None}
        return {
            "found": bool(data.get("found", False)),
            "next": data.get("next"),
            "done": bool(data.get("done", False)),
            "last_image": data.get("last_image"),
            "last_image_at": data.get("last_image_at"),
        }
    except Exception:
        return {"found": False, "next": None, "done": False, "last_image": None, "last_image_at": None}


def write_progress(camera, state):
    data = {"folder": camera["folder"], **state}
    target = camera["path"] / ".history.json"
    with tempfile.NamedTemporaryFile(dir=camera["path"], suffix=".tmp", delete=False, mode="w", encoding="utf-8") as stream:
        json.dump(data, stream, indent=2)
        temporary = Path(stream.name)
    temporary.replace(target)


def latest_capture(camera):
    latest = None
    if not camera["path"].exists():
        return None
    for path in camera["path"].iterdir():
        if not path.is_file() or path.suffix.lower() not in EXTENSIONS.values():
            continue
        for pattern in (STAMP, STAMP + "_%f"):
            try:
                captured = datetime.strptime(path.stem, pattern).timestamp()
                latest = captured if latest is None else max(latest, captured)
                break
            except ValueError:
                continue
    return latest


def next_capture(camera):
    latest = latest_capture(camera)
    state = read_progress(camera)
    if state["found"] and state["next"] is not None:
        return state["next"] if latest is None else max(state["next"], latest + camera["interval_seconds"])
    if latest is None:
        return time.time() if camera["start"] is None else camera["start"]
    return latest + camera["interval_seconds"]


def save_image(folder, content, captured, extension, resize=None, quality=100, historical=False):
    pattern = STAMP + "_%f" if historical and captured.microsecond else STAMP
    target = folder / (captured.strftime(pattern) + extension)
    if historical and target.exists():
        return target
    if resize is not None:
        with Image.open(BytesIO(content)) as image:
            image_format = image.format
            image.thumbnail(resize, Image.Resampling.LANCZOS)
            output = BytesIO()
            options = {"quality": quality, "subsampling": 0} if image_format == "JPEG" else {}
            image.save(output, format=image_format, **options)
            content = output.getvalue()
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=folder, suffix=".part", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content)
        stamp = captured
        while target.exists():
            target = folder / (stamp.strftime(STAMP + "_%f") + extension)
            stamp += timedelta(microseconds=1)
        temporary.rename(target)
        return target
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


async def save_response(camera, response, captured, historical=False):
    media_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
    if media_type not in EXTENSIONS or not response.content:
        raise ValueError("Resposta vazia ou Content-Type de imagem nao suportado.")
    saving = asyncio.create_task(asyncio.to_thread(
        save_image, camera["path"], response.content, captured, EXTENSIONS[media_type],
        camera.get("resize"), camera.get("quality", 100), historical,
    ))
    try:
        path = await asyncio.shield(saving)
    except asyncio.CancelledError:
        await saving
        raise
    logging.info("[%s] Imagem salva: %s", camera["folder"], path.name)
    return path


async def request(client, url, timeout):
    async with asyncio.timeout(timeout):
        response = await client.get(url)
    response.raise_for_status()
    return response


def playback_url(camera, endpoint, start, end=None):
    url = httpx.URL(camera["url"]).copy_with(path="/Interface/Cameras/Playback/" + endpoint)
    start_dt = datetime.fromtimestamp(start)
    params = {"ResponseFormat": "Text"}
    if end is None:
        width, height = camera.get("capture_resolution", [-1, -1])
        params.update(Date=start_dt.strftime("%Y.%m.%d"), Time=start_dt.strftime("%H.%M.%S.%f")[:-3],
                      Width=str(width), Height=str(height), Quality=str(camera.get("quality", 100)))
    else:
        end_dt = datetime.fromtimestamp(end)
        params.update(StartDate=start_dt.strftime("%Y.%m.%d"), StartTime=start_dt.strftime("%H.%M.%S.%f")[:-3],
                      EndDate=end_dt.strftime("%Y.%m.%d"), EndTime=end_dt.strftime("%H.%M.%S.%f")[:-3],
                      Video="TRUE", Audio="FALSE", Metadata="FALSE")
    return url.copy_merge_params(params)


def first_recording(response, start, end):
    codes = re.findall(r"RESPONSE_CODE=(\d+)", response.text)
    if any(code != "0" for code in codes):
        raise ValueError("Digifort recusou a consulta de gravacoes.")
    dates = []
    for line in response.text.splitlines():
        match = re.match(r"^(\d{4}\.\d{2}\.\d{2}) (\d{2}\.\d{2}\.\d{2}\.\d{3})", line.strip())
        if match:
            try:
                date_str, time_str = match.groups()
                dt = datetime.strptime(f"{date_str} {time_str}", "%Y.%m.%d %H.%M.%S.%f")
                ts = dt.timestamp()
                if start <= ts <= end:
                    dates.append(ts)
            except ValueError:
                pass
    return min(dates) if dates else None


async def collect_history(camera, client, timeout, stop_event, retry_seconds=DEFAULT_RETRY_SECONDS):
    state = read_progress(camera)
    if state["done"]:
        return
    horizon = time.time() if camera.get("end") is None else camera["end"]
    while not stop_event.is_set():
        if state["done"]:
            return
        due = state["next"] if state["next"] is not None else (camera["start"] if camera["start"] is not None else HISTORY_BEGIN)
        if due > horizon:
            state["done"] = True
            write_progress(camera, state)
            logging.info("[%s] Historico concluido.", camera["folder"])
            return
        updated = dict(state)
        try:
            if not state["found"]:
                end = min(due + camera.get("history_search_hours", 6) * 3600, horizon)
                url = playback_url(camera, "GetTimelineData", due, end)
                response = await request(client, url, timeout)
                first = first_recording(response, due, end)
                updated["next"] = end if first is None else first
                updated["found"] = first is not None
                updated["done"] = first is None and end == horizon
                logging.info("[%s] Busca de historico: %s (%s)", camera["folder"],
                             datetime.fromtimestamp(due).isoformat(), "encontrado" if first is not None else "sem gravacao")
            else:
                due = round(due, 3)
                captured = datetime.fromtimestamp(due)
                pattern = STAMP + "_%f" if captured.microsecond else STAMP
                existing = camera["path"] / (captured.strftime(pattern) + ".jpg")
                saved_path = existing if existing.exists() else None
                if saved_path is None:
                    response = await request(client, playback_url(camera, "GetSnapshot", due), timeout)
                    missing = re.search(r"RESPONSE_CODE=1020[01](?:\D|$)", response.text) if not response.headers.get("content-type", "").startswith("image/") else None
                    if missing:
                        logging.warning("[%s] Sem imagem em %s; mantendo intervalo.", camera["folder"], captured.isoformat())
                    else:
                        saved_path = await save_response(camera, response, captured, historical=True)
                if saved_path is not None:
                    updated.update(last_image=saved_path.name, last_image_at=captured.isoformat(timespec="microseconds"))
                updated["next"] = round(due + camera["interval_seconds"], 3)
            write_progress(camera, updated)
            state = updated
            if state["done"]:
                logging.info("[%s] Historico concluido.", camera["folder"])
                return
        except (httpx.HTTPError, TimeoutError, OSError, ValueError) as error:
            logging.warning("[%s] Falha no historico (%s); reconectando a camera em %gs...",
                            camera["folder"], type(error).__name__, retry_seconds)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=retry_seconds)
                break
            except TimeoutError:
                pass


async def collect_live(camera, client, timeout, stop_event, retry_seconds=DEFAULT_RETRY_SECONDS):
    try:
        due = next_capture(camera)
    except OSError:
        due = time.time() if camera["start"] is None else camera["start"]
    interval = camera["interval_seconds"]
    end_date = camera.get("end")
    while not stop_event.is_set():
        if end_date is not None and (time.time() > end_date or due > end_date):
            logging.info("[%s] Periodo concluido.", camera["folder"])
            return
        sleep_time = max(0, due - time.time())
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=sleep_time)
            break
        except TimeoutError:
            pass
        if stop_event.is_set():
            break
        attempted = time.time()
        if end_date is not None and attempted > end_date:
            logging.info("[%s] Periodo concluido.", camera["folder"])
            return
        try:
            req_timeout = timeout if end_date is None else min(timeout, end_date - attempted)
            response = await request(client, camera["url"], req_timeout)
            captured = datetime.now()
            if end_date is not None and captured.timestamp() > end_date:
                return
            path = await save_response(camera, response, captured)
            due = captured.timestamp() + interval
            state = read_progress(camera)
            state.update(next=due, found=True, done=True, last_image=path.name,
                         last_image_at=captured.isoformat(timespec="microseconds"))
            write_progress(camera, state)
        except (httpx.HTTPError, TimeoutError, OSError, ValueError) as error:
            detail = f"HTTP {error.response.status_code}" if isinstance(error, httpx.HTTPStatusError) else type(error).__name__
            logging.warning("[%s] Falha na captura (%s); reconectando a camera em %gs...",
                            camera["folder"], detail, retry_seconds)
            due = time.time() + retry_seconds
            continue
        if due <= time.time():
            due = time.time() + interval


async def camera_worker(camera, client, timeout, stop_event, retry_seconds=DEFAULT_RETRY_SECONDS):
    logging.info("[%s] Worker da camera ativo. Intervalo: %gs; retry: %gs.",
                 camera["folder"], camera["interval_seconds"], retry_seconds)
    while not stop_event.is_set():
        try:
            if camera.get("history"):
                await collect_history(camera, client, timeout, stop_event, retry_seconds)
            await collect_live(camera, client, timeout, stop_event, retry_seconds)
            break
        except asyncio.CancelledError:
            break
        except Exception as error:
            logging.warning("[%s] Erro inesperado na camera (%s: %s); repetindo em %gs...",
                            camera["folder"], type(error).__name__, error, retry_seconds)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=retry_seconds)
                break
            except TimeoutError:
                pass


# ============================================================================
# Uploader CVAT
# ============================================================================

def image_date(name):
    stem = Path(name.replace("\\", "/")).stem
    for pattern in DATE_FORMATS:
        try:
            return datetime.strptime(stem, pattern)
        except ValueError:
            pass
    return None


def file_signature(path):
    stats = path.stat()
    return stats.st_size, stats.st_mtime_ns


def task_name(camera, images):
    first = images[0][0].isoformat(timespec="microseconds")
    last = images[-1][0].isoformat(timespec="microseconds")
    return f"{camera} | {first} | {last}"


def parse_task(task):
    if not task.name or task.name.endswith(PENDING_SUFFIX):
        return None
    parts = [part.strip() for part in task.name.split("|")]
    if len(parts) == 3:
        try:
            return {
                "camera": parts[0].casefold(),
                "start": datetime.fromisoformat(parts[1]),
                "end": datetime.fromisoformat(parts[2]),
                "legacy": False,
            }
        except ValueError:
            pass
    return None


def scan_cameras(images_dir):
    cameras = {}
    if not images_dir.is_dir():
        return cameras
    for path in images_dir.iterdir():
        if path.is_file() and not path.name.endswith(".part") and path.suffix.lower() in CVAT_EXTENSIONS:
            date = image_date(path.name)
            if date is not None:
                cameras.setdefault(images_dir.name, []).append((date, path))
    for images in cameras.values():
        images.sort(key=lambda item: item[0])
    return cameras


def existing_periods(tasks, cameras):
    periods, issues = [], []
    for task in tasks:
        parsed = parse_task(task)
        if parsed:
            periods.append(parsed)
        elif task.name and task.name.endswith(PENDING_SUFFIX):
            issues.append(f"Task pendente: {task.id} ({task.name}). Revise no CVAT antes de prosseguir.")
    return periods, issues


def plan_batches(cameras, periods, images_per_task):
    batches, skipped_total = [], 0
    for camera_name, images in cameras.items():
        matched = [p for p in periods if p["camera"] == camera_name.casefold()]
        latest_end = max((p["end"] for p in matched), default=None)
        if latest_end is not None:
            new_images = [img for img in images if img[0] > latest_end]
            skipped_total += len(images) - len(new_images)
        else:
            new_images = images
        for i in range(0, len(new_images) - len(new_images) % images_per_task, images_per_task):
            batches.append((camera_name, new_images[i:i + images_per_task]))
    return batches, skipped_total


def confirm_upload(task, expected_images):
    expected_count = len(expected_images)
    task.fetch()
    if getattr(task, "size", None) != expected_count:
        raise ValueError(f"Task {task.id} reportou {task.size} frames; esperado {expected_count}.")


def remove_uploaded(images, signatures, images_dir):
    for _, path in images:
        try:
            if signatures.get(path) == file_signature(path):
                path.unlink(missing_ok=True)
        except OSError:
            pass


def resolve_project(client, config, dry_run):
    project_id = config.get("CVAT_PROJECT_ID")
    if project_id:
        return client.projects.retrieve(int(project_id))
    camera_name = config["IMAGES_DIR"].name
    projects = client.projects.list()
    matched = [p for p in projects if p.name.strip().casefold() == camera_name.strip().casefold()]
    if len(matched) == 1:
        return matched[0]
    if len(matched) > 1:
        raise ValueError(f"Multiplos projetos encontrados para {camera_name}. Especifique project_id.")
    if dry_run:
        return None
    labels = config.get("CVAT_LABELS", [])
    spec = {"name": camera_name}
    if labels:
        spec["labels"] = [{"name": label} for label in labels]
    return client.projects.create(spec=spec)


def run_cvat_folder(client, config, dry_run):
    cameras = scan_cameras(config["IMAGES_DIR"])
    if not any(cameras.values()) and config.get("CVAT_PROJECT_ID") is not None:
        return
    project = resolve_project(client, config, dry_run)
    if not any(cameras.values()):
        return
    periods, issues = existing_periods(project.get_tasks(), cameras) if project else ([], [])
    if issues:
        for issue in issues:
            logging.error(issue)
        raise ValueError("Upload CVAT bloqueado por pendencias acima.")
    batches, skipped = plan_batches(cameras, periods, config["IMAGES_PER_TASK"])
    if not batches:
        waiting = sum(len(imgs) for imgs in cameras.values()) - skipped
        if waiting > 0:
            logging.info("[%s] Lote incompleto (%d/%d imagens). Aguardando novas capturas.",
                         config["IMAGES_DIR"].name, waiting, config["IMAGES_PER_TASK"])
        return
    logging.info("[%s] %d lote(s) pronto(s) para upload (total %d imagens).",
                 config["IMAGES_DIR"].name, len(batches), sum(len(imgs) for _, imgs in batches))
    if dry_run:
        for camera, images in batches:
            logging.info("SIMULACAO: Criaria task '%s' com %d imagens.", task_name(camera, images), len(images))
        return
    for camera, images in batches:
        name = task_name(camera, images)
        signatures = {path: file_signature(path) for _, path in images}
        task = client.tasks.create(spec={"name": name + PENDING_SUFFIX, "project_id": project.id})
        logging.info("Task %s criada: %s; iniciando upload...", task.id, name)
        try:
            task.upload_data(
                resources=[str(path) for _, path in images],
                resource_type=ResourceType.LOCAL,
                params={"image_quality": 100, "sorting_method": "predefined"}
            )
            confirm_upload(task, images)
            task.update({"name": name})
            logging.info("Task %s concluida e confirmada com %d imagens.", task.id, len(images))
            if config.get("AUTO_REMOVE", False):
                remove_uploaded(images, signatures, config["IMAGES_DIR"])
        except Exception:
            logging.error("Falha no upload da task %s.", task.id)
            raise


def cvat_cycle(cvat_config, dry_run=False):
    folders = cvat_config.get("FOLDERS", [])
    if not folders:
        return
    with make_client(host=cvat_config["CVAT_URL"],
                     credentials=(cvat_config["CVAT_USERNAME"], cvat_config["CVAT_PASSWORD"])) as client:
        if cvat_config.get("CVAT_ORGANIZATION"):
            client.organization_slug = cvat_config["CVAT_ORGANIZATION"]
        for folder_cfg in folders:
            merged = {**cvat_config, **folder_cfg}
            run_cvat_folder(client, merged, dry_run)


async def cvat_worker(cvat_config, stop_event, dry_run=False, retry_seconds=DEFAULT_RETRY_SECONDS):
    logging.info("[CVAT] Worker do uploader ativo. Verificando a cada %gs; retry: %gs.",
                 cvat_config.get("POLL_INTERVAL_SECONDS", retry_seconds), retry_seconds)
    poll_interval = cvat_config.get("POLL_INTERVAL_SECONDS", retry_seconds)
    while not stop_event.is_set():
        try:
            await asyncio.to_thread(cvat_cycle, cvat_config, dry_run)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=poll_interval)
                break
            except TimeoutError:
                pass
        except asyncio.CancelledError:
            break
        except Exception as error:
            logging.warning("[CVAT] Falha na comunicacao com CVAT (%s: %s); tentando reconectar em %gs...",
                            type(error).__name__, error, retry_seconds)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=retry_seconds)
                break
            except TimeoutError:
                pass


# ============================================================================
# Orquestracao da Pipeline Unificada
# ============================================================================

def parse_cameras_config(camera_env_path):
    values = dotenv_values(camera_env_path, interpolate=False)
    raw_cameras = json.loads(values.get("CAMERAS") or "[]")
    if not isinstance(raw_cameras, list) or not raw_cameras:
        raise ValueError("CAMERAS deve ser uma lista JSON com ao menos uma camera.")
    output_dir = Path(values.get("OUTPUT_DIR", "./volume/image"))
    output_dir = (camera_env_path.parent / output_dir).resolve()
    timeout = positive_number(values.get("REQUEST_TIMEOUT", "10"))
    now = time.time()
    cameras = []
    for index, camera in enumerate(raw_cameras, 1):
        c = dict(camera)
        folder = c.get("folder", "")
        if not isinstance(folder, str) or not folder.strip():
            raise ValueError(f"Camera {index}: nome de pasta invalido.")
        c["interval_seconds"] = positive_number(c.get("interval_seconds", 60))
        c["start"] = parse_date(c.get("start_date"), now)
        c["end"] = parse_date(c.get("end_date"), now)
        c["quality"] = int(c.get("quality", 100))
        c["resize"] = resolution(c["resize"]) if c.get("resize") is not None else None
        capture_size = resolution(c.get("capture_resolution", [1920, 1080]))
        c["capture_resolution"] = capture_size
        url = httpx.URL(c["url"])
        if url.path.lower() == "/interface/cameras/getsnapshot":
            c["url"] = str(url.copy_merge_params({
                "Profile": url.params.get("Profile", "Recording"),
                "Width": str(capture_size[0]), "Height": str(capture_size[1]),
                "KeepAspectRatio": "TRUE", "Quality": str(c["quality"]),
            }))
        c["path"] = (output_dir / folder).resolve()
        c["path"].mkdir(parents=True, exist_ok=True)
        cameras.append(c)
    return cameras, timeout


def parse_cvat_config(cvat_env_path, dry_run=None, send=False):
    values = dotenv_values(cvat_env_path, interpolate=False)
    folders_raw = json.loads(values.get("FOLDERS") or "[]")
    folders = []
    for f in folders_raw:
        item = dict(f)
        item["IMAGES_DIR"] = (cvat_env_path.parent / item["images_dir"]).resolve()
        item["IMAGES_PER_TASK"] = int(item.get("images_per_task", 50))
        item["AUTO_REMOVE"] = bool(item.get("auto_remove", values.get("AUTO_REMOVE", "false").lower() == "true"))
        folders.append(item)
    is_dry = True
    if send:
        is_dry = False
    elif dry_run is not None:
        is_dry = dry_run
    else:
        is_dry = values.get("DRY_RUN", "true").strip().lower() != "false"
    return {
        "CVAT_URL": values.get("CVAT_URL"),
        "CVAT_USERNAME": values.get("CVAT_USERNAME"),
        "CVAT_PASSWORD": values.get("CVAT_PASSWORD"),
        "CVAT_ORGANIZATION": values.get("CVAT_ORGANIZATION"),
        "CVAT_LABELS": json.loads(values.get("CVAT_LABELS") or "[]"),
        "POLL_INTERVAL_SECONDS": positive_number(values.get("POLL_INTERVAL_SECONDS", 13)),
        "FOLDERS": folders,
        "DRY_RUN": is_dry,
    }


async def run_pipeline_unified(camera_env_path, cvat_env_path=None, collect_only=False,
                               dry_run=None, send=False, retry_seconds=DEFAULT_RETRY_SECONDS):
    cameras, timeout = parse_cameras_config(camera_env_path)
    cvat_config = None
    if not collect_only and cvat_env_path and cvat_env_path.is_file():
        try:
            cvat_config = parse_cvat_config(cvat_env_path, dry_run, send)
        except Exception as error:
            logging.warning("Configuracao CVAT invalida (%s). Executando somente coleta.", error)
    stop_event = asyncio.Event()

    def handle_signal():
        logging.info("Sinal recebido. Encerrando pipeline com seguranca...")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (getattr(signal, "SIGINT", None), getattr(signal, "SIGTERM", None)):
        if sig is not None:
            try:
                loop.add_signal_handler(sig, handle_signal)
            except NotImplementedError:
                pass

    tasks = []
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        for camera in cameras:
            task = asyncio.create_task(
                camera_worker(camera, client, timeout, stop_event, retry_seconds=retry_seconds)
            )
            tasks.append(task)
        if cvat_config:
            cvat_task = asyncio.create_task(
                cvat_worker(cvat_config, stop_event, dry_run=cvat_config["DRY_RUN"], retry_seconds=retry_seconds)
            )
            tasks.append(cvat_task)
        logging.info("Pipeline em execucao. %d camera(s) ativas; CVAT: %s. Pressione Ctrl+C para encerrar.",
                     len(cameras), "ativo" if cvat_config else "desativado")
        await stop_event.wait()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    logging.info("Pipeline finalizada.")


# ============================================================================
# Compatibilidade de Execucao por Subprocessos (supervise)
# ============================================================================

def command_for(item, uv):
    if item["phase"] == "sync":
        return [uv, "sync", "--directory", str(item["folder"]), "--cache-dir", str(BASE_DIR / ".uv-cache")]
    python = item["folder"] / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    return [str(python), str(item["folder"] / "main.py")]


def start_stage(item, uv):
    options = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {}
    item["process"] = subprocess.Popen(command_for(item, uv), cwd=item["folder"], env=item["env"], **options)
    logging.info("[%s] %s iniciado (PID %s).", item["name"],
                 "Preparacao das dependencias" if item["phase"] == "sync" else "Processo", item["process"].pid)


def stop_stages(stages):
    for item in stages:
        process = item["process"]
        if process is None or process.poll() is not None:
            continue
        try:
            process.send_signal(signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGINT)
            process.wait(timeout=8)
        except (OSError, subprocess.TimeoutExpired):
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


def supervise(stages, restart, uv):
    try:
        while True:
            for item in stages:
                if item["done"]:
                    continue
                if item["process"] is None:
                    if time.monotonic() >= item["restart_at"]:
                        try:
                            start_stage(item, uv)
                        except OSError:
                            item["restart_at"] = time.monotonic() + restart
                            logging.warning("[%s] Nao iniciou; nova tentativa em %ss.", item["name"], restart)
                    continue
                code = item["process"].poll()
                if code is None:
                    continue
                item["process"] = None
                if code == 0 and item["phase"] == "sync":
                    item["phase"] = "run"
                elif code == 0:
                    item["done"] = True
                    logging.info("[%s] Etapa finalizada normalmente.", item["name"])
                else:
                    item["restart_at"] = time.monotonic() + restart
                    logging.warning("[%s] Processo encerrou com codigo %s; repetindo em %ss. Demais etapas continuam.",
                                    item["name"], code, restart)
            if all(item["done"] for item in stages):
                return
            time.sleep(0.5)
    finally:
        stop_stages(stages)


# ============================================================================
# Ponto de Entrada (CLI)
# ============================================================================

def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", type=Path, default=BASE_DIR / "pipeline.env")
    parser.add_argument("--check", action="store_true", help="Validar referencias, sem conectar/iniciar processos.")
    parser.add_argument("--collect-only", action="store_true", help="Ignorar o CVAT nesta execucao.")
    parser.add_argument("--dry-run", action="store_true", help="Simular envio sem criar tasks no CVAT.")
    parser.add_argument("--send", action="store_true", help="Forcar envio real ao CVAT.")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        stages, restart = load_pipeline(args.config, args.collect_only)
        for item in stages:
            logging.info("Etapa habilitada: %s; configuracao: %s", item["name"], item["env_file"])
        if args.check:
            logging.info("Referencias conferidas com sucesso. Câmeras e CVAT estao prontos para a pipeline.")
            return 0
        camera_env = next((s["env_file"] for s in stages if s["name"] == "coleta"), None)
        cvat_env = next((s["env_file"] for s in stages if s["name"] == "cvat"), None)
        asyncio.run(run_pipeline_unified(
            camera_env_path=camera_env,
            cvat_env_path=cvat_env,
            collect_only=args.collect_only or (cvat_env is None),
            dry_run=args.dry_run if args.dry_run else None,
            send=args.send,
            retry_seconds=restart
        ))
        return 0
    except KeyboardInterrupt:
        logging.info("Pipeline encerrada pelo usuario.")
        return 130
    except (ValueError, OSError) as error:
        logging.error("Pipeline nao iniciou: %s", error)
        return 1


if __name__ == "__main__":
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, signal.default_int_handler)
    raise SystemExit(main())
