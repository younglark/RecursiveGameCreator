"""Accounting against local fixtures only; no models, engines or paid evaluation."""
from concurrent.futures import ThreadPoolExecutor
import csv
import json
from pathlib import Path
import shutil

import pytest

from game_agent_lite import usage
from game_agent_lite.agent import Agent, MockAgent
from game_agent_lite.cli import main
from game_agent_lite.loop import Loop
from game_agent_lite.store import Store, atomic_json, read_json


def job(root, name="builder-a", *, number=0, stage="draft", role="builder", tokens=None, completed=True):
    path = root / f"rounds/{number:04d}/jobs" / name
    atomic_json(path / "request.json", {"role": role, "model": "fixture"})
    atomic_json(path / "io/input.json", {"stage": stage})
    if completed:
        atomic_json(path / "result.json", {"usage": None})
    if tokens is not None:
        atomic_json(path / "jobs/role/trial/result.json", {
            "id": name, "agent_result": tokens})
    return path


def counts(inputs=100, cached=60, outputs=20):
    return {"n_input_tokens": inputs, "n_cache_tokens": cached, "n_output_tokens": outputs,
            "cost_usd": None}


def test_harbor_uses_one_source_and_excludes_copied_evidence(tmp_path):
    path = job(tmp_path, tokens=counts())
    atomic_json(path / "jobs/role/trial/agent/trajectory.json", {
        "final_metrics": {"total_prompt_tokens": 100, "total_cached_tokens": 60,
                          "total_completion_tokens": 20}})
    shutil.copytree(path, tmp_path / "rounds/0000/jobs/reviewer-x/io/A/copied-job")
    summary = usage.refresh(tmp_path)
    assert summary["total"]["calls"] == 1
    assert summary["total"]["total_tokens"] == 120  # not 180; not doubled by trajectory
    assert summary["total"]["cost_usd"] is None
    assert summary["calls"][0]["sources"] == ["jobs/role/trial/result.json"]


def test_fallback_uses_last_session_snapshot_not_sum(tmp_path):
    path = job(tmp_path, completed=False)
    log = path / "jobs/role/trial/agent/sessions/session.jsonl"
    log.parent.mkdir(parents=True)
    log.write_text('\n'.join(json.dumps(event) for event in [
        {"type": "session_meta", "payload": {"id": "session1"}},
        *({"type": "event_msg", "payload": {"type": "token_count", "info": {
            "total_token_usage": {"input_tokens": n, "cached_input_tokens": 0, "output_tokens": 5}}}}
          for n in (10, 30))]))
    summary = usage.summarize(tmp_path)
    assert summary["total"]["known_total_tokens"] == 35
    assert summary["total"]["usage_status"] == "partial"
    assert summary["calls"][0]["call_status"] == "incomplete"


def test_local_turn_usage_sums_turns_and_preserves_known_partial(tmp_path):
    path = job(tmp_path)
    events = [{"type": "turn.completed", "usage": value} for value in (
        {"input_tokens": 100, "cached_input_tokens": 50, "output_tokens": 20},
        {"input_tokens": 30, "cached_input_tokens": 10, "output_tokens": 10})]
    (path / "process.log").write_text('\n'.join(map(json.dumps, events)))
    assert usage.summarize(tmp_path)["total"]["total_tokens"] == 160
    events.append({"type": "turn.completed", "usage": None})
    (path / "process.log").write_text('\n'.join(map(json.dumps, events)))
    summary = usage.summarize(tmp_path)
    assert summary["total"]["known_total_tokens"] == 160
    assert summary["total"]["usage_status"] == "partial"


def test_unknown_call_is_not_zero_and_invalid_counts_are_rejected(tmp_path):
    job(tmp_path, tokens=counts())
    bad = job(tmp_path, "builder-b", tokens={}) / "jobs/role/trial/result.json"
    bad.write_text(json.dumps({"agent_result": {"n_input_tokens": -1, "n_cache_tokens": True,
                         "n_output_tokens": float("nan"), "cost_usd": float("inf")}}))
    summary = usage.summarize(tmp_path)["total"]
    assert summary["total_tokens"] is None and summary["known_total_tokens"] == 120
    assert summary["unknown_calls"] == 1 and summary["usage_status"] == "partial"
    assert summary["cost_usd"] is None
    assert usage.normalize({"input_tokens": 10, "cached_input_tokens": 11})["cached_input_tokens"] is None


