"""Queue public GameCraft tasks through the existing CLI, exactly rounds 0/1/2.

prepare and run --dry-run never invoke a model, engine, provider or Docker.
This is an outer scheduler; generation, Player and judge aggregation stay unchanged.
"""
import argparse
import csv
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import re
import signal
import subprocess
import sys
import threading
import time
import uuid

from jsonschema import ValidationError

from game_agent_lite.paths import load_saved_config, portable_config
from game_agent_lite.agent import child_env
from game_agent_lite.cli import load_config
from game_agent_lite.store import Store, atomic_json, file_hash, read_json, redact, safe_path, version
from game_agent_lite.gamecraft_recordings import validate_recordings
from game_agent_lite.types import CONFIG_SCHEMA, validate
from game_agent_lite.usage import saved_reference

ROUNDS = 3
FORMAT = "gamecraft-batch-v1"
ROOT = Path(__file__).resolve().parents[1]
BENCH_COMMIT = "a43347534374df9a0c1a6c001aa9380862783f6d"
MEMBERS = ("cjj", "why", "jmd")
ASSIGNMENT = ROOT / "examples/gamecraft-45-assignment.json"
EVAL_IMAGE = "rsi-gamecreator/gamecraft-eval:godot-4.6.2-v1"
TASK_ID = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
ENVIRONMENT = (
    "[RSI execution environment; not part of the official task]\n"
    "Fully implement the public task's functionality, content and demonstration requirements.\n"
    "Use Builder's actual Game source workspace; /workspace/game is the official submission path.\n"
    "The official shared asset library and screenshot tools are not mounted. Use framework capabilities, "
    "Hunyuan asset generation and Blender modeling scripts.\n"
    "The prepared role image includes Godot for targeted Builder checks. Blender and formal gameplay/recording "
    "execute in the framework's runtime containers. Use the Compatibility renderer.\n"
    "Player records short demonstrations through code policies; the framework exports actual inputs and runs official offline replay. "
    "Each clip is at most 20 seconds; together they cover public requirements, without requiring a win in every clip. Retain coverage gaps honestly. "
    "These are demonstration constraints, not a requirement to shorten normal gameplay or change its rules. "
    "Official scoring runs separately.\n\n[Official task, original text]\n"
)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def git(path, *args):
    return subprocess.check_output(["git", "-C", str(path), *args], text=True).strip()


def code_identity():
    names = git(ROOT, "ls-files", "--cached", "--others", "--exclude-standard", "--",
                "src", "scripts", "docker", "pyproject.toml", "uv.lock").splitlines()
    return {"commit": git(ROOT, "rev-parse", "HEAD"),
            "files_sha256": digest({n: file_hash(ROOT / n) if (ROOT / n).is_file() else None
                                    for n in sorted(set(names))})}


def assignments(bench):
    groups = read_json(ASSIGNMENT)
    names = [name for group in groups.values() for name in group]
    if (set(groups) != set(MEMBERS) or any(len(g) != 15 for g in groups.values())
            or len(set(names)) != 45):
        raise ValueError("Fixed assignment must contain 15 distinct tasks per member, 45 total")
    discover(bench, names)
    return groups


def relative_config(config, directory):
    """Only serialize path references; load_config resolves them before execution."""
    return portable_config(config, directory)


def judge_profile(path):
    # Read only evaluation settings into metadata; never persist keys or env contents.
    settings = {}
    allowed = {"GAMECRAFT_BENCH_JUDGE", "GAMECRAFT_BENCH_JUDGE_MODEL",
               "GAMECRAFT_BENCH_JUDGE_OPENAI_BASE_URL", "OPENAI_BASE_URL"}
    for line in path.read_text().splitlines():
        key, sep, value = line.strip().partition("=")
        if sep and key in allowed:
            settings[key] = value.strip().strip("\"'")
    backend = settings.get("GAMECRAFT_BENCH_JUDGE", "openai")
    model = settings.get("GAMECRAFT_BENCH_JUDGE_MODEL")
    if backend != "openai" or not model:
        raise ValueError("Batch scoring requires an explicit OpenAI-compatible judge/model in its env file")
    return {"backend": backend, "model": model, "settings_sha256": digest(settings)}


