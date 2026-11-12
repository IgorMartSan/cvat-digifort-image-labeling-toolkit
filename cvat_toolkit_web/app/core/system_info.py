from __future__ import annotations

import logging
from typing import Any, Dict, Optional
import psutil

logger = logging.getLogger("cvat_toolkit.system_info")

# Inicialização não bloqueante do contador de CPU
try:
    psutil.cpu_percent(interval=None)
except Exception:
    pass

_nvml_initialized: Optional[bool] = None


def _init_nvml() -> bool:
    global _nvml_initialized
    if _nvml_initialized is not None:
        return _nvml_initialized

    try:
        import pynvml
        pynvml.nvmlInit()
        _nvml_initialized = True
    except Exception as exc:
        logger.debug(f"NVIDIA NVML não pôde ser inicializado: {exc}")
        _nvml_initialized = False

    return _nvml_initialized


def get_system_resources() -> Dict[str, Any]:
    """Coleta telemetria em tempo real dos recursos da máquina:

    - Uso de CPU (%) e contagem de núcleos
    - Memória RAM usada, total e porcentagem (%)
    - Uso de GPU (%) e temperatura (°C)
    - Memória VRAM usada, total e porcentagem (%)
    """
    # 1. CPU
    try:
        cpu_percent = round(float(psutil.cpu_percent(interval=None)), 1)
        cpu_count = psutil.cpu_count(logical=True) or 1
    except Exception as exc:
        logger.error(f"Erro ao ler CPU via psutil: {exc}")
        cpu_percent = 0.0
        cpu_count = 1

    # 2. RAM
    try:
        mem = psutil.virtual_memory()
        ram_used_gb = round(mem.used / (1024 ** 3), 1)
        ram_total_gb = round(mem.total / (1024 ** 3), 1)
        ram_percent = round(mem.percent, 1)
    except Exception as exc:
        logger.error(f"Erro ao ler RAM via psutil: {exc}")
        ram_used_gb = 0.0
        ram_total_gb = 0.0
        ram_percent = 0.0

    # 3. GPU e VRAM
    gpu_data: Dict[str, Any] = {
        "available": False,
        "name": "N/A",
        "short_name": "N/A",
        "percent": 0,
        "temperature": None,
    }
    vram_data: Dict[str, Any] = {
        "available": False,
        "used_gb": 0.0,
        "total_gb": 0.0,
        "percent": 0.0,
    }

    if _init_nvml():
        try:
            import pynvml
            device_count = pynvml.nvmlDeviceGetCount()
            if device_count > 0:
                handle = pynvml.nvmlDeviceGetHandleByIndex(0)
                raw_name = pynvml.nvmlDeviceGetName(handle)
                name = raw_name.decode("utf-8") if isinstance(raw_name, bytes) else str(raw_name)
                
                # Nome curto mais amigável para exibição
                short_name = (
                    name.replace("NVIDIA GeForce ", "")
                    .replace("NVIDIA ", "")
                    .replace("GeForce ", "")
                    .strip()
                )

                util = pynvml.nvmlDeviceGetUtilizationRates(handle)
                mem_info = pynvml.nvmlDeviceGetMemoryInfo(handle)
                
                try:
                    temp = int(pynvml.nvmlDeviceGetTemperature(handle, pynvml.NVML_TEMPERATURE_GPU))
                except Exception:
                    temp = None

                gpu_percent = int(util.gpu)
                vram_used_gb = round(mem_info.used / (1024 ** 3), 1)
                vram_total_gb = round(mem_info.total / (1024 ** 3), 1)
                vram_percent = round((mem_info.used / mem_info.total * 100), 1) if mem_info.total > 0 else 0.0

                gpu_data = {
                    "available": True,
                    "name": name,
                    "short_name": short_name,
                    "percent": gpu_percent,
                    "temperature": temp,
                    "device_count": device_count,
                }
                vram_data = {
                    "available": True,
                    "used_gb": vram_used_gb,
                    "total_gb": vram_total_gb,
                    "percent": vram_percent,
                }
        except Exception as exc:
            logger.debug(f"Falha ao consultar NVML: {exc}")

    # Fallback com PyTorch caso NVML não esteja disponível mas CUDA esteja
    if not gpu_data["available"]:
        try:
            import torch
            if torch.cuda.is_available():
                name = torch.cuda.get_device_name(0)
                short_name = (
                    name.replace("NVIDIA GeForce ", "")
                    .replace("NVIDIA ", "")
                    .replace("GeForce ", "")
                    .strip()
                )
                total = torch.cuda.get_device_properties(0).total_memory / (1024 ** 3)
                allocated = torch.cuda.memory_allocated(0) / (1024 ** 3)
                vram_percent = round((allocated / total * 100), 1) if total > 0 else 0.0

                gpu_data = {
                    "available": True,
                    "name": name,
                    "short_name": short_name,
                    "percent": 0,
                    "temperature": None,
                    "device_count": torch.cuda.device_count(),
                }
                vram_data = {
                    "available": True,
                    "used_gb": round(allocated, 1),
                    "total_gb": round(total, 1),
                    "percent": vram_percent,
                }
        except Exception as exc:
            logger.debug(f"Fallback PyTorch CUDA falhou: {exc}")

    return {
        "cpu": {
            "percent": cpu_percent,
            "cores": cpu_count,
        },
        "ram": {
            "used_gb": ram_used_gb,
            "total_gb": ram_total_gb,
            "percent": ram_percent,
        },
        "gpu": gpu_data,
        "vram": vram_data,
    }