def test_stage_round_retry_model_and_csv_totals_are_idempotent(tmp_path):
    for name, stage, number, completed in (("builder-a", "draft", 0, False),
            ("builder-b", "draft", 0, True), ("builder-c", "integrate", 0, True),
            ("player-incumbent/analysis-a", "play", 1, True)):
        job(tmp_path, name, number=number, stage=stage, tokens=counts(),
            role="player" if number else "builder", completed=completed)
    phase = usage.refresh(tmp_path)
    assert phase["total"]["known_total_tokens"] == 480
    assert [(s["phase"], s["known_total_tokens"]) for s in phase["stages"]] == [
        ("draft", 240), ("integrate", 120), ("play", 120)]
    assert [r["known_total_tokens"] for r in phase["rounds"]] == [360, 120]
    assert usage.refresh(tmp_path)["total"] == phase["total"]
    with (tmp_path / "usage-rounds.csv").open() as stream:
        rows = list(csv.DictReader(stream))
    assert [r["known_total_tokens"] for r in rows] == ["360", "120"]
    assert rows[0]["cost_usd"] == ""
    assert phase["by_model"][0]["model"] == "fixture"


def test_failed_call_preserves_original_error_and_usage(tmp_path, config):
    config["backend"] = "codex"
    Path(config["auth_file"]).write_text('{}')
    original = RuntimeError("original failure")
    class Runner:
        def run(self, argv, cwd, log, **kwargs):
            log.write_text(json.dumps({"type": "turn.completed", "usage": {
                "input_tokens": 15, "cached_input_tokens": 5, "output_tokens": 3}}))
            raise original
    path = tmp_path / "rounds/0000/jobs/builder-a"
    with pytest.raises(RuntimeError) as exc:
        Agent(config, Runner()).call("builder", path, {"stage": "draft"})
    assert exc.value is original
    saved = read_json(path / "usage.json")
    assert saved["tokens"]["total_tokens"] == 18 and saved["call_status"] == "failed"
    assert saved["reasoning"] == config["reasoning"]


def test_broken_accounting_does_not_change_agent_output(tmp_path, config, monkeypatch):
    config["backend"] = "codex"
    Path(config["auth_file"]).write_text('{}')
    class Runner:
        def run(self, argv, cwd, log, **kwargs):
            atomic_json(cwd / "result.json", {"summary": "unchanged", "checks": [], "remaining": []})
            log.write_text('')
    def broken(*args, **kwargs):
        raise OSError('accounting storage failure')
    monkeypatch.setattr(usage, "atomic_json", broken)
    assert Agent(config, Runner()).call("builder", tmp_path / "job", {}) == {"summary": "unchanged", "checks": [], "remaining": []}
    assert usage.refresh(tmp_path) is None


def test_concurrent_receipts_are_separate_and_retest_excludes_original(tmp_path):
    original = tmp_path / "original"
    job(original, tokens=counts(1000, 500, 20))
    root = tmp_path / "retest"
    def write(number):
        path = root / f"round-{number:04d}/player/author-abc"
        atomic_json(path / "request.json", {"role": "player", "model": "fixture"})
        (path / "process.log").write_text(json.dumps({"type": "turn.completed", "usage": {
            "input_tokens": 10, "cached_input_tokens": 0, "output_tokens": 2}}))
        usage.capture(path, {"backend": "codex", "reasoning": "high"},
                      "player", {"phase": "policy_author"}, "completed")
    with ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(write, range(3)))
    result = usage.refresh(root, scope="player_retest")
    assert result["total"]["total_tokens"] == 36 and len(result["rounds"]) == 3
    assert all(c["scope"] == "player_retest" for c in result["calls"])
    assert not (original / "usage.json").exists()


