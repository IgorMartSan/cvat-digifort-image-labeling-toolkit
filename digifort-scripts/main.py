"""Coletor HTTP/HTTPS. Execute: uv run python main.py (Ctrl+C para encerrar).

Configure CAMERAS como lista JSON no .env ao lado deste arquivo. Por camera:
  url, folder, interval_seconds: endereco, pasta exclusiva e intervalo em segundos.
  history: true para recuperar gravacoes Digifort antes de continuar ao vivo.
  start_date: "2026-10-01T08:30:00" para inicio fixo, ou 30 para 30 dias atras.
              Omitido/null: gravacao mais antiga (history=true), ou agora (false).
  end_date: "2026-10-29T18:00:00" para fim fixo, ou 0 para agora e encerrar.
            Omitido/null: coleta continuamente, sem data final.
  Datas numericas sao dias de 24 horas antes do inicio desta execucao (>= 0).
  Datas ISO aceitam hora e fuso: "2026-10-01T08:30:00-03:00". Sem fuso: local.
  resize: null preserva os bytes; [1280, 720] reduz mantendo a proporcao.
  capture_resolution: [1920, 1080] e quality: 100 configuram snapshots Digifort.
  history_search_hours: 6; history_retry_seconds: 30 controlam busca e tentativas.

Exemplos de periodo (campos dentro do objeto da camera):
  Ultimos 30 dias e continuar ao vivo: "history": true, "start_date": 30.
  Ultimos 30 dias e parar no presente: "start_date": 30, "end_date": 0,
                                      "history": true.
  Periodo fixo: "start_date": "2026-10-01T08:00:00",
               "end_date": "2026-10-02T18:00:00", "history": true.

RETOMADA: imagens existentes sempre tem prioridade sobre o inicio configurado.
Continua da ultima imagem + intervalo, preservando lacunas ja verificadas no
.history.json. Use outra pasta vazia para coletar novamente um periodo anterior.
O JSON registra folder, last_image, last_image_at e next_capture_at, inclusive
em cameras somente ao vivo. Remover imagens nao remove o ponto de retomada.
O fim e inclusivo: nenhum arquivo novo recebe horario posterior a end_date.
Sem fim, ao alcancar o presente segue ao vivo. Cada camera termina independentemente.
OUTPUT_DIR=./volume/image e REQUEST_TIMEOUT=10 sao opcoes globais do .env.
Veja README.md e .env.example para configuracoes completas e detalhes.
"""

import asyncio
import json
import logging
import math
import os
import re
import tempfile
import time
from io import BytesIO
from datetime import datetime, timedelta
from pathlib import Path

import httpx
from dotenv import load_dotenv
from PIL import Image

BASE_DIR = Path(__file__).resolve().parent
STAMP = "%Y-%m-%d_%H-%M-%S"
HISTORY_BEGIN = datetime(1970, 1, 2).timestamp()
EXTENSIONS = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp",
              "image/gif": ".gif", "image/bmp": ".bmp", "image/tiff": ".tiff"}


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
    """Aceita ISO 8601, dias atras como numero JSON, ou null/ausente."""
    if value is None or value == "":
        return None
    if type(value) in (int, float):
        try:
            if not math.isfinite(value) or value < 0:
                raise ValueError("Dias atras devem ser um numero finito >= 0.")
            result = now - value * 86400
            datetime.fromtimestamp(result)  # Validar a faixa suportada pelo sistema.
        except (OverflowError, OSError) as error:
            raise ValueError("Numero de dias fora da faixa suportada.") from error
        return result
    if not isinstance(value, str):
        raise ValueError("Data deve ser ISO 8601, numero de dias atras ou null.")
    return datetime.fromisoformat(value).timestamp()


