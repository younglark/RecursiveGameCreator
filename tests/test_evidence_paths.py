"""Relative evidence contracts and offline recovery of already recorded play."""
from pathlib import Path
from unittest.mock import Mock

import pytest
from jsonschema import ValidationError

import game_agent_lite.policy as policy_module
from game_agent_lite.agent import Agent, MockAgent
from game_agent_lite.loop import Loop
from game_agent_lite.policy import _relative_analysis, recover_policy_analysis, valid_policy_play
from game_agent_lite.store import Store, atomic_json, file_hash, read_json
from game_agent_lite.types import POLICY_REVIEW_SCHEMA, REVIEW_SCHEMA, validate


@pytest.mark.parametrize("prefix,schema", [("evidence", POLICY_REVIEW_SCHEMA), ("A", REVIEW_SCHEMA)])
@pytest.mark.parametrize("invalid", ["/workspace/io/{p}/frame.png", "{p}/frame.png：说明",
                                    "{p}/../frame.png", "`{p}/frame.png`"])
def test_schema_rejects_non_relative_or_annotated_paths(prefix, schema, invalid):
    value = ({"experience": "fixture", "problems": [], "suggestions": []}
             if prefix == "evidence" else {"winner": "tie", "reason": "fixture"})
    validate({**value, "evidence": [f"{prefix}/frame.png"]}, schema)
    with pytest.raises(ValidationError):
        validate({**value, "evidence": [invalid.format(p=prefix)]}, schema)


@pytest.mark.parametrize("role", ["player", "reviewer"])
def test_real_agent_schema_uses_existing_relative_paths(tmp_path, config, role):
    config["backend"] = "codex"
    Path(config["auth_file"]).write_text("{}")
    job = tmp_path / "role"
    prefixes = ["evidence"] if role == "player" else ["A", "B"]
    paths = [f"{p}/frame.png" for p in prefixes]
    for path in paths:
        target = job / "io" / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"fixture")
    class Runner:
        def run(self, argv, cwd, log, **kwargs):
            schema = read_json(cwd / "schema.json")
            assert read_json(cwd / "input.json")["evidence_paths"] == paths
            assert schema["properties"]["evidence"]["items"] == {"type": "string", "enum": paths}
            value = ({"experience": "fixture", "problems": [], "suggestions": []}
                     if role == "player" else {"winner": "tie", "reason": "fixture"})
            with pytest.raises(ValidationError):
                validate({**value, "evidence": [prefixes[0] + "/missing.png"]}, schema)
            atomic_json(cwd / "result.json", {**value, "evidence": paths})
            log.write_text("")
    result = Agent(config, Runner()).call(role, job, {"phase": "policy_analyze"} if role == "player" else {})
    assert result["evidence"] == paths


@pytest.mark.parametrize("citation", ["/tmp/frame.png", "evidence/../frame.png",
    "evidence/missing.png：说明", "frame.png", "evidence/frame.png 与 evidence/other.png",
    "/workspace/io/evidence/frame.png：另见 evidence/other.png"])
def test_legacy_normalization_does_not_guess_or_drop_ambiguous_references(tmp_path, citation):
    with pytest.raises(ValueError, match="unavailable or ambiguous"):
        _relative_analysis({"experience": "fixture", "problems": [], "suggestions": [],
            "evidence": [citation]}, tmp_path, ["evidence/frame.png", "evidence/other.png"])


def failed_analysis(tmp_path, config, *, local_paths=False):
    config["rounds"] = 1
    config["player"].update(mode="policy", parallel_episodes=1)
    class LegacyOutput(MockAgent):
        def call(self, role, job, context, **kwargs):
            value = super().call(role, job, context, **kwargs)
            if context.get("phase") == "policy_analyze":
                prefix = str((job / "io").resolve()) + "/" if local_paths else "/workspace/io/"
                value["evidence"] = [prefix + p + "：已检查的真实证据" for p in value["evidence"]]
                atomic_json(job / "io/result.json", value)
            return value
    store = Store(tmp_path / "run")
    store.create(config)
    state = Loop(store, config, agent=LegacyOutput()).run()
    assert state["status"] == "failed" and state["phase"] == "play"
    directory = store.root / "rounds/0000"
    marker = read_json(directory / "player-session-candidate.json")
    job = directory / "jobs" / marker["job"]
    return store, directory, job, marker["version"]


@pytest.mark.parametrize("local_paths", [False, True])
def test_resume_recovers_report_without_engine_model_or_runtime_relabeling(tmp_path, config, monkeypatch, local_paths):
    store, directory, job, expected = failed_analysis(tmp_path, config, local_paths=local_paths)
    before = {p: file_hash(p) for p in job.rglob("*") if p.is_file()}
    policy = read_json(job / "policy.json")
    attempts = sorted(job.glob("episodes/*/attempt-*"))
    # A complete recorded batch must not instantiate a new runtime, even when
    # current framework identity differs from that recorded in policy settings.
    monkeypatch.setattr(policy_module, "PolicySession", Mock(side_effect=AssertionError("no new runtime")))
    agent = Mock()
    agent.call.side_effect = AssertionError("no model call")
    factory = Mock(side_effect=AssertionError("no game replay"))
    state = Loop(store, config, agent=agent, game_factory=factory).run(resume=True)
    assert state["status"] == "completed", state["error"]
    assert state["best"]["version"] == expected
    report = read_json(job / "play.json")
    assert report["settings"] == policy["settings"]
    assert all(file_hash(path) == digest for path, digest in before.items())
    assert sorted(job.glob("episodes/*/attempt-*")) == attempts
    analysis = read_json(job / "analysis.json")
    assert all(p.startswith("evidence/") and "：" not in p for p in analysis["evidence"])
    assert read_json(job / "analysis-recovery.json")["replayed"] is False
    assert valid_policy_play(report, job, expected, allow_mock=True)
    assert read_json(directory / "played-candidate.json")["sha256"] == file_hash(job / "play.json")
    agent.call.assert_not_called()
    factory.assert_not_called()


@pytest.mark.parametrize("corruption", ["video", "bundle", "missing_frame_citation", "unknown_citation", "partial"])
def test_recovery_keeps_evidence_validation(tmp_path, config, corruption):
    _, _, job, expected = failed_analysis(tmp_path, config)
    raw = next(job.glob("analysis-*/io/result.json"))
    if corruption == "partial":
        (job / "episodes/0003/completed.json").unlink()
        assert recover_policy_analysis(job, expected, allow_mock=True) is None
        return
    if corruption == "video":
        episode = read_json(job / read_json(job / "episodes/0001/completed.json")["path"])
        (job / episode["video"]["artifact"]["path"]).write_text("tampered")
    elif corruption == "bundle":
        next((raw.parent / "evidence").rglob("*.png")).write_bytes(b"tampered")
    else:
        value = read_json(raw)
        if corruption == "missing_frame_citation":
            value["evidence"] = [p for p in value["evidence"] if "/0003/" not in p]
        else:
            value["evidence"].append("/workspace/io/evidence/missing.png：不存在")
        atomic_json(raw, value)
    with pytest.raises(ValueError):
        recover_policy_analysis(job, expected, allow_mock=True)
    assert not (job / "analysis.json").exists()
    assert not (job / "play.json").exists()
