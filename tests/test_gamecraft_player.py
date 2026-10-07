"""GameCraft Player wiring with synthetic engine/replay artifacts; no rollout or judge."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
from types import ModuleType, SimpleNamespace

import pytest
from jsonschema import ValidationError

from game_agent_lite import gamecraft_player as module
from game_agent_lite.agent import Agent, MockAgent
from game_agent_lite.cli import load_config
from game_agent_lite.game import MockGame, valid_play
from game_agent_lite.gamecraft import validate_gamecraft_submission
from game_agent_lite.policy import PolicySession, export_policy_bundle, recover_policy_analysis
from game_agent_lite.store import Stopped, artifact, atomic_json, read_json, version
from game_agent_lite.types import POLICY_PLAN_SCHEMA, role_schema, validate
from test_gamecraft_export import all_bytes


@pytest.fixture
def configured(config, tmp_path, monkeypatch):
    config.update(engine="godot", entry="project.godot", backend="codex")
    config["player"].update(mode="policy", parallel_episodes=1)
    config["runtime"]["mode"] = "docker"
    config["benchmark"] = {"name": "gamecraft", "task": "puzzle-fixture", "path": str(tmp_path / "bench")}
    def identity(cfg):
        assert cfg["runtime"]["image"] in (module.EVAL_IMAGE, "sha256:" + "a" * 64)
        return {"image_id": "sha256:" + "a" * 64, "protocol": 2, "framework_sha256": "fixture"}
    monkeypatch.setattr("game_agent_lite.runtime.runtime_identity", identity)
    monkeypatch.setattr("game_agent_lite.policy._extract_frames", lambda *a: [])
    def forbidden(*a, **kw):
        raise AssertionError("No real subprocess, Docker, generated game or model is allowed")
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    return config


class FixtureEngine(MockGame):
    def start(self):
        assert self.native_settings["fresh_launch"]
        assert self.native_settings["viewport"] == [1280, 720]
        super().start()

    def reset(self, seed):
        raise AssertionError("Fresh scenario must not be reset")

    def native_policy_start(self, **settings):
        assert settings["max_seconds"] == 20
        super().native_policy_start(**settings)

    def stop(self):
        result = super().stop()
        # Only a serializer fixture, never passed to an actual judge.
        result["origin"] = "engine"
        return result


def new_session(tmp_path, config):
    project = tmp_path / "game"
    project.mkdir()
    (project / "project.godot").write_text("config_version=5\n")
    return PolicySession(config, project, tmp_path / "player", version(project), FixtureEngine,
                         context={"round": 2})


@pytest.fixture
def replay(monkeypatch):
    calls = []
    def run(config, submission, output, image_id, stop):
        calls.append(submission)
        assert image_id == "sha256:" + "a" * 64
        _, manifest, _ = validate_gamecraft_submission(submission)
        output.mkdir(parents=True)
        refs = []
        for demo in manifest["demos"]:
            folder = output / str(demo["episode"])
            folder.mkdir()
            frame, video = folder / "frame.png", folder / "play.mp4"
            frame.write_bytes(b"synthetic frame")
            video.write_bytes(b"synthetic video")
            atomic_json(folder / "result.json", {"episode": demo["episode"], "status": "completed",
                "reason": "fixture replay", "trace_sha256": demo["trace"]["sha256"],
                "duration_seconds": demo["duration_frames"] / 30, "goal_verified": None,
                "frames": [artifact(frame, output)], "video": artifact(video, output), "logs": []})
            refs.append(artifact(folder / "result.json", output))
        atomic_json(output / "results.json", {"episodes": refs, "godot": "fixture"})
    monkeypatch.setattr(module, "replay_submission", run)
    return calls


def test_records_exports_replays_then_analyzes_and_resumes(configured, tmp_path, replay):
    play = new_session(tmp_path, configured)
    before = all_bytes(play.project)
    class Roles(MockAgent):
        phases = []
        def call(self, role, job, context, **kwargs):
            self.phases.append(context["phase"])
            assert context["benchmark"]["max_seconds"] == 20
            assert "path" not in context["benchmark"]
            if context["phase"] == "policy_analyze":
                assert len(replay) == 1
                summary = read_json(job / "io/evidence/summary.json")
                assert len(summary["gamecraft"]["episodes"]) == 3
                assert summary["gamecraft"]["goal_verified"] is None
                assert len(summary["gamecraft"]["coverage"]) == 3
                assert summary["gamecraft"]["coverage_warnings"] == []
                assert all(e["goal"] == "fixture goal" for e in summary["episodes"])
            return super().call(role, job, context, **kwargs)
    agent = Roles()
    report = play.run(agent)
    assert agent.phases == ["policy_author", "policy_analyze"]
    assert valid_play(report, play.job, play.version)
    manifest = read_json(play.job / "gamecraft/submission/manifest.json")
    assert manifest["round"] == 2 and manifest["replay_environment"]["image_id"] == "sha256:" + "a" * 64
    assert all(d["duration_frames"] <= 600 and d["goal_verified"] is None for d in manifest["demos"])
    goals = read_json(play.job / report["gamecraft_goals"]["path"])
    assert not goals["all_goals_supported"] and not goals["independently_verified"]
    assert all(g["replay"]["outcome"] == "unverified" for g in goals["episodes"])
    coverage = read_json(play.job / report["gamecraft_coverage"]["path"])
    assert coverage["version"] == play.version and coverage["plan_sha256"] == report["plan"]["sha256"]
    assert not coverage["independently_verified"]
    assert all(r["replay"]["status"] == "unverified" for r in coverage["requirements"])
    assert play.run(agent) == report and len(replay) == 1
    assert agent.phases == ["policy_author", "policy_analyze"]
    assert all_bytes(play.project) == before
    # The existing A/B bundle also exposes replay frames without code or policy.
    export_policy_bundle(report, play.job, tmp_path / "comparison")
    assert list((tmp_path / "comparison/replay").rglob("*.png"))
    assert not list((tmp_path / "comparison").rglob("*.gd"))
    next((play.job / "gamecraft/replay").rglob("frame.png")).write_bytes(b"tampered")
    assert not valid_play(report, play.job, play.version)
    with pytest.raises(ValueError, match="changed"):
        play.run(agent)


def test_replay_failure_kept_without_false_success(configured, tmp_path, monkeypatch):
    def unavailable(*a):
        raise RuntimeError("offline fixture engine error")
    monkeypatch.setattr(module, "replay_submission", unavailable)
    play = new_session(tmp_path, configured)
    report = play.run(MockAgent())
    replay = read_json(play.job / report["gamecraft"]["path"])
    assert replay["status"] == "unavailable" and "engine error" in replay["reason"]
    assert replay["goal_verified"] is None
    assert not read_json(play.job / "gamecraft-goals.json")["all_goals_supported"]
    assert len(report["episodes"]) == 3


def test_interrupted_replay_reuses_recordings(configured, tmp_path, replay, monkeypatch):
    original = module.replay_submission
    def interrupted(*args):
        args[2].mkdir(parents=True)
        (args[2] / "partial.log").write_text("interrupted fixture")
        raise Stopped("fixture stop")
    monkeypatch.setattr(module, "replay_submission", interrupted)
    play, agent = new_session(tmp_path, configured), MockAgent()
    with pytest.raises(Stopped):
        play.run(agent)
    before = all_bytes(play.job / "episodes")
    monkeypatch.setattr(module, "replay_submission", original)
    assert play.run(agent)["gamecraft"]
    assert all_bytes(play.job / "episodes") == before
    assert len(list((play.job / "gamecraft").glob("interrupted-replay-*"))) == 1
    assert agent.calls == ["player", "player"]


def test_deadline_is_exportable_but_not_victory(configured, tmp_path, replay):
    class Deadline(FixtureEngine):
        reason = "benchmark demo time limit reached; target completion not established"
        def native_policy_start(self, **settings):
            super().native_policy_start(**settings)
            path = self.native_policy_trace()
            rows = [json.loads(line) for line in path.read_text().splitlines()]
            rows[-1].update(status="truncated", reason=self.reason, time_seconds=20.01)
            path.write_text("".join(json.dumps(r) + "\n" for r in rows))
        def native_policy_status(self):
            return {**super().native_policy_status(), "status": "truncated", "reason": self.reason,
                    "elapsed_seconds": 20.01}
    play = new_session(tmp_path, configured)
    play.game_factory = Deadline
    report = play.run(MockAgent())
    assert valid_play(report, play.job, play.version)
    _, manifest, _ = validate_gamecraft_submission(play.job / "gamecraft/submission")
    assert all(d["duration_frames"] == 600 and d["source_status"] == "truncated" for d in manifest["demos"])
    assert not read_json(play.job / "gamecraft-goals.json")["all_goals_supported"]
    # The normal exporter also understands the marked report, without a prepared-dir workaround.
    from game_agent_lite.gamecraft import export_recorded_report
    dest = tmp_path / "new-export"
    export_recorded_report(play.project, play.job / "play.json", dest, expected=play.version,
                           round_number=2, source_project="fixture")
    assert validate_gamecraft_submission(dest)[1]["demos"][0]["source_status"] == "truncated"


def test_simulated_benchmark_never_starts_replay(configured, tmp_path, replay):
    configured["backend"] = "mock"
    play = new_session(tmp_path, configured)
    report = play.run(MockAgent())
    assert not replay
    assert read_json(play.job / report["gamecraft"]["path"])["status"] == "unavailable"


def test_missing_replay_citation_rejected_in_run_and_recovery(configured, tmp_path, replay):
    class Missing(MockAgent):
        def call(self, role, job, context, **kwargs):
            result = super().call(role, job, context, **kwargs)
            if context["phase"] == "policy_analyze":
                result["evidence"] = [p for p in result["evidence"] if not p.startswith("evidence/replay/")]
                atomic_json(job / "io/result.json", result)
            return result
    play = new_session(tmp_path, configured)
    with pytest.raises(ValueError, match="cite every replay"):
        play.run(Missing())
    with pytest.raises(ValueError, match="cite every replay"):
        recover_policy_analysis(play.job, play.version)


def test_goal_assessments_bind_time_episode_and_side(configured, tmp_path, replay):
    play = new_session(tmp_path, configured)
    report = play.run(MockAgent())
    io = next(play.job.glob("analysis-*/io"))
    rows = []
    for i in range(1, 4):
        rows.append({"episode": i, **{side: {"outcome": "won", "time_seconds": .002,
            "evidence": [next((io / f"evidence/{folder}/{i:04d}").glob("*.png")).relative_to(io).as_posix()]}
            for side, folder in (("recording", "episodes"), ("replay", "replay"))}})
    atomic_json(io / "goal_results.json", rows)
    module.save_goal_assessment(play.job, io, report)
    assert read_json(play.job / "gamecraft-goals.json")["all_goals_supported"]
    for change in ("wrong_side", "time_limit", "past_clip", "no_evidence"):
        edited = deepcopy(rows)
        value = edited[0]["replay"]
        if change == "wrong_side": value["evidence"] = edited[0]["recording"]["evidence"]
        if change == "time_limit": value["time_seconds"] = 21
        if change == "past_clip": value["time_seconds"] = 2
        if change == "no_evidence": value["evidence"] = []
        atomic_json(io / "goal_results.json", edited)
        with pytest.raises(ValueError):
            module.save_goal_assessment(play.job, io, report)


def test_profile_is_explicit_and_resolves_relative_path(configured, tmp_path):
    path = tmp_path / "examples/test.json"
    path.parent.mkdir()
    configured["benchmark"]["path"] = "../bench"
    atomic_json(path, configured)
    result = load_config(path)
    assert result["benchmark"]["path"] == str(tmp_path / "bench")
    for field, value in (("engine", "html"), ("runtime", {"mode": "local"}),
                         ("player", {"mode": "gui"}), ("player", {"mode": "policy", "trajectories_per_version": 11})):
        invalid = {**configured, field: value}
        atomic_json(path, invalid)
        with pytest.raises(ValueError): load_config(path)
    configured.pop("benchmark")
    atomic_json(path, configured)
    assert load_config(path)["benchmark"] is None


@pytest.mark.parametrize("marked", [False, True])
def test_prompt_appended_only_for_marked_player(configured, tmp_path, marked):
    auth = tmp_path / "auth.json"
    auth.write_text("{}")
    configured["auth_file"] = str(auth)
    class Runner:
        def run(self, argv, cwd, log, **kw):
            atomic_json(cwd / "result.json", {"experience": "fixture", "problems": [], "suggestions": [],
                                             "evidence": ["evidence/summary.json"]})
            log.write_text("")
    context = {"phase": "policy_analyze", "benchmark": module.context(configured) if marked else None}
    job = tmp_path / "role"
    Agent(configured, Runner()).call("player", job, context)
    prompt = read_json(job / "request.json")["prompt"]
    assert ("GameCraft execution supplement" in prompt) == marked
    assert "Four aspects of experience feedback" in prompt
    schema = role_schema("player", {"phase": "policy_author", "trajectories_per_version": 3,
                                     "benchmark": module.context(configured) if marked else None})
    episode = schema["properties"]["episodes"]["items"]
    assert set(episode["properties"]) == set(episode["required"])
    assert ("goal" in episode["required"]) == marked
    assert ("coverage" in schema["properties"]) == marked
    assert ("coverage" in schema["required"]) == marked
    if marked:
        assert "handoff.game.brief" in prompt and "coverage-results.json" in prompt
        assert "每条演示以 **20 秒以内" not in prompt
        assert "victory within each" not in context["benchmark"]["goal"]


@pytest.mark.parametrize("render_threads", [4, 6])
def test_replay_container_has_no_network_credentials_or_desktop(configured, tmp_path, monkeypatch, render_threads):
    configured["runtime"]["render_threads"] = render_threads
    benchmark = Path(configured["benchmark"]["path"])
    code = benchmark / "gamecraft_bench/verifier/replay.py"
    code.parent.mkdir(parents=True)
    code.write_text("fixture")
    commands, cleanups = [], []
    monkeypatch.setattr("game_agent_lite.runtime.cleanup_owner", lambda path, **kw: cleanups.append(path))
    monkeypatch.setattr(module.CommandRunner, "run", lambda self, argv, *a, **kw: commands.append(argv))
    module.replay_submission(configured, tmp_path / "submission", tmp_path / "replay",
                             "sha256:" + "a" * 64, lambda: None)
    command = commands[0]
    assert command[command.index("--network") + 1] == "none"
    assert "--read-only" in command and "--env-file" not in command
    assert f"LP_NUM_THREADS={render_threads}" in command
    assert read_json(tmp_path / "replay/container-runtime.json")["render_threads"] == render_threads
    assert command[-1] == "game_agent_lite.gamecraft_replay_worker"
    assert not any(".env" in arg or "docker.sock" in arg for arg in command)
    assert len(cleanups) == 2


@pytest.fixture
def coverage_plan():
    return {"summary": "Public requirements only", "episodes": [
        {"profile": f"profile-{i}", "purpose": "Demonstrate an interaction", "goal": "Show its result",
         "scenario": None} for i in range(3)], "coverage": [
        {"id": f"R{i}", "requirement": f"Public requirement {i}", "episodes": [i],
         "expected": "Input and visible state change", "omission_reason": ""} for i in range(1, 4)]}


@pytest.mark.parametrize("case", ["duplicate_id", "missing_reason", "unknown_episode", "orphan_episode",
                                  "duplicate_episode", "bool_episode", "blank_requirement", "empty"])
def test_coverage_plan_rejects_invalid_assignments(coverage_plan, case):
    rows = coverage_plan["coverage"]
    if case == "duplicate_id": rows[1]["id"] = rows[0]["id"]
    elif case == "missing_reason": rows[0].update(episodes=[], omission_reason="  ")
    elif case == "unknown_episode": rows[0]["episodes"] = [4]
    elif case == "orphan_episode": rows[1]["episodes"] = [1]
    elif case == "duplicate_episode": rows[0]["episodes"] = [1, 1]
    elif case == "bool_episode": rows[0]["episodes"] = [True]
    elif case == "blank_requirement": rows[0]["requirement"] = "  "
    elif case == "empty": rows.clear()
    with pytest.raises((ValueError, ValidationError)):
        module.coverage_plan_warnings(coverage_plan)


def test_coverage_warnings_and_legacy_schema(coverage_plan):
    assert module.coverage_plan_warnings(coverage_plan) == []
    coverage_plan["coverage"][0]["episodes"] = [1, 2]
    coverage_plan["coverage"][1].update(episodes=[], omission_reason="No suitable existing scenario")
    warnings = module.coverage_plan_warnings(coverage_plan)
    assert any("no assigned episode" in w for w in warnings)
    assert any("Episodes 1 and 2" in w for w in warnings)
    legacy = {k: v for k, v in coverage_plan.items() if k != "coverage"}
    validate(legacy, POLICY_PLAN_SCHEMA)  # Stored old plans are still readable.
    with pytest.raises(ValidationError):
        validate(legacy, role_schema("player", {"phase": "policy_author", "benchmark": True,
                                               "trajectories_per_version": 3}))


def test_coverage_codex_schema_keeps_uniqueness_validation_local(coverage_plan):
    schema = role_schema("player", {"phase": "policy_author", "benchmark": True,
                                    "trajectories_per_version": 3})
    episodes = schema["properties"]["coverage"]["items"]["properties"]["episodes"]
    assert "uniqueItems" not in episodes
    coverage_plan["coverage"][0]["episodes"] = [1, 1]
    validate(coverage_plan, schema)
    with pytest.raises(ValidationError):
        validate(coverage_plan, POLICY_PLAN_SCHEMA)
    with pytest.raises(ValidationError):
        module.coverage_plan_warnings(coverage_plan)


def coverage_rows(io):
    return [{"id": f"R{i}", **{side: {"status": "shown", "reason": "Observed the requested interaction",
        "evidence": [next((io / f"evidence/{folder}/{i:04d}").glob("*.png")).relative_to(io).as_posix()]}
        for side, folder in (("recording", "episodes"), ("replay", "replay"))}} for i in range(1, 4)]


def test_coverage_evidence_integrity_and_nonwinning_goals(configured, tmp_path, replay):
    play = new_session(tmp_path, configured)
    report = play.run(MockAgent())
    io = next(play.job.glob("analysis-*/io"))
    rows = coverage_rows(io)
    rows[0]["replay"].update(status="partial", reason="Only part of the planned interaction was visible")
    atomic_json(io / "coverage-results.json", rows)
    report["gamecraft_coverage"] = module.save_coverage_assessment(play.job, io, report)
    assert valid_play(report, play.job, play.version)
    assert not read_json(play.job / "gamecraft-goals.json")["all_goals_supported"]
    # A changed coverage file or even just its referenced image must invalidate the receipt.
    saved = (play.job / "coverage-results.json").read_bytes()
    (play.job / "coverage-results.json").write_text("{}")
    assert not valid_play(report, play.job, play.version)
    (play.job / "coverage-results.json").write_bytes(saved)
    (io / rows[0]["recording"]["evidence"][0]).write_bytes(b"changed")
    assert not valid_play(report, play.job, play.version)


@pytest.mark.parametrize("case", ["missing_id", "duplicate_id", "wrong_order", "wrong_side", "wrong_episode",
                                  "traversal", "missing_file", "no_image", "invalid_status"])
def test_coverage_claims_reject_unavailable_or_mismatched_evidence(configured, tmp_path, replay, case):
    play = new_session(tmp_path, configured)
    report = play.run(MockAgent())
    io = next(play.job.glob("analysis-*/io"))
    rows = coverage_rows(io)
    finding = rows[0]["replay"]
    if case == "missing_id": rows.pop()
    elif case == "duplicate_id": rows[1]["id"] = "R1"
    elif case == "wrong_order": rows.reverse()
    elif case == "wrong_side": finding["evidence"] = rows[0]["recording"]["evidence"]
    elif case == "wrong_episode": finding["evidence"] = rows[1]["replay"]["evidence"]
    elif case == "traversal":
        finding["evidence"] = ["evidence/replay/0001/../../../" + rows[0]["recording"]["evidence"][0]]
    elif case == "missing_file": finding["evidence"] = ["evidence/replay/0001/absent.png"]
    elif case == "no_image": finding["evidence"] = []
    elif case == "invalid_status": finding["status"] = "won"
    atomic_json(io / "coverage-results.json", rows)
    with pytest.raises((ValueError, ValidationError)):
        module.save_coverage_assessment(play.job, io, report)


def test_coverage_recovery_requires_no_new_player_or_replay(configured, tmp_path, replay):
    play, agent = new_session(tmp_path, configured), MockAgent()
    report = play.run(agent)
    io = next(play.job.glob("analysis-*/io"))
    atomic_json(io / "result.json", read_json(play.job / "analysis.json"))
    (play.job / "play.json").unlink()
    recovered = recover_policy_analysis(play.job, play.version)
    assert recovered["gamecraft_coverage"] == report["gamecraft_coverage"]
    assert valid_play(recovered, play.job, play.version)
    assert agent.calls == ["player", "player"] and len(replay) == 1


def test_legacy_gamecraft_recovery_and_new_job_upgrades_coverage(configured, tmp_path, replay, monkeypatch):
    class Legacy(MockAgent):
        def call(self, role, job, context, **kw):
            result = super().call(role, job, context, **kw)
            if context["phase"] == "policy_author": result.pop("coverage")
            return result
    def legacy_schema(role, context):
        schema = deepcopy(role_schema(role, context))
        schema["properties"].pop("coverage", None)
        schema["required"] = [k for k in schema["required"] if k != "coverage"]
        return schema
    play, agent = new_session(tmp_path, configured), Legacy()
    # Produce a historical fixture with the former output contract, not a real rollout.
    with monkeypatch.context() as patch:
        patch.setattr("game_agent_lite.policy.role_schema", legacy_schema)
        report = play.run(agent)
    assert "gamecraft_coverage" not in report
    assert play.run(agent) == report
    io = next(play.job.glob("analysis-*/io"))
    atomic_json(io / "result.json", read_json(play.job / "analysis.json"))
    recovered = recover_policy_analysis(play.job, play.version)
    assert valid_play(recovered, play.job, play.version) and "gamecraft_coverage" not in recovered
    assert agent.calls == ["player", "player"] and len(replay) == 1
    new = PolicySession(configured, play.project, tmp_path / "new-player", play.version, FixtureEngine,
                        context={"round": 2}, parent={"path": play.job / "play.json",
                        "version": play.version, "reference": "old/play.json"})
    next_agent = MockAgent()
    updated = new.run(next_agent)
    assert updated["gamecraft_coverage"] and updated["inheritance"]["author_called"]
    assert next_agent.calls == ["player", "player"]


def test_worker_uses_upstream_replay_and_keeps_partial_failures(tmp_path, monkeypatch):
    from game_agent_lite import gamecraft_replay_worker as worker
    package = ModuleType("gamecraft_bench")
    verifier = ModuleType("gamecraft_bench.verifier")
    replay = ModuleType("gamecraft_bench.verifier.replay")
    sampling = ModuleType("gamecraft_bench.verifier.score")
    calls = []
    def replay_trace(**kwargs):
        calls.append(kwargs)
        assert kwargs["viewport"] == (1280, 720) and kwargs["fps"] == 30
        assert kwargs["record_size"] == (854, 480)
        assert kwargs["project_dir"].is_dir()
        if len(calls) == 2:
            raise RuntimeError("fixture replay error")
        kwargs["output_mp4"].write_bytes(b"synthetic mp4")
        return SimpleNamespace(duration_seconds=20)
    def frames(video, output, **kwargs):
        assert kwargs["interval_seconds"] == .5 and kwargs["max_window_seconds"] == 20
        output.mkdir()
        frame = output / "frame.png"
        frame.write_bytes(b"fixture PNG")
        return [frame]
    replay.replay_trace, sampling._sample_frames = replay_trace, frames
    for name, mod in (("gamecraft_bench", package), ("gamecraft_bench.verifier", verifier),
                      ("gamecraft_bench.verifier.replay", replay), ("gamecraft_bench.verifier.score", sampling)):
        monkeypatch.setitem(sys.modules, name, mod)
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: None)
    monkeypatch.setattr(subprocess, "check_output", lambda *a, **kw: "fixture godot")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "original-user-data"))
    submission, output = tmp_path / "submission", tmp_path / "output"
    (submission / "game").mkdir(parents=True)
    (submission / "game/project.godot").write_text("fixture")
    atomic_json(submission / "manifest.json", {"demos": [{"episode": i, "duration_frames": 600,
        "trace": {"path": f"game/demo_outputs/{i}.json", "sha256": "a" * 64}} for i in (1, 2)]})
    before = all_bytes(submission)
    worker.main(submission, output, tmp_path / "temporary")
    result = read_json(output / "results.json")
    assert result["official_score"] is None and result["judge_calls"] is False
    episodes = [read_json(output / ref["path"]) for ref in result["episodes"]]
    assert [e["status"] for e in episodes] == ["completed", "failed"]
    assert all(e["goal_verified"] is None for e in episodes)
    assert len(calls) == 2 and calls[0]["project_dir"] == calls[1]["project_dir"]
    assert all_bytes(submission) == before
