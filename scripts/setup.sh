#!/bin/sh
# Run from any directory: sh /path/to/this/repository/scripts/setup.sh
set -eu

usage() {
    cat <<'EOF'
Usage: sh scripts/setup.sh [--minimal | --skip-images]

Default: install the Python environment, Harbor and Codex, build both Docker
images, and check the tools. Git, Node.js 22/npm and a running local Docker
daemon (with Compose and Buildx) are prerequisites.

  --minimal      Python dependencies and offline mock smoke test only.
  --skip-images  Install all host tools; leave image building for later.
  -h, --help     Show this help without installing anything.

Tools are installed under this checkout's .tools/ and .venv/, without sudo
or shell-profile edits. Login and optional asset credentials are configured
separately; setup never starts a paid model or asset-generation job.
EOF
}

fail() { printf '\nSetup error: %s\n' "$*" >&2; exit 1; }
note() { printf '\n==> %s\n' "$*"; }
require() { command -v "$1" >/dev/null 2>&1 || fail "Missing $1. $2"; }

RGC_MODE=full
case $# in
    0) ;;
    1)
        case $1 in
            --minimal) RGC_MODE=minimal ;;
            --skip-images) RGC_MODE=host ;;
            -h|--help) usage; exit 0 ;;
            *) usage >&2; fail "Unknown option: $1" ;;
        esac ;;
    *) usage >&2; fail 'Choose at most one setup mode.' ;;
esac

RGC_ROOT=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd -P)
cd "$RGC_ROOT"
[ -f pyproject.toml ] && [ -f uv.lock ] || fail 'Run this script from a complete source checkout/archive.'
case $(uname -s) in Darwin|Linux) ;; *) fail 'Supported hosts are macOS and Linux.' ;; esac

# Fail before changing the environment when required system tools are missing.
if [ "$RGC_MODE" != minimal ]; then
    require git 'Install Git before full setup.'
    require node 'Install Node.js 22 (including npm) before full setup.'
    require npm 'Install Node.js 22 (including npm) before full setup.'
    git --version >/dev/null
    RGC_NODE_MAJOR=$(node -p 'process.versions.node.split(".")[0]')
    [ "$RGC_NODE_MAJOR" = 22 ] || fail "Node.js 22 is required; found major version $RGC_NODE_MAJOR."
fi
if [ "$RGC_MODE" = full ]; then
    require docker 'Install and start Docker, or use --skip-images for host tools only.'
    docker info >/dev/null 2>&1 || fail 'Docker is not ready. Start the local daemon and check docker info.'
    docker compose version >/dev/null 2>&1 || fail 'Docker Compose is missing.'
    docker buildx version >/dev/null 2>&1 || fail 'Docker Buildx is missing.'
fi

RGC_UV_VERSION=0.11.11
RGC_HARBOR_VERSION=0.13.0
RGC_CODEX_VERSION=0.153.4
RGC_UV="$RGC_ROOT/.tools/uv/uv"
RGC_TEMP=
cleanup() { if [ -n "$RGC_TEMP" ]; then rm -rf -- "$RGC_TEMP"; fi; }
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

note "Preparing uv $RGC_UV_VERSION"
mkdir -p "$RGC_ROOT/.tools/uv"
if [ ! -x "$RGC_UV" ] || [ "$("$RGC_UV" --version | cut -d ' ' -f 2)" != "$RGC_UV_VERSION" ]; then
    # Reuse an existing matching binary without changing its installation.
    RGC_EXISTING_UV=$(command -v uv || true)
    if [ -n "$RGC_EXISTING_UV" ] && [ "$("$RGC_EXISTING_UV" --version | cut -d ' ' -f 2)" = "$RGC_UV_VERSION" ]; then
        cp "$RGC_EXISTING_UV" "$RGC_UV"
    else
        require curl 'Install curl to download the pinned uv installer.'
        RGC_TEMP=$(mktemp -d "${TMPDIR:-/tmp}/rgc-setup.XXXXXX")
        curl --fail --location --silent --show-error \
            "https://astral.sh/uv/$RGC_UV_VERSION/install.sh" -o "$RGC_TEMP/install-uv.sh"
        UV_INSTALL_DIR="$RGC_ROOT/.tools/uv" UV_NO_MODIFY_PATH=1 sh "$RGC_TEMP/install-uv.sh"
        cleanup
        RGC_TEMP=
    fi