def discover(bench, selected=()):
    tasks = {}
    for path in sorted((bench / "tasks").glob("*/instruction.md")):
        name = path.parent.name
        if name == "example":
            continue
        if not TASK_ID.fullmatch(name) or path.is_symlink() or path.parent.is_symlink():
            raise ValueError(f"Unsupported task path: {path}")
        tasks[name] = path
    if not tasks:
        raise ValueError("No public tasks/*/instruction.md found")
    if selected:
        if len(set(selected)) != len(selected) or set(selected) - tasks.keys():
            raise ValueError("--task must name distinct discovered tasks (example is excluded)")
        tasks = {name: tasks[name] for name in sorted(selected)}
    return tasks


def prepare(bench, template, batch, *, env_file=None, selected=(), mock=False,
            worker=None, score=False, judge_env_file=None, judge_max_tokens=4096):
    bench, batch = bench.resolve(), batch.resolve()
    scoring = None
    if score:
        if mock or judge_max_tokens <= 0:
            raise ValueError("Scoring requires a live batch and a positive judge budget")
        judge_env_file = (judge_env_file or ROOT / ".env.gamecraft").resolve()
        scoring = {"evidence_source": "player_recording", "judge_max_tokens": judge_max_tokens,
                   "env_file": os.path.relpath(judge_env_file, batch), "judge": judge_profile(judge_env_file)}
    shared = None
    if worker is not None:
        if worker not in MEMBERS or selected:
            raise ValueError("Choose one --worker or --task, not both")
        groups = assignments(bench)
        selected = groups[worker]
        if not mock and (git(bench, "rev-parse", "HEAD") != BENCH_COMMIT or
                         git(bench, "status", "--porcelain", "--untracked-files=no")):
            raise ValueError(f"Team batches require clean GameCraft commit {BENCH_COMMIT}")
        shared = {"members": groups, "benchmark_commit": BENCH_COMMIT,
                  "code": code_identity(), "template_sha256": file_hash(template), "rounds": ROUNDS,
                  "model": "gpt-6-astra", "reasoning": "high", "episodes": 3,
                  "parallel_episodes": 1, "render_threads": 4, "evidence_source": "player_recording",
                  "score": score, "judge_max_tokens": judge_max_tokens if score else None,
                  "instruction_sha256": {name: file_hash(path) for name, path in
                      discover(bench, [n for group in groups.values() for n in group]).items()}}
        shared["judge"] = scoring["judge"] if score else None
    tasks = discover(bench, selected)
    if worker:
        tasks = {name: tasks[name] for name in selected}  # Preserve the supplied team order.
    config = load_config(template)
    if config["engine"] != "godot" or config["source"] is not None:
        raise ValueError("GameCraft requires a Godot template without a seed project")
    if env_file is not None:
        config["assets"]["env_file"] = str(env_file.expanduser().resolve())
    config["rounds"] = ROUNDS
    config["player"]["mode"] = "policy"
    if worker:
        config.update(model="gpt-6-astra", reasoning="high")
        config["player"].update(trajectories_per_version=3, parallel_episodes=1)
        config["runtime"]["render_threads"] = 4
    if mock:
        config["backend"] = "mock"
    if config["backend"] == "mock":
        if score:
            raise ValueError("Mock evidence cannot be scored")
        # Exercise controller rounds offline; no policy containers or real games.
        config["player"]["mode"] = "gui"
        config["assets"].update(max_images=0, max_models=0, max_blender=0)
    elif config["runtime"]["mode"] != "docker":
        raise ValueError("Use a Docker Godot template for live batches")
    prepared = []
    for name, path in tasks.items():
        instruction = path.read_bytes()
        native = worker is not None or score or (config.get("benchmark") or {}).get("evidence_source") == "player_recording"
        environment = ENVIRONMENT
        if native:
            environment = environment.replace("the framework exports actual inputs and runs official offline replay",
                "the framework retains actual inputs and native Player recordings without JSON replay")
        cfg = {**deepcopy(config), "brief": environment + instruction.decode("utf-8")}
        if not mock and config["backend"] != "mock":
            cfg["benchmark"] = {**(config.get("benchmark") or {}), "name": "gamecraft", "task": name, "path": str(bench)}
            if native:
                cfg["benchmark"].update(evidence_source="player_recording", image=EVAL_IMAGE)
        elif config["backend"] == "mock":
            cfg["benchmark"] = None
        if worker:
            cfg = relative_config(cfg, batch / "configs")
        validate(cfg, CONFIG_SCHEMA)
        prepared.append((name, instruction, cfg))
    batch.mkdir(parents=True, exist_ok=False)
    manifest = {"format": FORMAT, "rounds": ROUNDS, "created_at": time.time(),
                "benchmark": str(bench), "template": str(template.resolve()),
                "mode": "mock" if config["backend"] == "mock" else "live", "tasks": []}
    if worker:
        manifest.update(worker=worker, assignment_sha256=digest(shared),
                        benchmark=os.path.relpath(bench, batch), template=os.path.relpath(template.resolve(), batch))
        atomic_json(batch / "assignment.json", shared)
    if score:
        manifest["scoring"] = scoring
    for name, instruction, cfg in prepared:
        target = batch / "instructions" / f"{name}.md"
        target.parent.mkdir(exist_ok=True)
        target.write_bytes(instruction)
        destination = batch / "configs" / f"{name}.json"
        atomic_json(destination, cfg)
        manifest["tasks"].append({"id": name, "instruction_sha256": file_hash(target),
                                  "config_sha256": file_hash(destination)})
    atomic_json(batch / "batch.json", manifest)
    refresh(batch, manifest, persist=True)
    return manifest


