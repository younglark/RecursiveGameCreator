"""Offline runtime transport, isolation, provenance and recovery checks."""
import json
from pathlib import Path
import subprocess
import sys
from unittest.mock import Mock

import pytest
from jsonschema import ValidationError

from game_agent_lite import runtime as module
from game_agent_lite.agent import MockAgent, terminate
from game_agent_lite.cli import load_config
from game_agent_lite.game import GodotGame, MockGame, make_game
from game_agent_lite.policy import PolicySession, valid_policy_play
from game_agent_lite.store import Stopped, atomic_json, read_json, version


@pytest.fixture
def runtime_config(config):
    config.update(engine="godot", backend="codex", entry="project.godot")
    config["runtime"]["mode"] = "docker"
    return config


def engine(tmp_path, config):
    project = tmp_path / "source"
    project.mkdir(exist_ok=True)
    (project / "project.godot").write_text("config_version=5\n")
    return module.DockerGame(config, project, tmp_path / "engine")


def identity():
    return {"protocol": module.PROTOCOL, "framework_sha256": "f" * 64, "image_id": "sha256:" + "a" * 64}


def test_config_defaults_opt_in_and_factory(tmp_path, runtime_config):
    path = tmp_path / "config.json"
    atomic_json(path, {"engine": "godot", "brief": "fixture"})
    config = load_config(path)
    assert config["runtime"]["mode"] == "local"
    assert type(make_game(config, tmp_path, tmp_path / "old", lambda: None)) is GodotGame
    atomic_json(path, {"engine": "godot", "brief": "fixture", "runtime": {"mode": "docker"}})
    config = load_config(path)
    assert config["runtime"]["health_timeout_seconds"] == 30
    assert config["runtime"]["render_threads"] == 4
    assert isinstance(make_game(config, tmp_path, tmp_path / "new", lambda: None), module.DockerGame)
    config["backend"] = "mock"
    assert isinstance(make_game(config, tmp_path, tmp_path / "mock", lambda: None), MockGame)
    for bad in ({"mode": "invalid"}, {"mode": "docker", "health_timeout_seconds": 0}, {"secret": "x"},
                {"render_threads": 0}, {"render_threads": -1}, {"render_threads": 1.5}, {"render_threads": True}):
        atomic_json(path, {"engine": "godot", "brief": "fixture", "runtime": bad})
        with pytest.raises(ValidationError):
            load_config(path)
    atomic_json(path, {"engine": "html", "brief": "fixture", "runtime": {"mode": "docker"}})
    config = load_config(path)
    assert isinstance(make_game(config, tmp_path, tmp_path / "html", lambda: None), module.DockerGame)


def test_identity_resolves_image_and_rejects_missing(monkeypatch, runtime_config):
    docker = Mock(return_value=subprocess.CompletedProcess([], 0, json.dumps([
        {"Id": identity()["image_id"], "Os": "linux", "Architecture": "arm64"}]), ""))
    monkeypatch.setattr(module, "_docker", docker)
    result = module.runtime_identity(runtime_config)
    assert result["image_id"] == identity()["image_id"] and len(result["framework_sha256"]) == 64
    assert result["render_threads"] == 4
    runtime_config["runtime"]["render_threads"] = 6
    assert module.runtime_identity(runtime_config) == {**result, "render_threads": 6}
    docker.return_value = subprocess.CompletedProcess([], 1, "", "No such image")
    with pytest.raises(RuntimeError, match="No automatic pull or desktop fallback"):
        module.runtime_identity(runtime_config)
    assert all(call.args[0][:2] == ["image", "inspect"] for call in docker.call_args_list)


@pytest.mark.parametrize("purpose,readonly", [("play", True), ("build", False)])
@pytest.mark.parametrize("render_threads", [4, 6])
def test_launch_mounts_and_no_host_credentials(tmp_path, monkeypatch, runtime_config, purpose, readonly, render_threads):
    runtime_config["runtime"]["render_threads"] = render_threads
    game = engine(tmp_path, runtime_config)
    monkeypatch.setenv("HUNYUAN_API_KEY", "private-fixture-key")
    monkeypatch.setattr(module, "runtime_identity", lambda _: {**identity(), "render_threads": render_threads})
    popen = Mock()
    monkeypatch.setattr(module.subprocess, "Popen", popen)
    game._request = Mock(return_value={**identity()})
    game._launch(purpose)
    argv = popen.call_args.args[0]
    mounts = [argv[i + 1] for i, a in enumerate(argv) if a == "--mount"]
    assert len(mounts) == 3
    assert next(m for m in mounts if "target=/source" in m).endswith(",readonly") == readonly
    assert next(m for m in mounts if "target=/framework" in m).endswith(",readonly")
    assert argv[argv.index("--network") + 1] == "none"
    assert "--read-only" in argv and "--init" in argv and "--privileged" not in argv
    assert "-p" not in argv and "/var/run/docker.sock" not in str(argv)
    assert identity()["image_id"] in argv
    assert f"LP_NUM_THREADS={render_threads}" in argv
    assert "private-fixture-key" not in str(popen.call_args)
    sent = game._request.call_args.kwargs["config"]
    assert set(sent) == {"engine", "entry", "build_command", "check_command", "headless"}
    assert read_json(game.output / "container-runtime.json")["desktop_attached"] is False
    assert read_json(game.output / "container-runtime.json")["render_threads"] == render_threads


