"""Offline exporter tests. All saved gameplay evidence below is synthetic fixture data."""
import json
import subprocess

import pytest

from game_agent_lite import gamecraft
from game_agent_lite.cli import main
from game_agent_lite.store import artifact, atomic_json, file_hash, read_json, version


def action(keys, pointer=None):
    return {"keys": keys, "duration_ms": 100, "pointer": pointer}


@pytest.fixture
def saved_run(tmp_path):
    root = tmp_path / "run"
    directory = root / "rounds/0000"
    project = directory / "integrate-fixture/project"
    project.mkdir(parents=True)
    (project / "project.godot").write_text('config_version=5\n[application]\nconfig/name="Fixture"\n')
    (project / "game.gd").write_text("extends Node\n")
    (project / "assets").mkdir()
    (project / "assets/tile.png").write_bytes(b"fixture asset")
    (project / "demo_outputs").mkdir()
    (project / "demo_outputs/old.json").write_text("{}")
    (project / ".env.hunyuan").write_text("HUNYUAN_API_KEY=must-not-export")
    (project / ".godot").mkdir()
    (project / ".godot/cache").write_text("cache")
    job = directory / "jobs/player-candidate-fixture"
    job.mkdir(parents=True)
    (job / "policy.py").write_text("raise RuntimeError('export must never execute this file')\n")
    atomic_json(job / "plan.json", {"summary": "fixture", "episodes": [
        {"profile": f"profile_{i}", "purpose": f"purpose {i}"} for i in range(3)]})
    plan = read_json(job / "plan.json")
    identity = version(project)
    report = {"format": "policy-v1", "version": identity, "settings": {"engine": "godot", "seed": 42},
              "policy": artifact(job / "policy.py", job), "plan": artifact(job / "plan.json", job),
              "trajectories_per_version": 3, "episodes": []}
    for i in range(3):
        folder = job / f"episodes/{i+1:04d}/attempt-fixture"
        folder.mkdir(parents=True)
        rows = []
        state = {"status": "playing", "x": 0}
        def row(kind, when, **data):
            rows.append({"type": kind, "time_seconds": when, "version": identity,
                         "policy_sha256": report["policy"]["sha256"], **data})
        row("initial", .05, state=state)
        actions = [action(["ArrowRight"]), action(["ArrowRight", "Space"]),
                   action(["Space"]), action([])]
        for index, (start, input_action) in enumerate(zip([.1, .3, .6, .9], actions)):
            row("input", start+.001, index=index, action=input_action, before=state, reason="fixture")
            after = {"status": "playing", "x": index+1}
            row("feedback", start+.15, index=index, action=input_action, before=state, after=after,
                action_start_seconds=start)
            state = after
        row("end", 1.8, status="completed", reason="test objective ended")
        trace = folder / "trajectory.jsonl"
        trace.write_text("\n".join(json.dumps(r) for r in rows)+"\n")
        video = folder / "play.webm"
        video.write_bytes(b"synthetic recorded video fixture")
        episode = {"version": identity, "policy_sha256": report["policy"]["sha256"], "index": i,
                   **plan["episodes"][i], "seed": 42+i, "steps": 4, "status": "completed",
                   "reason": "test objective ended", "elapsed_seconds": 1.2,
                   "trajectory": artifact(trace, job), "video": {"origin": "engine", "artifact": artifact(video, job)}}
        atomic_json(folder / "result.json", episode)
        report["episodes"].append(artifact(folder / "result.json", job))
    atomic_json(job / "play.json", report)
    atomic_json(directory / "check.json", {"passed": True, "version": identity,
                                          "project": project.relative_to(root).as_posix()})
    atomic_json(directory / "played-candidate.json", {
        "report": (job / "play.json").relative_to(root).as_posix(), "sha256": file_hash(job / "play.json")})
    atomic_json(root / "state.json", {"status": "running", "round": 1, "phase": "design", "best": {
        "project": project.relative_to(root).as_posix(), "version": identity, "round": 0,
        "play": (job / "play.json").relative_to(root).as_posix()}})
    atomic_json(root / "config.json", {"engine": "godot", "backend": "harbor"})
    return root


def saved(root):
    directory = root / "rounds/0000"
    job = directory / "jobs/player-candidate-fixture"
    return directory, job, read_json(job / "play.json")