def load_manifest(batch):
    manifest = read_json(batch / "batch.json")
    if (manifest.get("format") != FORMAT or manifest.get("rounds") != ROUNDS
            or manifest.get("mode") not in ("mock", "live")):
        raise ValueError("Unsupported batch manifest or round count")
    names = [item["id"] for item in manifest["tasks"]]
    if (not names or len(set(names)) != len(names)
            or any(not TASK_ID.fullmatch(n) or n == "example" for n in names)):
        raise ValueError("Invalid task inventory")
    if manifest.get("worker"):
        shared = read_json(batch / "assignment.json")
        if (digest(shared) != manifest.get("assignment_sha256") or
                names != shared["members"][manifest["worker"]]):
            raise ValueError("Frozen team assignment changed")
        scoring = manifest.get("scoring")
        if (bool(scoring) != shared["score"] or (scoring and
                (scoring["judge_max_tokens"] != shared["judge_max_tokens"] or scoring["judge"] != shared["judge"]))):
            raise ValueError("Frozen team scoring settings changed")
    return manifest


def paths(batch, task):
    name = task["id"]
    return (safe_path(batch, f"configs/{name}.json"),
            safe_path(batch, f"runs/{name}"), safe_path(batch, f"logs/{name}.log"))


def active(run):
    path = run / "run.lock"
    if not path.exists():
        return False
    with path.open("r") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(lock, fcntl.LOCK_UN)
    return False


def inspect_task(batch, task):
    config_path, run, log = paths(batch, task)
    row = {"task": task["id"], "run": str(run), "log": str(log), "status": "pending",
           "round": None, "phase": None, "error": None, "command": None,
           "usage": saved_reference(run)}
    try:
        if file_hash(config_path) != task["config_sha256"]:
            raise ValueError("Frozen task config changed; prepare a new batch")
        if file_hash(safe_path(batch, f"instructions/{task['id']}.md")) != task["instruction_sha256"]:
            raise ValueError("Frozen public instruction changed")
        config = load_config(config_path)
        if config["rounds"] != ROUNDS:
            raise ValueError("Every task must run exactly three rounds")
        if run.exists():
            if active(run):
                row["status"] = "active"
                return row
            if load_saved_config(run) != config:
                raise ValueError("Run config differs from its frozen batch config")
            state = read_json(run / "state.json")
            row.update(status=state["status"], round=state["round"], phase=state["phase"],
                       error=state.get("error"))
            if row["status"] == "completed":
                if state["round"] != ROUNDS or [h["round"] for h in state["history"]] != list(range(ROUNDS)):
                    raise ValueError("Completed state does not contain rounds 0, 1, 2")
                return row
            if row["status"] == "needs_reconciliation":
                return row
            if row["status"] not in ("ready", "failed", "stopped", "running"):
                raise ValueError("Unexpected run status")
            row["command"] = "resume"
        else:
            row["command"] = "start"
            result = batch / "results" / f"{task['id']}.json"
            if result.exists():
                row.update(status="failed", error=read_json(result).get("error"))
    except (OSError, ValueError, KeyError, TypeError, ValidationError) as exc:
        row.update(status="invalid", command=None, error=f"{type(exc).__name__}: {exc}")
    return redact(row)


