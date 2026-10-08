"""Offline tests for the outer GameCraft scheduler; no live rollouts."""
import importlib.util
import io
import json
from pathlib import Path
import shutil
import subprocess
import threading

import pytest

from game_agent_lite.store import Store, atomic_json, file_hash, read_json, version

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/run_gamecraft_batch.py"
spec = importlib.util.spec_from_file_location("gamecraft_batch", SCRIPT)
batch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(batch)


@pytest.fixture
def prepared(tmp_path, config):
    bench = tmp_path / "public benchmark"
    for name in ("alpha-game", "beta-game", "gamma-game", "example"):
        path = bench / "tasks" / name / "instruction.md"
        path.parent.mkdir(parents=True)
        path.write_bytes(f"# {name}\n公开题目，保留要求。\r\n".encode())
    # A hidden file must not be read or incorporated into prompts.
    (bench / "hidden-rubric.txt").write_text("DO_NOT_INCLUDE_HIDDEN_RUBRIC")
    config.update(engine="godot", entry="project.godot", backend="harbor", rounds=None)
    config["runtime"]["mode"] = "docker"
    template = tmp_path / "template.json"
    atomic_json(template, config)
    root = tmp_path / "batch with spaces"
    manifest = batch.prepare(bench, template, root, env_file=tmp_path / "missing-secrets.env")
    return root, manifest, bench, template


def state_for(root, task, status="failed", round_number=1):
    cfg, run, _ = batch.paths(root, task)
    store = Store(run)
    if not run.exists():
        store.create(read_json(cfg))
    state = store.load()
    state.update(status=status, round=round_number, phase="design",
                 history=[{"round": n} for n in range(round_number)])
    store.save(state)
    return store


def forbidden(*args, **kwargs):
    raise AssertionError("No subprocess expected")


