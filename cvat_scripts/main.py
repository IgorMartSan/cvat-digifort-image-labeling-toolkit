"""Cria tasks de imagens em um projeto CVAT existente, por camera e periodo.

uv run python main.py --dry-run : consulta o CVAT e mostra o plano, sem enviar.
uv run python main.py --send    : verifica duplicatas e envia os lotes novos.
Sem argumento, usa DRY_RUN do .env (padrao true).

FOLDERS: lista JSON no .env, com um objeto por pasta/projeto.
Cada objeto: images_dir, project_id, images_per_task (padrao 50),
auto_remove (opcional) e organization (opcional).
O nome de cada pasta identifica a camera. Cada pasta tem retomada propria.
LOOP=true: verifica continuamente; POLL_INTERVAL_SECONDS=5 define a espera.
AUTO_REMOVE=true: remove somente os arquivos do lote confirmado no CVAT.
Lotes incompletos aguardam novas imagens. --dry-run sempre verifica uma vez.
CVAT_URL, CVAT_USERNAME, CVAT_PASSWORD: conexao compartilhada.
CVAT_ORGANIZATION e AUTO_REMOVE: padroes globais, substituiveis no JSON.

Nomes aceitos: 2026-10-29_08-00-00.jpg, com _microssegundos opcional, e o formato
antigo 2026_10_29_08_00_00.png. A ordem e cronologica.
Task: CAMERA | primeira-data-hora | ultima-data-hora (inclui microssegundos).
Retoma apos a maior data final das tasks dessa camera, mesmo se mudar os lotes.
Todas as imagens anteriores ou iguais a essa data sao ignoradas, inclusive lacunas.
Tasks antigas sao verificadas pelos nomes das imagens. Camera/periodo ambiguos
bloqueiam o envio e mostram os IDs para revisao. Veja README.md e .env.example.
Execute uma instancia por projeto/camera; o CVAT nao garante nomes unicos.
"""

import argparse
import json
import logging
import os
import re
import time
from collections import Counter
from datetime import datetime
from itertools import groupby
from pathlib import Path
from urllib.parse import urlsplit

from cvat_sdk import make_client
from cvat_sdk.core.proxies.tasks import ResourceType
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
PENDING_SUFFIX = " | uploading"
EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff", ".gif"}
DATE_FORMATS = ("%Y-%m-%d_%H-%M-%S_%f", "%Y-%m-%d_%H-%M-%S",
                "%Y_%m_%d_%H_%M_%S_%f", "%Y_%m_%d_%H_%M_%S")


def image_date(name):
    stem = Path(name.replace("\\", "/")).stem
    for pattern in DATE_FORMATS:
        try:
            return datetime.strptime(stem, pattern)
        except ValueError:
            pass
    return None


def positive_int(value, name):
    if not str(value).isdigit() or int(value) <= 0:
        raise ValueError(f"{name} deve ser um inteiro positivo.")
    return int(value)


def env_bool(name, default):
    value = os.environ.get(name, default).strip().lower()
    if value not in ("true", "false"):
        raise ValueError(f"{name} deve ser true ou false.")
    return value == "true"