def round_result(batch, manifest, task, number, generation_status):
    """Index the candidate of this round, including rejected or unscorable rounds."""
    _, run, _ = paths(batch, task)
    directory = run / f"rounds/{number:04d}"
    row = {"task": task["id"], "worker": manifest.get("worker"), "round": number,
           "generation_status": generation_status, "status": "pending", "source_version": None,
           "promoted": None, "project": None, "player_report": None, "recordings": None,
           "manifest_sha256": None, "evaluation": None, "recording_score": None,
           "official_score": None, "error": None,
           "round_artifacts": os.path.relpath(directory, batch)}
    try:
        if not (directory / "check.json").is_file():
            if generation_status == "completed":
                raise ValueError("Completed round is missing check.json")
            return row
        check = read_json(directory / "check.json")
        row.update(source_version=check["version"],
                   project=os.path.relpath(safe_path(run, check["project"]), batch))
        if (directory / "select.json").is_file():
            row["promoted"] = read_json(directory / "select.json")["promoted"]
        if not check["passed"]:
            row.update(status="build_failed", error="Candidate did not pass build checks")
            return row
        if not (directory / "play.json").is_file() and generation_status != "completed":
            return row
        candidate = read_json(directory / "play.json").get("candidate")
        if not candidate:
            raise ValueError("Round has no candidate Player report")
        report = safe_path(run, candidate["report"])
        if file_hash(report) != candidate["sha256"]:
            raise ValueError("Candidate Player report hash changed")
        recordings = report.parent / "gamecraft/recordings"
        data = read_json(recordings / "manifest.json")
        if (data["task"] != task["id"] or data["round"] != number or
                data["source_version"] != check["version"] or data["evidence_source"] != "player_recording"):
            raise ValueError("Recordings do not belong to this round's candidate")
        row.update(status="unscored" if manifest.get("scoring") else "not_requested",
                   player_report=os.path.relpath(report, batch), recordings=os.path.relpath(recordings, batch),
                   manifest_sha256=file_hash(recordings / "manifest.json"))
        # Prefer a verified valid attempt; never rerun a valid low score.
        for attempt in sorted((batch / "scores" / task["id"] / f"round-{number:04d}").glob("attempt-*")):
            if not attempt.is_dir():
                continue
            path = attempt / "result.json"
            if not path.exists():
                # The scorer may have finished just before the scheduler was interrupted.
                # Reuse a complete matching evaluation without paying for it again.
                try:
                    value = score_value(attempt, row, manifest["scoring"], len(data["demos"]))
                except (OSError, ValueError, KeyError, TypeError):
                    row.update(status="score_failed", error="Interrupted scoring attempt; no complete evaluation")
                    continue
                row.update(status="scored", recording_score=value, error=None,
                           evaluation=os.path.relpath(attempt / "evaluation.json", batch))
                break
            result = read_json(path)
            if (result.get("manifest_sha256") != row["manifest_sha256"] or
                    result.get("scoring_sha256") != digest(manifest.get("scoring"))):
                raise ValueError("Previous score belongs to different evidence or scoring settings")
            row.update(status="score_failed", error=result.get("error"),
                       evaluation=os.path.relpath(path.parent / "evaluation.json", batch))
            if result.get("status") == "scored":
                evaluation = path.parent / "evaluation.json"
                if file_hash(evaluation) != result["evaluation_sha256"]:
                    raise ValueError("Saved evaluation changed")
                row.update(status="scored", recording_score=result["recording_score"], error=None)
                break
    except (OSError, ValueError, KeyError, TypeError) as exc:
        row.update(status="evidence_unavailable", error=f"{type(exc).__name__}: {exc}", recording_score=None)
    return redact(row)


def score_value(output, row, scoring, demo_count):
    data = read_json(output / "evaluation.json")
    host = read_json(output / "host-manifest.json")
    value = data.get("recording_score")
    if (data.get("mode") != "gamecraft-player-recording-scoring" or data.get("task") != row["task"]
            or not data.get("build_ok") or data.get("errors") != []
            or data.get("official_score") is not None or data.get("scored_demos") != demo_count
            or data.get("replayed_demos") != 0 or data.get("judge_model") != scoring["judge"]["model"]
            or data.get("judge_max_tokens_override") != scoring["judge_max_tokens"]
            or host.get("judge_max_tokens_override") != scoring["judge_max_tokens"]
            or host["submission"]["manifest_sha256"] != row["manifest_sha256"]
            or host["submission"].get("source_version") != row["source_version"]
            or host["submission"].get("round") != row["round"]
            or host["submission"].get("task") != row["task"]
            or type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1):
        raise ValueError("Scorer did not produce a complete matching recording score; inspect evaluation/verifier log")
    return value


