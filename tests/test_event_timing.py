"""Readable event times and per-attempt durations; models and games are Mock."""
from datetime import datetime
import json
import sys
import time

import pytest

from game_agent_lite import loop as loop_module
from game_agent_lite import store as store_module
from game_agent_lite.loop import Loop
from game_agent_lite.store import Store, Stopped, UncertainSubmission


def events(store):
    return [json.loads(line) for line in (store.root / "events.jsonl").read_text().splitlines()]


def test_timestamp_timezone_legacy_events_and_console_redaction(tmp_path, config, monkeypatch, capsys):
    store = Store(tmp_path / "run")
    store.create(config)
    legacy = '{"time": 1.0, "kind": "stage_completed", "phase": "design"}\n'
    (store.root / "events.jsonl").write_text(legacy)
    try:
        with monkeypatch.context() as patch:
            patch.setenv("TZ", "Asia/Shanghai")
            time.tzset()
            patch.setattr(store_module.time, "time", lambda: 1789788666.885053)
            store.event("stage_completed", round=0, phase="draft", duration_seconds=2499.363)
            store.event("stage_failed", round=0, phase="assets", duration_seconds=0.125,
                        error="RuntimeError: Bearer timing-secret")
    finally:
        time.tzset()
    log = (store.root / "events.jsonl").read_text()
    assert log.startswith(legacy)
    row = events(store)[1]
    assert row["time"] == 1789788666.885053
    assert row["timestamp"] == "2026-09-19 11:31:06+08:00"
    assert datetime.fromisoformat(row["timestamp"]).timestamp() == int(row["time"])
    output = capsys.readouterr().out
    assert "[2026-09-19 11:31:06+08:00] round=0 draft 完成，本次耗时 41分39秒" in output
    assert "本次耗时 0.12秒" in output
    assert "timing-secret" not in output + log
    assert "[REDACTED]" in output


def test_lost_console_does_not_fail_event_persistence(tmp_path, config, monkeypatch):
    store = Store(tmp_path / "run")
    store.create(config)
    class ClosedPipe:
        def write(self, text):
            raise BrokenPipeError("reader closed")
        def flush(self):
            raise BrokenPipeError("reader closed")
    monkeypatch.setattr(sys, "stdout", ClosedPipe())
    store.event("stage_completed", round=0, phase="draft", duration_seconds=3700)
    assert events(store)[0]["duration_seconds"] == 3700


@pytest.mark.parametrize("failure, status, event_kind", [
    (Stopped, "stopped", "stage_stopped"),
    (KeyboardInterrupt, "stopped", "stage_stopped"),
    (RuntimeError, "failed", "stage_failed"),
    (UncertainSubmission, "needs_reconciliation", "stage_blocked"),
])
def test_attempt_timing_excludes_pause_and_preserves_round_ids(
        tmp_path, config, monkeypatch, failure, status, event_kind):
    clock = {"monotonic": 10.0, "wall": 1789785967.243181}
    monkeypatch.setattr(loop_module, "monotonic", lambda: clock["monotonic"])
    monkeypatch.setattr(store_module.time, "time", lambda: clock["wall"])
    store = Store(tmp_path / "run")
    store.create(config)

    class TimedLoop(Loop):
        interrupted = False
        def stage(self, phase, state, directory):
            clock["monotonic"] += 65.25
            # A wall-clock adjustment must not distort stage duration.
            clock["wall"] -= 600
            if phase == "draft" and not self.interrupted:
                self.interrupted = True
                raise failure("fixture interruption")
            return super().stage(phase, state, directory)

    loop = TimedLoop(store, config)
    first = loop.run()
    assert first["status"] == status and first["phase"] == "draft"
    interrupted = [e for e in events(store) if e["kind"] == event_kind]
    assert len(interrupted) == 1
    assert interrupted[0]["round"] == 0 and interrupted[0]["phase"] == "draft"
    assert interrupted[0]["duration_seconds"] == 65.25
    clock["monotonic"] += 86400  # Overnight pause before an explicit resume.
    resumed = loop.run(resume=True)
    assert resumed["status"] == "completed"
    rows = events(store)
    completed = [e for e in rows if e["kind"] == "stage_completed"]
    assert len(completed) == 16  # All eight stages, across two rounds.
    assert all(e["duration_seconds"] == 65.25 for e in completed)
    assert [e["round"] for e in completed if e["phase"] == "select"] == [0, 1]
    assert len([e for e in rows if e["kind"] == "stage_started"
                and e["phase"] == "draft" and e["round"] == 0]) == 2
    assert [e["status"] for e in rows if e["kind"] == "run_state"] == [status, "completed"]


def test_stop_between_stages_does_not_invent_a_stage_duration(tmp_path, config):
    store = Store(tmp_path / "run")
    store.create(config)
    store.request_stop()
    assert Loop(store, config).run()["status"] == "stopped"
    assert [e["kind"] for e in events(store)] == ["run_state"]
    assert "duration_seconds" not in events(store)[0]