def edit_episode(root, index=0, *, edit_rows=None, edit_result=None):
    directory, job, report = saved(root)
    result_path = job / report["episodes"][index]["path"]
    result = read_json(result_path)
    if edit_rows:
        trace = job / result["trajectory"]["path"]
        rows = [json.loads(line) for line in trace.read_text().splitlines()]
        edit_rows(rows)
        trace.write_text("\n".join(json.dumps(r) for r in rows)+"\n")
        result["trajectory"] = artifact(trace, job)
    if edit_result:
        edit_result(result)
    atomic_json(result_path, result)
    report["episodes"][index] = artifact(result_path, job)
    save_report(root, report)


def save_report(root, report):
    directory, job, _ = saved(root)
    atomic_json(job / "play.json", report)
    receipt = read_json(directory / "played-candidate.json")
    receipt["sha256"] = file_hash(job / "play.json")
    atomic_json(directory / "played-candidate.json", receipt)


def all_bytes(root):
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def test_export_preserves_originals_and_never_executes_anything(saved_run, tmp_path, monkeypatch):
    before = all_bytes(saved_run)
    def forbidden(*args, **kwargs):
        raise AssertionError("Exporter must not start any subprocess")
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    dest = gamecraft.export_gamecraft(saved_run, tmp_path / "submission")
    assert all_bytes(saved_run) == before
    manifest = read_json(dest / "manifest.json")
    assert manifest["replay_verified"] is False and manifest["official_score"] is None
    assert manifest["time_basis"] == "wall_clock_since_agent_reset"
    assert all(d["initial_state_check"] == "unverified" for d in manifest["demos"])
    assert manifest["demos"][0]["export_range"] == {
        "kind": "full_episode", "start_seconds": 0.0, "end_seconds": 1.2}
    video = manifest["demos"][0]["source_video"]
    assert file_hash(saved_run / video["path"]) == video["sha256"]
    assert manifest["fps"] == 30 and manifest["viewport"] == [1280, 720]
    assert len(manifest["demos"]) == 3 and manifest["replaced_demo_outputs_in_copy"]
    assert not (dest / "game/.env.hunyuan").exists() and not (dest / "game/.godot").exists()
    assert not list(dest.rglob("policy.py")) and not list(dest.rglob("trajectory.jsonl"))
    assert sorted(p.name for p in (dest / "game/demo_outputs").iterdir()) == ["0001.json", "0002.json", "0003.json"]
    assert (dest / "game/assets/tile.png").read_bytes() == b"fixture asset"
    trace = read_json(dest / "game/demo_outputs/0001.json")
    assert trace == {"duration_frames": 36, "events": [
        {"frame": 3, "type": "key_down", "keycode": "RIGHT"},
        {"frame": 9, "type": "key_down", "keycode": "SPACE"},
        {"frame": 18, "type": "key_up", "keycode": "RIGHT"},
        {"frame": 27, "type": "key_up", "keycode": "SPACE"},
        {"frame": 36, "type": "wait"}]}
    assert manifest["exported_project_version"] == version(dest / "game")


def test_mouse_hold_coordinate_mapping_and_final_key_release(saved_run, tmp_path):
    def edit(rows):
        for row in rows:
            if row.get("index") == 1:
                row["action"]["pointer"] = {"x": .5, "y": .25, "button": "right"}
            if row.get("index") == 2:
                row["action"]["pointer"] = {"x": 1, "y": 1, "button": "none"}
            if row.get("index") == 3:
                row["action"]["keys"] = ["KeyW", "w", "Control"]
    edit_episode(saved_run, edit_rows=edit)
    dest = gamecraft.export_gamecraft(saved_run, tmp_path / "export", episodes=[1])
    events = read_json(dest / "game/demo_outputs/0001.json")["events"]
    assert {"frame": 9, "type": "mouse_down", "button": "right", "x": 640, "y": 180} in events
    assert {"frame": 12, "type": "mouse_up", "button": "right", "x": 640, "y": 180} in events
    assert {"frame": 18, "type": "mouse_move", "x": 1279, "y": 719} in events
    assert len([e for e in events if e["type"] == "key_down" and e["keycode"] == "W"]) == 1
    assert {"frame": 36, "type": "key_up", "keycode": "W"} in events
    assert {"frame": 36, "type": "key_up", "keycode": "CTRL"} in events