def load_config():
    load_dotenv(BASE_DIR / ".env", interpolate=False)
    cameras = json.loads(os.environ.get("CAMERAS", "[]"))
    if not isinstance(cameras, list) or not cameras:
        raise ValueError("CAMERAS deve ser uma lista JSON com ao menos uma camera.")
    output = Path(os.environ.get("OUTPUT_DIR", "./volume/image"))
    output = (BASE_DIR / output).resolve()
    timeout = positive_number(os.environ.get("REQUEST_TIMEOUT", "10"))
    now = time.time()  # Um unico instante de referencia para todas as datas relativas.
    folders = set()
    for index, camera in enumerate(cameras, 1):
        if not isinstance(camera, dict):
            raise ValueError(f"Camera {index}: esperado um objeto JSON.")
        folder = camera.get("folder", "")
        if (not isinstance(folder, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", folder)
                or re.fullmatch(r"CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9]", folder, re.I)):
            raise ValueError(f"Camera {index}: nome de pasta invalido.")
        if folder.casefold() in folders:
            raise ValueError(f"Camera {index}: pasta repetida.")
        folders.add(folder.casefold())
        if not isinstance(camera.get("url"), str):
            raise ValueError(f"Camera {index}: URL obrigatoria.")
        url = httpx.URL(camera["url"])
        if url.scheme not in ("http", "https") or not url.host:
            raise ValueError(f"Camera {index}: use uma URL HTTP/HTTPS valida.")
        if type(camera.get("history", False)) is not bool:
            raise ValueError(f"Camera {index}: history deve ser true ou false.")
        if camera.get("history") and (url.path.lower() != "/interface/cameras/getsnapshot" or not url.params.get("Camera")):
            raise ValueError(f"Camera {index}: historico requer URL de snapshot Digifort com Camera.")
        camera["history_search_hours"] = positive_number(camera.get("history_search_hours", 6))
        camera["history_retry_seconds"] = positive_number(camera.get("history_retry_seconds", 30))
        camera["resize"] = resolution(camera["resize"]) if camera.get("resize") is not None else None
        quality = camera.get("quality", 100)
        if type(quality) is not int or not 1 <= quality <= 100:
            raise ValueError(f"Camera {index}: quality deve ser um inteiro de 1 a 100.")
        camera["quality"] = quality
        capture_size = resolution(camera.get("capture_resolution", [1920, 1080]))
        if url.path.lower() == "/interface/cameras/getsnapshot":
            # Digifort usa 352x240 se nao receber dimensoes explicitas.
            camera["url"] = str(url.copy_merge_params({
                "Profile": url.params.get("Profile", "Recording"),
                "Width": str(capture_size[0]), "Height": str(capture_size[1]),
                "KeepAspectRatio": "TRUE", "Quality": str(quality),
            }))
        camera["interval_seconds"] = positive_number(camera["interval_seconds"])
        if camera.get("history") and camera["interval_seconds"] < 0.001:
            raise ValueError(f"Camera {index}: historico requer intervalo >= 0.001 segundo.")
        # Datas sem fuso seguem o horario local da maquina.
        camera["start"] = parse_date(camera.get("start_date"), now)
        camera["end"] = parse_date(camera.get("end_date"), now)
        if camera["start"] is not None and camera["end"] is not None and camera["end"] < camera["start"]:
            raise ValueError(f"Camera {index}: end_date deve ser igual ou posterior ao inicio.")
        camera["path"] = (output / folder).resolve()
        if camera["path"].parent != output:
            raise ValueError(f"Camera {index}: pasta fora do OUTPUT_DIR.")
    for camera in cameras:
        camera["path"].mkdir(parents=True, exist_ok=True)
    return cameras, timeout


def latest_capture(camera):
    latest = None
    for path in camera["path"].iterdir():
        if not path.is_file() or path.suffix.lower() not in EXTENSIONS.values():
            continue
        for pattern in (STAMP, STAMP + "_%f"):
            try:
                captured = datetime.strptime(path.stem, pattern).timestamp()
            except ValueError:
                continue
            latest = captured if latest is None else max(latest, captured)
            break
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
    start = datetime.fromtimestamp(start)
    params = {"ResponseFormat": "Text"}
    if end is None:
        width, height = camera.get("capture_resolution", [-1, -1])
        params.update(Date=start.strftime("%Y.%m.%d"), Time=start.strftime("%H.%M.%S.%f")[:-3],
                      Width=str(width), Height=str(height), Quality=str(camera.get("quality", 100)))
    else:
        end = datetime.fromtimestamp(end)
        params.update(StartDate=start.strftime("%Y.%m.%d"), StartTime=start.strftime("%H.%M.%S.%f")[:-3],
                      EndDate=end.strftime("%Y.%m.%d"), EndTime=end.strftime("%H.%M.%S.%f")[:-3],
                      Video="TRUE", Audio="FALSE", Metadata="FALSE")
    return url.copy_merge_params(params)


def first_recording(response, start, end):
    # Uma resposta vazia multipart indica ausencia de gravacoes, nao falha de rede.
    codes = re.findall(r"RESPONSE_CODE=(\d+)", response.text)
    if any(code != "0" for code in codes):
        raise ValueError("Digifort recusou a consulta de gravacoes.")
    if not response.headers.get("content-type", "").startswith("multipart/"):
        raise ValueError("Resposta de timeline invalida.")
    candidates = []
    for block in response.text.split("--DigifortBoundary"):
        if "TIMELINEDATA_MEDIAKIND=VIDEO" not in block:
            continue
        match = re.search(r"TIMELINEDATA_STARTTIME=([^\r\n]+)", block)
        if match is None:
            raise ValueError("Timeline sem data inicial.")
        stamp = datetime.fromisoformat(match[1].strip()).timestamp()
        if stamp <= end:
            candidates.append(max(start, stamp))
    if response.text.strip() and not candidates:
        raise ValueError("Timeline sem trecho de video valido no periodo.")
    return min(candidates) if candidates else None


async def oldest_recording(camera, client, timeout):
    end = time.time()
    if camera.get("end") is not None:
        end = min(end, camera["end"])
    url = playback_url(camera, "GetTimelineData", HISTORY_BEGIN, end)
    candidate = None
    has_data = False
    # Ler so o primeiro trecho evita baixar a timeline completa de anos.
    async with asyncio.timeout(timeout):
        async with client.stream("GET", url) as response:
            response.raise_for_status()
            if not response.headers.get("content-type", "").startswith("multipart/"):
                raise ValueError("Digifort recusou a consulta da gravacao mais antiga.")
            video = False
            async for line in response.aiter_lines():
                if line.strip() and not line.startswith("--"):
                    has_data = True
                if line.startswith("--"):
                    video = False
                if line.startswith("RESPONSE_CODE=") and line.split("=", 1)[1].strip() != "0":
                    raise ValueError("Digifort recusou a consulta de gravacoes.")
                if line.startswith("TIMELINEDATA_MEDIAKIND="):
                    video = line.split("=", 1)[1].strip() == "VIDEO"
                if video and line.startswith("TIMELINEDATA_STARTTIME="):
                    candidate = datetime.fromisoformat(line.split("=", 1)[1].strip()).timestamp()
                    break
    if candidate is None:
        if has_data:
            raise ValueError("Timeline sem trecho de video valido.")
        return None
    if not HISTORY_BEGIN <= candidate <= end:
        raise ValueError("Data de gravacao fora do periodo consultado.")
    # Confirmar todos os trechos anteriores, sem presumir a ordem do streaming.
    response = await request(client, playback_url(camera, "GetTimelineData", HISTORY_BEGIN, candidate), timeout)
    earlier = first_recording(response, HISTORY_BEGIN, candidate)
    return candidate if earlier is None else earlier


def read_progress(camera):
    url = httpx.URL(camera["url"])
    # Dias relativos mudam a cada execucao; a identidade preserva o valor configurado.
    start_identity = camera["start"]
    if type(camera.get("start_date")) in (int, float):
        start_identity = ["days_ago", camera["start_date"]]
    identity = [url.host, url.port, url.params.get("Camera", url.path), start_identity, camera["interval_seconds"]]
    if camera.get("end") is not None:
        end_identity = camera["end"]
        if type(camera.get("end_date")) in (int, float):
            end_identity = ["days_ago", camera["end_date"]]
        identity.append(["end", end_identity])
    state = dict(identity=identity, folder=camera["folder"], next=camera["start"],
                 found=False, done=False, last_image=None, last_image_at=None)
    path = camera["path"] / ".history.json"
    if path.exists():
        try:
            saved = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(saved, dict) or saved.get("folder", camera["folder"]) != camera["folder"]:
                raise ValueError("Progresso pertence a outra pasta.")
            if (saved.get("identity") == identity and type(saved.get("next")) in (float, int)
                    and math.isfinite(saved["next"])
                    and saved["next"] >= (HISTORY_BEGIN if camera["start"] is None or type(camera.get("start_date")) in (int, float) else camera["start"])
                    and type(saved.get("found")) is bool and type(saved.get("done")) is bool):
                state.update(saved)
                if (type(camera.get("end_date")) in (int, float)
                        and state["next"] <= camera["end"]):
                    state["done"] = False  # Um fim relativo pode ter avancado desde a ultima execucao.
            last = saved.get("last_image_at")
            same_source = isinstance(saved.get("identity"), list) and saved["identity"][:3] == identity[:3]
            if same_source and isinstance(last, str):
                captured = datetime.fromisoformat(last).timestamp()
                resume = captured + camera["interval_seconds"]
                if state["found"] and state["next"] is not None:
                    resume = max(resume, state["next"])
                state.update(next=resume, found=True, last_image=saved.get("last_image"), last_image_at=last)
        except (ValueError, AttributeError, OSError, OverflowError):
            logging.warning("[%s] Progresso invalido; retomando pela data configurada", camera["folder"])
    return state


def write_progress(camera, state):
    path = camera["path"] / ".history.json"
    temporary = path.with_suffix(".json.tmp")
    state["folder"] = camera["folder"]
    state.setdefault("last_image", None)
    state.setdefault("last_image_at", None)
    state["next_capture_at"] = None if state["next"] is None else datetime.fromtimestamp(state["next"]).isoformat(timespec="microseconds")
    temporary.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(path)


async def collect_history(camera, client, timeout):
    while True:
        try:
            state = read_progress(camera)
            latest = latest_capture(camera)
            if latest is not None:
                resume = latest + camera["interval_seconds"]
                # Preservar lacunas ja verificadas, mas nunca voltar antes da ultima imagem.
                if state["found"] and state["next"] is not None:
                    resume = max(resume, state["next"])
                state.update(next=resume, found=True, done=False)
                logging.info("[%s] Retomando apos a ultima imagem: %s", camera["folder"],
                             datetime.fromtimestamp(latest).isoformat())
            elif state["found"] and state["next"] is not None:
                # O uploader pode ter removido todas as imagens ja enviadas.
                state["done"] = False
            break
        except OSError:
            logging.error("[%s] Falha ao ler progresso; tentando novamente", camera["folder"])
            await asyncio.sleep(camera["history_retry_seconds"])
    if state["done"]:
        return
    logging.info("[%s] Iniciando historico", camera["folder"])
    while True:
        # Alterar o estado em memoria somente depois de persistir o novo progresso.
        updated = state.copy()
        try:
            now = time.time()
            due = state["next"]
            end_date = camera.get("end")
            horizon = now if end_date is None else min(now, end_date)
            if due is None:
                first = await oldest_recording(camera, client, timeout)
                updated["next"] = now if first is None else first
                updated["found"] = first is not None
                updated["done"] = first is None
                logging.info("[%s] Gravacao mais antiga: %s", camera["folder"],
                             "nenhuma disponivel" if first is None else datetime.fromtimestamp(first).isoformat())
            elif due >= now or (end_date is not None and round(due, 3) > end_date):
                updated["done"] = True
                write_progress(camera, updated)
                logging.info("[%s] Historico concluido", camera["folder"])
                return
            elif not state["found"]:
                end = min(due + camera["history_search_hours"] * 3600, horizon)
                response = await request(client, playback_url(camera, "GetTimelineData", due, end), timeout)
                first = first_recording(response, due, end)
                updated["next"] = end if first is None else first
                updated["found"] = first is not None
                updated["done"] = first is None and end == horizon
                logging.info("[%s] Busca de historico: %s (%s)", camera["folder"],
                             datetime.fromtimestamp(due).isoformat(), "encontrado" if first is not None else "sem gravacao")
            else:
                # O horario do arquivo historico e o solicitado, e nao o do download.
                due = round(due, 3)
                captured = datetime.fromtimestamp(due)
                pattern = STAMP + "_%f" if captured.microsecond else STAMP
                existing = camera["path"] / (captured.strftime(pattern) + ".jpg")
                saved_path = existing if existing.exists() else None
                if saved_path is None:
                    response = await request(client, playback_url(camera, "GetSnapshot", due), timeout)
                    missing = re.search(r"RESPONSE_CODE=1020[01](?:\D|$)", response.text) if not response.headers.get("content-type", "").startswith("image/") else None
                    if missing:
                        logging.warning("[%s] Sem imagem em %s; mantendo intervalo", camera["folder"], captured.isoformat())
                    else:
                        saved_path = await save_response(camera, response, captured, historical=True)
                if saved_path is not None:
                    updated.update(last_image=saved_path.name, last_image_at=captured.isoformat(timespec="microseconds"))
                updated["next"] = round(due + camera["interval_seconds"], 3)
            write_progress(camera, updated)
            state = updated
            if state["done"]:
                logging.info("[%s] Historico concluido", camera["folder"])
                return
        except (httpx.HTTPError, TimeoutError, OSError, ValueError) as error:
            logging.error("[%s] Falha no historico (%s); repetindo o mesmo ponto em %gs",
                          camera["folder"], type(error).__name__, camera["history_retry_seconds"])
            await asyncio.sleep(camera["history_retry_seconds"])
        await asyncio.sleep(0)


async def collect(camera, client, timeout):
    if camera.get("history"):
        await collect_history(camera, client, timeout)
    try:
        due = next_capture(camera)
    except OSError:
        logging.error("[%s] Falha ao consultar imagens existentes", camera["folder"])
        due = time.time() if camera["start"] is None else camera["start"]
    interval = camera["interval_seconds"]
    end_date = camera.get("end")
    while True:
        if end_date is not None and (time.time() > end_date or due > end_date):
            logging.info("[%s] Periodo concluido", camera["folder"])
            return
        await asyncio.sleep(max(0, due - time.time()))
        attempted = time.time()
        if end_date is not None and attempted > end_date:
            logging.info("[%s] Periodo concluido", camera["folder"])
            return
        try:
            request_timeout = timeout if end_date is None else min(timeout, end_date - attempted)
            response = await request(client, camera["url"], request_timeout)
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
            # Nao registrar a URL: ela pode conter credenciais.
            detail = f"HTTP {error.response.status_code}" if isinstance(error, httpx.HTTPStatusError) else type(error).__name__
            logging.error("[%s] Falha na captura (%s)", camera["folder"], detail)
            due = attempted + interval
        # Se a requisicao demorou mais que o intervalo, nao acumular tentativas.
        if due <= time.time():
            due = time.time() + interval


async def main():
    cameras, timeout = load_config()
    logging.info("Iniciando coleta de %d camera(s). Ctrl+C para encerrar.", len(cameras))
    for camera in cameras:
        logging.info("[%s] Inicio configurado: %s; fim: %s", camera["folder"],
                     "automatico" if camera["start"] is None else datetime.fromtimestamp(camera["start"]).isoformat(),
                     "sem limite" if camera["end"] is None else datetime.fromtimestamp(camera["end"]).isoformat())
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        async with asyncio.TaskGroup() as tasks:
            for camera in cameras:
                tasks.create_task(collect(camera, client, timeout))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Coleta encerrada.")
    except (ValueError, KeyError, TypeError, OSError, httpx.InvalidURL) as error:
        logging.error("Configuracao invalida (%s). Confira o .env.", type(error).__name__)
        raise SystemExit(1)
