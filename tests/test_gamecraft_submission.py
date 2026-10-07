"""Saved-input export -> scoring handoff, entirely offline with synthetic artifacts."""
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from game_agent_lite import gamecraft
from game_agent_lite.store import artifact, atomic_json, file_hash, read_json, version
from test_gamecraft_export import saved_run, all_bytes, edit_episode
from test_gamecraft_prepare import native_run, prepared_recording

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/score_gamecraft.py"
spec = importlib.util.spec_from_file_location("score_gamecraft", SCRIPT)
scoring = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scoring)


def forbidden(*args, **kwargs):
    raise AssertionError("Real subprocesses, games, Docker and models must not start in these tests")


@pytest.fixture(autouse=True)
def no_processes(monkeypatch):
    for name in ("Popen", "run", "check_output"):
        monkeypatch.setattr(subprocess, name, forbidden)


@pytest.fixture
def submission(saved_run, tmp_path):
    return gamecraft.export_gamecraft(saved_run, tmp_path / "export with spaces", episodes=[1, 3])


@pytest.fixture
def score_args(tmp_path):
    benchmark = tmp_path / "benchmark"
    rubric = benchmark / "tasks/racing-drift-circuit/tests/rubric.json"
    rubric.parent.mkdir(parents=True)
    rubric.write_text("{}")
    cli = benchmark / "gamecraft_bench/verifier/cli.py"
    cli.parent.mkdir(parents=True)
    cli.write_text("raise AssertionError('Fixture verifier must never execute')\n")
    return ["--task", "racing-drift-circuit", "--benchmark", str(benchmark),
            "--output", str(tmp_path / "score output")]


def reseal(submission, manifest, trace=None):
    """Recompute digests to test protocol validation independently of hash validation."""
    if trace is not None:
        path = submission / manifest["demos"][0]["trace"]["path"]
        atomic_json(path, trace)
        manifest["demos"][0]["trace"] = artifact(path, submission)
    manifest["exported_project_version"] = version(submission / "game")
    atomic_json(submission / "manifest.json", manifest)


def test_bundle_is_portable_and_validation_is_read_only(submission, saved_run, tmp_path):
    before = all_bytes(saved_run)
    moved = tmp_path / "another machine" / "bundle"
    shutil.copytree(submission, moved)
    # Source paths are informational, not dependencies of scoring.
    saved_run.rename(tmp_path / "original run moved")
    bundle_before = all_bytes(moved)
    project, manifest, digest = gamecraft.validate_gamecraft_submission(moved)
    assert project == moved / "game"
    assert [d["episode"] for d in manifest["demos"]] == [1, 3]
    assert digest == file_hash(moved / "manifest.json")
    assert all_bytes(moved) == bundle_before
    assert all_bytes(tmp_path / "original run moved") == before


def test_old_v1_bundles_remain_usable(submission):
    manifest = read_json(submission / "manifest.json")
    manifest.pop("time_basis")
    for demo in manifest["demos"]:
        for field in ("source_video", "export_range", "initial_state_check"):
            demo.pop(field)
    atomic_json(submission / "manifest.json", manifest)
    assert gamecraft.validate_gamecraft_submission(submission)[1] == manifest


@pytest.mark.parametrize("path", ["game.gd", "assets/tile.png", "demo_outputs/0001.json"])
def test_changed_project_assets_or_trace_are_rejected(submission, path):
    (submission / "game" / path).write_text("changed")
    with pytest.raises(ValueError, match="changed"):
        gamecraft.validate_gamecraft_submission(submission)


def test_trace_hash_is_checked_separately(submission):
    manifest = read_json(submission / "manifest.json")
    manifest["demos"][0]["trace"]["sha256"] = "0" * 64
    atomic_json(submission / "manifest.json", manifest)
    with pytest.raises(ValueError, match="artifact"):
        gamecraft.validate_gamecraft_submission(submission)


@pytest.mark.parametrize("change", ["extra", "missing", "duplicate_episode", "duplicate_file"])
def test_demo_list_must_exactly_match_manifest(submission, change):
    manifest = read_json(submission / "manifest.json")
    if change == "extra":
        (submission / "game/demo_outputs/old.json").write_text("{}")
    elif change == "missing":
        manifest["demos"].pop()
    elif change == "duplicate_episode":
        manifest["demos"][1]["episode"] = 1
    else:
        manifest["demos"][1]["trace"] = manifest["demos"][0]["trace"]
    reseal(submission, manifest)
    with pytest.raises(ValueError, match="match|duplicate"):
        gamecraft.validate_gamecraft_submission(submission)


@pytest.mark.parametrize("path", ["../outside.json", "/tmp/outside.json", "game/../outside.json",
                                  "game/demo_outputs/../0001.json", "game\\demo_outputs\\0001.json"])
def test_trace_paths_stay_in_bundle(submission, path):
    manifest = read_json(submission / "manifest.json")
    manifest["demos"][0]["trace"]["path"] = path
    atomic_json(submission / "manifest.json", manifest)
    with pytest.raises(ValueError, match="relative"):
        gamecraft.validate_gamecraft_submission(submission)


