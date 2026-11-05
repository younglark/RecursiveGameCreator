import json
from pathlib import Path

import pytest
from jsonschema import ValidationError

from game_agent_lite.agent import Agent, child_env
from game_agent_lite.cli import load_config
from game_agent_lite.paths import portable_config, resolve_paths
from game_agent_lite.store import atomic_json, read_json
from game_agent_lite.types import CONFIG_SCHEMA, DEFAULTS, validate
from game_agent_lite.usage import read_tokens, shape_grok_usage

ROOT = Path(__file__).parents[1]
SUMMARY = {"summary": "fixture", "checks": [], "remaining": []}


def grok_config(config, tmp_path):
    config["backend"] = "grok"
    config["model"] = "grok-4.7"
    config["reasoning"] = "high"
    config["grok"] = "grok"
    config["grok_auth_file"] = str(tmp_path / "grok-auth.json")
    Path(config["grok_auth_file"]).write_text("{}\n")
    return config


def result_log(structured, usage=None, **extra):
    event = {"type": "result", "subtype": "success", "is_error": False,
             "stop_reason": "end_turn", "structured_output": structured}
    if usage is not None:
        event["usage"] = usage
    event.update(extra)
    return json.dumps({"type": "system", "subtype": "init"}) + "\n" + json.dumps(event) + "\n"


class Runner:
    def __init__(self, structured, usage=None, **extra):
        self.structured = structured
        self.usage = usage
        self.extra = extra
        self.seen = None

    def run(self, argv, cwd, log, **kwargs):
        self.seen = (argv, kwargs["env"], cwd)
        home = Path(kwargs["env"]["GROK_HOME"])
        self.sandbox = (home / "sandbox.toml").read_text()
        self.auth_linked = (home / "auth.json").is_symlink()
        atomic_json(cwd / "result.json", {"summary": "written by the model"})
        log.write_text(result_log(self.structured, self.usage, **self.extra))


def test_grok_builder_command_isolates_home_and_replaces_model_file(tmp_path, config, monkeypatch):
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    monkeypatch.setenv("HUNYUAN_API_KEY", "hunyuan-secret")
    grok_config(config, tmp_path)
    usage = {"input_tokens": 1499, "output_tokens": 76,
             "cache_read_input_tokens": 2432, "cache_creation_input_tokens": 0}
    runner = Runner(SUMMARY, usage, total_cost_usd=0.0015878)
    project = tmp_path / "project with spaces"
    project.mkdir()
    job = tmp_path / "job"
    result = Agent(config, runner).call("builder", job, {"stage": "draft"}, project=project)
    assert result == SUMMARY
    assert read_json(job / "io/result.json") == SUMMARY
    argv, env, cwd = runner.seen
    assert argv[0] == "grok" and "--prompt-file" in argv and "-p" not in argv
    assert "--add-dir" not in argv and "--trust" not in argv
    assert argv[argv.index("-m") + 1] == "grok-4.7"
    assert argv[argv.index("--effort") + 1] == "high"
    assert argv[argv.index("--sandbox") + 1] == "role"
    assert argv[argv.index("--tools") + 1] == (
        "read_file,grep,list_dir,search_replace,write,run_terminal_cmd")
    assert json.loads(argv[argv.index("--json-schema") + 1])["required"] == [
        "summary", "checks", "remaining"]
    assert Path(argv[argv.index("--cwd") + 1]) == cwd
    home = Path(env["GROK_HOME"])
    assert argv[argv.index("--leader-socket") + 1] == str(home / "leader.sock")
    assert env["GROK_MEMORY"] == "0" and env["GROK_DISABLE_AUTOUPDATER"] == "1"
    assert "XAI_API_KEY" not in env and "HUNYUAN_API_KEY" not in env
    assert runner.auth_linked
    sandbox = runner.sandbox
    assert 'extends = "workspace"' in sandbox
    assert json.dumps(str(project.resolve())) in sandbox
    prompt = (job / "prompt.txt").read_text()
    assert "Write files with tools" in prompt
    assert "final answer is only the JSON object" in prompt
    tokens, sources = read_tokens(job, "grok")
    assert sources == ["grok-usage.json"]
    assert tokens["input_tokens"] == 3931
    assert tokens["cached_input_tokens"] == 2432
    assert tokens["output_tokens"] == 76
    assert tokens["total_tokens"] == 4007
    assert tokens["cost_usd"] == pytest.approx(0.0015878)
    assert read_json(job / "result.json")["usage"]["input_tokens"] == 3931