def score_task(batch, manifest, task, cancelled=lambda: False, *, on_progress=None):
    """Only score existing candidate recordings after generation; no Agent calls."""
    scoring = manifest.get("scoring")
    if not scoring or inspect_task(batch, task)["status"] != "completed":
        return
    for number in range(ROUNDS):
        if cancelled() or (batch / "STOP").exists():
            break
        row = round_result(batch, manifest, task, number, "completed")
        if row["status"] not in ("unscored", "score_failed"):
            print(f"[{task['id']}] round={number} score skip: {row['status']}", flush=True)
            continue
        # Freeze/check every file before charging a judge; retain failed checks too.
        parent = batch / "scores" / task["id"] / f"round-{number:04d}"
        parent.mkdir(parents=True, exist_ok=True)
        existing = [p for p in parent.glob("attempt-*") if p.is_dir()]
        index = max((int(p.name.removeprefix("attempt-")) for p in existing), default=0) + 1
        output = parent / f"attempt-{index:04d}"
        receipt = {"manifest_sha256": row["manifest_sha256"], "scoring_sha256": digest(scoring),
                   "started_at": time.time(), "status": "score_failed", "recording_score": None}
        try:
            recordings = safe_path(batch, row["recordings"])
            _, data, manifest_hash = validate_recordings(recordings)
            if manifest_hash != row["manifest_sha256"] or version(safe_path(batch, row["project"])) != row["source_version"]:
                raise ValueError("Candidate game or recordings changed")
            command = [sys.executable, "-u", str(ROOT / "scripts/score_gamecraft.py"),
                       "--task", task["id"], "--recordings", str(recordings),
                       "--benchmark", str((batch / manifest["benchmark"]).resolve()),
                       "--env-file", str((batch / scoring["env_file"]).resolve()),
                       "--judge-max-tokens", str(scoring["judge_max_tokens"]), "--output", str(output)]
            print(f"[{task['id']}] round={number} score attempt={index}: existing Player recordings", flush=True)
            with output.with_suffix(".log").open("w") as log:
                result = subprocess.run(command, cwd=ROOT, stdin=subprocess.DEVNULL,
                                        stdout=log, stderr=subprocess.STDOUT, env=child_env())
            if result.returncode != 0:
                raise RuntimeError(f"Scorer exited {result.returncode}; see {os.path.relpath(output.with_suffix('.log'), batch)}")
            value = score_value(output, row, scoring, len(data["demos"]))
            receipt.update(status="scored", recording_score=value,
                           evaluation_sha256=file_hash(output / "evaluation.json"))
        except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
            receipt["error"] = redact(f"{type(exc).__name__}: {exc}")
        receipt["ended_at"] = time.time()
        atomic_json(output / "result.json", receipt)
        print(f"[{task['id']}] round={number} {receipt['status']}: {receipt.get('recording_score')}", flush=True)
        if on_progress is None:
            refresh(batch, manifest, persist=True)
        else:
            # The scheduler owns shared summaries; workers only write their receipts.
            on_progress()


def check_environment(batch, manifest):
    """Preflight only for the new team/scoring mode; legacy batches stay compatible."""
    if manifest.get("worker"):
        shared = read_json(batch / "assignment.json")
        if code_identity()["files_sha256"] != shared["code"]["files_sha256"]:
            raise ValueError("Framework code changed since prepare; restore it or use a new batch")
        if manifest["mode"] != "mock":
            bench = (batch / manifest["benchmark"]).resolve()
            if git(bench, "rev-parse", "HEAD") != BENCH_COMMIT or git(bench, "status", "--porcelain", "--untracked-files=no"):
                raise ValueError("Frozen benchmark version changed")
    if manifest.get("scoring"):
        scoring = manifest["scoring"]
        if judge_profile((batch / scoring["env_file"]).resolve()) != scoring["judge"]:
            raise ValueError("Judge settings changed since prepare; API key rotation alone is allowed")
    if manifest["mode"] == "mock" or not (manifest.get("worker") or manifest.get("scoring")):
        return
    images = {}
    for task in manifest["tasks"]:
        config = load_config(paths(batch, task)[0])
        for tag in (config["runtime"]["image"], config["benchmark"].get("image", EVAL_IMAGE)):
            if tag not in images:
                info = json.loads(subprocess.check_output(["docker", "image", "inspect", tag, "--format", "{{json .}}"], text=True))
                images[tag] = {"id": info["Id"], "architecture": info["Architecture"], "os": info["Os"]}
    destination = batch / "environment.json"
    if destination.exists():
        if read_json(destination)["images"] != images:
            raise ValueError("Docker images changed since first run; restore them or use a new batch")
    else:
        atomic_json(destination, {"images": images, "system": platform.system(), "machine": platform.machine(),
                                  "python": platform.python_version(), "recorded_at": time.time()})


