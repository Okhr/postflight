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
      # OpenCL, for Gyroflow's warping: rusticl/Mesa on AMD, NEO on Intel, pocl as
      # the CPU fallback. NVIDIA cannot be shipped (its driver is injected by the
      # container runtime), but the ICD pointing at it is created below.
      ocl-icd-libopencl1 mesa-opencl-icd intel-opencl-icd pocl-opencl-icd clinfo \
      # Vulkan, which is what Gyroflow's wgpu backend uses when OpenCL exposes no
      # GPU. mesa-vulkan-drivers covers AMD (radv) and Intel (anv); NVIDIA's ICD
      # comes from the container runtime. vulkan-tools is the probe.
      libvulkan1 mesa-vulkan-drivers vulkan-tools \
      # VAAPI decoding (decode only, see services/proxy.py). One image has to cover
      # every vendor: mesa-va-drivers is AMD radeonsi, intel-media-va-driver is
      # Intel Gen9+ (iHD) and i965 the older parts. Without the Intel ones, an
      # Intel host fails exactly the way an NVIDIA host does: libva resolves the
      # DRM driver name, looks for `iHD_drv_video.so`, and finds nothing.
      # `vainfo` earns its 3 MB the same way `clinfo` does: on a new machine, it
      # is the difference between a diagnosis and an afternoon.
      mesa-va-drivers intel-media-va-driver i965-va-driver libva2 libva-drm2 vainfo \
      # Runtime dependencies of the Gyroflow binary (Qt6 is bundled, libc++ is not)
      libc++1 libc++abi1 \
      libgl1 libegl1 libgbm1 libdrm2 libglx0 libopengl0 \
      libasound2t64 libpulse0 libdbus-1-3 libglib2.0-0t64 \
      libfontconfig1 libfreetype6 fontconfig \
      libxkbcommon0 libx11-6 libxcb1 \
      libkrb5-3 libgssapi-krb5-2 libpcre2-8-0 libbrotli1 \
    && rm -rf /var/lib/apt/lists/*

# NVIDIA OpenCL, for Gyroflow on an NVIDIA host. The library itself is never in the
# image (it is bind-mounted by the container runtime, hence the compose override),
# but the ICD naming it has to exist, and no package provides it here.
#
# Measured 2026-08-17: this dangling ICD is harmless on a machine with no NVIDIA
# driver. The loader cannot dlopen the soname, skips the vendor, and `clinfo` still
# exits 0 listing the platforms that do work. So it ships unconditionally rather
# than being an image variant.
RUN mkdir -p /etc/OpenCL/vendors \
 && echo "libnvidia-opencl.so.1" > /etc/OpenCL/vendors/nvidia.icd

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
