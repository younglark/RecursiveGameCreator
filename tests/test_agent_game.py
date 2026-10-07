import json
from pathlib import Path
import tomllib
from unittest.mock import Mock

import pytest
from jsonschema import ValidationError

from game_agent_lite.agent import Agent, MockAgent
from game_agent_lite.game import HTMLGame, GodotGame, MockGame, PlaySession, valid_play
from game_agent_lite.store import atomic_json, read_json

ACTION = {"keys": ["ArrowRight"], "duration_ms": 100, "pointer": None}


def test_local_codex_arguments_schema_and_unknown_usage(tmp_path, config):
    config["backend"] = "codex"
    Path(config["auth_file"]).write_text("{}")
    class Runner:
        def run(self, argv, cwd, log, **kwargs):
            assert argv[0:2] == ["codex", "exec"]
            assert argv[argv.index("--model") + 1] == "gpt-6-astra"
            assert "--output-schema" in argv and "--output-last-message" in argv
            assert "HUNYUAN_API_KEY" not in kwargs["env"]
            assert "OPENAI_API_KEY" not in kwargs["env"]
            atomic_json(cwd / "result.json", {"summary": "fixture", "checks": [], "remaining": []})
            log.write_text('{"type":"turn.completed","usage":null}\n')
    project = tmp_path / "project with spaces"
    project.mkdir()
    job = tmp_path / "job"
    result = Agent(config, Runner()).call("builder", job, {"stage": "draft"}, project=project)
    assert result == {"summary": "fixture", "checks": [], "remaining": []}
    assert read_json(job / "result.json")["usage"] is None


def test_harbor_mounts_local_auth_and_no_global_timeout(tmp_path, config):
    config["backend"] = "harbor"
    Path(config["auth_file"]).write_text("{}")
    job = tmp_path / "job"
    (job / "io").mkdir(parents=True)
    agent = Agent(config)
    argv, env = agent.harbor_command(job, "fixture", None, "player")
    mounts = json.loads(argv[argv.index("--mounts") + 1])
    assert len(mounts) == 1 and mounts[0]["target"] == "/workspace/io"
    assert env["CODEX_AUTH_JSON_PATH"] == config["auth_file"]
    assert "--upload" not in argv
    assert "--no-delete" in argv and "--delete" not in argv
    compose = read_json(job / "task/environment/docker-compose.yaml")
    assert compose["services"]["main"]["pull_policy"] == "never"
    task = tomllib.loads((job / "task/task.toml").read_text())
    assert "timeout_sec" not in task["agent"]
    assert task["environment"]["build_timeout_sec"] == float("inf")
    project = tmp_path / "project"
    project.mkdir()
    argv, _ = agent.harbor_command(job, "fixture", project, "designer")
    mounts = json.loads(argv[argv.index("--mounts") + 1])
    assert mounts[1]["read_only"] is True


def test_harbor_trial_error_and_reward_are_not_confused(tmp_path):
    path = tmp_path / "trial/result.json"
    atomic_json(path, {"agent_result": {}, "verifier_result": {"rewards": {"reward": 1}},
                       "exception_info": {"exception_type": "NonZeroAgentExitCodeError"}})
    with pytest.raises(RuntimeError, match="NonZeroAgentExitCodeError"):
        Agent.check_harbor(tmp_path)
    atomic_json(path, {"agent_result": {}, "verifier_result": {"rewards": {"reward": 1}}})
    Agent.check_harbor(tmp_path)


def test_harbor_reports_underlying_docker_error(tmp_path):
    path = tmp_path / "trial/result.json"
    atomic_json(path, {"agent_result": {}, "verifier_result": None,
        "exception_info": {"exception_type": "RuntimeError",
        "exception_message": "Compose failed with long argv. Error response from daemon: pull access denied for game-agent-lite/codex"}})
    with pytest.raises(RuntimeError, match="pull access denied.*exception.txt"):
        Agent.check_harbor(tmp_path)


def test_role_output_schema_rejects_malformed_results(tmp_path, config):
    config["backend"] = "codex"
    Path(config["auth_file"]).write_text("{}")
    class Runner:
        def run(self, argv, cwd, log, **kwargs):
            atomic_json(cwd / "result.json", {"unexpected": True})
            log.write_text("")
    with pytest.raises(ValidationError):
        Agent(config, Runner()).call("builder", tmp_path / "job", {})


