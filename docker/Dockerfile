ARG RUNTIME_IMAGE=rsi-gamecreator/runtime:full-v1
FROM ${RUNTIME_IMAGE} AS game_tools
FROM node:22-bookworm-slim
ARG CODEX_VERSION=0.153.4
ARG PLAYWRIGHT_VERSION=1.62.0
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/playwright \
    NODE_PATH=/opt/browser-tools/node_modules \
    PATH=/opt/browser-tools/venv/bin:/opt/browser-tools/node_modules/.bin:$PATH
RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-venv git ripgrep ca-certificates xvfb ffmpeg \
    libgl1-mesa-dri libglx-mesa0 libx11-6 libxcursor1 libxinerama1 libxrandr2 \
    libxi6 libasound2 libpulse0 libudev1 libfontconfig1 fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/* \
    && npm install -g @openai/codex@${CODEX_VERSION} \
    && python3 -m venv /opt/browser-tools/venv \
    && /opt/browser-tools/venv/bin/pip install --no-cache-dir playwright==${PLAYWRIGHT_VERSION} Pillow==11.3.0 \
    && npm install --prefix /opt/browser-tools --save-exact playwright@${PLAYWRIGHT_VERSION} \
    && /opt/browser-tools/venv/bin/playwright install --with-deps chromium \
    && chmod -R a+rX /opt/browser-tools /opt/playwright \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1001 runner \
    && mkdir -p /workspace/project /workspace/io \
    && chown -R runner:runner /workspace
COPY --from=game_tools /usr/local/bin/godot /usr/local/bin/godot
RUN godot --version
WORKDIR /workspace/project
USER runner
COPY browser-smoke.py /opt/browser-tools/browser-smoke.py
# Run with --network none after building to check HTML, WebGL and real dragging.
CMD ["sleep", "infinity"]
