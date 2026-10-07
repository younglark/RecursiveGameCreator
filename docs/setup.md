# Environment setup

For the standard installation, follow [Installation](../README.md#installation). This reference covers manual image builds, authentication diagnostics, and setup troubleshooting. Run commands from the repository root with the project environment activated.

## System prerequisites

Use macOS or Linux with Git, Node.js 22/npm, and a local Docker daemon with Compose and Buildx. Start Docker before running the full setup script. On macOS, `xcode-select --install` provides the Git command-line prerequisites if needed. On Linux, configure Docker for your normal user. Docker Desktop must allow bind mounts from the checkout directory.

```sh
git --version
node --version
npm --version
docker info
docker compose version
docker buildx version
```

The runtime uses Linux containers, virtual displays, and software rendering; no host GPU is required. Native Windows and automatic synchronization to a remote Docker host are not supported.

## Installation modes

| Command | Installs and checks |
| --- | --- |
| `sh scripts/setup.sh` | Python environment, Harbor, Codex, both Docker images, engine/browser checks, and offline mock smoke test |
| `sh scripts/setup.sh --minimal` | Python environment and offline mock smoke test |
| `sh scripts/setup.sh --skip-images` | All host tools and offline mock smoke test; Docker images built separately |

The script uses uv 0.11.11, Python 3.12, Harbor 0.13.0, and Codex CLI 0.153.4. Project packages are locked by `uv.lock`; Harbor has its own isolated tool environment. Tools are installed in `.tools/` and exposed through `.venv/bin/`. Setup preserves global tool installations and shell profiles. Python downloads and package caches use the tools' standard user cache locations. `curl` is needed if a matching uv binary is unavailable.

Activate `.venv` in each terminal: `source .venv/bin/activate` for zsh/bash, or `source .venv/bin/activate.fish` for fish. After moving the checkout, rerun setup to recreate machine-specific environments and tool links. Use `sh scripts/setup.sh --help` to inspect the modes without installing anything.

## Authentication

```sh
codex login -c 'cli_auth_credentials_store="file"'
codex login status
python -c 'from pathlib import Path; assert (Path.home()/".codex/auth.json").is_file(), "Authentication file is missing"; print("Authentication file found")'
```

The adapter uses file-backed authentication at `~/.codex/auth.json`. Set `auth_file` in a local configuration for another location. Keep credentials outside version control. Hunyuan credentials and benchmark-judge credentials are configured separately.

The default model is `gpt-6-astra` with `high` reasoning. Choose a model available to your account in the configuration before running generation. Model, asset, and external judge services use the accounts you configure and may incur charges; setup and mock tests do not invoke them.

## Docker images

The default `sh scripts/setup.sh` already performs this step. If you used `--skip-images`, or want to rebuild manually, build the runtime **first**, then the role image. The role image copies the same Godot binary from that runtime, so Builder does not need to download an engine in each stage.

```sh
docker build -f docker/runtime.Dockerfile \
  -t rsi-gamecreator/runtime:full-v1 docker

docker build -t game-agent-lite/codex:0.153.4-godot-v1 docker
```

| Image | Contents |
| --- | --- |
| `rsi-gamecreator/runtime:full-v1` | Python 3.12, Godot 4.7.1, Blender 4.3.2, Chromium/Playwright, FFmpeg, virtual display, software rendering, fonts |
| `game-agent-lite/codex:0.153.4-godot-v1` | Codex CLI 0.153.4, Node 22, Python, browser self-test tools, matching Godot, FFmpeg and virtual display |

These tags refer to **locally built images**, not an assumed public image registry. The first build downloads dependencies and can take time; subsequent runs reuse the images. Build contexts and versions are recorded in the Dockerfiles. Runtime containers remain isolated per execution.

Check the installed engine and browser tools without invoking a model:

```sh
docker run --rm --network none --entrypoint godot \
  rsi-gamecreator/runtime:full-v1 --version

docker run --rm --network none --entrypoint godot \
  game-agent-lite/codex:0.153.4-godot-v1 --version

docker run --rm --network none --entrypoint python3 \
  game-agent-lite/codex:0.153.4-godot-v1 /opt/browser-tools/browser-smoke.py
```

For a benchmark that uses another runtime, build a matching role image with `--build-arg RUNTIME_IMAGE=<locally-built-runtime-tag>` and set its tag as `harbor_image`. See [benchmark setup](benchmarks.md).

## Troubleshooting

| Symptom | Action |
| --- | --- |
| `uv`, `harbor` or `codex` not found | Activate `.venv` in the current shell; run full setup or `--skip-images` if you only ran `--minimal` |
| Docker connection failure | Start Docker and verify `docker info` as the same normal user |
| Missing local image | Build the runtime, then the role image, using the exact configured tags |
| Login exists but authentication file is missing | Use file-backed login or explicitly configure `auth_file`; do not print the file |
| Asset credential error | Configure `.env.hunyuan`, or set the corresponding asset counts to `0` |
| Existing run directory | Use `resume`, or choose a new `--run-dir` |
| Rendering slows with concurrency | Reduce game/episode concurrency; review CPU and memory pressure |
| Relative path stops resolving | Check the config/run base directory and whether an external dependency moved separately |
| Asset submission state is uncertain | Reconcile the existing provider job; do not automatically submit another paid job |
