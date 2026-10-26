# syntax=docker/dockerfile:1.7
# Build context is the repository root. Images exclude weights, data and secrets.
ARG PYTHON_IMAGE=python:3.11.16-slim-bookworm@sha256:4b4c524dc3dce996864e030c7bd9c6b0e517597189fee48f48e05b499442444b
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.12.17@sha256:1194b357d63b7bea6c121d8eef5d08d29c26ffbcfc64ec5ebcefd642ea759edb

FROM ${UV_IMAGE} AS uvbin
FROM ${PYTHON_IMAGE} AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_CACHE_DIR=/tmp/uv-cache \
    XDG_CACHE_HOME=/tmp/clawvoice-cache \
    NUMBA_CACHE_DIR=/tmp/clawvoice-cache/numba \
    HF_HOME=/tmp/clawvoice-cache/huggingface \
    PATH=/app/.venv/bin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates \
      ffmpeg \
      git \
      libgomp1 \
      libsndfile1 \
      passwd \
 && rm -rf /var/lib/apt/lists/*

RUN groupadd --gid 10001 clawvoice \
 && useradd --uid 10001 --gid 10001 --home-dir /app --no-create-home --shell /usr/sbin/nologin clawvoice \
 && install -d -o clawvoice -g clawvoice -m 0700 /data

WORKDIR /app
COPY --from=uvbin /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock README.md LICENSE THIRD_PARTY_NOTICES.md /app/
COPY src /app/src
COPY web /app/web

FROM base AS api
RUN --mount=type=cache,id=clawvoice-uv,target=/tmp/uv-cache,sharing=locked \
    uv sync --frozen --no-dev --no-editable
ENV CLAWVOICE_DATA_DIR=/data \
    CLAWVOICE_MODEL_DIR=/models/chatterbox-turbo \
    CLAWVOICE_WEB_DIR=/app/web \
    CLAWVOICE_BIND_HOST=0.0.0.0 \
    CLAWVOICE_BIND_PORT=8765
USER 10001:10001
CMD ["clawvoice", "serve", "--no-worker", "--host", "0.0.0.0", "--port", "8765"]

FROM base AS worker
# Candidate Pascal-compatible stack: Python 3.11, torch/torchaudio 2.6.0+cu124.
# The lock resolves these from the explicit CUDA 12.4 index. Target verification
# is still required; see docs/compatibility.md.
RUN --mount=type=cache,id=clawvoice-uv,target=/tmp/uv-cache,sharing=locked \
    uv sync --frozen --no-dev --extra inference --no-editable
ENV CLAWVOICE_DATA_DIR=/data \
    CLAWVOICE_MODEL_DIR=/models/chatterbox-turbo \
    CLAWVOICE_WEB_DIR=/app/web
USER 10001:10001
CMD ["clawvoice", "worker"]