def refresh(batch, manifest, *, persist=False, stopped=False):
    rows = [inspect_task(batch, task) for task in manifest["tasks"]]
    counts = dict(Counter(row["status"] for row in rows))
    summary = {"format": FORMAT, "rounds": ROUNDS, "mode": manifest["mode"],
               "status": "stopped" if stopped or (batch / "STOP").exists()
                         else "completed" if counts.get("completed") == len(rows) else "incomplete",
               "updated_at": time.time(), "counts": counts, "tasks": rows}
    results = None
    if manifest.get("worker") or manifest.get("scoring"):
        results = [round_result(batch, manifest, task, n, row["status"])
                   for task, row in zip(manifest["tasks"], rows) for n in range(ROUNDS)]
        summary["round_counts"] = dict(Counter(row["status"] for row in results))
        summary["worker"] = manifest.get("worker")
        summary["assignment_sha256"] = manifest.get("assignment_sha256")
        if manifest.get("scoring") and any(row["status"] != "scored" for row in results) and summary["status"] == "completed":
            summary["status"] = "incomplete"
    if persist:
        atomic_json(batch / "summary.json", summary)
        if results is not None:
            atomic_json(batch / "results.json", {"assignment_sha256": manifest.get("assignment_sha256"),
                                                "expected_rounds": len(results), "rounds": results})
            temp = batch / "results.csv.tmp"
            with temp.open("w", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=list(results[0]))
                writer.writeheader()
                writer.writerows(results)
            temp.replace(batch / "results.csv")
    return summary


def request_stop(batch, manifest):
    (batch / "STOP").touch()
    for task in manifest["tasks"]:
        _, run, _ = paths(batch, task)
        if run.is_dir() and active(run):
            Store(run).request_stop()


def launch(batch, task, command):
    config, run, log = paths(batch, task)
    run.parent.mkdir(exist_ok=True)
    log.parent.mkdir(exist_ok=True)
    argv = [sys.executable, "-u", "-m", "game_agent_lite.cli", command]
    argv += [str(config), "--run-dir", str(run)] if command == "start" else [str(run)]
    stream = log.open("a")
    stream.write(f"\nBatch {command} at {time.time()}\n")
    stream.flush()
    try:
        process = subprocess.Popen(argv, cwd=batch, stdin=subprocess.DEVNULL, stdout=stream,
                                   stderr=subprocess.STDOUT, env=child_env(), start_new_session=True)
    except BaseException:
        stream.close()
        raise
    return process, stream


