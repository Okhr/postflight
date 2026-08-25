# syntax=docker/dockerfile:1
#
# Two images out of one file, sharing a base layer.
#
# The split is worth it in one direction only, and by more than it looks. Measured:
# the API image comes to 656 MB against the 1.98 GB of the single image that came
# before, so 1.33 GB less, two thirds of it. Summing the obvious parts (Gyroflow at
# 165 MB, Mesa Vulkan at 90 MB, the VA drivers at 18 MB, two of the three LLVM copies)
# only accounts for about half of that: the drivers drag in a long transitive tail.
# What is actually the API's own comes to 508 KB of compiled frontend.
#
# 138 MB of LLVM stays in the API image, pulled in by ffmpeg's own dependencies. It
# cannot be dropped without dropping ffmpeg, which the grading preview needs.
#
# So: `--target api` for the dispatcher, `--target worker` for the machines that do
# the work. One file rather than two, so the base layer is built once and shared in
# the registry and in the CI cache.
#
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
# 2. Base: what both images need
# ---------------------------------------------------------------------------
# ubuntu:25.04 rather than python:slim: it ships a recent Mesa (rusticl for OpenCL,
# which Gyroflow uses for warping) and it is the distribution the Gyroflow binary is
# built against. The API does not need Mesa, but sharing the base with the worker is
# worth more than the few megabytes a different base would save.
FROM ubuntu:25.04 AS base

ENV DEBIAN_FRONTEND=noninteractive

# ffmpeg is in the base and not in the worker alone: the API runs it too, for the
# grading preview (one filtered frame in 0.32 s, which is why the preview is a real
# ffmpeg frame and not a shader reimplementation) and for the clip analysis.
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates \
      python3 python3-venv python3-pip \
      ffmpeg \
    && rm -rf /var/lib/apt/lists/*

ENV VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH"
RUN python3 -m venv "$VIRTUAL_ENV"

COPY backend/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt && rm /tmp/requirements.txt

WORKDIR /app
COPY backend/app /app/app
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

ENV XDG_RUNTIME_DIR=/tmp/runtime \
    HOME=/tmp \
    PYTHONUNBUFFERED=1 \
    PF_DATA_DIR=/data

VOLUME ["/data"]
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]

# ---------------------------------------------------------------------------
# 3. API: the dispatcher, and the only one that serves the frontend
# ---------------------------------------------------------------------------
# It owns the database and hands jobs out over HTTP. It never decodes a rush, never
# warps a frame and never merges a file, so it carries no OpenCL, no Vulkan, no VA
# driver and no Gyroflow. What hardware there is belongs to the workers, and they
# report it themselves.
FROM base AS api

COPY --from=frontend /build/dist /app/static
ENV PF_STATIC_DIR=/app/static
EXPOSE 8000
CMD ["api"]

# ---------------------------------------------------------------------------
# 4. Worker: everything that touches pixels
# ---------------------------------------------------------------------------
# One worker image for every machine, whatever its hardware, because the choice is
# made by probing at startup and not by the image. A NAS VM with no GPU and a desktop
# with an RTX 3090 run the same bytes.
FROM base AS worker

ARG GYROFLOW_VERSION=1.6.3
ARG MP4_MERGE_VERSION=0.1.11

RUN apt-get update && apt-get install -y --no-install-recommends \
      curl \
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

# The rush the startup benchmark runs on, and it has to be a real one: a render needs
# a gyro track, and nothing can synthesize or trim one. ffmpeg refuses to mux `djmd`
# at all (codec `none`), and copying it into a MOV carries the bytes but loses the
# track identity, after which Gyroflow reads no gyro whatsoever.
#
# 0.5 s of DJI O3 footage, 9 MB. Measured against a 1.8 s clip weighing 25 MB: the
# short one reports the render at 27.2 then 26.8 img/s across two runs where the long
# one reports 38.8 then 36.4, so it is both lighter and *more* repeatable. The whole
# benchmark then costs 3.6 s, which is why nothing caches it.
COPY docker/bench/clip.mp4 /opt/bench/clip.mp4

ENV QT_QPA_PLATFORM=offscreen \
    RUSTICL_ENABLE=radeonsi

CMD ["worker"]