@pytest.mark.parametrize("benchmark", [None, {"name": "gamecraft", "task": "fixture", "path": "/unused"}])
def test_report_backfills_all_games_without_changing_source(tmp_path, config, benchmark):
    config["benchmark"] = benchmark
    store = Store(tmp_path / "run")
    store.create(config)
    path = job(store.root, tokens=counts())
    request = (path / "request.json").read_bytes()
    assert main(["report", str(store.root)]) == 0
    assert (store.root / "usage-stages.csv").is_file()
    assert (store.root / "usage-rounds.csv").is_file()
    assert "120" in (store.root / "report.md").read_text()
    assert (path / "request.json").read_bytes() == request
    assert not (path / "usage.json").exists()  # backfill doesn't rewrite old call receipts


def test_summary_reference_is_read_only(tmp_path):
    assert usage.saved_reference(tmp_path)["total_tokens"] is None
    assert not list(tmp_path.iterdir())
    job(tmp_path, tokens=counts())
    usage.refresh(tmp_path)
    before = {p: p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    assert usage.saved_reference(tmp_path)["total_tokens"] == 120
    assert before == {p: p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}


def test_loop_events_rounds_and_resume_keep_decisions_unchanged(tmp_path, config):
    class MeteredFixture(MockAgent):
        def call(self, role, path, context, **kwargs):
            result = super().call(role, path, context, **kwargs)
            atomic_json(path / "request.json", {"role": role, "model": "fixture"})
            (path / "process.log").write_text(json.dumps({"type": "turn.completed", "usage": {
                "input_tokens": 10, "cached_input_tokens": 5, "output_tokens": 2}}))
            usage.capture(path, {"backend": "codex", "reasoning": "high"}, role, context, "completed")
            return result
    store = Store(tmp_path / "run")
    store.create(config)
    agent = MeteredFixture(["B", "A"])
    loop = Loop(store, config, agent=agent)
    state = loop.run()
    assert state["status"] == "completed" and all(item["promoted"] for item in state["history"])
    summary = read_json(store.root / "usage.json")
    assert summary["total"]["total_tokens"] == len(agent.calls) * 12
    events = [json.loads(line) for line in (store.root / "events.jsonl").read_text().splitlines()]
    last_stage = [e for e in events if e["kind"] == "stage_completed"][-1]
    assert last_stage["round"] == 1 and last_stage["phase"] == "select"
    assert last_stage["usage"]["total"]["total_tokens"] == summary["total"]["total_tokens"]
    assert sum(r["total_tokens"] for r in summary["rounds"]) == summary["total"]["total_tokens"]
    calls = list(agent.calls)
    loop.run(resume=True)
    assert agent.calls == calls
    assert usage.refresh(store.root)["total"] == summary["total"]


def test_gameasg_summary_references_common_accounting(tmp_path, config, monkeypatch):
    import importlib.util
    import sys
    script = Path(__file__).resolve().parents[1] / "validation/run_gameasg_pilot.py"
    if not script.is_file():
        pytest.skip("Local GameASG helper is not shipped in the package")
    spec = importlib.util.spec_from_file_location("usage_asg_fixture", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    bench = tmp_path / "benchmark"
    for name in module.DOCS:
        file = bench / "task/fixture" / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text('fixture')
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(module, "BENCH", bench)
    monkeypatch.setattr(module, "make_config", lambda *a: (config, {"fixture": "fixture"}))
    monkeypatch.setattr(module.subprocess, "run", lambda *a, **k: None)  # image inspection only
    def fake_task(task, exp_id, batch):
        run = tmp_path / "runs/gameasg" / exp_id / task
        job(run, tokens=counts())
        return {"task": task, "status": "evaluated", "evaluation": {"fixture_score": 1}}
    monkeypatch.setattr(module, "run_task", fake_task)
    monkeypatch.setattr(sys, "argv", [str(script), "--tasks", "fixture", "--exp-id", "usage-fixture"])
    assert module.main() == 0
    root = tmp_path / "validation/gameasg/usage-fixture"
    result = read_json(root / "results.json")[0]
    assert result["usage"]["total_tokens"] == 120
    assert result["evaluation"] == {"fixture_score": 1}
    assert (root / result["run"] / result["usage"]["report"]).is_file()