def test_release_then_repress_survives_frame_quantization(saved_run, tmp_path):
    def edit(rows):
        for row in rows:
            if row.get("index") == 0:
                row["action"]["keys"] = ["Space"]
            if row.get("index") == 1:
                row["action"]["keys"] = []
                row["time_seconds"] = .300 if row["type"] == "input" else .303
                if row["type"] == "feedback": row["action_start_seconds"] = .300
            if row.get("index") == 2:
                row["time_seconds"] = .310 if row["type"] == "input" else .45
                if row["type"] == "feedback": row["action_start_seconds"] = .310
    edit_episode(saved_run, edit_rows=edit)
    dest = gamecraft.export_gamecraft(saved_run, tmp_path / "export", episodes=[1])
    events = read_json(dest / "game/demo_outputs/0001.json")["events"]
    same_frame = [e["type"] for e in events if e["frame"] == 9]
    assert same_frame == ["key_up", "key_down"]


def test_long_episode_requires_explicit_clip_and_preserves_initial_wait(saved_run, tmp_path):
    def edit(rows):
        for row in rows:
            if row["type"] not in ("initial", "end"):
                row["time_seconds"] += 19.6
                if row["type"] == "feedback": row["action_start_seconds"] += 19.6
            if row["type"] == "end": row["time_seconds"] = 26
    edit_episode(saved_run, edit_rows=edit, edit_result=lambda d: d.update(elapsed_seconds=25))
    out = tmp_path / "export"
    with pytest.raises(ValueError, match="clip-long"):
        gamecraft.export_gamecraft(saved_run, out, episodes=[1])
    assert not out.exists()
    gamecraft.export_gamecraft(saved_run, out, episodes=[1], clip_long=True)
    trace = read_json(out / "game/demo_outputs/0001.json")
    assert trace["duration_frames"] == 600
    assert trace["events"][0]["frame"] == 591
    assert {"frame": 600, "type": "key_up", "keycode": "RIGHT"} in trace["events"]
    assert all(e["frame"] <= 600 for e in trace["events"])
    demo = read_json(out / "manifest.json")["demos"][0]
    assert demo["clipped"] and demo["exported_steps"] == 2 and demo["source_steps"] == 4
    assert demo["export_range"] == {"kind": "prefix", "start_seconds": 0.0, "end_seconds": 20}
    assert gamecraft.validate_gamecraft_submission(out)[1]["demos"][0] == demo


def test_round_selection_and_scenario_are_explicit_and_do_not_edit_game(saved_run, tmp_path):
    state = read_json(saved_run / "state.json")
    state["best"] = None
    atomic_json(saved_run / "state.json", state)
    with pytest.raises(ValueError, match="No best"):
        gamecraft.export_gamecraft(saved_run, tmp_path / "no-best")
    dest = gamecraft.export_gamecraft(saved_run, tmp_path / "export", round_number=0,
                                     episodes=[3, 1], scenarios=["3=late_game"])
    assert read_json(dest / "game/demo_outputs/0003.json")["scenario"] == "late_game"
    assert "scenario" not in read_json(dest / "game/demo_outputs/0001.json")
    assert [d["episode"] for d in read_json(dest / "manifest.json")["demos"]] == [1, 3]
    assert gamecraft.validate_gamecraft_submission(dest)[0] == dest / "game"


@pytest.mark.parametrize("selected", [[], [0], [4], [1, 1], [True], list(range(1, 12))])
def test_episode_selection_is_not_silently_filtered(saved_run, tmp_path, selected):
    with pytest.raises(ValueError, match="episodes"):
        gamecraft.export_gamecraft(saved_run, tmp_path / "export", episodes=selected)


@pytest.mark.parametrize("scenarios", [["1"], ["1=../evil"], ["2=foo"], ["1=a", "1=b"]])
def test_bad_scenario_mapping_rejected(saved_run, tmp_path, scenarios):
    with pytest.raises(ValueError, match="scenario|Scenario"):
        gamecraft.export_gamecraft(saved_run, tmp_path / "export", episodes=[1], scenarios=scenarios)


@pytest.mark.parametrize("field,value", [("engine", "html"), ("backend", "mock")])
def test_no_html_or_simulated_exports(saved_run, tmp_path, field, value):
    config = read_json(saved_run / "config.json")
    config[field] = value
    atomic_json(saved_run / "config.json", config)
    with pytest.raises(ValueError, match="real Godot"):
        gamecraft.export_gamecraft(saved_run, tmp_path / "export")


@pytest.mark.parametrize("kind", ["policy", "trajectory", "video", "missing_video", "project", "report", "cross_version", "pending",
                                  "wrong_state", "clock", "nan", "unsupported", "failed", "mock_video"])