fi
"$RGC_UV" --version

note 'Installing Python 3.12 and locked project dependencies'
"$RGC_UV" python install 3.12
UV_PROJECT_ENVIRONMENT="$RGC_ROOT/.venv" "$RGC_UV" sync --python 3.12 --extra test --locked
# Activation makes these project-local tools available to the controller.
ln -sf ../../.tools/uv/uv "$RGC_ROOT/.venv/bin/uv"
PATH="$RGC_ROOT/.venv/bin:$PATH"
export PATH

if [ "$RGC_MODE" != minimal ]; then
    note "Installing isolated Harbor $RGC_HARBOR_VERSION"
    UV_TOOL_DIR="$RGC_ROOT/.tools/uv-tools" UV_TOOL_BIN_DIR="$RGC_ROOT/.tools/bin" \
        "$RGC_UV" tool install --python 3.12 --force "harbor==$RGC_HARBOR_VERSION"
    ln -sf ../../.tools/bin/harbor "$RGC_ROOT/.venv/bin/harbor"

    note "Installing project-local Codex CLI $RGC_CODEX_VERSION"
    mkdir -p "$RGC_ROOT/.tools/codex"
    npm install --prefix "$RGC_ROOT/.tools/codex" --no-save --package-lock=false \
        --no-audit --no-fund "@openai/codex@$RGC_CODEX_VERSION"
    ln -sf ../../.tools/codex/node_modules/.bin/codex "$RGC_ROOT/.venv/bin/codex"
    "$RGC_ROOT/.venv/bin/harbor" --version
    "$RGC_ROOT/.venv/bin/codex" --version
fi

if [ "$RGC_MODE" = full ]; then
    note 'Building the game runtime (the first build downloads large dependencies)'
    docker build -f docker/runtime.Dockerfile -t rsi-gamecreator/runtime:full-v1 docker
    note 'Building the role image with the matching Godot engine'
    docker build -t game-agent-lite/codex:0.153.4-godot-v1 docker
    note 'Checking the isolated engine and browser tools'
    docker run --rm --network none --entrypoint godot rsi-gamecreator/runtime:full-v1 --version
    docker run --rm --network none --entrypoint godot game-agent-lite/codex:0.153.4-godot-v1 --version
    docker run --rm --network none --entrypoint python3 game-agent-lite/codex:0.153.4-godot-v1 \
        /opt/browser-tools/browser-smoke.py
fi

note 'Running an offline controller smoke test (no model/API calls)'
mkdir -p "$RGC_ROOT/.cache"
RGC_TEMP=$(mktemp -d "$RGC_ROOT/.cache/setup-smoke.XXXXXX")
"$RGC_ROOT/.venv/bin/game-agent" --help >/dev/null
if ! "$RGC_ROOT/.venv/bin/game-agent" start examples/mock.json --run-dir "$RGC_TEMP/run" >"$RGC_TEMP/smoke.log" 2>&1; then
    cat "$RGC_TEMP/smoke.log" >&2
    fail 'Offline smoke test failed; see output above.'
fi
cleanup
RGC_TEMP=

note 'Setup complete. Activate the environment in each new terminal:'
printf '  zsh/bash: source .venv/bin/activate\n  fish:     source .venv/bin/activate.fish\n'
case $RGC_MODE in
    minimal) printf '\nPython/mock setup is ready. Run sh scripts/setup.sh for real generation.\n' ;;
    host) printf '\nHost tools are ready; Docker images were not built. See docs/setup.md (Docker images).\n' ;;
esac
if [ "$RGC_MODE" != minimal ]; then
    printf '\nNext, follow Authentication and Quick Start in README.md.\n'
fi
