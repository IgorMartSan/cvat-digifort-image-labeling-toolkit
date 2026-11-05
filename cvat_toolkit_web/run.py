from __future__ import annotations

import os
import socket
import uvicorn
from dotenv import load_dotenv

load_dotenv()
os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")


def get_local_ip() -> str:
    """Tenta obter o IP principal da máquina na rede local."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"


if __name__ == "__main__":
    host = os.getenv("WEB_HOST", "0.0.0.0")
    port = int(os.getenv("WEB_PORT", "3012"))
    local_ip = get_local_ip()

    print("\n" + "=" * 60)
    print(" ⚡ CVAT AI Toolkit Web inicializado!")
    print("=" * 60)
    print(f" • Acesso Local:   http://localhost:{port}")
    print(f" • Acesso Remoto:  http://{local_ip}:{port}")
    print(f" • Escutando em:   {host}:{port}")
    print("=" * 60 + "\n")

    uvicorn.run("app.main:app", host=host, port=port, reload=True, timeout_graceful_shutdown=1)
