"""Preparation wiring with synthetic engine artifacts; never calls a model/judge."""
from copy import deepcopy
import json
import shutil
import subprocess

import pytest

from game_agent_lite import gamecraft_prepare as module
from game_agent_lite.agent import Agent
from game_agent_lite.cli import main
from game_agent_lite.game import MockGame
from game_agent_lite.gamecraft import export_gamecraft, validate_gamecraft_submission
from game_agent_lite.store import artifact, atomic_json, read_json
from game_agent_lite.types import DEFAULTS
from test_gamecraft_export import saved_run, saved, save_report, all_bytes


@pytest.fixture
def native_run(saved_run):
    _, job, report = saved(saved_run)
    (job / "policy.gd").write_text('extends RefCounted\n')
    report["policy"] = artifact(job / "policy.gd", job)
    report["settings"]["policy_backend"] = "godot"
    save_report(saved_run, report)
    config = deepcopy(DEFAULTS)
    config.update(engine="godot", backend="codex", brief="fixture", entry="project.godot")
    atomic_json(saved_run / "config.json", config)
    return saved_run


def forbidden(*a, **kw):
    raise AssertionError("No model, real engine, judge or process permitted")


def test_dry_run_is_read_only_and_scopes_limit(native_run, tmp_path, monkeypatch):
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(Agent, "call", forbidden)
    before = all_bytes(native_run)
    output = tmp_path / "demo"
    result = module.prepare_gamecraft(native_run, output, episodes=[2], scenarios=["2=battle"], dry_run=True)
    assert result["execution"] == {"purpose": "gamecraft_demo", "max_seconds": 20,
        "fresh_launch": True, "viewport": [1280, 720], "scenarios": {"profile_1": "battle"}}
    assert not output.exists() and all_bytes(native_run) == before
    assert main(["prepare-gamecraft", str(native_run), "--output", str(output), "--dry-run"]) == 0


@pytest.mark.parametrize("limited", [False, True])
def test_prepare_preserves_source_and_never_labels_timeout_success(native_run, tmp_path, monkeypatch, limited):
    class FixtureEngine(MockGame):
        configured = []
        reset_calls = 0
        def configure_policy(self, source, **settings):
            type(self).configured.append(settings)
        def reset(self, seed):
            type(self).reset_calls += 1
            super().reset(seed)
        def native_policy_start(self, **settings):
            assert settings["max_seconds"] == 20
            super().native_policy_start(**settings)
        def native_policy_status(self):
            result = super().native_policy_status()
            if limited:
                result.update(status="truncated", reason="benchmark demo time limit reached; target completion not established",
                              elapsed_seconds=20.01)
            return result
        def stop(self):
            video = super().stop()
            video["origin"] = "engine"  # Synthetic protocol fixture, not an actual video.
            return video
    monkeypatch.setattr(module, "make_game", FixtureEngine)
    monkeypatch.setattr("game_agent_lite.policy._extract_frames", lambda *a: [])
    monkeypatch.setattr(Agent, "call", forbidden)
    before = all_bytes(native_run)
    output = tmp_path / "demo"
    result = module.prepare_gamecraft(native_run, output, episodes=[1], scenarios=["1=battle"])
    assert result["replay_verified"] is False
    _, manifest, _ = validate_gamecraft_submission(output / "submission")
    demo = manifest["demos"][0]
    assert demo["scenario"] == "battle"
    assert demo["initial_state_check"] == "fresh_launch_recorded"
    assert demo["duration_frames"] <= 600
    assert demo["source_status"] == ("truncated" if limited else "completed")
    assert demo["goal_verified"] is None
    status = read_json(output / "status.json")
    assert status["ready_for_export"] and status["goal_verified"] is None
    assert status["episodes"][0]["status"] == demo["source_status"]
    assert manifest["time_basis"] == "engine_input_wall_clock"
    assert FixtureEngine.reset_calls == 0  # Never reset the CLI scenario back to the title.
    assert FixtureEngine.configured[0]["viewport"] == [1280, 720]
    assert all_bytes(native_run) == before


@pytest.mark.parametrize("kwargs", [{"episodes": [0]}, {"episodes": [1, 1]},
                                   {"scenarios": ["1=../bad"]}, {"scenarios": ["9=unknown"]}])
def test_invalid_preparation_rejected_before_launch(native_run, tmp_path, monkeypatch, kwargs):
    monkeypatch.setattr(module, "make_game", forbidden)
    with pytest.raises(ValueError):
        module.prepare_gamecraft(native_run, tmp_path / "demo", **kwargs)
    assert not (tmp_path / "demo").exists()