@pytest.mark.parametrize("field,value", [("format", "unknown"), ("engine", "html"), ("fps", 60),
                                        ("viewport", [960, 540]), ("demos", []), ("round", True)])
def test_unsupported_manifest_rejected(submission, field, value):
    manifest = read_json(submission / "manifest.json")
    manifest[field] = value
    atomic_json(submission / "manifest.json", manifest)
    with pytest.raises(ValueError, match="schema"):
        gamecraft.validate_gamecraft_submission(submission)


@pytest.mark.parametrize("event", [
    {"frame": -1, "type": "wait"}, {"frame": True, "type": "wait"},
    {"frame": 37, "type": "wait"}, {"frame": 3, "type": "scroll"},
    {"frame": 3, "type": "key_down", "keycode": "F12"},
    {"frame": 3, "type": "mouse_move", "x": 1280, "y": 0},
    {"frame": 3, "type": "mouse_down", "x": 0, "y": 0, "button": "middle"},
])
def test_invalid_event_rejected_even_with_updated_hashes(submission, event):
    manifest = read_json(submission / "manifest.json")
    trace = {"duration_frames": 36, "events": [event]}
    reseal(submission, manifest, trace)
    with pytest.raises(ValueError, match="schema|duration_frames"):
        gamecraft.validate_gamecraft_submission(submission)


@pytest.mark.parametrize("change", ["order", "duration", "scenario", "range", "clipped"])
def test_time_and_scenario_metadata_are_consistent(submission, change):
    manifest = read_json(submission / "manifest.json")
    trace = read_json(submission / manifest["demos"][0]["trace"]["path"])
    if change == "order":
        trace["events"].reverse()
    elif change == "duration":
        trace["duration_frames"] = 40
    elif change == "scenario":
        trace["scenario"] = "different_start"
    elif change == "range":
        manifest["demos"][0]["export_range"]["start_seconds"] = 1
    else:
        manifest["demos"][0]["clipped"] = True
    reseal(submission, manifest, trace)
    with pytest.raises(ValueError, match="ordered|match"):
        gamecraft.validate_gamecraft_submission(submission)


def test_submission_dry_run_validates_without_launch_or_output(submission, score_args, tmp_path, capsys):
    before = all_bytes(submission)
    assert scoring.main([*score_args, "--submission", str(submission), "--dry-run"]) == 0
    output = capsys.readouterr()
    data = json.loads(output.out[output.out.index("{\n"):])
    assert data["submission"]["episodes"] == [1, 3]
    assert data["submission"]["goal_verified"] is None
    assert f"type=bind,source={submission / 'game'},target=/submission,readonly" in data["command"]
    assert "unverified" in output.err
    assert not (tmp_path / "score output").exists()
    assert all_bytes(submission) == before


def test_player_recorded_image_and_task_are_used_for_scoring(submission, score_args, capsys):
    manifest = read_json(submission / "manifest.json")
    image_id = "sha256:" + "a" * 64
    manifest.update(replay_environment={"image_id": image_id}, benchmark_task="racing-drift-circuit")
    atomic_json(submission / "manifest.json", manifest)
    assert scoring.main([*score_args, "--submission", str(submission), "--dry-run"]) == 0
    output = capsys.readouterr()
    data = json.loads(output.out[output.out.index("{\n"):])
    assert image_id in data["command"]
    assert data["submission"]["recorded_replay_image"] == image_id
    assert scoring.main([*score_args, "--submission", str(submission), "--image", "different:tag", "--dry-run"]) == 0
    assert "overrides" in capsys.readouterr().err
    manifest["benchmark_task"] = "puzzle-fixture"
    atomic_json(submission / "manifest.json", manifest)
    with pytest.raises(SystemExit):
        scoring.main([*score_args, "--submission", str(submission), "--dry-run"])


def test_time_limited_player_bundle_reaches_scoring_without_replay(native_run, prepared_recording, score_args, tmp_path, capsys):
    bundle = gamecraft.export_gamecraft(native_run, tmp_path / "deadline-bundle", prepared_dir=prepared_recording)
    before = all_bytes(prepared_recording)
    assert scoring.main([*score_args, "--submission", str(bundle), "--dry-run"]) == 0
    output = capsys.readouterr()
    data = json.loads(output.out[output.out.index("{\n"):])
    assert data["submission"]["episodes"] == [1, 2, 3]
    assert data["submission"]["goal_verified"] is None
    assert "prefixes" in output.err
    assert not (tmp_path / "score output").exists() and all_bytes(prepared_recording) == before


@pytest.mark.parametrize("sources", [[], ["--project", "a", "--submission", "b"]])
def test_cli_requires_exactly_one_source(score_args, sources):
    with pytest.raises(SystemExit) as exc:
        scoring.main([*score_args, *sources, "--dry-run"])
    assert exc.value.code == 2


