from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import shutil
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, quote, urlparse

import httpx
from cvat_sdk import make_client
from cvat_sdk.core.proxies.tasks import ResourceType

from app.core.config import APP_ROOT, DATA_DIR, Settings

logger = logging.getLogger("cvat_toolkit.acquisition")

WORKERS_FILE = DATA_DIR / "acquisition_workers.json"
CAMERAS_DATA_DIR = DATA_DIR / "cameras"
CAMERA_URLS_HISTORY_FILE = DATA_DIR / "camera_urls_history.json"
DEFAULT_RETRY_INTERVAL = 13.0
STAMP_FORMAT = "%Y-%m-%d_%H-%M-%S"
DEFAULT_SEED_URL = "rtsp://ax12813:ims@2024@10.247.72.55/Interface/Cameras/Media?Camera=VO2C01"


def get_saved_camera_urls() -> List[str]:
    """Retorna lista de URLs de câmeras salvas no histórico, inicializando com seed se inexistente."""
    if not CAMERA_URLS_HISTORY_FILE.is_file():
        urls = [DEFAULT_SEED_URL]
        try:
            CAMERA_URLS_HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
            CAMERA_URLS_HISTORY_FILE.write_text(json.dumps(urls, indent=2), encoding="utf-8")
        except Exception as exc:
            logger.warning(f"Não foi possível salvar seed de URLs: {exc}")
        return urls

    try:
        content = CAMERA_URLS_HISTORY_FILE.read_text(encoding="utf-8")
        data = json.loads(content)
        if isinstance(data, list):
            return [str(u).strip() for u in data if str(u).strip()]
    except Exception as exc:
        logger.error(f"Erro ao ler histórico de URLs de câmeras: {exc}")
    return []


def save_camera_url(url: str) -> None:
    """Adiciona uma URL ao início do histórico, evitando duplicatas."""
    clean = (url or "").strip()
    if not clean:
        return
    urls = get_saved_camera_urls()
    urls = [u for u in urls if u != clean]
    urls.insert(0, clean)
    urls = urls[:50]
    try:
        CAMERA_URLS_HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        CAMERA_URLS_HISTORY_FILE.write_text(json.dumps(urls, indent=2), encoding="utf-8")
    except Exception as exc:
        logger.error(f"Erro ao salvar URL no histórico: {exc}")


def delete_camera_url(url: str) -> bool:
    """Remove uma URL do histórico."""
    clean = (url or "").strip()
    urls = get_saved_camera_urls()
    if clean in urls:
        urls.remove(clean)
        try:
            CAMERA_URLS_HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
            CAMERA_URLS_HISTORY_FILE.write_text(json.dumps(urls, indent=2), encoding="utf-8")
            return True
        except Exception as exc:
            logger.error(f"Erro ao excluir URL do histórico: {exc}")
    return False


def extract_camera_name_from_url(url: str) -> Optional[str]:
    """Extrai automaticamente o identificador da câmera da query string (?Camera=...) ou do path."""
    if not url:
        return None
    try:
        parsed = urlparse(url)
        qs = parse_qs(parsed.query)
        for key in ("Camera", "camera", "cam", "Cam"):
            if key in qs and qs[key]:
                return qs[key][0].strip()
        parts = [p for p in parsed.path.split("/") if p]
        if parts:
            last = parts[-1]
            if last.lower() not in {"media", "getsnapshot", "snapshot", "live", "mjpeg", "video"}:
                return last
    except Exception:
        pass
    return None


def format_saved_camera_item(url: str) -> Dict[str, Any]:
    """Formata os dados de uma URL salva no histórico para exibição limpa em dropdowns."""
    clean_url = (url or "").strip()
    is_rtsp = clean_url.lower().startswith("rtsp://")
    proto = "RTSP" if is_rtsp else "HTTP"
    cam_name = extract_camera_name_from_url(clean_url) or "Câmera"
    host = ""
    try:
        parsed = urlparse(clean_url.replace("rtsp://", "http://"))
        host = parsed.hostname or ""
    except Exception:
        pass
    label = f"{cam_name} • {host} ({proto})" if host else f"{cam_name} ({proto})"
    return {
        "url": clean_url,
        "name": cam_name,
        "host": host,
        "protocol": proto,
        "label": label,
    }


def build_digifort_http_url(
    host: str,
    camera: str,
    port: int = 8601,
    username: str = "",
    password: str = "",
    width: int = 1920,
    height: int = 1080,
    quality: int = 100,
) -> str:
    """Monta a URL HTTP padronizada da API do Digifort para captura de snapshots."""
    clean_host = host.strip()
    if not clean_host.startswith(("http://", "https://")):
        clean_host = f"http://{clean_host}"

    parsed = urlparse(clean_host)
    scheme = parsed.scheme
    netloc = parsed.netloc

    # Adiciona ou substitui porta se informada e ainda nao constar no netloc
    if ":" not in netloc and port:
        netloc = f"{netloc}:{port}"

    # Monta URL com credenciais embutidas se fornecidas
    auth_prefix = ""
    if username:
        user_enc = quote(username, safe="")
        pass_enc = quote(password, safe="") if password else ""
        auth_prefix = f"{user_enc}:{pass_enc}@"
        # Remove credenciais pre-existentes do netloc para nao duplicar
        if "@" in netloc:
            netloc = netloc.split("@", 1)[1]
        netloc = f"{auth_prefix}{netloc}"

    cam_param = quote(camera, safe="")
    return (
        f"{scheme}://{netloc}/Interface/Cameras/GetSnapshot"
        f"?Camera={cam_param}&Profile=Recording&Width={width}&Height={height}&KeepAspectRatio=TRUE&Quality={quality}"
    )


