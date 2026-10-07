"""Read-only planning and mocked Player/scorer wiring; no Docker, model or judge."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

from game_agent_lite.store import atomic_json, read_json, version

spec = importlib.util.spec_from_file_location("retest", Path(__file__).resolve().parents[1] /
                                             "scripts/retest_gamecraft_player.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


@pytest.fixture
def setup(tmp_path, config, monkeypatch):
    run, bench, output = (tmp_path / n for n in ("original", "benchmark", "new results"))
    for name in ("tasks/example/instruction.md", "tasks/example/tests/rubric.json",
                 "gamecraft_bench/verifier/replay.py"):
        path = bench / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture")
    run.mkdir()
    config.update(engine="godot", entry="project.godot", backend="harbor", reasoning="xhigh")
    # New re-tests override old parallel/resource settings without rewriting history.
    config["player"]["parallel_episodes"] = 3
    config["runtime"]["render_threads"] = 1
    atomic_json(run / "config.json", config)
    for number in range(3):
        folder = run / f"rounds/{number:04d}"
        project = folder / "integrate-fixture/project"
        project.mkdir(parents=True)
        (project / "project.godot").write_text(f"; round {number}")
        atomic_json(folder / "check.json", {"passed": True, "version": version(project),
                                           "project": str(project.relative_to(run))})
    env = tmp_path / "judge.env"
    env.write_text("SECRET_TEST_VALUE")
    args = [str(run), "--task", "example", "--rounds", "0", "1", "2",
            "--benchmark", str(bench), "--output", str(output), "--env-file", str(env),
            "--evidence-source", "official_replay"]
    def forbidden(*a, **kw):
        raise AssertionError("No real process, Player or model call allowed")
    monkeypatch.setattr(module.subprocess, "Popen", forbidden)
    monkeypatch.setattr(module, "Agent", forbidden)
    monkeypatch.setattr(module, "PolicySession", forbidden)
    return run, output, args


def original_bytes(root):
    return {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def test_dry_run_all_rounds_is_read_only(setup, capsys):
    run, output, args = setup
    before = original_bytes(run)
    assert module.main(args + ["--score", "--dry-run"]) == 0
    assert not output.exists()
    assert original_bytes(run) == before
    text = capsys.readouterr().out
    assert '"reasoning": "high"' in text
    assert '"max_demo_seconds": 20' in text
    assert '"parallel_episodes": 1' in text
    assert '"render_threads": 4' in text
    assert "SECRET_TEST_VALUE" not in text


@pytest.mark.parametrize("invalid", ["changed", "failed_build", "duplicate", "negative", "output", "inside_run"])
def test_invalid_inputs_never_launch(setup, invalid):
    run, output, args = setup
    receipt = run / "rounds/0000/check.json"
    if invalid == "changed":
        (run / read_json(receipt)["project"] / "project.godot").write_text("changed")
    elif invalid == "failed_build":
        atomic_json(receipt, {**read_json(receipt), "passed": False})
    elif invalid in ("duplicate", "negative"):
        args[args.index("--rounds") + 1] = "1" if invalid == "duplicate" else "-1"
    elif invalid == "inside_run":
        args[args.index("--output") + 1] = str(run / "new")
    else:
        output.mkdir()
    with pytest.raises(SystemExit) as exc:
        module.main(args + ["--dry-run"])
    assert exc.value.code == 2


@pytest.mark.parametrize("flag,value", [("--parallel-episodes", "0"), ("--render-threads", "0"),
                                        ("--render-threads", "-1"), ("--render-threads", "1.5")])
def test_invalid_execution_settings_never_launch(setup, flag, value):
    _, output, args = setup
    with pytest.raises(SystemExit) as exc:
        module.main(args + [flag, value, "--dry-run"])
    assert exc.value.code == 2 and not output.exists()


def fake_player(monkeypatch, *, fail_round=None, native=False, parallel=1, render_threads=4):
    calls = []
    class Session:
        def __init__(self, config, project, job, expected, factory, **kw):
            self.number, self.job, self.expected = kw["context"]["round"], job, expected
            assert config["player"]["trajectories_per_version"] == 3
            assert config["player"]["parallel_episodes"] == parallel
            assert config["runtime"]["render_threads"] == render_threads
            assert config["benchmark"]["task"] == "example"
            assert config["benchmark"]["evidence_source"] == ("player_recording" if native else "official_replay")
            assert config["reasoning"] == "high"
            calls.append(self.number)

        def run(self, agent):
            if self.number == fail_round:
                raise ValueError("Fixture Player failure")
            self.job.mkdir(parents=True)
            atomic_json(self.job / "gamecraft.json", {"status": "recorded" if native else "completed"})
            atomic_json(self.job / ("gamecraft/recordings/manifest.json" if native else "gamecraft/submission/manifest.json"), {
                "round": self.number, "source_version": self.expected})
            return {"version": self.expected}
    monkeypatch.setattr(module, "PolicySession", Session)
    monkeypatch.setattr(module, "Agent", lambda c: object())
    monkeypatch.setattr(module, "valid_policy_play", lambda *a: True)
    monkeypatch.setattr(module, "validate_gamecraft_submission", lambda p:
                        (p / "game", read_json(p / "manifest.json"), "fixture"))
    monkeypatch.setattr(module, "validate_recordings", lambda p:
                        (p / "game", read_json(p / "manifest.json"), "fixture"))
    return calls


def test_explicit_resource_settings_do_not_change_episode_count(setup, monkeypatch):
    run, output, args = setup
    before = original_bytes(run)
    calls = fake_player(monkeypatch, parallel=2, render_threads=6)
    assert module.main(args + ["--parallel-episodes", "2", "--render-threads", "6"]) == 0
    plan = read_json(output / "plan.json")
    assert (plan["episodes_per_round"], plan["parallel_episodes"], plan["render_threads"]) == (3, 2, 6)
    assert calls == [0, 1, 2] and original_bytes(run) == before


def test_new_player_submission_scored_for_each_round(setup, monkeypatch):
    run, output, args = setup
    before = original_bytes(run)
    calls = fake_player(monkeypatch)
    scoring = []
    def score(argv, **kwargs):
        assert "--project" not in argv and "--submission" in argv
        submission = Path(argv[argv.index("--submission") + 1])
        assert submission.is_relative_to(output)
        scoring.append(submission)
        target = Path(argv[argv.index("--output") + 1])
        atomic_json(target / "evaluation.json", {"official_score": .5})
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(module.subprocess, "run", score)
    assert module.main(args + ["--score"]) == 0
    assert calls == [0, 1, 2] and len(scoring) == 3
    assert [r["official_score"] for r in read_json(output / "results.json")] == [.5] * 3
    assert original_bytes(run) == before
    with pytest.raises(SystemExit):
        module.main(args + ["--score"])  # Cannot overwrite/repeat a completed paid attempt.


def test_default_native_recordings_scored_for_each_round(setup, monkeypatch):
    run, output, args = setup
    args = args[:-2]  # No explicit mode: new re-tests default to native recording.
    before = original_bytes(run)
    calls = fake_player(monkeypatch, native=True)
    scoring = []
    def score(argv, **kwargs):
        assert "--project" not in argv and "--submission" not in argv and "--recordings" in argv
        bundle = Path(argv[argv.index("--recordings") + 1])
        assert bundle.is_relative_to(output) and bundle.name == "recordings"
        scoring.append(bundle)
        target = Path(argv[argv.index("--output") + 1])
        atomic_json(target / "evaluation.json", {"recording_score": .6, "official_score": None})
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(module.subprocess, "run", score)
    assert module.main(args + ["--score"]) == 0
    assert calls == [0, 1, 2] and len(scoring) == 3
    results = read_json(output / "results.json")
    assert [r["recording_score"] for r in results] == [.6] * 3
    assert all(r["official_score"] is None and r["evidence_source"] == "player_recording" for r in results)
    assert original_bytes(run) == before


def test_failure_retained_and_remaining_rounds_still_run_without_scoring(setup, monkeypatch):
    _, output, args = setup
    calls = fake_player(monkeypatch, fail_round=1)
    assert module.main(args) == 1
    assert calls == [0, 1, 2]
    results = read_json(output / "results.json")
    assert [r["status"] for r in results] == ["player_completed", "failed", "player_completed"]
    assert results[1]["error"].endswith("Fixture Player failure")


def test_invalid_new_player_evidence_is_not_scored(setup, monkeypatch):
    _, output, args = setup
    fake_player(monkeypatch)
    monkeypatch.setattr(module, "valid_policy_play", lambda *a: False)
    calls = []
    monkeypatch.setattr(module.subprocess, "run", lambda *a, **kw: calls.append(a))
    assert module.main(args + ["--score"]) == 1
    assert calls == []
    assert all(r["status"] == "failed" for r in read_json(output / "results.json"))
