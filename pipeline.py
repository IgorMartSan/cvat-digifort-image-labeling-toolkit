"""Ponto de entrada na raiz para o servico de pipeline unificada.

Redireciona a execucao para o modulo do servico em pipeline/main.py.
"""

import signal
from pipeline.main import *

if __name__ == "__main__":
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, signal.default_int_handler)
    raise SystemExit(main())