def fetch_digifort_camera_telemetry(
    url: str,
    camera_name: Optional[str] = None,
    timeout: float = 4.0,
) -> Dict[str, Any]:
    """Consulta o status e telemetria PTZ (Pan, Tilt, Zoom, Presets) da câmera na API HTTP do Digifort."""
    telemetry: Dict[str, Any] = {
        "available": False,
        "ptz_supported": False,
        "ptz_initialized": False,
        "pan": None,
        "tilt": None,
        "zoom": None,
        "current_preset": None,
        "current_preset_name": None,
        "presets": [],
        "active": None,
        "working": None,
        "in_use": False,
        "in_use_user": None,
        "error": None,
    }

    if not url:
        return telemetry

    try:
        clean = (url or "").strip()
        parsed = urlparse(clean.replace("rtsp://", "http://"))
        host = parsed.hostname
        if not host:
            return telemetry

        port = parsed.port or 8601
        cam = camera_name or extract_camera_name_from_url(clean) or ""
        auth = (parsed.username, parsed.password) if parsed.username else None

        with httpx.Client(timeout=timeout, verify=False) as client:
            # 1. Consulta PTZ Status
            try:
                ptz_url = f"http://{host}:{port}/Interface/Cameras/PTZ/GetStatus"
                params = {"Cameras": cam, "ResponseFormat": "JSON"} if cam else {"ResponseFormat": "JSON"}
                ptz_resp = client.get(ptz_url, params=params, auth=auth)
                if getattr(ptz_resp, "status_code", None) == 200:
                    data = ptz_resp.json()
                    if isinstance(data, dict):
                        ptz_list = data.get("Response", {}).get("Data", {}).get("PTZStatus", [])
                        if ptz_list and isinstance(ptz_list, list) and isinstance(ptz_list[0], dict):
                            cam_ptz = ptz_list[0]
                            pos = cam_ptz.get("CurrentPosition", {})
                            usage = cam_ptz.get("Usage", {})
                            telemetry["available"] = True
                            telemetry["ptz_supported"] = bool(pos.get("Supported"))
                            telemetry["ptz_initialized"] = bool(pos.get("Initialized"))
                            telemetry["pan"] = pos.get("Pan")
                            telemetry["tilt"] = pos.get("Tilt")
                            telemetry["zoom"] = pos.get("Zoom")
                            telemetry["current_preset"] = cam_ptz.get("CurrentPreset")
                            telemetry["in_use"] = bool(usage.get("InUse"))
                            telemetry["in_use_user"] = usage.get("User")
            except Exception as e:
                telemetry["error"] = f"PTZ query: {e}"

            # 2. Consulta Status Geral da Câmera (Active, Working)
            try:
                cam_url = f"http://{host}:{port}/Interface/Cameras/GetStatus"
                params = {"Cameras": cam, "ResponseFormat": "JSON"} if cam else {"ResponseFormat": "JSON"}
                cam_resp = client.get(cam_url, params=params, auth=auth)
                if getattr(cam_resp, "status_code", None) == 200:
                    data = cam_resp.json()
                    if isinstance(data, dict):
                        cams = data.get("Response", {}).get("Data", {}).get("Cameras", [])
                        if cams and isinstance(cams, list) and isinstance(cams[0], dict):
                            c_info = cams[0]
                            telemetry["available"] = True
                            telemetry["active"] = c_info.get("Active")
                            telemetry["working"] = c_info.get("Working")
            except Exception:
                pass

            # 3. Se houver PTZ e preset ativo, consulta lista de Presets para pegar o nome
            if telemetry.get("ptz_supported") and cam:
                try:
                    presets_url = f"http://{host}:{port}/Interface/Cameras/PTZ/GetPresets"
                    presets_resp = client.get(presets_url, params={"Camera": cam, "ResponseFormat": "JSON"}, auth=auth)
                    if getattr(presets_resp, "status_code", None) == 200:
                        p_data = presets_resp.json()
                        if isinstance(p_data, dict):
                            raw_presets = p_data.get("Response", {}).get("Data", {}).get("Presets", [])
                            if isinstance(raw_presets, list):
                                valid_presets = [
                                    {"id": p.get("ID"), "name": p.get("Description") or f"Preset {p.get('ID')}"}
                                    for p in raw_presets
                                    if isinstance(p, dict) and p.get("ID") is not None
                                ]
                                telemetry["presets"] = valid_presets
                                curr_id = telemetry.get("current_preset")
                                if curr_id is not None and curr_id >= 0:
                                    for p in valid_presets:
                                        if p["id"] == curr_id:
                                            telemetry["current_preset_name"] = p["name"]
                                            break
                except Exception:
                    pass

    except Exception as exc:
        logger.debug(f"Erro ao buscar telemetria Digifort: {exc}")
        telemetry["error"] = str(exc)

    return telemetry


def format_position_tag(telemetry: Optional[Dict[str, Any]]) -> str:
    """
    Gera sufixo de identificação da posição/preset da câmera para o nome do arquivo da imagem.
    Coloca o ID e o Nome do posicionamento após a data:
    - Se houver preset ativo com nome: _Preset-{ID}_{Nome} (ex: _Preset-1_Medicao)
    - Se houver preset ativo sem nome: _Preset-{ID} (ex: _Preset-1)
    - Se for câmera PTZ sem preset, mas com coordenadas: _Pan41_Tilt39_Zoom46
    - Se câmera fixa ou sem telemetria: string vazia
    """
    if not telemetry or not telemetry.get("available"):
        return ""

    preset = telemetry.get("current_preset")
    preset_name = telemetry.get("current_preset_name")

    # 1. Se houver preset ativo (>= 0)
    if preset is not None and preset >= 0:
        clean_name = ""
        if preset_name:
            import unicodedata
            nfkd = unicodedata.normalize("NFKD", str(preset_name))
            ascii_name = nfkd.encode("ASCII", "ignore").decode("ASCII")
            clean_name = re.sub(r"[^a-zA-Z0-9_-]", "", ascii_name.replace(" ", "-"))

        if clean_name:
            return f"_Preset-{preset}_{clean_name}"
        return f"_Preset-{preset}"

    # 2. Se for câmera PTZ sem preset ativo, mas com coordenadas Pan/Tilt/Zoom
    if telemetry.get("ptz_supported"):
        pan = telemetry.get("pan")
        tilt = telemetry.get("tilt")
        zoom = telemetry.get("zoom")
        parts = []
        if pan is not None:
            parts.append(f"Pan{round(pan)}")
        if tilt is not None:
            parts.append(f"Tilt{round(tilt)}")
        if zoom is not None:
            parts.append(f"Zoom{round(zoom)}")
        if parts:
            return "_" + "_".join(parts)

    return ""