def load_config():
    load_dotenv(BASE_DIR / ".env", interpolate=False)
    config = {key: os.environ.get(key, "").strip() for key in (
        "CVAT_URL", "CVAT_USERNAME", "CVAT_ORGANIZATION"
    )}
    config["CVAT_PASSWORD"] = os.environ.get("CVAT_PASSWORD", "")
    for key in ("CVAT_URL", "CVAT_USERNAME", "CVAT_PASSWORD"):
        if not config[key]:
            raise ValueError(f"Configure {key} no .env.")
    url = urlsplit(config["CVAT_URL"])
    if url.scheme not in ("http", "https") or not url.hostname or url.username or url.password:
        raise ValueError("CVAT_URL deve ser HTTP/HTTPS, com credenciais nos campos separados.")
    config["DRY_RUN"] = env_bool("DRY_RUN", "true")
    config["LOOP"] = env_bool("LOOP", "true")
    config["AUTO_REMOVE"] = env_bool("AUTO_REMOVE", "false")
    config["POLL_INTERVAL_SECONDS"] = positive_int(os.environ.get("POLL_INTERVAL_SECONDS", "5"), "POLL_INTERVAL_SECONDS")
    folders = json.loads(os.environ.get("FOLDERS", "[]"))
    if not isinstance(folders, list) or not folders:
        raise ValueError("FOLDERS deve ser uma lista JSON com pelo menos uma pasta.")
    paths, destinations, configured = set(), set(), []
    for index, entry in enumerate(folders, 1):
        if not isinstance(entry, dict) or not isinstance(entry.get("images_dir"), str) or not entry["images_dir"].strip():
            raise ValueError(f"FOLDERS[{index}]: informe images_dir como caminho de pasta.")
        path = (BASE_DIR / entry["images_dir"]).resolve()
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", path.name):
            raise ValueError(f"FOLDERS[{index}]: nome da pasta/camera invalido.")
        project = positive_int(entry.get("project_id", ""), f"FOLDERS[{index}].project_id")
        limit = positive_int(entry.get("images_per_task", 50), f"FOLDERS[{index}].images_per_task")
        remove = entry.get("auto_remove", config["AUTO_REMOVE"])
        organization = entry.get("organization", config["CVAT_ORGANIZATION"])
        if type(remove) is not bool or not isinstance(organization, str):
            raise ValueError(f"FOLDERS[{index}]: auto_remove deve ser booleano e organization deve ser texto.")
        organization = organization.strip()
        identity = (organization, project, path.name.casefold())
        normalized_path = os.path.normcase(str(path))
        if normalized_path in paths:
            raise ValueError(f"FOLDERS[{index}]: pasta repetida; configure cada pasta uma unica vez.")
        if identity in destinations:
            raise ValueError(f"FOLDERS[{index}]: camera repetida no mesmo projeto/organizacao.")
        paths.add(normalized_path)
        destinations.add(identity)
        configured.append(config | dict(IMAGES_DIR=path, CVAT_PROJECT_ID=project,
                          IMAGES_PER_TASK=limit, AUTO_REMOVE=remove, CVAT_ORGANIZATION=organization))
    for folder in configured:
        folder["IMAGES_DIR"].mkdir(parents=True, exist_ok=True)
    config["FOLDERS"] = configured
    return config


def scan_cameras(root):
    folders = [(root.name, root)]
    cameras = {}
    for name, folder in folders:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", name):
            raise ValueError(f"Nome de camera invalido: {name}. Use letras, numeros, _ ou -.")
        if any(camera.casefold() == name.casefold() for camera in cameras):
            raise ValueError("Nomes de camera repetidos, ignorando maiusculas/minusculas.")
        images = []
        for path in sorted(folder.iterdir()):
            if not path.is_file() or path.suffix.lower() not in EXTENSIONS:
                continue
            stamp = image_date(path.name)
            try:
                size = path.stat().st_size
            except FileNotFoundError:
                continue
            if stamp is None or size == 0:
                logging.warning("[%s] Ignorando imagem vazia ou sem data valida: %s", name, path.name)
                continue
            images.append((stamp, path))
        cameras[name] = sorted(images)
    if not any(cameras.values()) and any(folder.is_dir() and any(
            p.is_file() and p.suffix.lower() in EXTENSIONS for p in folder.iterdir()) for folder in root.iterdir()):
        raise ValueError("images_dir deve ser a pasta da camera, nao a raiz com varias cameras.")
    return cameras


def task_period(name):
    parts = name.split(" | ")
    if len(parts) != 3 or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", parts[0]):
        return None
    try:
        start, end = (datetime.fromisoformat(part) for part in parts[1:])
    except ValueError:
        return None
    if start.tzinfo is not None or end.tzinfo is not None or end < start:
        return None
    return parts[0], start, end


