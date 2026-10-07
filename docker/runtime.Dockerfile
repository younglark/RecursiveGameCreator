# Shared environment only. The controller mounts the matching framework read-only.
FROM python:3.12-slim-bookworm AS godot
ARG GODOT_VERSION=4.7.1
ARG TARGETARCH
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl unzip xvfb ffmpeg libgl1-mesa-dri libglx-mesa0 \
    libx11-6 libxcursor1 libxinerama1 libxrandr2 libxi6 libasound2 libpulse0 libudev1 libfontconfig1 \
    && rm -rf /var/lib/apt/lists/*
RUN set -eu; \
    case "$TARGETARCH" in arm64) GODOT_ARCH=arm64 ;; amd64) GODOT_ARCH=x86_64 ;; *) exit 1 ;; esac; \
    GODOT_FILE="Godot_v${GODOT_VERSION}-stable_linux.${GODOT_ARCH}.zip"; \
    GODOT_URL="https://github.com/godotengine/godot-builds/releases/download/${GODOT_VERSION}-stable"; \
    curl -fsSL --retry 3 "$GODOT_URL/$GODOT_FILE" -o /tmp/godot.zip; \
    curl -fsSL --retry 3 "$GODOT_URL/SHA512-SUMS.txt" -o /tmp/godot-checksums; \
    GODOT_SUM=$(awk -v name="$GODOT_FILE" '$2 == name || $2 == "*"name {print $1}' /tmp/godot-checksums); \
    test -n "$GODOT_SUM"; \
    printf '%s  /tmp/godot.zip\n' "$GODOT_SUM" | sha512sum -c -; \
    unzip -q /tmp/godot.zip -d /tmp/godot; \
    install -m 755 "/tmp/godot/Godot_v${GODOT_VERSION}-stable_linux.${GODOT_ARCH}" /usr/local/bin/godot; \
    rm -rf /tmp/godot /tmp/godot.zip /tmp/godot-checksums

FROM node:22-bookworm-slim AS node

# Debian 13 supplies Blender 4.3.2 for both arm64 and amd64.
FROM python:3.12-slim-trixie
ARG BLENDER_VERSION=4.3.2+dfsg-2
ARG PLAYWRIGHT_VERSION=1.62.0
COPY --from=godot /usr/local/bin/godot /usr/local/bin/godot
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx
RUN apt-get update && apt-get install -y --no-install-recommends \
    blender=${BLENDER_VERSION} ca-certificates xvfb ffmpeg \
    libgl1-mesa-dri libglx-mesa0 libx11-6 libxcursor1 libxinerama1 libxrandr2 \
    libxi6 libasound2t64 libpulse0 libudev1 libfontconfig1 fonts-noto-cjk fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/playwright
RUN pip install --no-cache-dir jsonschema==4.26.0 playwright==${PLAYWRIGHT_VERSION} \
    && playwright install --with-deps chromium \
    && chmod -R a+rX /opt/playwright \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /output /source /framework
# Blender uses Debian's embedded Python, not /usr/local Python 3.12.
RUN apt-get update && apt-get install -y --no-install-recommends python3-numpy python3-pil \
    && rm -rf /var/lib/apt/lists/* \
    && blender --background --factory-startup --python-exit-code 1 --python-expr \
       "import bpy, numpy; bpy.ops.export_scene.gltf(filepath='/tmp/runtime-smoke.glb', export_format='GLB')" \
    && rm /tmp/runtime-smoke.glb
LABEL org.opencontainers.image.title="RSI-GameCreator HTML/Godot/Blender runtime" \
      org.opencontainers.image.version="full-v1"
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 LIBGL_ALWAYS_SOFTWARE=1
USER 1000:1000
WORKDIR /output
ENTRYPOINT ["python3", "-u", "-m", "game_agent_lite.runtime_worker"]
