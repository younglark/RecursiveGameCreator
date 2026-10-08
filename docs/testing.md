# Testing

Run these commands from the repository root after [environment setup](setup.md).

## Offline checks

```sh
.venv/bin/uv sync --python 3.12 --extra test --locked
.venv/bin/pytest
.venv/bin/uv lock --check
.venv/bin/uv build
```

The default suite uses fixtures and mocked model responses. It covers the four-role workflow, saved-run recovery, evidence validation, benchmark scheduling, configuration paths, and game exports. It does not call paid model, asset, or judge services. Tests requiring unavailable external tools are skipped.

GitHub Actions runs the offline suite on Linux and macOS, then checks CLI execution, moving and resuming a mock run, standalone game export, and installation from the built wheel.

## Runtime integration checks

Build the runtime image with the setup script, then opt in to Docker tests:

```sh
env GAME_AGENT_RUNTIME_IMAGE=rsi-gamecreator/runtime:full-v1 \
  .venv/bin/pytest tests/test_runtime_integration.py tests/test_runtime_full_integration.py -ra
```

These tests run fixed HTML and Godot fixtures to exercise real input, recording, isolation, and recovery. They require a running Docker daemon and are separate from model-backed benchmark evaluation. See [runtime setup](runtime.md) for image dependencies and [benchmark setup](benchmarks.md) for benchmark commands.

To check an installed Harbor environment, set `GAME_AGENT_HARBOR_PYTHON` to its Python interpreter and run `tests/test_harbor_compatibility.py`. Transport is mocked in this compatibility test.
