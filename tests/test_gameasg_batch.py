"""Offline batch orchestration tests: fake generation/evaluation, no live experiments."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest

from game_agent_lite.cli import load_config
from game_agent_lite.store import Store, atomic_json, file_hash, read_json, version

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/run_gameasg_batch.py"
spec = importlib.util.spec_from_file_location("gameasg_batch", SCRIPT)
batch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(batch)


def forbidden(*args, **kwargs):
    raise AssertionError("No model, Docker or evaluator process is allowed in this test")


@pytest.fixture
def workspace(tmp_path, config, monkeypatch):
    root = tmp_path / "repo with spaces"
    root.mkdir()
    bench = root / "benchmarks/GameASG-Bench"
    for i in range(47):
        for name in batch.DOCS:
            file = bench / "task" / f"task-{i:02d}" / name
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_bytes((name + "\r\n公开需求\r\n").encode())
        for name in ("checks.json", "checks.js"):
            file = bench / "tests" / f"task-{i:02d}" / name
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text("DO_NOT_EXPOSE_HIDDEN_TESTS")
    template = root / "examples/html-docker.json"
    config.update(backend="harbor", source=None)
    config["runtime"]["mode"] = "docker"
    config["player"]["parallel_episodes"] = 1
    config["auth_file"] = str(root / "auth.json")
    atomic_json(template, config)
    (root / ".env.hunyuan").write_text("DO_NOT_READ_CREDENTIALS")
    (root / "auth.json").write_text("DO_NOT_READ_AUTH")
    monkeypatch.setattr(batch, "ROOT", root)
    monkeypatch.setattr(batch, "benchmark_identity", lambda p: batch.BENCH_COMMIT)
    monkeypatch.setattr(batch, "code_identity", lambda: {"commit": "fixture", "files_sha256": "fixture-hash"})
    monkeypatch.setattr(batch.subprocess, "check_output", forbidden)
    monkeypatch.setattr(batch.subprocess, "Popen", forbidden)
    return root, bench, template


def prepare(workspace, tasks=("task-00",), **kwargs):
    _, bench, template = workspace
    return batch.prepare(bench, template, tasks=tasks, exp_id="fixture", **kwargs)


def finish_generation(root, manifest, task, status="completed"):
    work, run, _ = batch.paths(root, manifest, task)
    cfg = load_config(work / "config.local.json")
    if not run.exists():
        Store(run).create(cfg)
    project = run / "rounds/0000/integrate-test/project"
    project.mkdir(parents=True, exist_ok=True)
    (project / "index.html").write_text("<html>real fixture game</html>")
    atomic_json(run / "rounds/0000/check.json", {"passed": True,
        "project": str(project.relative_to(run)), "version": version(project)})
    state = Store(run).load()
    state.update(status=status, round=1 if status == "completed" else 0,
                 phase="design" if status == "completed" else "play",
                 history=[{"round": 0}] if status == "completed" else [])
    Store(run).save(state)
    return run, project


def fake_reports(output, name, *, low=True):
    atomic_json(output / "summary-test.json", {"game": name, "mode": "test-only", "test_phase": "all", "role": "target",
        "evaluation_time": 2, "l1": {"passed": 1, "total": 2, "status": "OK"},
        "l2": {"passed": 1 if low else 3, "total": 3, "status": "OK"}})
    atomic_json(output / "report-l1.json", {"checks": [{"status": "PASS"}, {"status": "FAIL"}]})
    atomic_json(output / "report-l2.json", {"checks": [
        {"id": "a", "level": "P0", "status": "PASS"},
        {"id": "b", "level": "P1", "status": "FAIL" if low else "PASS"},
        {"id": "c", "level": "P2", "status": "FAIL" if low else "PASS"},
        {"id": "d", "level": "P2", "status": "NOT_APPLICABLE"}]})
    (output / "test.log").write_text("official fixture log")


def install_fake_launch(root, manifest, monkeypatch, *, fail_generation=(), fail_evaluation=()):
    calls = []
    real_preflight = batch.preflight
    monkeypatch.setattr(batch, "preflight", lambda b, m, docker=False: real_preflight(b, m, docker=False))
    def launch(command, **kwargs):
        name = kwargs["run"].name
        task = next(t for t in manifest["tasks"] if t["id"] == name)
        work, run, output = batch.paths(root, manifest, task)
        if kwargs.get("generation"):
            calls.append((name, command[4]))
            finish_generation(root, manifest, task, "failed" if name in fail_generation else "completed")
            return 1 if name in fail_generation else 0
        calls.append((name, "evaluate"))
        if name in fail_evaluation:
            (output / "test.log").write_text("network / process failure")
            return 2
        fake_reports(output, name)
        return 0
    monkeypatch.setattr(batch, "launch", launch)
    return calls


def contents(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def test_all_tasks_are_frozen_complete_and_portable(workspace):
    root, bench, template = workspace
    names = batch.discover(bench)
    assert len(names) == len(set(names)) == 47
    path, manifest = batch.prepare(bench, template, all_tasks=True, exp_id="all-tasks")
    assert [t["id"] for t in manifest["tasks"]] == names
    assert manifest["task_ids"] == names
    assert manifest["protocol_sha256"]
    for task in manifest["tasks"]:
        cfg = read_json(path / task["id"] / "config.local.json")
        assert cfg["rounds"] == 1 and cfg["reasoning"] == "high" and cfg["benchmark"] is None
        assert cfg["player"]["parallel_episodes"] == 1
        assert cfg["player"]["trajectories_per_version"] == 3
        assert not Path(cfg["assets"]["env_file"]).is_absolute()
        assert "DO_NOT_EXPOSE_HIDDEN_TESTS" not in cfg["brief"]
        assert "DO_NOT_READ" not in json.dumps(cfg)
        for file in batch.DOCS:
            assert (path / task["id"] / "task-docs" / file).read_bytes() == (bench / "task" / task["id"] / file).read_bytes()
    assert not (root / "runs").exists()


def test_prepare_and_run_dry_run_are_read_only(workspace):
    root = workspace[0]
    before = contents(root)
    prepare(workspace, dry_run=True)
    assert before == contents(root)
    path, _ = prepare(workspace)
    before = contents(root)
    result = batch.run_batch(path, dry_run=True)
    assert result["counts"] == {"pending": 1}
    assert before == contents(root)
    assert not (path / "run.lock").exists()


@pytest.mark.parametrize("tasks", [("task-00", "task-00"), ("unknown",), ()])
def test_invalid_selection_rejected(workspace, tasks):
    with pytest.raises(ValueError):
        prepare(workspace, tasks=tasks)


def test_all_tasks_and_individual_tasks_are_mutually_exclusive(workspace):
    before = contents(workspace[0])
    with pytest.raises(ValueError, match="--all-tasks"):
        prepare(workspace, all_tasks=True)
    assert contents(workspace[0]) == before


def test_existing_batch_is_not_overwritten(workspace):
    path, _ = prepare(workspace)
    before = contents(path)
    with pytest.raises(ValueError, match="已存在"):
        prepare(workspace)
    assert contents(path) == before


def test_failed_generation_resumes_then_low_score_is_not_retried(workspace, monkeypatch):
    path, manifest = prepare(workspace, tasks=("task-00", "task-01"))
    calls = install_fake_launch(path, manifest, monkeypatch, fail_generation=("task-00",))
    result = batch.run_batch(path)
    assert calls == [("task-00", "start"), ("task-01", "start"), ("task-01", "evaluate")]
    assert result["counts"] == {"failed": 1, "evaluated": 1}
    calls = install_fake_launch(path, manifest, monkeypatch)
    result = batch.run_batch(path)
    assert calls == [("task-00", "resume"), ("task-00", "evaluate")]
    assert result["counts"] == {"evaluated": 2}
    assert result["tasks"][0]["metrics"]["l1"] == {"passed": 1, "total": 2}
    assert result["tasks"][0]["metrics"]["strict_task_success"] is None
    calls.clear()
    batch.run_batch(path)
    assert calls == []
    assert (path / "results.csv").is_file()


def test_failed_evaluation_only_retries_evaluation_and_archives(workspace, monkeypatch):
    path, manifest = prepare(workspace)
    calls = install_fake_launch(path, manifest, monkeypatch, fail_evaluation=("task-00",))
    batch.run_batch(path)
    work, run, output = batch.paths(path, manifest, manifest["tasks"][0])
    assert (work / "evaluations/0001/reports/test.log").read_text() == "network / process failure"
    # Partial official reports left by an interrupted process must be retained but not trusted.
    fake_reports(output, "task-00")
    calls = install_fake_launch(path, manifest, monkeypatch)
    batch.run_batch(path)
    assert calls == [("task-00", "evaluate")]
    assert (work / "evaluations/0002/previous/report-l2.json").is_file()
    assert (work / "evaluations/0001/exit.json").is_file()


@pytest.mark.parametrize("damage", ["missing", "wrong-task", "count", "error"])
def test_incomplete_reports_do_not_count_as_evaluated(workspace, monkeypatch, damage):
    path, manifest = prepare(workspace)
    task = manifest["tasks"][0]
    finish_generation(path, manifest, task)
    install_fake_launch(path, manifest, monkeypatch)
    original = batch.launch
    def broken(*args, **kwargs):
        rc = original(*args, **kwargs)
        _, _, output = batch.paths(path, manifest, task)
        if damage == "missing":
            (output / "report-l2.json").unlink()
        else:
            value = read_json(output / "summary-test.json")
            if damage == "wrong-task":
                value["game"] = "another-game"
            elif damage == "count":
                value["l1"]["passed"] = 999
            else:
                value["l2"]["status"] = "ERROR"
            atomic_json(output / "summary-test.json", value)
        return rc
    monkeypatch.setattr(batch, "launch", broken)
    result = batch.run_batch(path)
    assert result["tasks"][0]["status"] == "ready_to_evaluate"
    assert not (path / "task-00/evaluation.json").exists()


def test_stopped_generation_returning_zero_never_evaluates(workspace, monkeypatch):
    path, manifest = prepare(workspace)
    calls = install_fake_launch(path, manifest, monkeypatch)
    def stopped(command, **kwargs):
        assert kwargs["generation"]
        finish_generation(path, manifest, manifest["tasks"][0], "stopped")
        return 0
    monkeypatch.setattr(batch, "launch", stopped)
    result = batch.run_batch(path)
    assert result["tasks"][0]["status"] == "stopped"
    assert calls == []


def test_stop_does_not_dispatch_next_game(workspace, monkeypatch):
    path, manifest = prepare(workspace, tasks=("task-00", "task-01"))
    install_fake_launch(path, manifest, monkeypatch)
    def stopped(command, **kwargs):
        run, _ = finish_generation(path, manifest, manifest["tasks"][0], "stopped")
        with Store(run).lock():
            batch.stop(path)
            assert (run / "STOP").is_file()
        return 0
    monkeypatch.setattr(batch, "launch", stopped)
    result = batch.run_batch(path)
    assert result["counts"] == {"stopped": 1, "pending": 1}


def test_duplicate_controller_and_active_task_are_protected(workspace, monkeypatch):
    path, manifest = prepare(workspace)
    calls = install_fake_launch(path, manifest, monkeypatch)
    with Store(path).lock():
        with pytest.raises(RuntimeError, match="active controller"):
            batch.run_batch(path)
    with batch.task_lock(path / "task-00/task.lock"):
        assert batch.run_batch(path)["counts"] == {"active": 1}
    assert calls == []


def test_uncertain_assets_are_not_resubmitted(workspace, monkeypatch):
    path, manifest = prepare(workspace)
    finish_generation(path, manifest, manifest["tasks"][0], "needs_reconciliation")
    calls = install_fake_launch(path, manifest, monkeypatch)
    assert batch.run_batch(path)["counts"] == {"needs_reconciliation": 1}
    assert calls == []


@pytest.mark.parametrize("target", ["source", "report", "submission"])
def test_changed_evidence_is_rejected(workspace, monkeypatch, target):
    path, manifest = prepare(workspace)
    install_fake_launch(path, manifest, monkeypatch)
    batch.run_batch(path)
    work, run, output = batch.paths(path, manifest, manifest["tasks"][0])
    if target == "source":
        (run / "rounds/0000/integrate-test/project/index.html").write_text("modified")
    elif target == "report":
        (work / "evaluations/0001/reports/report-l2.json").write_text("{}")
    else:
        # An interrupted evaluation has no accepted receipt; never overwrite its altered game.
        (work / "evaluation.json").unlink()
        (output / "index.html").write_text("modified")
        result = batch.run_batch(path)
        assert "改变" in result["tasks"][0]["last_attempt"]["error"]
        assert (output / "index.html").read_text() == "modified"
        return
    assert batch.inspect_task(path, manifest, manifest["tasks"][0])["status"] == "invalid"


def test_config_docs_and_code_drift_are_rejected(workspace, monkeypatch):
    path, manifest = prepare(workspace)
    monkeypatch.setattr(batch, "code_identity", lambda: {"files_sha256": "changed"})
    with pytest.raises(ValueError, match="代码已改变"):
        batch.run_batch(path, dry_run=True)
    doc = path / "task-00/task-docs/target.md"
    doc.write_text("changed")
    assert batch.inspect_task(path, manifest, manifest["tasks"][0])["status"] == "invalid"
    doc.write_bytes((workspace[1] / "task/task-00/target.md").read_bytes())
    cfg = read_json(path / "task-00/config.local.json")
    cfg["rounds"] = 2
    atomic_json(path / "task-00/config.local.json", cfg)
    assert batch.inspect_task(path, manifest, manifest["tasks"][0])["status"] == "invalid"


def test_image_identity_changes_are_rejected_without_starting_containers(workspace, monkeypatch):
    path, manifest = prepare(workspace)
    monkeypatch.setattr(batch.shutil, "which", lambda p: p)
    calls = []
    def inspect(command, **kwargs):
        assert command[:3] == ["docker", "image", "inspect"]
        calls.append(command)
        return json.dumps([{"Id": "sha256:one", "Architecture": "amd64", "Os": "linux"}])
    monkeypatch.setattr(batch.subprocess, "check_output", inspect)
    batch.preflight(path, manifest, docker=True)
    assert len(calls) == 3
    original = read_json(path / "environment.json")
    monkeypatch.setattr(batch.subprocess, "check_output", lambda *a, **k: json.dumps([
        {"Id": "sha256:two", "Architecture": "amd64", "Os": "linux"}]))
    with pytest.raises(ValueError, match="镜像"):
        batch.preflight(path, manifest, docker=True)
    assert read_json(path / "environment.json") == original


def test_status_does_not_rewrite_evidence_or_usage(workspace, monkeypatch):
    path, manifest = prepare(workspace)
    install_fake_launch(path, manifest, monkeypatch)
    batch.run_batch(path)
    before = contents(workspace[0])
    assert batch.main(["status", str(path)]) == 0
    assert contents(workspace[0]) == before


def test_all_tasks_cli_dry_run_is_read_only(workspace):
    before = contents(workspace[0])
    assert batch.main(["prepare", "--all-tasks", "--bench", str(workspace[1]),
                       "--config", str(workspace[2]), "--dry-run"]) == 0
    assert contents(workspace[0]) == before


def test_launcher_streams_and_child_inherits_task_lease(tmp_path):
    # Only a local two-line Python fixture, not a model/game/evaluator subprocess.
    lease_path = tmp_path / "task.lock"
    with batch.task_lock(lease_path) as lease:
        program = "import fcntl, os; fcntl.fcntl(%d, fcntl.F_GETFD); print('fixture-ok', flush=True)" % lease
        assert batch.launch([sys.executable, "-c", program], cwd=tmp_path, log=tmp_path / "log",
                            batch=tmp_path, run=tmp_path / "unused", lease=lease) == 0
    assert not batch.active(lease_path)
    assert (tmp_path / "log").read_text() == "fixture-ok\n"
