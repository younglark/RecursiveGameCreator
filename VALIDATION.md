# Release preparation validation

Validated on 2026-10-08 on macOS/Apple Silicon with Python 3.12.13 and a local Docker Linux/aarch64 daemon. These are development validation results, not new paper benchmark scores.

## Results

| Check | Result |
| --- | --- |
| Original copied source, before changes | 574 passed, 25 opt-in tests skipped |
| Offline suite with efficiency changes | 582 passed, 25 opt-in tests skipped (39.11 s) |
| Final batch-path, scheduling, handoff, retention and relocation regression subset | 61 passed (10.28 s) |
| Real Docker HTML integration | 2 passed (19.13 s): parallel input/storage/video/media and fixed-policy batch/resume |
| Real Docker Godot integration | 2 passed (32.44 s): fixed-policy batch/resume and 3D Compatibility rendering |
| Installed Harbor parsing/discovery and image-preserving cleanup | 1 passed (1.39 s); model and Docker transport mocked |
| New role Docker image build | Completed successfully |
| New role-image Godot check | 4.7.1.stable.official.a13da4feb |
| New role-image browser smoke | HTML, WebGL, pointer drag and Pillow passed with network disabled |
| Python packaging | Locked dependency check, wheel and source distribution build passed |
| Fresh wheel installation outside the checkout | CLI, packaged prompts/bridges, two-round mock, relocated resume and standalone export passed |
| Documentation links | Local file targets resolved |
| Original project preservation | All 106 originally copied files matched their pre-edit SHA-256 hashes |

The new role image is `game-agent-lite/codex:0.153.4-godot-v1`. The live runtime fixtures used the existing `rsi-gamecreator/runtime:full-v1` image, image ID `sha256:c2636cf9b4a25ad99c7bd66c1b7585fbab916bfd486f7b268bff57b493182ea6`. The role image was rebuilt during this preparation; the base runtime was reused.

## New regression coverage

`tests/test_efficiency.py` covers:

- Moving a checkout/run, deleting the old location, and resuming from relative configuration.
- Independent writable copies: changing a copied asset cannot change its source.
- Excluding reserved validation output while retaining actual test source.
- Passing Builder checks, changed-file hashes and unfinished work into integration.
- Concurrent anonymous reviews with separate A/B views and unchanged tie behavior.
- Keeping a completed comparison when its sibling fails; retrying only missing work.
- Respecting host-wide game capacity and releasing it for waiting work.
- Cleaning disposable successful Player workspaces while preserving valid evidence and recovery.
- Exporting a self-contained checked game independently of its run directory.

## Reproduce

```sh
uv sync --python 3.12 --extra test --locked
.venv/bin/pytest
.venv/bin/pytest tests/test_gamecraft_batch.py tests/test_gameasg_batch.py tests/test_efficiency.py -ra
uv lock --check
uv build
```

After building the runtime image, the four selected live fixtures can be run with:

```sh
env GAME_AGENT_RUNTIME_IMAGE=rsi-gamecreator/runtime:full-v1 \
  .venv/bin/pytest -ra \
  tests/test_runtime_full_integration.py::test_html_three_parallel_inputs_storage_video_and_media \
  tests/test_runtime_full_integration.py::test_html_policy_batch_resume_without_host_browser_or_ffmpeg \
  tests/test_runtime_integration.py::test_full_fixed_policy_batch_and_completed_episode_resume \
  tests/test_runtime_integration.py::test_basic_3d_compatibility_rendering
```

The role-image checks are in the README. Harbor compatibility is opt-in through `GAME_AGENT_HARBOR_PYTHON`, pointing to the interpreter where Harbor is installed. The GitHub Actions workflow defines Linux/macOS offline and packaging checks; a remote Actions run was not part of this local validation.

## Setup-script update

The environment setup is now available as `sh scripts/setup.sh`, with `--minimal` and `--skip-images` modes. Shell syntax validation passed. Six installer regression tests passed: full/minimal/host-only orchestration, reruns, paths containing spaces, missing prerequisite failures before installation, and side-effect-free help/invalid options. External downloads and Docker commands are replaced with test tools in those six cases.

The actual `--minimal` script was also run successfully from outside the repository, installing the locked environment and completing the offline mock smoke check. The actual `--skip-images` script also passed: it installed project-local Harbor 0.13.0 and Codex CLI 0.153.4 and completed the same smoke check. Existing Docker images were not rebuilt during this script update; image builds and runtime checks were validated in the earlier preparation. These installer checks supplement the earlier framework validation above.

## Limits

No paid end-to-end model generation, asset-provider calls, or full benchmark reevaluation was performed for this optimization. Fixed-policy runtime tests verify execution and evidence handling, not generated-game quality. The offline suite's skipped integration tests must not be counted as passed. Numerical end-to-end speedups and storage savings require a controlled comparison on representative runs and the target filesystem; none are asserted here.

Source code uses the MIT License; paper figures and other non-code assets are outside that code license unless separately stated. Repository publication does not imply a new model-backed benchmark or performance measurement.