def test_snapshot_capture(
    url: str,
    timeout: float = 8.0,
    use_rtsp: bool = False,
    camera_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Testa captura de snapshot em memoria para preview visual instantaneo e le telemetria/PTZ."""
    clean_url = (url or "").strip()
    if not clean_url:
        return {"success": False, "error": "URL da câmera não informada."}

    is_rtsp = use_rtsp or clean_url.lower().startswith("rtsp://")
    extracted_cam = camera_name or extract_camera_name_from_url(clean_url)

    if is_rtsp:
        import cv2
        cap = None
        try:
            cap = cv2.VideoCapture(clean_url)
            if not cap.isOpened():
                return {"success": False, "error": "Não foi possível abrir o stream RTSP. Verifique endereço e credenciais."}
            ret, frame = cap.read()
            if not ret or frame is None:
                return {"success": False, "error": "Falha ao ler frame do stream RTSP da câmera."}
            ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 95])
            if not ok:
                return {"success": False, "error": "Falha na codificação JPEG do frame."}
            b64_data = base64.b64encode(buf.tobytes()).decode("utf-8")
            h, w = frame.shape[:2]

            # Salva no histórico automaticamente em caso de sucesso
            save_camera_url(clean_url)

            # Busca telemetria / posicionamento PTZ
            telemetry = fetch_digifort_camera_telemetry(clean_url, camera_name=extracted_cam)

            return {
                "success": True,
                "preview_base64": f"data:image/jpeg;base64,{b64_data}",
                "width": w,
                "height": h,
                "size_kb": round(len(buf) / 1024, 1),
                "protocol": "RTSP",
                "extracted_camera": extracted_cam,
                "camera_info": telemetry,
            }
        except Exception as exc:
            return {"success": False, "error": f"Erro RTSP: {str(exc)}"}
        finally:
            if cap is not None:
                cap.release()

    # Captura HTTP Digifort
    try:
        with httpx.Client(timeout=timeout, verify=False) as client:
            resp = client.get(clean_url)
            resp.raise_for_status()
            content_type = resp.headers.get("content-type", "").lower()
            if not resp.content or ("image" not in content_type and "octet-stream" not in content_type):
                return {
                    "success": False,
                    "error": f"A resposta do Digifort não é uma imagem válida (Content-Type: {content_type}).",
                }
            b64_data = base64.b64encode(resp.content).decode("utf-8")

            # Salva no histórico automaticamente em caso de sucesso
            save_camera_url(clean_url)

            # Busca telemetria / posicionamento PTZ
            telemetry = fetch_digifort_camera_telemetry(clean_url, camera_name=extracted_cam)

            return {
                "success": True,
                "preview_base64": f"data:image/jpeg;base64,{b64_data}",
                "size_kb": round(len(resp.content) / 1024, 1),
                "protocol": "HTTP Snapshot",
                "extracted_camera": extracted_cam,
                "camera_info": telemetry,
            }
    except Exception as exc:
        return {"success": False, "error": f"Erro de conexão com Digifort: {str(exc)}"}


@dataclass
class AcquisitionWorkerConfig:
    id: str
    camera_name: str
    source_type: str = "digifort"  # "digifort" ou "rtsp"
    digifort_host: str = ""
    digifort_port: int = 8601
    username: str = ""
    password: str = ""
    snapshot_url: str = ""
    use_rtsp: bool = False
    cvat_project_id: Optional[int] = None
    cvat_project_name: str = ""
    cvat_organization: str = ""
    interval_seconds: float = 600.0
    images_per_task: int = 200
    auto_remove: bool = True
    ptz_pan: Optional[float] = None
    ptz_tilt: Optional[float] = None
    ptz_zoom: Optional[float] = None
    ptz_preset: Optional[str] = None
    ptz_mode: str = "any"  # "any" ou "presets"
    selected_presets: list[int] = field(default_factory=list)
    capture_mode: str = "live"  # "live" ou "history"
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    sync_pipeline: bool = True
    auto_start: bool = True
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())


class ActiveWorker:
    def __init__(self, config: AcquisitionWorkerConfig):
        self.config = config
        self.status: str = "stopped"  # "running", "paused", "stopped", "reconnecting", "error"
        self.last_status_message: str = "Pronto para iniciar"
        self.captured_count: int = 0
        self.tasks_created: int = 0
        self.last_capture_time: Optional[str] = None
        self.next_capture_time: Optional[str] = None
        self.current_recording_time: Optional[str] = None
        self._last_image_filename: Optional[str] = None
        self._last_image_iso: Optional[str] = None
        self.logs: deque = deque(maxlen=40)
        self.history_cursor: Optional[float] = None
        self._stop_event = threading.Event()
        self._pause_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.camera_folder = CAMERAS_DATA_DIR / config.camera_name
        self.camera_folder.mkdir(parents=True, exist_ok=True)

        # Pré-carrega checkpoint se existir na pasta da câmera
        self.load_checkpoint()

    @property
    def history_file(self) -> Path:
        """Caminho do arquivo .history.json que persiste o ponto exato de parada para retomada pós-reboot."""
        return self.camera_folder / ".history.json"

    def get_current_recording_time_display(self) -> Optional[str]:
        """Retorna a data e hora formatada em que a gravação se encontra atualmente."""
        if self.history_cursor is not None and self.config.capture_mode == "history":
            try:
                return datetime.fromtimestamp(self.history_cursor).strftime("%d/%m/%Y %H:%M:%S")
            except Exception:
                pass
        return self.current_recording_time

    def _resolve_start_timestamp(self) -> float:
        if self.config.start_date:
            try:
                raw = str(self.config.start_date).strip()
                if raw.replace(".", "", 1).isdigit():
                    days = float(raw)
                    return datetime.now().timestamp() - (days * 86400)
                return datetime.fromisoformat(raw).timestamp()
            except Exception:
                pass
        return datetime.now().timestamp() - (30 * 86400)

    def _resolve_end_timestamp(self) -> float:
        now_ts = datetime.now().timestamp()
        if self.config.end_date:
            try:
                raw = str(self.config.end_date).strip()
                if raw == "0":
                    return now_ts
                if raw.replace(".", "", 1).isdigit():
                    return now_ts - (float(raw) * 86400)
                return datetime.fromisoformat(raw).timestamp()
            except Exception:
                pass
        return now_ts

    def get_history_progress(self) -> Dict[str, Any]:
        """Calcula o percentual e limites de início e fim da gravação histórica para exibição na UI."""
        if self.config.capture_mode != "history" or self.history_cursor is None:
            return {"percent": None, "start_formatted": None, "end_formatted": None}

        start_ts = self._resolve_start_timestamp()
        end_ts = self._resolve_end_timestamp()

        start_formatted = datetime.fromtimestamp(start_ts).strftime("%d/%m/%Y %H:%M") if start_ts else "-"
        end_formatted = datetime.fromtimestamp(end_ts).strftime("%d/%m/%Y %H:%M") if self.config.end_date else "Ao vivo"

        percent = None
        if start_ts and end_ts and end_ts > start_ts:
            ratio = (self.history_cursor - start_ts) / (end_ts - start_ts)
            percent = max(0.0, min(100.0, round(ratio * 100, 1)))

        return {
            "percent": percent,
            "start_formatted": start_formatted,
            "end_formatted": end_formatted,
        }

    def save_checkpoint(self, last_filename: Optional[str] = None, last_file_dt: Optional[datetime] = None) -> None:
        """
        Salva atomicamente o ponto de parada (.history.json) na pasta da câmera.
        Garante que, se o sistema reiniciar, a gravação continue exatamente do mesmo segundo.
        """
        try:
            curr_time_str = self.get_current_recording_time_display()
            rec_iso = datetime.fromtimestamp(self.history_cursor).isoformat() if self.history_cursor else None
            state = {
                "folder": self.config.camera_name,
                "worker_id": self.config.id,
                "capture_mode": self.config.capture_mode,
                "history_cursor": self.history_cursor,
                "current_recording_time": curr_time_str,
                "current_recording_iso": rec_iso,
                "last_image": last_filename or self._last_image_filename,
                "last_image_at": last_file_dt.isoformat() if last_file_dt else self._last_image_iso,
                "captured_count": self.captured_count,
                "tasks_created": self.tasks_created,
                "interval_seconds": self.config.interval_seconds,
                "updated_at": datetime.now().isoformat(),
            }
            tmp_path = self.history_file.with_suffix(".json.tmp")
            tmp_path.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            tmp_path.replace(self.history_file)
        except Exception as exc:
            logger.warning(f"[{self.config.camera_name}] Erro ao salvar checkpoint .history.json: {exc}")

    def load_checkpoint(self) -> bool:
        """
        Carrega o ponto de parada salvo em .history.json ou infere a partir dos arquivos já salvos.
        Retorna True se um checkpoint anterior foi recuperado com sucesso.
        """
        # 1. Tenta carregar .history.json existente
        if self.history_file.is_file():
            try:
                data = json.loads(self.history_file.read_text(encoding="utf-8"))
                saved_cursor = data.get("history_cursor")
                if saved_cursor is not None and isinstance(saved_cursor, (int, float)):
                    self.history_cursor = float(saved_cursor)
                    self.captured_count = max(self.captured_count, int(data.get("captured_count", 0)))
                    self.tasks_created = max(self.tasks_created, int(data.get("tasks_created", 0)))
                    self._last_image_filename = data.get("last_image")
                    self._last_image_iso = data.get("last_image_at")
                    saved_rec_time = data.get("current_recording_time") or datetime.fromtimestamp(self.history_cursor).strftime("%d/%m/%Y %H:%M:%S")
                    self.current_recording_time = saved_rec_time
                    if self._last_image_iso:
                        try:
                            self.last_capture_time = datetime.fromisoformat(self._last_image_iso).strftime("%H:%M:%S")
                        except Exception:
                            pass
                    return True
            except Exception as exc:
                logger.warning(f"[{self.config.camera_name}] Falha ao ler .history.json: {exc}")

        # 2. Se não houver .history.json, verifica se há fotos já capturadas na pasta
        try:
            images = sorted(
                [p for p in self.camera_folder.iterdir() if p.is_file() and p.suffix.lower() in {".jpg", ".jpeg", ".png"}],
                key=lambda p: p.name,
            )
            if images:
                last_img = images[-1]
                match = re.match(r"^(\d{4}-\d{2}-\d{2})_(\d{2}-\d{2}-\d{2})", last_img.stem)
                if match:
                    dt_str = f"{match.group(1)} {match.group(2).replace('-', ':')}"
                    last_dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S")
                    resumed_cursor = last_dt.timestamp() + self.config.interval_seconds
                    self.history_cursor = resumed_cursor
                    self.current_recording_time = datetime.fromtimestamp(resumed_cursor).strftime("%d/%m/%Y %H:%M:%S")
                    self.last_capture_time = last_dt.strftime("%H:%M:%S")
                    self._last_image_filename = last_img.name
                    self._last_image_iso = last_dt.isoformat()
                    self.save_checkpoint(last_filename=last_img.name, last_file_dt=last_dt)
                    return True
        except Exception as exc:
            logger.debug(f"[{self.config.camera_name}] Erro ao verificar imagens existentes: {exc}")

        return False

    def append_log(self, message: str) -> None:
        now_str = datetime.now().strftime("%H:%M:%S")
        entry = f"[{now_str}] {message}"
        self.logs.append(entry)
        logger.info(f"[{self.config.camera_name}] {message}")

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._pause_event.clear()
        self.status = "running"
        self.config.auto_start = True

        # Carrega checkpoint para garantir retomada contínua
        resumed = self.load_checkpoint()
        if resumed and self.current_recording_time:
            self.last_status_message = f"Retomando gravação histórica de onde parou ({self.current_recording_time})"
            self.append_log(f"🔄 Retomando gravação a partir do checkpoint salvo: {self.current_recording_time}")
        else:
            self.last_status_message = "Worker em execução contínua"
            self.append_log("Worker iniciado.")

        self._thread = threading.Thread(target=self._worker_loop, daemon=True)
        self._thread.start()

    def pause(self) -> None:
        self._pause_event.set()
        self.status = "paused"
        self.config.auto_start = False
        self.save_checkpoint()
        self.last_status_message = "Worker pausado pelo usuário"
        self.append_log("Worker pausado.")

    def resume(self) -> None:
        self._pause_event.clear()
        self.status = "running"
        self.config.auto_start = True
        self.last_status_message = "Worker retomado"
        self.append_log("Worker retomado.")

    def stop(self) -> None:
        self._stop_event.set()
        self._pause_event.clear()
        self.status = "stopped"
        self.config.auto_start = False
        self.save_checkpoint()
        self.last_status_message = "Worker encerrado"
        self.append_log("Worker interrompido.")

    def _worker_loop(self) -> None:
        """Loop principal do worker com resiliencia total e retry a cada 13s."""
        while not self._stop_event.is_set():
            if self._pause_event.is_set():
                time.sleep(1.0)
                continue

            try:
                # 1. Captura snapshot da camera (retorna sucesso e se é download histórico acelerado)
                success, is_history_download = self._capture_frame()
                if not success:
                    self.status = "reconnecting"
                    self.last_status_message = f"Câmera offline. Tentando reconectar a cada {int(DEFAULT_RETRY_INTERVAL)}s..."
                    self.append_log(f"⚠️ Falha de captura. Tentando novamente em {int(DEFAULT_RETRY_INTERVAL)}s...")
                    self._wait_seconds(DEFAULT_RETRY_INTERVAL)
                    continue

                self.status = "running"
                if is_history_download:
                    hist_time = self.get_current_recording_time_display() or ""
                    self.last_status_message = f"Baixando gravações históricas acelerado ({hist_time})..."
                else:
                    self.last_status_message = "Câmera conectada. Captura ao vivo ativa."

                # 2. Verifica se atingiu lote para criar tarefa no CVAT
                self._check_and_upload_batch()

                # 3. Aguarda próximo ciclo:
                if is_history_download:
                    # No modo histórico, o download é rápido/acelerado (sem esperar 10 min na vida real)
                    self._wait_seconds(0.2)
                else:
                    # No modo ao vivo, aguarda o intervalo configurado em tempo real
                    next_dt = datetime.now().timestamp() + self.config.interval_seconds
                    self.next_capture_time = datetime.fromtimestamp(next_dt).strftime("%H:%M:%S")
                    self._wait_seconds(self.config.interval_seconds)

            except Exception as exc:
                self.status = "reconnecting"
                self.last_status_message = f"Erro no ciclo: {str(exc)}"
                self.append_log(f"❌ Erro: {exc}. Retentando em {int(DEFAULT_RETRY_INTERVAL)}s...")
                self._wait_seconds(DEFAULT_RETRY_INTERVAL)

        self.status = "stopped"

    def _wait_seconds(self, seconds: float) -> None:
        start_wait = time.time()
        while time.time() - start_wait < seconds:
            if self._stop_event.is_set():
                break
            time.sleep(0.1)

    def _capture_frame(self) -> Tuple[bool, bool]:
        """
        Captura um snapshot (ao vivo ou histórico) e salva na pasta da câmera respeitando filtros PTZ.
        Retorna (sucesso, is_history_download).
        """
        now = datetime.now()
        url = (self.config.snapshot_url or "").strip()
        if not url:
            url = build_digifort_http_url(
                host=self.config.digifort_host,
                camera=self.config.camera_name,
                port=self.config.digifort_port,
                username=self.config.username,
                password=self.config.password,
            )

        # 1. Modo Histórico ou Ao Vivo
        is_history = (self.config.capture_mode == "history" and bool(self.config.start_date))

        # Consulta telemetria (posição, preset, Pan/Tilt/Zoom) se for câmera ao vivo ou se houver parâmetros PTZ
        telemetry: Dict[str, Any] = {}
        if not is_history or (self.config.ptz_mode == "presets" and self.config.selected_presets):
            try:
                telemetry = fetch_digifort_camera_telemetry(url, camera_name=self.config.camera_name, timeout=3.0)
            except Exception as e:
                logger.debug(f"Aviso ao consultar telemetria PTZ: {e}")

        # 2. Filtro PTZ: Apenas aplicável em captura AO VIVO (Live), pois a telemetria PTZ da API Digifort (PTZ/GetStatus)
        # mede a rotação física real dos motores da câmera AGORA, e não durante uma gravação de dias atrás.
        if not is_history and self.config.ptz_mode == "presets" and self.config.selected_presets:
            curr_preset = telemetry.get("current_preset")
            if curr_preset is not None and curr_preset not in self.config.selected_presets:
                preset_label = telemetry.get("current_preset_name") or f"Preset {curr_preset}"
                self.append_log(f"⏸️ Câmera em #{curr_preset} ({preset_label}). Fora dos presets autorizados {self.config.selected_presets}. Foto ignorada.")
                return True, False

        # Gera sufixo de identificação da posição/preset da câmera (ex: _P1-Medicao ou _Pan41_Tilt39_Zoom46)
        pos_tag = format_position_tag(telemetry)
        if not pos_tag and is_history and len(self.config.selected_presets) == 1:
            pos_tag = f"_P{self.config.selected_presets[0]}"

        # 3. Modo Histórico (Gravações passadas do Digifort com avanço temporal de intervalo)
        if is_history:
            if self.history_cursor is None:
                if not self.load_checkpoint():
                    self.history_cursor = self._resolve_start_timestamp()
                    self.save_checkpoint()

            now_ts = datetime.now().timestamp()
            if self.config.end_date:
                end_ts = self._resolve_end_timestamp()
                if self.history_cursor >= end_ts:
                    self.append_log("🏁 Fim do período histórico configurado atingido. Encerrando worker.")
                    self.save_checkpoint()
                    self._stop_event.set()
                    return True, False
            elif self.history_cursor >= now_ts:
                if not getattr(self, "_caught_up_to_live", False):
                    self._caught_up_to_live = True
                    self.append_log(f"🏁 Gravações passadas concluídas! Transicionando para captura contínua ao vivo a cada {int(self.config.interval_seconds)}s.")
                is_history = False

        if is_history:
            captured_dt = datetime.fromtimestamp(self.history_cursor)
            stamp_str = captured_dt.strftime(STAMP_FORMAT)
            filename = f"{stamp_str}{pos_tag}.jpg"
            target_path = self.camera_folder / filename
            if target_path.exists():
                filename = f"{stamp_str}_{captured_dt.microsecond}{pos_tag}.jpg"
                target_path = self.camera_folder / filename

            parsed = urlparse(url.replace("rtsp://", "http://"))
            host = parsed.hostname or self.config.digifort_host or "10.247.72.55"
            port = parsed.port or self.config.digifort_port or 8601
            auth = (parsed.username, parsed.password) if parsed.username else None
            playback_url = f"http://{host}:{port}/Interface/Cameras/Playback/GetSnapshot"
            params = {
                "Camera": self.config.camera_name,
                "Date": captured_dt.strftime("%Y.%m.%d"),
                "Time": captured_dt.strftime("%H.%M.%S.000"),
                "Width": "-1",
                "Height": "-1",
                "Quality": "100",
            }
            try:
                with httpx.Client(timeout=10.0, verify=False) as client:
                    resp = client.get(playback_url, params=params, auth=auth)
                    resp.raise_for_status()
                    if not resp.content or "image" not in resp.headers.get("content-type", ""):
                        # Gravação inexistente/lacuna: avança intervalo e salva checkpoint
                        self.history_cursor += self.config.interval_seconds
                        self.current_recording_time = self.get_current_recording_time_display()
                        self.save_checkpoint()
                        return True, True
                    target_path.write_bytes(resp.content)
                self.history_cursor += self.config.interval_seconds
                self.captured_count += 1
                self.last_capture_time = captured_dt.strftime("%H:%M:%S")
                self.current_recording_time = captured_dt.strftime("%d/%m/%Y %H:%M:%S")
                self._last_image_filename = filename
                self._last_image_iso = captured_dt.isoformat()
                self.save_checkpoint(last_filename=filename, last_file_dt=captured_dt)
                self.append_log(f"📸 Foto histórica salva ({captured_dt.strftime('%d/%m/%Y %H:%M:%S')}): {filename}")
                return True, True
            except Exception as e:
                self.append_log(f"⚠️ Erro ao buscar snapshot histórico: {e}")
                self.history_cursor += self.config.interval_seconds
                self.current_recording_time = self.get_current_recording_time_display()
                self.save_checkpoint()
                return True, True

        # 3. Captura Ao Vivo (Tempo Real)
        stamp_str = now.strftime(STAMP_FORMAT)
        filename = f"{stamp_str}{pos_tag}.jpg"
        target_path = self.camera_folder / filename
        if target_path.exists():
            filename = f"{stamp_str}_{now.microsecond}{pos_tag}.jpg"
            target_path = self.camera_folder / filename

        is_rtsp = self.config.use_rtsp or url.lower().startswith("rtsp://")
        if is_rtsp:
            import cv2
            cap = None
            try:
                cap = cv2.VideoCapture(url)
                if not cap.isOpened():
                    return False, False
                ret, frame = cap.read()
                if not ret or frame is None:
                    return False, False
                cv2.imwrite(str(target_path), frame, [int(cv2.IMWRITE_JPEG_QUALITY), 95])
            except Exception:
                return False, False
            finally:
                if cap is not None:
                    cap.release()
        else:
            try:
                with httpx.Client(timeout=10.0, verify=False) as client:
                    resp = client.get(url)
                    resp.raise_for_status()
                    if not resp.content:
                        return False, False
                    target_path.write_bytes(resp.content)
            except Exception:
                return False, False

        self.captured_count += 1
        self.last_capture_time = now.strftime("%H:%M:%S")
        self.append_log(f"📸 Foto salva: {filename}")
        return True, False

    def _check_and_upload_batch(self) -> None:
        """Verifica se há imagens suficientes para fechar um lote e enviar ao CVAT."""
        images = sorted(
            [p for p in self.camera_folder.iterdir() if p.is_file() and p.suffix.lower() in {".jpg", ".jpeg", ".png"}],
            key=lambda p: p.name,
        )

        total_local = len(images)
        needed = self.config.images_per_task

        if total_local < needed:
            return

        batch = images[:needed]

        def format_task_dt(stem_name: str) -> str:
            parts = stem_name.split("_")
            if len(parts) >= 2:
                d = parts[0]
                t = parts[1].replace("-", ":")
                return f"{d} {t}"
            return stem_name

        first_dt = format_task_dt(batch[0].stem)
        last_dt = format_task_dt(batch[-1].stem)
        proj_name = self.config.cvat_project_name or self.config.camera_name
        task_name = f"{proj_name} | {first_dt} a {last_dt}"

        self.append_log(f"📦 Lote completo atingido ({len(batch)} fotos). Criando task no CVAT: '{task_name}'...")

        try:
            settings = Settings.load()
            with make_client(host=settings.host, credentials=(settings.username, settings.password)) as client:
                if self.config.cvat_organization:
                    client.organization_slug = self.config.cvat_organization

                # Resolve projeto destino
                project_id = self.config.cvat_project_id
                if not project_id:
                    # Busca ou cria projeto com nome especificado ou da camera
                    target_name = self.config.cvat_project_name or self.config.camera_name
                    matching = [p for p in client.projects.list() if p.name.casefold() == target_name.casefold()]
                    if matching:
                        project_id = int(matching[0].id)
                    else:
                        new_proj = client.projects.create(spec={"name": target_name})
                        project_id = int(new_proj.id)
                        self.append_log(f"📁 Novo projeto criado no CVAT: '{target_name}' (ID {project_id})")

                # Cria a task
                task = client.tasks.create(spec={"name": task_name, "project_id": project_id})
                task.upload_data(
                    resources=[str(p) for p in batch],
                    resource_type=ResourceType.LOCAL,
                    params={"image_quality": 100, "sorting_method": "predefined"},
                )

                self.tasks_created += 1
                self.append_log(f"✅ Task criada com sucesso no CVAT (ID {task.id}) com {len(batch)} imagens!")

                # Limpeza automatica das imagens do lote se configurado
                if self.config.auto_remove:
                    for p in batch:
                        try:
                            p.unlink(missing_ok=True)
                        except Exception:
                            pass
                    self.append_log(f"🧹 {len(batch)} imagens locais removidas após confirmação no CVAT.")

        except Exception as exc:
            self.append_log(f"⚠️ Falha no envio ao CVAT: {exc}. As imagens serão mantidas localmente para nova tentativa.")

    def to_dict(self) -> Dict[str, Any]:
        host_display = self.config.digifort_host
        if not host_display and self.config.snapshot_url:
            try:
                parsed = urlparse(self.config.snapshot_url)
                host_display = parsed.hostname or "stream"
            except Exception:
                host_display = "stream"

        prog = self.get_history_progress()
        curr_rec_time = self.get_current_recording_time_display()

        return {
            "id": self.config.id,
            "camera_name": self.config.camera_name,
            "source_type": self.config.source_type,
            "digifort_host": host_display,
            "snapshot_url": self.config.snapshot_url,
            "cvat_project_id": self.config.cvat_project_id,
            "cvat_project_name": self.config.cvat_project_name,
            "cvat_organization": self.config.cvat_organization,
            "interval_seconds": self.config.interval_seconds,
            "images_per_task": self.config.images_per_task,
            "auto_remove": self.config.auto_remove,
            "ptz_pan": self.config.ptz_pan,
            "ptz_tilt": self.config.ptz_tilt,
            "ptz_zoom": self.config.ptz_zoom,
            "ptz_preset": self.config.ptz_preset,
            "ptz_mode": self.config.ptz_mode,
            "selected_presets": self.config.selected_presets,
            "capture_mode": self.config.capture_mode,
            "start_date": self.config.start_date,
            "end_date": self.config.end_date,
            "sync_pipeline": self.config.sync_pipeline,
            "auto_start": getattr(self.config, "auto_start", True),
            "status": self.status,
            "last_status_message": self.last_status_message,
            "current_recording_time": curr_rec_time,
            "history_progress_percent": prog["percent"],
            "history_start_formatted": prog["start_formatted"],
            "history_end_formatted": prog["end_formatted"],
            "has_checkpoint": self.history_file.is_file(),
            "captured_count": self.captured_count,
            "tasks_created": self.tasks_created,
            "last_capture_time": self.last_capture_time,
            "next_capture_time": self.next_capture_time,
            "logs": list(self.logs)[-15:],
            "created_at": self.config.created_at,
        }


class AcquisitionManager:
    """Gerenciador central de todos os workers de aquisição de câmeras."""

    def __init__(self):
        self._workers: Dict[str, ActiveWorker] = {}
        self._lock = threading.Lock()
        self.load_workers()

    def load_workers(self) -> None:
        if not WORKERS_FILE.is_file():
            return
        try:
            data = json.loads(WORKERS_FILE.read_text(encoding="utf-8"))
            for item in data:
                cfg = AcquisitionWorkerConfig(**item)
                worker = ActiveWorker(cfg)
                self._workers[cfg.id] = worker
        except Exception as exc:
            logger.error(f"Erro ao carregar workers de aquisicao: {exc}")

    def save_workers(self) -> None:
        try:
            data = [asdict(w.config) for w in self._workers.values()]
            WORKERS_FILE.parent.mkdir(parents=True, exist_ok=True)
            WORKERS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception as exc:
            logger.error(f"Erro ao salvar workers de aquisicao: {exc}")

    def resume_saved_workers(self) -> None:
        """Inicia os workers marcados como ativos após reinício do serviço."""
        with self._lock:
            for worker in self._workers.values():
                if getattr(worker.config, "auto_start", True):
                    try:
                        worker.start()
                        logger.info(f"Worker {worker.config.id} ({worker.config.camera_name}) retomado com sucesso.")
                    except Exception as exc:
                        logger.error(f"Erro ao retomar worker {worker.config.id}: {exc}")

    def stop_all_workers(self) -> None:
        """Encerra graciosamente todos os workers salvando seus pontos de parada."""
        with self._lock:
            for worker in self._workers.values():
                try:
                    worker.save_checkpoint()
                    worker.stop()
                except Exception:
                    pass

    def create_worker(self, config: AcquisitionWorkerConfig, auto_start: bool = True) -> ActiveWorker:
        with self._lock:
            if config.id in self._workers:
                self._workers[config.id].stop()
            config.auto_start = auto_start
            worker = ActiveWorker(config)
            self._workers[config.id] = worker
            self.save_workers()
            if auto_start:
                worker.start()
            return worker

    def get_worker(self, worker_id: str) -> Optional[ActiveWorker]:
        return self._workers.get(worker_id)

    def list_workers(self) -> List[Dict[str, Any]]:
        return [w.to_dict() for w in self._workers.values()]

    def pause_worker(self, worker_id: str) -> bool:
        w = self.get_worker(worker_id)
        if w:
            w.pause()
            w.config.auto_start = False
            self.save_workers()
            return True
        return False

    def resume_worker(self, worker_id: str) -> bool:
        w = self.get_worker(worker_id)
        if w:
            w.resume()
            w.config.auto_start = True
            self.save_workers()
            return True
        return False

    def stop_worker(self, worker_id: str) -> bool:
        w = self.get_worker(worker_id)
        if w:
            w.stop()
            w.config.auto_start = False
            self.save_workers()
            return True
        return False

    def start_worker(self, worker_id: str) -> bool:
        w = self.get_worker(worker_id)
        if w:
            w.start()
            w.config.auto_start = True
            self.save_workers()
            return True
        return False

    def delete_worker(self, worker_id: str, delete_images: bool = False) -> bool:
        with self._lock:
            w = self._workers.pop(worker_id, None)
            if w:
                w.stop()
                if delete_images and w.camera_folder.is_dir():
                    try:
                        shutil.rmtree(w.camera_folder, ignore_errors=True)
                    except Exception:
                        pass
                self.save_workers()
                return True
            return False


acquisition_manager = AcquisitionManager()


def sync_pipeline_files(config: AcquisitionWorkerConfig) -> Dict[str, Any]:
    """Salva e sincroniza a configuração criada nos arquivos pipeline.env, digifort-scripts/.env e cvat_scripts/.env."""
    root_dir = Path(__file__).resolve().parent.parent.parent.parent
    results = {}
    try:
        # 1. digifort-scripts/.env
        digi_env_path = root_dir / "digifort-scripts" / ".env"
        cameras_list = []
        if digi_env_path.is_file():
            content = digi_env_path.read_text(encoding="utf-8")
            match = re.search(r"CAMERAS='(\[.*?\])'", content, re.DOTALL)
            if match:
                try:
                    cameras_list = json.loads(match.group(1))
                except Exception:
                    cameras_list = []

        cameras_list = [c for c in cameras_list if c.get("folder") != config.camera_name]
        is_hist = (config.capture_mode == "history")
        new_cam_entry = {
            "url": config.snapshot_url,
            "folder": config.camera_name,
            "interval_seconds": int(config.interval_seconds),
            "history": is_hist,
            "start_date": config.start_date if is_hist else None,
            "end_date": config.end_date if is_hist else None,
            "quality": 100,
        }
        cameras_list.append(new_cam_entry)
        cameras_json = json.dumps(cameras_list, indent=2)

        digi_content = (
            f"CAMERAS='{cameras_json}'\n"
            f"OUTPUT_DIR=./volume/image\n"
            f"REQUEST_TIMEOUT=10\n"
        )
        digi_env_path.parent.mkdir(parents=True, exist_ok=True)
        digi_env_path.write_text(digi_content, encoding="utf-8")
        results["digifort"] = str(digi_env_path)

        # 2. cvat_scripts/.env
        cvat_env_path = root_dir / "cvat_scripts" / ".env"
        settings = Settings.load()
        folders_list = []
        if cvat_env_path.is_file():
            content = cvat_env_path.read_text(encoding="utf-8")
            match = re.search(r"FOLDERS='(\[.*?\])'", content, re.DOTALL)
            if match:
                try:
                    folders_list = json.loads(match.group(1))
                except Exception:
                    folders_list = []

        folders_list = [f for f in folders_list if not str(f.get("images_dir", "")).endswith(f"/{config.camera_name}")]
        new_folder_entry = {
            "images_dir": f"../digifort-scripts/volume/image/{config.camera_name}",
            "images_per_task": config.images_per_task,
            "auto_remove": config.auto_remove,
        }
        if config.cvat_project_id:
            new_folder_entry["project_id"] = config.cvat_project_id
        if config.cvat_organization:
            new_folder_entry["organization"] = config.cvat_organization

        folders_list.append(new_folder_entry)
        folders_json = json.dumps(folders_list, indent=2)

        cvat_content = (
            f"CVAT_URL={settings.host}\n"
            f"CVAT_USERNAME={settings.username or ''}\n"
            f"CVAT_PASSWORD='{settings.password or ''}'\n"
            f"CVAT_ORGANIZATION={config.cvat_organization or ''}\n"
            f"CVAT_LABELS='[]'\n"
            f"DRY_RUN=false\n"
            f"LOOP=true\n"
            f"POLL_INTERVAL_SECONDS=5\n"
            f"AUTO_REMOVE={'true' if config.auto_remove else 'false'}\n"
            f"FOLDERS='{folders_json}'\n"
        )
        cvat_env_path.parent.mkdir(parents=True, exist_ok=True)
        cvat_env_path.write_text(cvat_content, encoding="utf-8")
        results["cvat"] = str(cvat_env_path)

        # 3. pipeline.env
        pipeline_env_path = root_dir / "pipeline.env"
        pipeline_cfg = {
            "cameras": {"env_file": "digifort-scripts/.env"},
            "cvat": {"enabled": True, "env_file": "cvat_scripts/.env"},
        }
        pipeline_content = (
            f"PIPELINE='{json.dumps(pipeline_cfg, indent=2)}'\n"
            f"RESTART_SECONDS=13\n"
        )
        pipeline_env_path.write_text(pipeline_content, encoding="utf-8")
        results["pipeline"] = str(pipeline_env_path)

        logger.info(f"Arquivos da pipeline sincronizados com sucesso: {results}")
    except Exception as exc:
        logger.error(f"Erro ao sincronizar arquivos da pipeline: {exc}")
    return results