def existing_periods(tasks, cameras):
    periods, issues = [], []
    for task in tasks:
        if task.name.endswith(PENDING_SUFFIX):
            parsed = task_period(task.name[:-len(PENDING_SUFFIX)])
            if parsed and parsed[0].casefold() in {name.casefold() for name in cameras}:
                issues.append(f"Task {task.id}: upload pendente de confirmacao; revise no CVAT antes de continuar.")
            continue
        parsed = task_period(task.name)
        if parsed:
            camera, start, end = parsed
            periods.append(dict(camera=camera.casefold(), start=start, end=end, id=task.id))
            if camera.casefold() in {name.casefold() for name in cameras} and getattr(task, "size", None) in (None, 0):
                issues.append(f"Task {task.id} da camera {camera} sem imagens prontas; confira o upload antes de avancar.")
            continue
        frames = task.get_frames_info()
        dated = [(image_date(frame.name), frame.name.replace("\\", "/")) for frame in frames]
        dated = [(stamp, name) for stamp, name in dated if stamp is not None]
        if not dated:
            if frames:
                issues.append(f"Task {task.id}: nome fora do padrao e imagens sem datas reconheciveis.")
            continue
        start, end = min(stamp for stamp, _ in dated), max(stamp for stamp, _ in dated)
        candidates = [camera for camera in cameras if
                      re.search(r"(?<![A-Za-z0-9_])" + re.escape(camera) + r"(?![A-Za-z0-9_])", task.name, re.I)
                      or any(camera.casefold() in [part.casefold() for part in Path(name).parts[:-1]]
                             for _, name in dated)]
        if len(candidates) == 1:
            periods.append(dict(camera=candidates[0].casefold(), start=start, end=end, id=task.id))
            logging.info("Task antiga %s identificada como camera %s pelas imagens/nome.", task.id, candidates[0])
        elif not candidates:
            affected = [camera for camera, images in cameras.items()
                        if images and start <= images[-1][0] and end >= images[0][0]]
            if affected:
                issues.append(f"Task {task.id}: imagens de {start.isoformat()} a {end.isoformat()}, "
                              f"mas camera desconhecida (possivel conflito com {', '.join(affected)}).")
        else:
            issues.append(f"Task {task.id}: mais de uma camera identificada; confira o nome e as imagens.")
    return periods, issues


def plan_batches(cameras, periods, limit):
    batches, skipped = [], 0
    for camera, images in cameras.items():
        covered = [period for period in periods if period["camera"] == camera.casefold()]
        latest = max(covered, key=lambda period: period["end"], default=None)
        if latest:
            logging.info("[%s] Ultima imagem enviada pelo nome das tasks: %s (task %s).",
                         camera, latest["end"].isoformat(), latest["id"])
        batch = []
        for stamp, items in groupby(images, key=lambda item: item[0]):
            group = list(items)
            if latest and stamp <= latest["end"]:
                skipped += len(group)
                continue
            if len(group) > limit:
                raise ValueError(f"{camera}: {len(group)} imagens no mesmo instante; aumente images_per_task.")
            if batch and len(batch) + len(group) > limit:
                raise ValueError(f"{camera}: limite divide imagens com o mesmo horario; ajuste images_per_task ou use microssegundos distintos.")
            batch.extend(group)
            if len(batch) == limit:
                batches.append((camera, batch))
                batch = []
    return batches, skipped


def task_name(camera, images):
    return f"{camera} | {images[0][0].isoformat(timespec='microseconds')} | {images[-1][0].isoformat(timespec='microseconds')}"


def file_signature(path):
    stat = path.stat()
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns


def confirm_upload(task, images):
    task.fetch()
    expected = Counter(path.name for _, path in images)
    actual = Counter(Path(frame.name.replace("\\", "/")).name for frame in task.get_frames_info())
    if task.size != len(images) or actual != expected:
        raise RuntimeError(f"Task {task.id}: quantidade/nomes no CVAT divergem do lote; arquivos locais preservados.")


def remove_uploaded(images, signatures, root):
    for _, path in images:
        try:
            if path.parent.resolve() != root.resolve() or path.is_symlink() or file_signature(path) != signatures[path]:
                logging.warning("Arquivo alterado ou fora da pasta; mantido: %s", path.name)
                continue
            path.unlink()
            logging.info("Imagem confirmada no CVAT e removida: %s", path.name)
        except FileNotFoundError:
            pass
        except OSError:
            logging.warning("Nao foi possivel remover %s; arquivo mantido para revisao.", path.name)


