# syntax=docker/dockerfile:1
FROM python:3.12-slim-bookworm

# Configurações do ambiente para máxima performance e compilação de bytecode
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"

# Instala apenas as bibliotecas essenciais de SO para OpenCV, PyTorch e monitoramento
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 \
    libglib2.0-0 \
    libgomp1 \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copia os binários standalone oficiais do uv (rápido e sem overhead)
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# Camada 1: Cópia isolada dos manifestos para cache de dependências
COPY pyproject.toml uv.lock ./

# Instala as dependências congeladas sem o código da aplicação (cache eficiente com cache mount)
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

# Camada 2: Cópia do código-fonte da aplicação
COPY . .

# Finaliza a sincronização do projeto
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# Cria pontos de montagem para persistência de dados
RUN mkdir -p /app/volume /app/data /app/models /app/logs /app/.cache

EXPOSE 3012

# Healthcheck interno do container
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:3012/api/health || exit 1

CMD ["python", "run.py"]
