# GameCraft Player recording/replay and scoring; upstream source mounted read-only.
ARG BASE_IMAGE=python:3.12-slim-bookworm
FROM ${BASE_IMAGE}
USER root
ARG GODOT_VERSION=4.6.2
ARG TARGETARCH
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl unzip xvfb xdotool ffmpeg x11-utils x11-xserver-utils \
    libgl1-mesa-dri libglx-mesa0 libegl1 libxcursor1 libxinerama1 libxrandr2 \
    libxi6 libasound2 libpulse0 libudev1 libfontconfig1 fonts-dejavu-core fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*
RUN set -eu; \
    case "$TARGETARCH" in arm64) arch=arm64 ;; amd64) arch=x86_64 ;; *) exit 1 ;; esac; \
    file="Godot_v${GODOT_VERSION}-stable_linux.${arch}.zip"; \
    url="https://github.com/godotengine/godot-builds/releases/download/${GODOT_VERSION}-stable"; \
    curl -fsSL --connect-timeout 20 --max-time 300 --retry 2 "$url/$file" -o /tmp/godot.zip; \
    curl -fsSL --connect-timeout 20 --max-time 120 --retry 2 "$url/SHA512-SUMS.txt" -o /tmp/sums; \
    sum=$(awk -v name="$file" '$2 == name || $2 == "*"name {print $1}' /tmp/sums); \
    test -n "$sum"; printf '%s  /tmp/godot.zip\n' "$sum" | sha512sum -c -; \
    unzip -q /tmp/godot.zip -d /tmp/godot; \
    install -m 755 "/tmp/godot/Godot_v${GODOT_VERSION}-stable_linux.${arch}" /usr/local/bin/godot; \
    rm -rf /tmp/godot /tmp/godot.zip /tmp/sums
COPY requirements.txt /opt/evaluator/requirements.txt
RUN pip install --no-cache-dir -r /opt/evaluator/requirements.txt \
    && pip freeze > /opt/evaluator/installed-packages.txt
COPY entrypoint.py /opt/evaluator/entrypoint.py
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/opt/gamecraft-bench LIBGL_ALWAYS_SOFTWARE=1 \
    GODOT_SILENCE_ROOT_WARNING=1 GAMECRAFT_BENCH_GODOT_BIN=/usr/local/bin/godot
RUN mkdir -p /workspace
WORKDIR /workspace
ENTRYPOINT ["python", "/opt/evaluator/entrypoint.py"]