def test_python_policy_requires_explicit_adaptation(native_run, tmp_path):
    _, job, report = saved(native_run)
    report["policy"] = artifact(job / "policy.py", job)
    save_report(native_run, report)
    with pytest.raises(ValueError, match="legacy Python"):
        module.prepare_gamecraft(native_run, tmp_path / "demo", dry_run=True)


def edit_prepared(prepared, *, edit_rows=None, edit_result=None):
    job = prepared / "player"
    report = read_json(job / "play.json")
    path = job / report["episodes"][0]["path"]
    episode = read_json(path)
    if edit_rows:
        trace = job / episode["trajectory"]["path"]
        rows = [json.loads(line) for line in trace.read_text().splitlines()]
        edit_rows(rows)
        trace.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
        episode["trajectory"] = artifact(trace, job)
    if edit_result:
        edit_result(episode)
    atomic_json(path, episode)
    report["episodes"][0] = artifact(path, job)
    atomic_json(job / "play.json", report)


@pytest.fixture
def prepared_recording(native_run, tmp_path):
    """A previously saved deadline stop, including a final partial key/mouse hold."""
    _, source_job, report = saved(native_run)
    prepared = tmp_path / "old preparation"
    job = prepared / "player"
    shutil.copytree(source_job, job)
    execution = {"purpose": "gamecraft_demo", "max_seconds": 20, "fresh_launch": True,
                 "viewport": [1280, 720], "scenarios": {}}
    report["settings"]["execution"] = execution
    reason = "benchmark demo time limit reached; target completion not established"
    for ref in report["episodes"]:
        path = job / ref["path"]
        episode = read_json(path)
        trace = job / episode["trajectory"]["path"]
        rows = [json.loads(line) for line in trace.read_text().splitlines()]
        for row in rows:
            row["policy_sha256"] = report["policy"]["sha256"]
            if row.get("index") == 3:
                row["action"].update(keys=["ArrowRight"], pointer={"x": .5, "y": .25, "button": "left"})
                row["time_seconds"] = 19.951 if row["type"] == "input" else 20.006
                if row["type"] == "feedback":
                    row.update(action_start_seconds=19.95, pointer_release_seconds=20.005)
            if row["type"] == "end":
                row.update(time_seconds=20.1, status="truncated", reason=reason)
        trace.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
        episode.update(status="truncated", reason=reason, elapsed_seconds=20.01,
                       policy_sha256=report["policy"]["sha256"], policy_backend="godot",
                       fresh_launch=True, scenario=None, trajectory=artifact(trace, job))
        atomic_json(path, episode)
        ref.update(artifact(path, job))
    atomic_json(job / "play.json", report)
    atomic_json(prepared / "preparation.json", {"round": 0, "source_version": report["version"],
        "source_policy_sha256": report["policy"]["sha256"], "source_episodes": [1, 2, 3], "execution": execution})
    atomic_json(prepared / "status.json", {"ready_for_export": False})
    state = read_json(native_run / "state.json")
    state["best"] = None
    atomic_json(native_run / "state.json", state)
    return prepared


