# Benchmark setup

Benchmark evaluation is optional. Finish the environment, authentication and base image setup in the [README](../README.md) first. Benchmark repositories and evaluator credentials are external dependencies; they are not redistributed here.

The batch adapters record the framework Git revision and file hashes. Run them from a Git checkout with at least one commit. If you extracted a source archive, initialize and commit that source tree first; ordinary generation and `export-game` do not require Git history.

## GameCraft-Bench

Clone the benchmark into a relative directory:

```sh
git clone https://github.com/FreedomIntelligence/gamecraft-bench.git benchmarks/gamecraft-bench
git -C benchmarks/gamecraft-bench checkout a43347534374df9a0c1a6c001aa9380862783f6d
```

Build the Godot 4.6.2 evaluator/runtime and a role image with the matching engine:

```sh
docker build -t rsi-gamecreator/gamecraft-eval:godot-4.6.2-v1 docker/gamecraft

docker build --build-arg RUNTIME_IMAGE=rsi-gamecreator/gamecraft-eval:godot-4.6.2-v1 \
  -t game-agent-lite/codex:0.153.4-gamecraft-v1 docker
```

Copy `examples/godot-docker.json` to `examples/gamecraft.local.json`. Set `harbor_image` to `game-agent-lite/codex:0.153.4-gamecraft-v1` and `runtime.image` to `rsi-gamecreator/gamecraft-eval:godot-4.6.2-v1`. Configure the asset service, or explicitly disable its requests in this local config.

Prepare a three-round batch for one public task. Preparation freezes requirements and config; it does not run a model:

```sh
.venv/bin/python scripts/run_gamecraft_batch.py prepare \
  --bench benchmarks/gamecraft-bench \
  --config examples/gamecraft.local.json \
  --task racing-drift-circuit \
  --batch-dir runs/gamecraft/example

.venv/bin/python scripts/run_gamecraft_batch.py run runs/gamecraft/example --dry-run
.venv/bin/python scripts/run_gamecraft_batch.py run runs/gamecraft/example --jobs 1
.venv/bin/python scripts/run_gamecraft_batch.py status runs/gamecraft/example
```

To run the included 45-task set, replace `--task racing-drift-circuit` with `--task-file examples/gamecraft-tasks.json` and choose a new batch directory. A custom task file is a JSON list of distinct public task IDs. Task-file batches use native Player recordings and retain the model and Player settings from your config. Task order and evaluation settings are frozen when preparing the batch.

The generation batch and the external scorer are separate. To score native Player recordings, copy `docker/gamecraft/judge.env.example` to `.env.gamecraft`, fill it locally, and use the explicit `--score --judge-env-file .env.gamecraft` options at batch preparation. Inspect [scoring requirements](gamecraft-scoring.md) before doing so; scoring calls an external judge.

Evidence modes are explicit: `official_replay` exports input events and invokes the upstream replayer; `player_recording` uses actual Player captures. They are different evidence-collection protocols and must be labeled accordingly. Efficiency changes do not switch an existing run between these modes.

For a single configured GameCraft run or a saved candidate, see [export and replay](gamecraft-export.md). Never interpret successful source export as an independently verified game win or benchmark score.

## GameASG-Bench

```sh
git clone https://github.com/areal-project/GameASG-Bench.git benchmarks/GameASG-Bench
git -C benchmarks/GameASG-Bench checkout 6427cb6517ce11ff350ec45fcae17b67c5aec245
```

Follow that checkout's own environment/image setup. Our adapter checks the pinned benchmark revision and uses the benchmark's evaluator; its Docker image is not bundled with this framework.

Discover command options without starting work:

```sh
.venv/bin/python scripts/run_gameasg_batch.py prepare --help
.venv/bin/python scripts/run_gameasg_batch.py run --help
```

Select public task IDs from the benchmark and provide a local config:

```sh
.venv/bin/python scripts/run_gameasg_batch.py prepare \
  --bench benchmarks/GameASG-Bench \
  --config examples/html-docker.json \
  --tasks astroman \
  --env-file .env.hunyuan \
  --dry-run
```

Use `--all-tasks` instead of `--tasks astroman` to select all 47 public tasks. Remove `--dry-run` after reviewing the reported configuration. The command prints the generated batch path; pass that exact path to `run`, `status` or `stop`. This GameASG adapter uses one generation round, unlike the GameCraft three-round adapter. It currently requires an asset environment file during preparation; basic nonbenchmark quickstarts do not.

## Reproducibility

Keep benchmark revisions, runtime image IDs, task inputs, capture modes, judge settings and generated version/evidence identities with results. Resume rejects changed frozen settings. Select tasks explicitly or use a task list. Keep the selected task IDs with reported results.

Batch manifests use format v2. Prepare a new batch when switching from an earlier adapter; existing v1 manifests are not rewritten or resumed with the new format.

Model calls, asset generation and external judging may incur costs. The offline test suite does not invoke them.
