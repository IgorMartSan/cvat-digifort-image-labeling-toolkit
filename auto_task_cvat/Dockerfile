# Base com suporte CUDA e cuDNN
FROM nvidia/cuda:12.2.2-cudnn8-devel-ubuntu22.04

# Atualiza e instala dependências do sistema
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg libsm6 libxext6 curl ca-certificates \
 && apt-get clean && rm -rf /var/lib/apt/lists/*

# Copia o binário do uv a partir de uma imagem oficial
COPY --from=ghcr.io/astral-sh/uv:0.5.30 /uv /uvx /bin/

# Configura diretório principal do projeto
WORKDIR /code

# Ativa compilação de bytecode e modo de cópia de pacotes
ENV UV_COMPILE_BYTECODE=1
ENV UV_LINK_MODE=copy

# Etapa para instalar as dependências (sem instalar o projeto ainda)
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    uv sync --frozen --no-install-project --no-dev

# Copia o restante do projeto
ADD . /code

# Instala o projeto (após copiar o código)
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# Atualiza PATH para rodar scripts Python instalados (se houver)
ENV PATH="/code/.venv/bin:/root/.local/bin:$PATH"

# Define diretório de execução
WORKDIR /code/app

# Comando padrão para execução
CMD ["uv", "run", "main_mjpeg_stream_funcional.py"]