def test_export_existing_deadline_evidence_without_replaying(native_run, prepared_recording, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(Agent, "call", forbidden)
    monkeypatch.setattr(module, "make_game", forbidden)
    before, evidence_before = all_bytes(native_run), all_bytes(prepared_recording)
    output = tmp_path / "submission"
    assert main(["export-gamecraft", str(native_run), "--prepared-dir", str(prepared_recording),
                 "--output", str(output)]) == 0  # Round inferred despite best=null.
    assert "retained as truncated" in capsys.readouterr().out
    _, manifest, _ = validate_gamecraft_submission(output)
    assert manifest["replay_verified"] is False and manifest["official_score"] is None
    assert len(manifest["demos"]) == 3
    for demo in manifest["demos"]:
        assert demo["source_status"] == "truncated" and demo["goal_verified"] is None
        assert demo["source_elapsed_seconds"] == 20.01 and demo["demo_time_limit_seconds"] == 20
        assert demo["duration_frames"] == 600 and demo["clipped"]
        assert demo["source_steps"] == demo["exported_steps"] == 4
        assert demo["initial_state_check"] == "fresh_launch_recorded"
        trace = read_json(output / demo["trace"]["path"])
        events = trace["events"]
        assert {"frame": 599, "type": "key_down", "keycode": "RIGHT"} in events
        assert {"frame": 600, "type": "key_up", "keycode": "RIGHT"} in events
        assert {"frame": 600, "type": "mouse_up", "button": "left", "x": 640, "y": 180} in events
        assert events[-1] == {"frame": 600, "type": "wait"}
        assert all(e["frame"] <= 600 for e in events)
    assert all_bytes(native_run) == before and all_bytes(prepared_recording) == evidence_before


@pytest.mark.parametrize("change", ["failed", "interrupted", "action_limit", "end_status", "end_reason", "pending",
    "after_deadline", "trace_hash", "video", "source_version", "policy", "episode_mapping", "execution", "scenario"])
def test_deadline_export_keeps_integrity_and_failure_checks(native_run, prepared_recording, tmp_path, change):
    job = prepared_recording / "player"
    if change in ("failed", "interrupted"):
        edit_prepared(prepared_recording, edit_result=lambda e: e.update(status=change))
    elif change == "action_limit":
        edit_prepared(prepared_recording, edit_result=lambda e: e.update(reason="configured action limit reached"))
    elif change in ("end_status", "end_reason", "pending", "after_deadline"):
        def edit(rows):
            if change == "end_status": rows[-1]["status"] = "completed"
            if change == "end_reason": rows[-1]["reason"] = "stop requested"
            if change == "pending": rows.pop(-2)
            if change == "after_deadline":
                rows[-3]["time_seconds"] = 20.002
                rows[-2]["action_start_seconds"] = 20.001
        edit_prepared(prepared_recording, edit_rows=edit)
    elif change == "trace_hash":
        next(job.glob("episodes/0001/*/trajectory.jsonl")).write_text("changed")
    elif change == "video":
        next(job.glob("episodes/0001/*/play.webm")).unlink()
    elif change == "scenario":
        edit_prepared(prepared_recording, edit_result=lambda e: e.update(scenario="unrecorded"))
    else:
        metadata = read_json(prepared_recording / "preparation.json")
        if change == "source_version": metadata["source_version"] = "0" * 64
        if change == "policy": metadata["source_policy_sha256"] = "0" * 64
        if change == "episode_mapping": metadata["source_episodes"] = [3, 2, 1]
        if change == "execution": metadata["execution"]["purpose"] = "normal_player"
        atomic_json(prepared_recording / "preparation.json", metadata)
    out = tmp_path / "rejected"
    before = all_bytes(prepared_recording)
    with pytest.raises(ValueError):
        export_gamecraft(native_run, out, prepared_dir=prepared_recording)
    assert not out.exists() and all_bytes(prepared_recording) == before


def test_prepared_episode_selection_and_output_boundary(native_run, prepared_recording, tmp_path):
    output = export_gamecraft(native_run, tmp_path / "subset", prepared_dir=prepared_recording, episodes=[3, 1])
    assert [d["source_episode"] for d in validate_gamecraft_submission(output)[1]["demos"]] == [1, 3]
    with pytest.raises(ValueError, match="round"):
        export_gamecraft(native_run, tmp_path / "wrong-round", prepared_dir=prepared_recording, round_number=1)
    with pytest.raises(ValueError, match="evidence directory"):
        export_gamecraft(native_run, prepared_recording / "player/export", prepared_dir=prepared_recording)


@pytest.mark.parametrize("field,value", [("source_status", "completed"), ("stop_reason", "stop requested"),
    ("goal_verified", True), ("demo_time_limit_seconds", None)])
def test_submission_cannot_mislabel_deadline_stop(native_run, prepared_recording, tmp_path, field, value):
    output = export_gamecraft(native_run, tmp_path / "export", prepared_dir=prepared_recording)
    manifest = read_json(output / "manifest.json")
    manifest["demos"][0][field] = value
    atomic_json(output / "manifest.json", manifest)
    with pytest.raises(ValueError):
        validate_gamecraft_submission(output)


def test_release_pacing_is_explicit_and_dry_run_only(native_run, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(Agent, "call", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    output = tmp_path / "new-demo"
    assert main(["prepare-gamecraft", str(native_run), "--output", str(output),
                 "--episode", "1", "--min-key-release-ms", "120", "--dry-run"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["execution"]["min_key_release_ms"] == 120
    assert plan["execution"]["max_seconds"] == 20
    assert not output.exists()
    default = module.prepare_gamecraft(native_run, output, dry_run=True)
    assert "min_key_release_ms" not in default["execution"]


@pytest.mark.parametrize("value", [-1, 2001, 12.5, True])
def test_invalid_release_pacing_rejected_before_execution(native_run, tmp_path, monkeypatch, value):
    monkeypatch.setattr(module, "make_game", forbidden)
    with pytest.raises(ValueError, match="min_key_release_ms"):
        module.prepare_gamecraft(native_run, tmp_path / "demo", min_key_release_ms=value)