def test_prepare_freezes_all_public_tasks_and_dry_run_is_read_only(prepared, monkeypatch):
    root, manifest, bench, template = prepared
    monkeypatch.setattr(batch.subprocess, "Popen", forbidden)
    assert [t["id"] for t in manifest["tasks"]] == ["alpha-game", "beta-game", "gamma-game"]
    assert read_json(template)["rounds"] is None
    before = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    for task in manifest["tasks"]:
        source = (bench / "tasks" / task["id"] / "instruction.md").read_bytes()
        assert (root / "instructions" / f"{task['id']}.md").read_bytes() == source
        config = read_json(root / "configs" / f"{task['id']}.json")
        assert config["rounds"] == 3
        assert config["brief"] == batch.ENVIRONMENT + source.decode()
        assert "Each clip is at most 20 seconds" in config["brief"] and "without requiring a win in every clip" in config["brief"]
        assert "每条以20秒内真实通关为目标" not in config["brief"]
        assert "DO_NOT_INCLUDE_HIDDEN_RUBRIC" not in config["brief"]
        assert config["player"]["mode"] == "policy"
        assert config["player"]["trajectories_per_version"] == 3
        assert config["benchmark"] == {"name": "gamecraft", "task": task["id"], "path": str(bench.resolve())}
        assert Path(config["assets"]["env_file"]).is_absolute()
    summary = batch.run_batch(root, dry_run=True)
    assert summary["counts"] == {"pending": 3}
    assert all(row["command"] == "start" for row in summary["tasks"])
    assert before == {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    assert not (root / "runs").exists()
    assert not (root / "run.lock").exists()


@pytest.mark.parametrize("selected", [["example"], ["unknown"], ["alpha-game", "alpha-game"]])
def test_reject_invalid_task_selection(prepared, selected):
    _, _, bench, _ = prepared
    with pytest.raises(ValueError, match="--task"):
        batch.discover(bench, selected)


@pytest.mark.parametrize("tamper", ["config", "instruction", "rounds", "schema"])
def test_changed_or_invalid_inputs_never_launch(prepared, monkeypatch, tamper):
    root, manifest, _, _ = prepared
    task = manifest["tasks"][0]
    cfg, _, _ = batch.paths(root, task)
    if tamper == "instruction":
        (root / "instructions" / f"{task['id']}.md").write_text("changed")
    else:
        config = read_json(cfg)
        config["rounds"] = None if tamper == "rounds" else "invalid" if tamper == "schema" else 1
        atomic_json(cfg, config)
        if tamper in ("rounds", "schema"):
            task["config_sha256"] = file_hash(cfg)
            atomic_json(root / "batch.json", manifest)
    monkeypatch.setattr(batch.subprocess, "Popen", forbidden)
    assert batch.inspect_task(root, task)["status"] == "invalid"
    assert batch.main(["run", str(root), "--dry-run"]) == 1


def test_resume_skip_active_reconciliation_and_config_mismatch(prepared):
    root, manifest, _, _ = prepared
    a, b, c = manifest["tasks"]
    state_for(root, a, "completed", 3)
    state_for(root, b, "needs_reconciliation")
    current = state_for(root, c, "running")
    with current.lock():
        assert batch.inspect_task(root, c)["status"] == "active"
        assert batch.inspect_task(root, c)["command"] is None
    assert batch.inspect_task(root, c)["command"] == "resume"
    assert batch.inspect_task(root, a)["command"] is None
    assert batch.inspect_task(root, b)["command"] is None
    cfg = read_json(current.root / "config.json")
    cfg["rounds"] = None
    atomic_json(current.root / "config.json", cfg)
    assert batch.inspect_task(root, c)["status"] == "invalid"


def test_three_completed_rounds_required(prepared):
    root, manifest, _, _ = prepared
    task = manifest["tasks"][0]
    state_for(root, task, "completed", 1)
    assert batch.inspect_task(root, task)["status"] == "invalid"


def test_parallel_scheduler_continues_after_failure_and_resumes_once(prepared, monkeypatch):
    root, manifest, _, _ = prepared
    state_for(root, manifest["tasks"][0], "stopped")
    live, calls, maximum = set(), [], []
    class Child:
        def __init__(self, task):
            self.task, self.polls, self.done = task, 0, False
        def poll(self):
            if self.done:
                return 1 if self.task["id"] == "alpha-game" else 0
            self.polls += 1
            if self.polls < 2:
                return None
            failed = self.task["id"] == "alpha-game"
            state_for(root, self.task, "failed" if failed else "completed", 1 if failed else 3)
            live.remove(self.task["id"])
            self.done = True
            return 1 if failed else 0
    def launch(root, task, command):
        calls.append((task["id"], command))
        live.add(task["id"])
        maximum.append(len(live))
        return Child(task), io.StringIO()
    monkeypatch.setattr(batch, "launch", launch)
    monkeypatch.setattr(batch.time, "sleep", lambda *_: None)
    result = batch.run_batch(root, jobs=2)
    assert max(maximum) == 2
    assert calls == [("alpha-game", "resume"), ("beta-game", "start"), ("gamma-game", "start")]
    assert result["counts"] == {"failed": 1, "completed": 2}
    calls.clear()
    batch.run_batch(root, jobs=2)
    assert calls == [("alpha-game", "resume")]


def test_batch_lock_prevents_duplicate_controllers(prepared, monkeypatch):
    root, _, _, _ = prepared
    monkeypatch.setattr(batch, "launch", forbidden)
    with Store(root).lock():
        with pytest.raises(RuntimeError, match="locked|controller|running"):
            batch.run_batch(root)


def test_stop_keeps_unstarted_tasks_pending(prepared, monkeypatch):
    root, manifest, _, _ = prepared
    calls = []
    class Child:
        def poll(self):
            run = root / "runs" / "alpha-game"
            if (run / "STOP").exists():
                state_for(root, manifest["tasks"][0], "stopped")
                return 0
            batch.request_stop(root, manifest)
            return None
    def launch(root, task, command):
        calls.append(task["id"])
        state_for(root, task, "running")
        return Child(), io.StringIO()
    monkeypatch.setattr(batch, "launch", launch)
    monkeypatch.setattr(batch.time, "sleep", lambda *_: None)
    result = batch.run_batch(root)
    assert calls == ["alpha-game"]
    assert result["status"] == "stopped"
    assert result["counts"] == {"stopped": 1, "pending": 2}
    assert batch.refresh(root, manifest)["status"] == "stopped"


def test_launch_uses_argument_list_and_separate_logs(prepared, monkeypatch):
    root, manifest, _, _ = prepared
    captured = []
    monkeypatch.setattr(batch.subprocess, "Popen", lambda argv, **kw: captured.append((argv, kw)))
    _, log = batch.launch(root, manifest["tasks"][0], "start")
    log.close()
    argv, kwargs = captured[0]
    assert argv[4:] == ["start", str(root / "configs/alpha-game.json"),
                        "--run-dir", str(root / "runs/alpha-game")]
    assert not kwargs.get("shell")
    assert kwargs["stdin"] == subprocess.DEVNULL
    assert kwargs["start_new_session"] is True


def test_real_cli_mock_batch_stops_after_round2_and_skips_finished(prepared, monkeypatch):
    old_root, _, bench, template = prepared
    root = old_root.parent / "offline batch"
    manifest = batch.prepare(bench, template, root, mock=True, selected=["alpha-game", "beta-game"])
    result = batch.run_batch(root, jobs=2)
    assert result["counts"] == {"completed": 2}, result
    for task in manifest["tasks"]:
        config, run, _ = batch.paths(root, task)
        cfg = read_json(config)
        assert cfg["backend"] == "mock" and cfg["player"]["mode"] == "gui"
        assert cfg["assets"]["max_images"] == cfg["assets"]["max_models"] == cfg["assets"]["max_blender"] == 0
        state = read_json(run / "state.json")
        assert state["round"] == 3
        assert [h["round"] for h in state["history"]] == [0, 1, 2]
        assert not (run / "rounds/0003").exists()
        assert all((run / f"rounds/{n:04d}/select.json").is_file() for n in range(3))
    monkeypatch.setattr(batch.subprocess, "Popen", forbidden)
    assert batch.run_batch(root)["status"] == "completed"
    with pytest.raises(ValueError, match="Mock evidence"):
        batch.export_batch(root, old_root.parent / "export")


def test_export_dispatches_selected_round_and_retains_failures(prepared, monkeypatch):
    root, manifest, _, _ = prepared
    for task in manifest["tasks"][:2]:
        state_for(root, task, "completed", 3)
    commands = []
    def export(argv, **kwargs):
        commands.append(argv)
        return subprocess.CompletedProcess(argv, 1 if len(commands) == 1 else 0)
    monkeypatch.setattr(batch.subprocess, "run", export)
    output = root.parent / "exports"
    results = batch.export_batch(root, output, round_number=2, clip_long=True)
    assert [r["status"] for r in results] == ["failed", "exported", "skipped"]
    assert all(cmd[-3:] == ["--round", "2", "--clip-long"] for cmd in commands)
    assert read_json(output / "export-summary.json")["official_score"] is None
    assert read_json(output / "export-summary.json")["replay_verified"] is False


def test_invalid_cli_arguments(prepared):
    root, _, _, _ = prepared
    assert batch.main(["run", str(root), "--jobs", "0", "--dry-run"]) == 1
    with pytest.raises(SystemExit):
        batch.parser().parse_args(["run", str(root), "--rounds", "5"])


def test_task_file_native_configs_relative_paths_and_frozen_protocol(prepared, monkeypatch):
    root, _, bench, template = prepared
    task_file = SCRIPT.parents[1] / "examples/gamecraft-tasks.json"
    names = batch.read_task_file(task_file)
    assert len(set(names)) == len(names) == 45
    for name in names:
        path = bench / "tasks" / name / "instruction.md"
        path.parent.mkdir(parents=True)
        path.write_text(f"Full public instruction: {name}\n")
    monkeypatch.setattr(batch, "git", lambda _, *args: batch.BENCH_COMMIT if args[0] == "rev-parse" else "")
    monkeypatch.setattr(batch, "code_identity", lambda: {"commit": "fixture", "files_sha256": "fixture"})
    monkeypatch.setattr(batch.subprocess, "Popen", forbidden)
    judge = root.parent / "judge.env"
    judge.write_text("GAMECRAFT_BENCH_JUDGE=openai\nGAMECRAFT_BENCH_JUDGE_MODEL=fixture\nOPENAI_API_KEY=secret-not-saved\n")
    hashes = set()
    for attempt in range(2):
        target = root.parent / f"batch-{attempt}"
        manifest = batch.prepare(bench, template, target, task_file=task_file, score=True, judge_env_file=judge)
        assert [t["id"] for t in manifest["tasks"]] == names
        hashes.add(manifest["protocol_sha256"])
        assert not Path(manifest["benchmark"]).is_absolute()
        assert not Path(manifest["scoring"]["env_file"]).is_absolute()
        config_path = target / "configs" / f"{names[0]}.json"
        raw, config = read_json(config_path), batch.load_config(config_path)
        assert raw["benchmark"]["evidence_source"] == "player_recording"
        assert not Path(raw["benchmark"]["path"]).is_absolute()
        assert not Path(raw["assets"]["env_file"]).is_absolute()
        original = batch.load_config(template)
        assert config["model"] == original["model"] and config["reasoning"] == original["reasoning"]
        assert config["rounds"] == 3
        assert config["player"]["parallel_episodes"] == original["player"]["parallel_episodes"]
        assert config["runtime"]["render_threads"] == original["runtime"]["render_threads"]
        before = {p: p.read_bytes() for p in target.rglob("*") if p.is_file()}
        assert batch.run_batch(target, dry_run=True)["counts"] == {"pending": 45}
        assert batch.run_batch(target, jobs=2, dry_run=True)["counts"] == {"pending": 45}
        assert before == {p: p.read_bytes() for p in target.rglob("*") if p.is_file()}
        assert len(read_json(target / "results.json")["rounds"]) == 135
        assert "secret-not-saved" not in "".join(p.read_text() for p in target.rglob("*.json"))
        manifest["scoring"]["judge_max_tokens"] = 2048
        atomic_json(target / "batch.json", manifest)
        with pytest.raises(ValueError, match="scoring settings"):
            batch.load_manifest(target)
    assert len(hashes) == 1


@pytest.mark.parametrize("names", [[], ["alpha-game", "alpha-game"], {"tasks": ["alpha-game"]}, [None]])
def test_invalid_task_file_does_not_create_batch(prepared, names):
    root, _, bench, template = prepared
    task_file = root.parent / "tasks.json"
    atomic_json(task_file, names)
    target = root.parent / "invalid-batch"
    with pytest.raises(ValueError, match="Task file"):
        batch.prepare(bench, template, target, task_file=task_file, mock=True)
    assert not target.exists()


def test_task_file_and_individual_task_are_mutually_exclusive(prepared):
    root, _, bench, template = prepared
    target = root.parent / "invalid-batch"
    with pytest.raises(ValueError, match="not both"):
        batch.prepare(bench, template, target, task_file=root / "unused.json", selected=["alpha-game"])
    assert not target.exists()


@pytest.fixture
def score_batch(prepared, request):
    from test_gamecraft_recordings import fixture_bundle
    root, manifest, _, _ = prepared
    manifest["tasks"] = manifest["tasks"][:getattr(request, "param", 1)]
    manifest["scoring"] = {"env_file": "judge.env", "judge_max_tokens": 4096,
                           "evidence_source": "player_recording", "judge": {"model": "fixture"}}
    atomic_json(root / "batch.json", manifest)
    for task in manifest["tasks"]:
        state_for(root, task, "completed", 3)
        _, run, _ = batch.paths(root, task)
        for n in range(3):
            original, session, report = fixture_bundle(root.parent / f"fixture-{task['id']}-{n}")
            directory = run / f"rounds/{n:04d}"
            job = directory / "jobs/player-candidate-fixture"
            recordings = job / "gamecraft/recordings"
            shutil.copytree(original, recordings)
            data = read_json(recordings / "manifest.json")
            data.update(task=task["id"], round=n)
            atomic_json(recordings / "manifest.json", data)
            shutil.copytree(session.project, directory / "project")
            atomic_json(job / "play.json", report)
            atomic_json(directory / "check.json", {"passed": True, "version": version(directory / "project"),
                                                   "project": f"rounds/{n:04d}/project"})
            atomic_json(directory / "play.json", {"candidate": {"report": str((job / "play.json").relative_to(run)),
                                                                  "sha256": file_hash(job / "play.json")},
                                                   "incumbent": {"report": "NEVER_USE_INCUMBENT"}})
            atomic_json(directory / "select.json", {"promoted": n == 0})
    return root, manifest, manifest["tasks"][0]


def fake_score(command, *, invalid=False):
    output = Path(command[command.index("--output") + 1])
    recordings = Path(command[command.index("--recordings") + 1])
    source = read_json(recordings / "manifest.json")
    atomic_json(output / "evaluation.json", {"mode": "gamecraft-player-recording-scoring",
        "task": source["task"], "build_ok": True, "errors": ["truncated judge reply"] if invalid else [],
        "official_score": None, "recording_score": .1, "scored_demos": 3, "replayed_demos": 0,
        "judge_model": "fixture", "judge_max_tokens_override": 4096})
    atomic_json(output / "host-manifest.json", {"judge_max_tokens_override": 4096,
        "submission": {"manifest_sha256": file_hash(recordings / "manifest.json"), "task": source["task"],
                       "round": source["round"], "source_version": source["source_version"]}})
    return subprocess.CompletedProcess(command, 0)


def test_scoring_each_candidate_then_retry_only_failed_round(score_batch, monkeypatch):
    root, manifest, task = score_batch
    monkeypatch.setattr(batch, "check_environment", lambda *_: None)
    monkeypatch.setattr(batch, "launch", forbidden)  # Must not regenerate or replay Player.
    calls = []
    def scoring(command, **kw):
        calls.append(command)
        assert command[-4:-2] == ["--judge-max-tokens", "4096"]
        return fake_score(command, invalid=len(calls) == 2)
    monkeypatch.setattr(batch.subprocess, "run", scoring)
    result = batch.run_batch(root)
    assert result["status"] == "incomplete"
    rows = read_json(root / "results.json")["rounds"]
    assert [r["status"] for r in rows] == ["scored", "score_failed", "scored"]
    assert rows[1]["recording_score"] is None and rows[2]["promoted"] is False
    assert all(r["official_score"] is None for r in rows)
    assert len(calls) == 3
    first = root / "scores/alpha-game/round-0000/attempt-0001/evaluation.json"
    before = first.read_bytes()
    assert batch.run_batch(root)["status"] == "completed"
    assert len(calls) == 4 and "round-0001" in calls[-1][-1] and "attempt-0002" in calls[-1][-1]
    assert first.read_bytes() == before
    assert batch.run_batch(root)["status"] == "completed" and len(calls) == 4
    assert "0.1" in (root / "results.csv").read_text()  # A low valid score is still retained.


@pytest.mark.parametrize("score_batch", [3], indirect=True)
@pytest.mark.parametrize("jobs", [1, 2])
def test_job_slots_include_queued_scores_and_other_games_keep_advancing(score_batch, monkeypatch, jobs):
    root, manifest, _ = score_batch
    for task in manifest["tasks"]:
        state_for(root, task, "ready", 0)
    monkeypatch.setattr(batch, "check_environment", lambda *_: None)
    first_score_started, second_generation_finished = threading.Event(), threading.Event()
    lock = threading.Lock()
    occupied, judging, maximum, launched, calls, summaries = set(), set(), [], [], [], []
    original_refresh = batch.refresh
    main_thread = threading.get_ident()

    def refresh(*args, **kwargs):
        if kwargs.get("persist"):
            summaries.append(threading.get_ident())
        return original_refresh(*args, **kwargs)

    class Child:
        def __init__(self, task):
            self.task = task

        def poll(self):
            _, run, _ = batch.paths(root, self.task)
            if (run / "STOP").exists():
                state_for(root, self.task, "stopped")
                return 1
            if jobs == 2 and self.task["id"] == "beta-game":
                if not first_score_started.is_set():
                    return None
                # The scheduler must poll this game while alpha's scorer is blocked.
                second_generation_finished.set()
            state_for(root, self.task, "completed", 3)
            return 0

    def launch(root, task, command):
        assert command == "resume"
        with lock:
            occupied.add(task["id"])
            maximum.append(len(occupied))
            assert len(occupied) <= jobs
            launched.append(task["id"])
        return Child(task), io.StringIO()

    def scoring(command, **kwargs):
        task = command[command.index("--task") + 1]
        output = Path(command[-1])
        number = int(output.parent.name.removeprefix("round-"))
        with lock:
            assert not judging
            judging.add(task)
            calls.append((task, number))
        try:
            if task == "alpha-game" and number == 0:
                first_score_started.set()
                if jobs == 2:
                    assert second_generation_finished.wait(5), "Scoring blocked generation polling"
                    assert "gamma-game" not in launched  # Queued beta still occupies its slot.
            return fake_score(command)
        finally:
            with lock:
                judging.remove(task)
                if number == 2:
                    occupied.remove(task)

    monkeypatch.setattr(batch, "refresh", refresh)
    monkeypatch.setattr(batch, "launch", launch)
    monkeypatch.setattr(batch.subprocess, "run", scoring)
    result = batch.run_batch(root, jobs=jobs)
    assert result["status"] == "completed" and result["round_counts"] == {"scored": 9}
    assert max(maximum) == jobs and not occupied and not judging
    assert calls == [(task["id"], n) for task in manifest["tasks"] for n in range(3)]
    assert summaries and set(summaries) == {main_thread}
    execution = read_json(next((root / "scheduler-runs").glob("*.json")))
    assert execution["jobs"] == jobs and execution["score_jobs"] == 1
    assert execution["status"] == "finished" and execution["ended_at"] >= execution["started_at"]
    # Rerunning completed games must not charge again, regardless of concurrency.
    monkeypatch.setattr(batch, "launch", forbidden)
    monkeypatch.setattr(batch.subprocess, "run", forbidden)
    assert batch.run_batch(root, jobs=2)["status"] == "completed"


@pytest.mark.parametrize("score_batch", [3], indirect=True)
def test_stop_during_scoring_preserves_current_round_and_resumes_queued_games(score_batch, monkeypatch):
    root, manifest, _ = score_batch
    monkeypatch.setattr(batch, "check_environment", lambda *_: None)
    monkeypatch.setattr(batch, "launch", forbidden)
    calls = []

    def scoring(command, **kwargs):
        calls.append(command)
        if len(calls) == 1:
            batch.request_stop(root, manifest)
            # The batch lock must remain held while the scoring worker finishes.
            with pytest.raises(RuntimeError, match="locked|controller|running"):
                with Store(root).lock():
                    pass
        return fake_score(command)

    monkeypatch.setattr(batch.subprocess, "run", scoring)
    result = batch.run_batch(root, jobs=2)
    assert result["status"] == "stopped" and len(calls) == 1
    rows = read_json(root / "results.json")["rounds"]
    assert [r["status"] for r in rows] == ["scored"] + ["unscored"] * 8
    first = root / rows[0]["evaluation"]
    before = first.read_bytes()
    assert batch.run_batch(root, jobs=2)["status"] == "completed"
    assert len(calls) == 9 and first.read_bytes() == before
    attempts = list((root / "scores").glob("*/round-*/attempt-*"))
    assert len([p for p in attempts if p.is_dir()]) == 9
    executions = [read_json(p) for p in (root / "scheduler-runs").glob("*.json")]
    assert {r["status"] for r in executions} == {"stopped", "finished"}


def test_scoring_worker_error_stops_other_controller_and_releases_batch_lock(prepared, monkeypatch):
    root, manifest, _, _ = prepared
    manifest["scoring"] = {"fixture": True}
    atomic_json(root / "batch.json", manifest)
    monkeypatch.setattr(batch, "check_environment", lambda *_: None)
    state_for(root, manifest["tasks"][0], "completed", 3)
    launched, logs = [], []

    class Child:
        def poll(self):
            run = batch.paths(root, manifest["tasks"][1])[1]
            if (run / "STOP").exists():
                state_for(root, manifest["tasks"][1], "stopped")
                return 0
            return None

    def launch(root, task, command):
        launched.append(task["id"])
        state_for(root, task, "running", 0)
        log = io.StringIO()
        logs.append(log)
        return Child(), log

    def scoring(*args, **kwargs):
        raise RuntimeError("unexpected scorer failure")

    monkeypatch.setattr(batch, "launch", launch)
    monkeypatch.setattr(batch, "score_task", scoring)
    with pytest.raises(RuntimeError, match="unexpected scorer failure"):
        batch.run_batch(root, jobs=2)
    assert launched == ["beta-game"] and logs[0].closed
    assert batch.inspect_task(root, manifest["tasks"][1])["status"] == "stopped"
    execution = read_json(next((root / "scheduler-runs").glob("*.json")))
    assert execution["status"] == "failed"
    with Store(root).lock():
        pass


@pytest.mark.parametrize("tamper", ["frame", "round", "report", "game"])
def test_wrong_or_changed_candidate_never_calls_judge(score_batch, monkeypatch, tamper):
    root, manifest, task = score_batch
    row = batch.round_result(root, manifest, task, 0, "completed")
    recordings = root / row["recordings"]
    if tamper == "frame":
        data = read_json(recordings / "manifest.json")
        (recordings / data["demos"][0]["frames"][0]["image"]["path"]).write_bytes(b"changed")
    elif tamper == "round":
        data = read_json(recordings / "manifest.json")
        data["round"] = 2
        atomic_json(recordings / "manifest.json", data)
    elif tamper == "report":
        (root / row["player_report"]).write_text("changed")
    else:
        (root / row["project"] / "project.godot").write_text("changed")
    monkeypatch.setattr(batch.subprocess, "run", forbidden)
    # Stop after round 0 so unrelated valid rounds cannot request a judge.
    checks = iter([False, True])
    batch.score_task(root, manifest, task, lambda: next(checks))
    assert batch.round_result(root, manifest, task, 0, "completed")["recording_score"] is None


def test_finished_score_survives_interruption_before_scheduler_receipt(score_batch, monkeypatch):
    root, manifest, task = score_batch
    monkeypatch.setattr(batch.subprocess, "run", lambda command, **kw: fake_score(command))
    checks = iter([False, True])
    batch.score_task(root, manifest, task, lambda: next(checks))
    result = root / "scores/alpha-game/round-0000/attempt-0001/result.json"
    result.unlink()
    row = batch.round_result(root, manifest, task, 0, "completed")
    assert row["status"] == "scored" and row["recording_score"] == .1


def test_protocol_code_changes_and_judge_model_changes_block_resume(tmp_path, monkeypatch):
    atomic_json(tmp_path / "protocol.json", {"code": {"files_sha256": "old"}})
    monkeypatch.setattr(batch, "code_identity", lambda: {"files_sha256": "new"})
    with pytest.raises(ValueError, match="Framework code changed"):
        batch.check_environment(tmp_path, {"protocol_sha256": "fixture", "mode": "mock"})
    env = tmp_path / "judge.env"
    env.write_text("GAMECRAFT_BENCH_JUDGE_MODEL=first\nOPENAI_API_KEY=secret1\n")
    profile = batch.judge_profile(env)
    env.write_text("GAMECRAFT_BENCH_JUDGE_MODEL=first\nOPENAI_API_KEY=secret2\n")
    assert batch.judge_profile(env) == profile
    env.write_text("GAMECRAFT_BENCH_JUDGE_MODEL=second\n")
    with pytest.raises(ValueError, match="Judge settings changed"):
        batch.check_environment(tmp_path, {"mode": "live", "scoring": {"env_file": "judge.env", "judge": profile}})