def test_player_sandbox_does_not_grant_project_writes(tmp_path, config, monkeypatch):
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    grok_config(config, tmp_path)
    runner = Runner({"experience": "played", "problems": [], "suggestions": []})
    project = tmp_path / "source"
    project.mkdir()
    Agent(config, runner).call("player", tmp_path / "job", {}, project=project)
    sandbox = runner.sandbox
    assert "read_write" not in sandbox
    assert str(project.resolve()) not in sandbox


def test_api_key_is_passed_only_to_the_grok_process(tmp_path, config, monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "xai-test-key")
    grok_config(config, tmp_path)
    config["grok_auth_file"] = str(tmp_path / "missing-auth.json")
    runner = Runner(SUMMARY)
    Agent(config, runner).call("builder", tmp_path / "job", {"stage": "draft"},
                               project=tmp_path)
    assert runner.seen[1]["XAI_API_KEY"] == "xai-test-key"
    assert "XAI_API_KEY" not in child_env()
    assert not runner.auth_linked


def test_missing_grok_login_fails_before_the_process_starts(tmp_path, config, monkeypatch):
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    grok_config(config, tmp_path)
    config["grok_auth_file"] = str(tmp_path / "missing-auth.json")

    class Boom:
        def run(self, *args, **kwargs):
            raise AssertionError("grok must not start without a login")

    with pytest.raises(ValueError, match="grok login or set XAI_API_KEY"):
        Agent(config, Boom()).call("builder", tmp_path / "job", {"stage": "draft"})


def test_exit_zero_without_structured_output_fails(tmp_path, config, monkeypatch):
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    grok_config(config, tmp_path)

    class Empty:
        def run(self, argv, cwd, log, **kwargs):
            log.write_text(json.dumps({"type": "result", "subtype": "success"}) + "\n")

    with pytest.raises(RuntimeError, match="without structured output"):
        Agent(config, Empty()).call("builder", tmp_path / "job", {"stage": "draft"})


def test_malformed_structured_output_is_rejected(tmp_path, config, monkeypatch):
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    grok_config(config, tmp_path)
    with pytest.raises(ValidationError):
        Agent(config, Runner({"unexpected": True})).call(
            "builder", tmp_path / "job", {"stage": "draft"})


def test_incomplete_or_partial_grok_cost_stays_unknown():
    event = {"usage_is_incomplete": True, "usage": {
        "input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0}, "total_cost_usd": 0}
    assert shape_grok_usage(event) is None
    partial = {"cost_is_partial": True, "total_cost_usd": 1.25, "usage": {
        "input_tokens": 100, "cache_read_input_tokens": 20,
        "cache_creation_input_tokens": 10, "output_tokens": 5}}
    shaped = shape_grok_usage(partial)
    assert shaped["input_tokens"] == 130
    assert shaped["cached_input_tokens"] == 30
    assert shaped["output_tokens"] == 5
    assert "cost_usd" not in shaped
    assert read_tokens(Path("/no/such-grok-job"), "grok")[1] == []


def test_grok_config_paths_and_example(tmp_path):
    with pytest.raises(ValidationError):
        validate({"engine": "html", "brief": "x", "backend": "openai"}, CONFIG_SCHEMA)
    validate({"engine": "html", "brief": "x", "backend": "grok"}, CONFIG_SCHEMA)
    config = dict(DEFAULTS)
    config.update(engine="html", brief="t", backend="grok")
    saved = portable_config(config, tmp_path)
    assert saved["grok_auth_file"] == "~/.grok/auth.json"
    assert saved["grok"] == "grok"
    assert Path(resolve_paths(saved, tmp_path)["grok_auth_file"]) == Path.home() / ".grok/auth.json"
    binary = tmp_path / "bin/grok"
    binary.parent.mkdir()
    binary.write_text("#!/bin/sh\n")
    path = tmp_path / "game.json"
    path.write_text(json.dumps({"engine": "html", "brief": "play", "backend": "grok",
                                "grok": "bin/grok"}))
    loaded = load_config(path)
    assert loaded["grok"] == str(binary.resolve())
    example = load_config(ROOT / "examples/quickstart-html-grok.json")
    assert example["backend"] == "grok" and example["model"] == "grok-4.7"
    assert example["assets"]["max_images"] == 0 and example["runtime"]["mode"] == "docker"