def test_cleanup_checks_label_before_stopping(tmp_path, monkeypatch):
    marker = tmp_path / "engine-runtime-owner.json"
    atomic_json(marker, {"container": "rsi-game-" + "a" * 32, "owner": "b" * 32})
    docker = Mock(return_value=subprocess.CompletedProcess([], 0, json.dumps([
        {"Config": {"Labels": {module.OWNER_LABEL: "wrong"}}, "State": {"Running": True}}]), ""))
    monkeypatch.setattr(module, "_docker", docker)
    with pytest.raises(ValueError, match="ownership mismatch"):
        module.cleanup_owner(marker, graceful=True)
    assert marker.exists() and docker.call_count == 1
    docker.side_effect = [subprocess.CompletedProcess([], 0, json.dumps([
        {"Config": {"Labels": {module.OWNER_LABEL: "b" * 32}}, "State": {"Running": True}}]), ""),
        subprocess.CompletedProcess([], 0, "", ""), subprocess.CompletedProcess([], 1, "", "No such container")]
    module.cleanup_runtimes(tmp_path)
    assert not marker.exists()
    assert docker.call_args_list[-2].args[0][:3] == ["stop", "--time", "20"]


def transport(game, script):
    game.process = subprocess.Popen([sys.executable, "-u", "-c", script], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    return game.process


def close_transport(process):
    try:
        terminate(process)
    except PermissionError:
        assert process.poll() is not None
    for stream in (process.stdin, process.stdout, process.stderr):
        stream.close()


def test_transport_heartbeats_not_total_deadline(tmp_path, runtime_config):
    game = engine(tmp_path, runtime_config)
    game.timeout = .25
    process = transport(game, '''import sys, json, time
r=json.loads(sys.stdin.readline())
for _ in range(8):
 print(json.dumps({"id":r["id"],"heartbeat":True}),flush=True)
 time.sleep(.07)
print(json.dumps({"id":r["id"],"result":{"x":123}}),flush=True)
''')
    try:
        assert game._request("observe_state") == {"x": 123}
    finally:
        close_transport(process)


@pytest.mark.parametrize("reply,expected", [
    ("time.sleep(5)", TimeoutError),
    ('print(json.dumps({"id":"wrong","result":{}}),flush=True)', ValueError),
    ('print(json.dumps({"id":r["id"],"error":"engine failed"}),flush=True)', RuntimeError),
    ("sys.exit(0)", RuntimeError)])
def test_transport_failure_never_replays(tmp_path, runtime_config, reply, expected):
    game = engine(tmp_path, runtime_config)
    game.timeout = .15
    process = transport(game, "import sys,json,time\nr=json.loads(sys.stdin.readline())\n" + reply)
    try:
        with pytest.raises(expected):
            game._request("policy_act", action={})
        assert game.broken
        with pytest.raises(RuntimeError, match="never replayed"):
            game._request("policy_act", action={})
    finally:
        close_transport(process)


def test_stop_cancels_transport_and_recovers_video_receipt(tmp_path, runtime_config, monkeypatch):
    game = engine(tmp_path, runtime_config)
    process = transport(game, "import time; time.sleep(5)")
    game.check_stop = Mock(side_effect=Stopped("stop"))
    try:
        with pytest.raises(Stopped):
            game._request("observe_state")
    finally:
        close_transport(process)
        game.process = None
    atomic_json(game.output / "runtime-shutdown.json", {"video": {"path": "play.mp4"}, "error": None})
    monkeypatch.setattr(game, "_dispose", Mock())
    assert game.stop()["path"] == game.output / "play.mp4"
    game._dispose.assert_called_once_with(graceful=True)


def test_observation_paths_cannot_escape_output(tmp_path, runtime_config):
    game = engine(tmp_path, runtime_config)
    for path in ("../../private.png", "/etc/passwd"):
        game._request = Mock(return_value={"public": {}, "image": path})
        with pytest.raises(ValueError, match="inside its workspace"):
            game.observe()


def test_policy_resume_pins_environment(tmp_path, runtime_config, monkeypatch):
    game = engine(tmp_path, runtime_config)
    monkeypatch.setattr(module, "runtime_identity", lambda _: identity())
    play = PolicySession(runtime_config, game.project, tmp_path / "player", version(game.project), MockGame)
    play.author(MockAgent())
    assert play.config["runtime"]["image"] == identity()["image_id"]
    assert runtime_config["runtime"]["image"] != identity()["image_id"]
    monkeypatch.setattr(module, "runtime_identity", lambda _: {**identity(), "framework_sha256": "changed"})
    resumed = PolicySession(runtime_config, game.project, play.job, play.version, MockGame)
    with pytest.raises(ValueError, match="refusing silent regeneration"):
        resumed.author(MockAgent())


def test_policy_evidence_requires_same_runtime(tmp_path, config):
    config["player"].update(mode="policy", parallel_episodes=1)
    project = tmp_path / "source"
    project.mkdir()
    (project / "index.html").write_text("fixture")
    play = PolicySession(config, project, tmp_path / "player", version(project), MockGame)
    report = play.run(MockAgent())
    assert valid_policy_play(report, play.job, play.version, allow_mock=True)
    report["settings"]["runtime"] = identity()
    assert not valid_policy_play(report, play.job, play.version, allow_mock=True)


def test_batch_identity_cannot_change_before_engine_start(tmp_path, runtime_config, monkeypatch):
    runtime_config["_runtime_identity"] = identity()
    monkeypatch.setattr(module, "runtime_identity", lambda _: {**identity(), "framework_sha256": "changed"})
    game = engine(tmp_path, runtime_config)
    with pytest.raises(ValueError, match="changed during"):
        game.start()
    assert not game.owner_file.exists()


def test_build_failure_still_disposes_owned_container(tmp_path, runtime_config):
    game = engine(tmp_path, runtime_config)
    game._launch = Mock()
    game._request = Mock(side_effect=RuntimeError("failed build"))
    game._dispose = Mock()
    with pytest.raises(RuntimeError, match="failed build"):
        game.build()
    game._dispose.assert_called_once_with(graceful=True)


def test_worker_partial_start_cleans_engine_and_records_error(tmp_path, monkeypatch):
    from game_agent_lite import runtime_worker
    monkeypatch.setattr(runtime_worker, "OUTPUT", tmp_path)
    worker = runtime_worker.RuntimeWorker(lambda: None)
    worker.started = True
    worker.game = Mock()
    worker.game.release_inputs.side_effect = OSError("bridge not ready")
    worker.game.stop.side_effect = RuntimeError("encoder failed")
    with pytest.raises(RuntimeError, match="encoder failed"):
        worker.close()
    worker.game.stop.assert_called_once()
    assert read_json(tmp_path / "runtime-shutdown.json")["error"] == "RuntimeError: encoder failed"
    assert worker.close() is None


def test_worker_refuses_provider_settings_and_paths_outside_output(tmp_path, monkeypatch):
    from game_agent_lite import runtime_worker
    monkeypatch.setattr(runtime_worker, "OUTPUT", tmp_path)
    worker = runtime_worker.RuntimeWorker(lambda: None)
    with pytest.raises(ValueError, match="Only HTML/Godot engine settings"):
        worker.dispatch("init", {"purpose": "play", "config": {"engine": "godot", "auth_file": "secret"}})
    with pytest.raises(ValueError):
        worker.encode(tmp_path.parent / "secret")


def test_docker_auto_remove_race_is_idempotent(tmp_path, monkeypatch):
    marker = tmp_path / "engine-runtime-owner.json"
    atomic_json(marker, {"container": "rsi-game-" + "a" * 32, "owner": "b" * 32})
    docker = Mock(side_effect=[
        subprocess.CompletedProcess([], 0, json.dumps([
            {"Config": {"Labels": {module.OWNER_LABEL: "b" * 32}}, "State": {"Running": False}}]), ""),
        subprocess.CompletedProcess([], 1, "", "removal of container is already in progress"),
        subprocess.CompletedProcess([], 1, "", "No such container")])
    monkeypatch.setattr(module, "_docker", docker)
    module.cleanup_owner(marker)
    assert not marker.exists() and docker.call_count == 3
