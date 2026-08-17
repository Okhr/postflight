# syntax=docker/dockerfile:1
# ---------------------------------------------------------------------------
# 1. Frontend build
# ---------------------------------------------------------------------------
FROM node:22-slim AS frontend

WORKDIR /build
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm install --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

# ---------------------------------------------------------------------------
# 2. Runtime
# ---------------------------------------------------------------------------
# ubuntu:25.04 rather than python:slim: it ships a recent Mesa (rusticl for
# OpenCL, which Gyroflow uses for warping) and it is the distribution the Gyroflow
# binary is built against.
FROM ubuntu:25.04 AS runtime

ARG GYROFLOW_VERSION=1.6.3
ARG MP4_MERGE_VERSION=0.1.11
ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl \
      python3 python3-venv python3-pip \
      ffmpeg \
      # OpenCL: rusticl/Mesa when /dev/dri is present, pocl as the CPU fallback.
      # This is what lets the stack run with or without a GPU.
      ocl-icd-libopencl1 mesa-opencl-icd pocl-opencl-icd clinfo \
      # VAAPI decoding (decode only, see services/proxy.py)
      mesa-va-drivers libva2 libva-drm2 \
      # Runtime dependencies of the Gyroflow binary (Qt6 is bundled, libc++ is not)
      libc++1 libc++abi1 \
      libgl1 libegl1 libgbm1 libdrm2 libglx0 libopengl0 \
      libasound2t64 libpulse0 libdbus-1-3 libglib2.0-0t64 \
      libfontconfig1 libfreetype6 fontconfig \
      libxkbcommon0 libx11-6 libxcb1 \
      libkrb5-3 libgssapi-krb5-2 libpcre2-8-0 libbrotli1 \
    && rm -rf /var/lib/apt/lists/*

# Gyroflow: the tarball ships lib/, plugins/, qml/ and camera_presets/
RUN curl -fsSL "https://github.com/gyroflow/gyroflow/releases/download/v${GYROFLOW_VERSION}/Gyroflow-linux64.tar.gz" \
      -o /tmp/gyroflow.tar.gz \
 && mkdir -p /opt/gyroflow \
 && tar -xzf /tmp/gyroflow.tar.gz -C /opt/gyroflow --strip-components=1 \
 && rm /tmp/gyroflow.tar.gz \
 && printf '#!/bin/sh\nexport LD_LIBRARY_PATH="/opt/gyroflow/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"\nexec /opt/gyroflow/gyroflow "$@"\n' \
      > /usr/local/bin/gyroflow \
 && chmod +x /usr/local/bin/gyroflow

# mp4_merge: the only way to join split rushes without destroying the gyro
# stream. ffmpeg cannot do it (codec `none`, rejected by both mp4 and mkv).
RUN curl -fsSL "https://github.com/gyroflow/mp4-merge/releases/download/v${MP4_MERGE_VERSION}/mp4_merge-linux64" \
      -o /usr/local/bin/mp4_merge \
 && chmod +x /usr/local/bin/mp4_merge

ENV VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH"
RUN python3 -m venv "$VIRTUAL_ENV"

COPY backend/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt && rm /tmp/requirements.txt

WORKDIR /app
COPY backend/app /app/app
COPY --from=frontend /build/dist /app/static
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

ENV QT_QPA_PLATFORM=offscreen \
    XDG_RUNTIME_DIR=/tmp/runtime \
    RUSTICL_ENABLE=radeonsi \
    HOME=/tmp \
    PYTHONUNBUFFERED=1 \
    VS_DATA_DIR=/data \
    VS_STATIC_DIR=/app/static

VOLUME ["/data"]
EXPOSE 8000

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["api"]