def run(config, dry_run):
    cameras = scan_cameras(config["IMAGES_DIR"])
    if not any(cameras.values()):
        logging.info("[%s] Pasta sem imagens prontas; aguardando a coleta.", config["IMAGES_DIR"].name)
        return
    with make_client(host=config["CVAT_URL"], credentials=(config["CVAT_USERNAME"], config["CVAT_PASSWORD"])) as client:
        if config["CVAT_ORGANIZATION"]:
            client.organization_slug = config["CVAT_ORGANIZATION"]
        project = client.projects.retrieve(config["CVAT_PROJECT_ID"])
        logging.info("Projeto CVAT: %s (ID %s); modo: %s", project.name, project.id,
                     "SIMULACAO" if dry_run else "ENVIO")
        periods, issues = existing_periods(project.get_tasks(), cameras)
        batches, skipped = plan_batches(cameras, periods, config["IMAGES_PER_TASK"])
        waiting = sum(len(images) for images in cameras.values()) - skipped - sum(len(images) for _, images in batches)
        for camera, images in batches:
            logging.info("PLANO: %s; %d imagens", task_name(camera, images), len(images))
        logging.info("Resumo: %d camera(s), %d task(s) novas, %d imagens a enviar, %d anteriores/iguais ao ultimo envio.",
                     len(cameras), len(batches), sum(len(images) for _, images in batches), skipped)
        if waiting:
            logging.info("Aguardando completar o proximo lote: %d/%d imagens novas.", waiting, config["IMAGES_PER_TASK"])
        for issue in issues:
            logging.error(issue)
        if issues:
            raise ValueError("Envio bloqueado: identifique camera e periodo das tasks acima usando o padrao CAMERA | inicio | fim.")
        if dry_run:
            logging.info("Simulacao concluida. Para enviar: uv run python main.py --send")
            return
        for camera, images in batches:
            name = task_name(camera, images)
            # Revalidar no servidor antes de cada criacao, inclusive tasks antigas.
            current, issues = existing_periods(project.get_tasks(), cameras)
            if issues or any(period["camera"] == camera.casefold() and period["end"] >= images[0][0]
                             for period in current):
                raise ValueError("O projeto mudou durante o envio. Execute novamente para recalcular os lotes.")
            signatures = {path: file_signature(path) for _, path in images}
            task = client.tasks.create(spec={"name": name + PENDING_SUFFIX, "project_id": project.id})
            logging.info("Task %s criada: %s; iniciando upload.", task.id, name)
            try:
                task.upload_data(resources=[str(path) for _, path in images], resource_type=ResourceType.LOCAL,
                                 params={"image_quality": 100, "sorting_method": "predefined"})
                confirm_upload(task, images)
                task.update({"name": name})
            except Exception:
                logging.error("Upload falhou na task %s. Revise essa task no CVAT antes de reenviar; nenhum arquivo local foi apagado.", task.id)
                raise
            logging.info("Task %s concluida com %d imagens.", task.id, len(images))
            if config.get("AUTO_REMOVE", False):
                remove_uploaded(images, signatures, config["IMAGES_DIR"])


def monitor(config, dry_run):
    while True:
        failures = 0
        for folder in config["FOLDERS"]:
            try:
                run(folder, dry_run)
            except Exception as error:
                failures += 1
                if isinstance(error, ValueError):
                    logging.error("%s", error)
                logging.error("[%s / projeto %s] Falha (%s; HTTP %s). As demais pastas continuam.",
                              folder["IMAGES_DIR"].name, folder["CVAT_PROJECT_ID"],
                              type(error).__name__, getattr(error, "status", "indisponivel"))
        if dry_run or not config["LOOP"]:
            if failures:
                raise ValueError(f"{failures} pasta(s) com falha; confira os avisos acima.")
            return
        time.sleep(config["POLL_INTERVAL_SECONDS"])


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Verificar e planejar sem criar tasks.")
    mode.add_argument("--send", action="store_true", help="Criar tasks e enviar imagens novas.")
    parser.add_argument("--once", action="store_true", help="Verificar uma vez, enviando somente lotes completos.")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        config = load_config()
        if args.once:
            config["LOOP"] = False
        monitor(config, args.dry_run or (config["DRY_RUN"] and not args.send))
        return 0
    except KeyboardInterrupt:
        logging.warning("Interrompido. Confira a ultima task no CVAT antes de reenviar.")
        return 130
    except (ValueError, OSError) as error:
        logging.error("%s", error)
        return 1
    except Exception as error:
        logging.error("Falha CVAT (%s; HTTP %s). Confira conexao, credenciais e permissoes.",
                      type(error).__name__, getattr(error, "status", "indisponivel"))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