def test_invalid_submission_never_launches_or_creates_output(submission, score_args, tmp_path, capsys):
    (submission / "game/assets/tile.png").write_text("changed")
    with pytest.raises(SystemExit) as exc:
        scoring.main([*score_args, "--submission", str(submission), "--smoke-test"])
    assert exc.value.code == 2
    assert "Invalid --submission" in capsys.readouterr().err
    assert not (tmp_path / "score output").exists()


def test_output_must_be_outside_submission(submission, score_args):
    with pytest.raises(SystemExit) as exc:
        scoring.main([*score_args, "--submission", str(submission), "--output", str(submission / "score"),
                      "--dry-run"])
    assert exc.value.code == 2


@pytest.mark.parametrize("contents", [None, "{bad json", "[]"])
def test_missing_or_malformed_manifest_is_a_cli_error(submission, score_args, contents):
    path = submission / "manifest.json"
    if contents is None:
        path.unlink()
    else:
        path.write_text(contents)
    with pytest.raises(SystemExit) as exc:
        scoring.main([*score_args, "--submission", str(submission), "--dry-run"])
    assert exc.value.code == 2


@pytest.mark.parametrize("changed", ["game", "manifest"])
def test_changes_during_setup_stop_before_container_launch(submission, score_args, tmp_path, monkeypatch, changed):
    def inspect_image(*args, **kwargs):
        if changed == "game":
            (submission / "game/game.gd").write_text("changed during setup")
        else:
            path = submission / "manifest.json"
            path.write_text(path.read_text() + "\n")
        return '{"Id":"fixture","Architecture":"arm64"}'
    def git_only(command, **kwargs):
        assert command[0] == "git", "Must reject changes before launching the container"
        return SimpleNamespace(returncode=0, stdout="fixture\n")
    monkeypatch.setattr(subprocess, "check_output", inspect_image)
    monkeypatch.setattr(subprocess, "run", git_only)
    with pytest.raises(SystemExit) as exc:
        scoring.main([*score_args, "--submission", str(submission), "--smoke-test"])
    assert exc.value.code == 2
    assert not (tmp_path / "score output").exists()


@pytest.mark.parametrize("smoke", [True, False])
def test_scoring_mounts_export_and_saves_exact_provenance(submission, saved_run, score_args, tmp_path, monkeypatch, smoke):
    before, source_before = all_bytes(submission), all_bytes(saved_run)
    calls = []
    def fake_run(command, **kwargs):
        calls.append(command)
        if command[0] == "git":
            return SimpleNamespace(returncode=0, stdout="fixture-commit\n")
        assert command[:2] == ["docker", "run"]
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(subprocess, "check_output", lambda command, **kwargs: json.dumps({
        "Id": "sha256:fixture-image", "Architecture": "arm64"}))
    env = tmp_path / "judge.env"
    env.write_text("OPENAI_API_KEY=synthetic-key-never-sent\n")
    args = [*score_args, "--submission", str(submission), "--env-file", str(env)]
    assert scoring.main(args + (["--smoke-test"] if smoke else [])) == 0
    command = calls[-1]
    assert f"type=bind,source={submission / 'game'},target=/submission,readonly" in command
    assert ("--network" in command) == smoke
    assert ("--env-file" in command) != smoke
    if smoke:
        assert command[command.index("--network") + 1] == "none"
    output = tmp_path / "score output"
    metadata = read_json(output / "host-manifest.json")
    assert metadata["submission"]["episodes"] == [1, 3]
    assert metadata["submission"]["round"] == 0
    assert metadata["submission"]["task"] == "racing-drift-circuit"
    assert metadata["submission"]["goal_verified"] is None
    assert metadata["submission"]["manifest_sha256"] == file_hash(output / "submission-manifest.json")
    assert (output / "submission-manifest.json").read_bytes() == (submission / "manifest.json").read_bytes()
    assert "synthetic-key" not in (output / "host-manifest.json").read_text()
    assert all_bytes(submission) == before and all_bytes(saved_run) == source_before


def test_legacy_project_entry_remains_usable(submission, score_args, tmp_path, monkeypatch):
    monkeypatch.setattr(subprocess, "check_output", lambda *a, **kw: '{"Id":"fixture","Architecture":"arm64"}')
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=0, stdout="fixture\n"))
    assert scoring.main([*score_args, "--project", str(submission / "game"), "--smoke-test"]) == 0
    assert "submission" not in read_json(tmp_path / "score output/host-manifest.json")
    assert not (tmp_path / "score output/submission-manifest.json").exists()


def test_clipped_submission_is_explicitly_reported(saved_run, tmp_path, score_args, capsys):
    def later_end(rows):
        rows[-1]["time_seconds"] = 26
    edit_episode(saved_run, edit_rows=later_end, edit_result=lambda e: e.update(elapsed_seconds=25))
    bundle = gamecraft.export_gamecraft(saved_run, tmp_path / "prefix", episodes=[1], clip_long=True)
    assert scoring.main([*score_args, "--submission", str(bundle), "--dry-run"]) == 0
    assert "prefixes" in capsys.readouterr().err