def run_batch(batch, *, jobs=1, dry_run=False):
    if jobs < 1:
        raise ValueError("--jobs must be positive")
    batch = batch.resolve()
    manifest = load_manifest(batch)
    if dry_run:
        return refresh(batch, manifest)
    # Reuse the same OS-lock implementation as runs, in a separate batch root.
    with Store(batch).lock():
        check_environment(batch, manifest)
        (batch / "STOP").unlink(missing_ok=True)  # Explicit run means resume this batch.
        pending = deque(manifest["tasks"])
        running = {}
        score_pending = deque()
        scoring = None
        cancel = threading.Event()
        progress = threading.Event()
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="gamecraft-score")
        previous = {sig: signal.signal(sig, lambda *_: cancel.set()) for sig in (signal.SIGINT, signal.SIGTERM)}
        execution = {"jobs": jobs, "score_jobs": 1 if manifest.get("scoring") else 0,
                     "started_at": time.time(), "status": "running"}
        execution_path = batch / "scheduler-runs" / f"{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:8]}.json"
        try:
            atomic_json(execution_path, execution)
            print(f"Batch concurrency: jobs={jobs}; score_jobs={execution['score_jobs']}; "
                  "generating, queued and scoring games all occupy a job slot", flush=True)
            while pending or running or score_pending or scoring is not None:
                changed = False
                if cancel.is_set() or (batch / "STOP").exists():
                    cancel.set()
                    pending.clear()
                    score_pending.clear()
                    for task, _, _ in running.values():
                        _, root, _ = paths(batch, task)
                        if root.is_dir():
                            Store(root).request_stop()
                if scoring is not None and scoring.done():
                    scoring.result()  # Surface unexpected worker errors and stop controllers safely.
                    scoring = None
                    changed = True
                for name, (task, process, log) in list(running.items()):
                    code = process.poll()
                    if code is None:
                        continue
                    log.close()
                    row = inspect_task(batch, task)
                    atomic_json(batch / "results" / f"{name}.json", {
                        "exit_code": code, "ended_at": time.time(), "status": row["status"],
                        "error": row["error"] or (f"Controller exited {code}" if code else None)})
                    del running[name]
                    print(f"[{name}] {row['status']} (exit {code})", flush=True)
                    if row["status"] == "completed" and manifest.get("scoring") and not cancel.is_set():
                        score_pending.append(task)
                    changed = True
                # A game keeps its slot until all its scoring has returned. In particular,
                # jobs=1 preserves generate -> score -> next game, with no overlap.
                while (pending and len(running) + len(score_pending) + (scoring is not None) < jobs
                       and not cancel.is_set()):
                    if (batch / "STOP").exists():
                        break
                    task = pending.popleft()
                    row = inspect_task(batch, task)
                    changed = True
                    if row["command"] is None:
                        print(f"[{task['id']}] skip: {row['status']}", flush=True)
                        if row["status"] == "completed" and manifest.get("scoring"):
                            score_pending.append(task)
                        continue
                    print(f"[{task['id']}] {row['command']}: rounds 0,1,2", flush=True)
                    try:
                        process, log = launch(batch, task, row["command"])
                        running[task["id"]] = (task, process, log)
                    except OSError as exc:
                        atomic_json(batch / "results" / f"{task['id']}.json",
                                    {"exit_code": None, "error": str(exc), "ended_at": time.time()})
                        print(f"[{task['id']}] launch failed: {redact(str(exc))}", flush=True)
                if score_pending and scoring is None and not cancel.is_set() and not (batch / "STOP").exists():
                    task = score_pending.popleft()
                    print(f"[{task['id']}] scoring started; retaining job slot", flush=True)
                    scoring = executor.submit(score_task, batch, manifest, task, cancel.is_set,
                                              on_progress=progress.set)
                if progress.is_set():
                    progress.clear()
                    changed = True
                if changed:
                    refresh(batch, manifest, persist=True, stopped=cancel.is_set())
                if running or scoring is not None:
                    time.sleep(.1)
            execution["status"] = "stopped" if cancel.is_set() else "finished"
        finally:
            # On an unexpected scheduler error, request graceful stop; never leave
            # launched controllers running as untracked background experiments.
            cancel.set()
            try:
                while running:
                    for name, (task, process, log) in list(running.items()):
                        _, root, _ = paths(batch, task)
                        if root.is_dir():
                            Store(root).request_stop()
                        if process.poll() is not None:
                            log.close()
                            del running[name]
                    if running:
                        time.sleep(.1)
            finally:
                # Let the in-flight scoring round save its receipt; cancel prevents
                # another round starting. Keep the batch lock until the worker exits.
                executor.shutdown(wait=True, cancel_futures=True)
                for sig, handler in previous.items():
                    signal.signal(sig, handler)
                if execution["status"] == "running":
                    execution["status"] = "failed"
                execution["ended_at"] = time.time()
                atomic_json(execution_path, execution)
        return refresh(batch, manifest, persist=True, stopped=execution["status"] == "stopped")