def test_corrupt_or_incomplete_evidence_cannot_be_exported(saved_run, tmp_path, kind):
    directory, job, report = saved(saved_run)
    episode = read_json(job / report["episodes"][0]["path"])
    if kind in ("policy", "trajectory", "video", "missing_video"):
        item = (report["policy"] if kind == "policy" else episode["trajectory"] if kind == "trajectory"
                else episode["video"]["artifact"])
        (job / item["path"]).write_text("changed")
        if kind == "missing_video":
            (job / item["path"]).unlink()
    elif kind == "project":
        (saved_run / read_json(directory / "check.json")["project"] / "game.gd").write_text("changed")
    elif kind == "report":
        (job / "play.json").write_text("{}")
    elif kind == "cross_version":
        edit_episode(saved_run, edit_rows=lambda rows: rows[2].update(version="another game"))
    elif kind == "pending":
        edit_episode(saved_run, edit_rows=lambda rows: rows.pop(-2))
    elif kind == "wrong_state":
        edit_episode(saved_run, edit_rows=lambda rows: rows[2].update(before={"x": 999}))
    elif kind == "clock":
        edit_episode(saved_run, edit_rows=lambda rows: rows[2].update(action_start_seconds=99))
    elif kind == "nan":
        edit_episode(saved_run, edit_rows=lambda rows: rows[2].update(time_seconds=float("nan")))
    elif kind == "unsupported":
        def edit(rows):
            for row in rows:
                if row.get("index") == 0: row["action"]["keys"] = ["F12"]
        edit_episode(saved_run, edit_rows=edit)
    else:
        edit_episode(saved_run, edit_result=lambda d: d.update(
            status="failed") if kind == "failed" else d.update(video={"origin": "mock"}))
    with pytest.raises(ValueError):
        gamecraft.export_gamecraft(saved_run, tmp_path / "export")
    assert not (tmp_path / "export").exists()


def test_failed_episode_is_not_hidden_but_another_can_be_selected(saved_run, tmp_path):
    edit_episode(saved_run, edit_result=lambda d: d.update(status="failed"))
    with pytest.raises(ValueError, match="incomplete"):
        gamecraft.export_gamecraft(saved_run, tmp_path / "all")
    gamecraft.export_gamecraft(saved_run, tmp_path / "selected", episodes=[2])
    assert [d["episode"] for d in read_json(tmp_path / "selected/manifest.json")["demos"]] == [2]


def test_workspace_escape_and_output_overwrite_are_rejected(saved_run, tmp_path):
    from tests.conftest import require_symlinks
    require_symlinks(tmp_path)
    with pytest.raises(ValueError, match="outside"):
        gamecraft.export_gamecraft(saved_run, saved_run / "export")
    out = tmp_path / "existing"
    out.mkdir()
    (out / "keep").write_text("do not replace")
    with pytest.raises(FileExistsError):
        gamecraft.export_gamecraft(saved_run, out)
    link = tmp_path / "link"
    link.symlink_to(tmp_path / "not-created")
    with pytest.raises(FileExistsError):
        gamecraft.export_gamecraft(saved_run, link)
    directory, _, _ = saved(saved_run)
    receipt = read_json(directory / "played-candidate.json")
    receipt["report"] = "../outside.json"
    atomic_json(directory / "played-candidate.json", receipt)
    with pytest.raises(ValueError, match="artifact"):
        gamecraft.export_gamecraft(saved_run, tmp_path / "escaped")


def test_export_failure_cleans_staging_and_leaves_source_untouched(saved_run, tmp_path, monkeypatch):
    before = all_bytes(saved_run)
    original = gamecraft.snapshot
    def changed_copy(source, target):
        original(source, target)
        (target / "game.gd").write_text("inconsistent copy")
    monkeypatch.setattr(gamecraft, "snapshot", changed_copy)
    with pytest.raises(ValueError, match="changed"):
        gamecraft.export_gamecraft(saved_run, tmp_path / "export")
    assert not (tmp_path / "export").exists() and not list(tmp_path.glob(".gamecraft-export-*"))
    assert all_bytes(saved_run) == before


def test_cli_export_does_not_start_loop(saved_run, tmp_path, monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        raise AssertionError("Must not instantiate rollout loop")
    monkeypatch.setattr("game_agent_lite.cli.Loop", forbidden)
    assert main(["export-gamecraft", str(saved_run), "--output", str(tmp_path / "cli"),
                 "--round", "0", "--episode", "2", "--scenario", "2=main"]) == 0
    assert "replay_verified=false" in capsys.readouterr().out
    assert read_json(tmp_path / "cli/game/demo_outputs/0002.json")["scenario"] == "main"
    assert main(["export-gamecraft", str(saved_run), "--output", str(tmp_path / "cli")]) == 1