def test_html_actual_input_contract_and_key_release(tmp_path, config):
    game = HTMLGame(config, tmp_path, tmp_path / "out")
    game.page = Mock()
    game.act({**ACTION, "pointer": {"x": .5, "y": .25, "button": "left"}})
    game.page.keyboard.down.assert_called_once_with("ArrowRight")
    game.page.keyboard.up.assert_called_once_with("ArrowRight")
    game.page.mouse.down.assert_called_once_with(button="left")
    game.page.mouse.up.assert_called_once_with(button="left")
    game.page.mouse.move.assert_called_once_with(479.5, 134.75)
    game.page.wait_for_timeout.side_effect = RuntimeError("input interrupted")
    with pytest.raises(RuntimeError):
        game.act(ACTION)
    assert game.page.keyboard.up.call_count == 2
    with pytest.raises(ValidationError):
        game.act({**ACTION, "duration_ms": 9000})


def test_godot_input_and_capture_contract(tmp_path, config):
    config["engine"] = "godot"
    game = GodotGame(config, tmp_path, tmp_path / "out")
    game.rpc = Mock(return_value={})
    game.act(ACTION)
    game.rpc.assert_called_with("act", ACTION)
    game.reset(42)
    game.rpc.assert_called_with("reset", {"seed": 42})
    game.frames = tmp_path
    with pytest.raises(ValueError, match="capture"):
        game.observe()


def test_player_feedback_trace_duplicate_requests_and_identity(tmp_path, config):
    job = tmp_path / "job"
    game = MockGame(config, tmp_path, job / "engine")
    session = PlaySession(game, job, "version-one", 42, 4)
    report = session.run(MockAgent())
    assert len(report["steps"]) == 1
    assert report["steps"][0]["before"]["public"]["x"] == 0
    assert report["steps"][0]["after"]["public"]["x"] == 1
    session.serve()  # Same request id cannot trigger another movement.
    assert game.x == 1
    assert valid_play(report, job, "version-one", allow_mock=True)
    assert not valid_play(report, job, "version-one")
    assert not valid_play(report, job, "version-two", allow_mock=True)
    exposed = read_json(job / "io/responses/move.json")
    assert "version" not in exposed
    assert not Path(exposed["image"]).is_absolute()


def test_observation_only_player_has_no_promotion_evidence(tmp_path, config):
    class NoInput(MockAgent):
        def call(self, role, job, *args, **kwargs):
            return {"experience": "did nothing", "problems": [], "suggestions": []}
    job = tmp_path / "job"
    report = PlaySession(MockGame(config, tmp_path, job / "engine"), job, "one", 42, 2).run(NoInput())
    assert not valid_play(report, job, "one", allow_mock=True)


def test_player_cannot_supply_its_own_screenshots_as_evidence(tmp_path, config):
    class Tamper(MockAgent):
        def call(self, role, job, *args, **kwargs):
            result = super().call(role, job, *args, **kwargs)
            for path in (job / "io/frames").glob("*.png"):
                path.write_bytes(b"fake")
            return result
    job = tmp_path / "job"
    report = PlaySession(MockGame(config, tmp_path, job / "engine"), job, "one", 42, 2).run(Tamper())
    assert valid_play(report, job, "one", allow_mock=True)
    private_image = job / report["steps"][0]["after"]["image"]["path"]
    assert private_image.read_bytes() != b"fake"


def test_harbor_stop_cleanup_is_scoped_to_own_label(tmp_path, monkeypatch):
    import game_agent_lite.agent as module
    marker = tmp_path / "harbor-owner.json"
    atomic_json(marker, {"label": "/unique/role with spaces", "active": True})
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        return Mock(stdout="abc123\n" if argv[1] == "ps" else "", returncode=0)
    monkeypatch.setattr(module.subprocess, "run", run)
    Agent.cleanup_harbor(marker)
    assert calls[0][-1] == "label=game-agent-lite.job=/unique/role with spaces"
    assert calls[1] == ["docker", "rm", "-f", "abc123"]
    Agent.cleanup_harbor(marker)
    assert len(calls) == 2