def export_batch(batch, output, *, round_number=None, clip_long=False):
    manifest = load_manifest(batch)
    if manifest["mode"] == "mock":
        raise ValueError("Mock evidence cannot be exported as real GameCraft submissions")
    output = output.resolve()
    if output.is_relative_to(batch.resolve()):
        raise ValueError("Export output must be outside the batch directory")
    if round_number is not None and round_number not in range(ROUNDS):
        raise ValueError("--round must be 0, 1 or 2")
    output.mkdir(parents=True, exist_ok=False)
    results = []
    for task in manifest["tasks"]:
        row = inspect_task(batch, task)
        item = {"task": task["id"], "status": "skipped", "error": None}
        if row["status"] != "completed":
            item["error"] = f"Run is {row['status']}; requires three completed rounds"
        else:
            argv = [sys.executable, "-m", "game_agent_lite.cli", "export-gamecraft", row["run"],
                    "--output", str(output / task["id"])]
            if round_number is not None:
                argv += ["--round", str(round_number)]
            if clip_long:
                argv += ["--clip-long"]
            with (output / f"{task['id']}.log").open("w") as log:
                process = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=log,
                                         stderr=subprocess.STDOUT, env=child_env())
            item.update(status="exported" if process.returncode == 0 else "failed",
                        exit_code=process.returncode)
        results.append(item)
        atomic_json(output / "export-summary.json", {"official_score": None,
                    "replay_verified": False, "tasks": results})
    return results


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    commands = p.add_subparsers(dest="command", required=True)
    prepare_cmd = commands.add_parser("prepare", help="Freeze public tasks and configs; no rollout")
    prepare_cmd.add_argument("--bench", type=Path, default=ROOT / "benchmarks/gamecraft-bench")
    prepare_cmd.add_argument("--config", type=Path, default=Path("examples/godot-docker.json"))
    prepare_cmd.add_argument("--batch-dir", type=Path, help="Default: runs/gamecraft/batch-<unique-id>")
    prepare_cmd.add_argument("--env-file", type=Path)
    prepare_cmd.add_argument("--task", action="append", default=[])
    prepare_cmd.add_argument("--worker", choices=MEMBERS, help="Fixed 45-task assignment; 15 tasks per member")
    prepare_cmd.add_argument("--score", action="store_true", help="After each game's 3 rounds, score every candidate's saved Player recordings")
    prepare_cmd.add_argument("--judge-env-file", type=Path, default=ROOT / ".env.gamecraft")
    prepare_cmd.add_argument("--judge-max-tokens", type=int, default=4096, help="Batch OpenAI judge budget, frozen at prepare (default: 4096)")
    prepare_cmd.add_argument("--mock", action="store_true", help="Offline controller fixture only")
    run = commands.add_parser("run", help="Start/resume tasks once each; skip completed or blocked tasks")
    run.add_argument("batch_dir", type=Path)
    run.add_argument("--jobs", type=int, default=1,
                     help="Concurrent games including generation/queued scoring/scoring (default: 1); at most one scorer")
    run.add_argument("--dry-run", action="store_true", help="Read-only plan; no subprocesses")
    for name in ("status", "stop"):
        commands.add_parser(name).add_argument("batch_dir", type=Path)
    export = commands.add_parser("export", help="Export completed tasks; no scoring or rollout")
    export.add_argument("batch_dir", type=Path)
    export.add_argument("--output", type=Path, required=True)
    export.add_argument("--round", type=int, choices=range(ROUNDS), dest="round_number")
    export.add_argument("--clip-long", action="store_true")
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command == "prepare":
            args.batch_dir = args.batch_dir or Path.cwd() / "runs" / "gamecraft" / (
                f"gamecraft45-{args.worker}-3rounds" if args.worker else
                f"batch-{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}")
            manifest = prepare(args.bench, args.config, args.batch_dir, env_file=args.env_file,
                               selected=args.task, mock=args.mock, worker=args.worker, score=args.score,
                               judge_env_file=args.judge_env_file, judge_max_tokens=args.judge_max_tokens)
            print(f"Prepared {len(manifest['tasks'])} tasks, exactly 3 rounds each; no rollout started.")
            print(f"Batch directory: {args.batch_dir.resolve()}")
            if args.worker:
                print(f"Assignment SHA256: {manifest['assignment_sha256']}")
            return 0
        batch = args.batch_dir.resolve()
        if args.command == "run":
            result = run_batch(batch, jobs=args.jobs, dry_run=args.dry_run)
            print(json.dumps(result if args.dry_run else {k: v for k, v in result.items() if k != "tasks"},
                             ensure_ascii=False, indent=2))
            return 0 if (args.dry_run and not result["counts"].get("invalid")) or result["status"] == "completed" else 1
        if args.command == "status":
            print(json.dumps(refresh(batch, load_manifest(batch)), ensure_ascii=False, indent=2))
        elif args.command == "stop":
            request_stop(batch, load_manifest(batch))
            print("Batch stop requested; wait for active controllers to save and exit.")
        else:
            results = export_batch(batch, args.output, round_number=args.round_number, clip_long=args.clip_long)
            print(json.dumps(dict(Counter(r["status"] for r in results)), ensure_ascii=False))
            return 0 if all(r["status"] == "exported" for r in results) else 1
        return 0
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, ValidationError, subprocess.CalledProcessError) as exc:
        print(redact(f"{type(exc).__name__}: {exc}"), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
