"""Engine selection, tool isolation and recovery without external services."""
from unittest.mock import Mock

import pytest

from game_agent_lite import runtime, policy
from game_agent_lite.assets import Assets
from game_agent_lite.game import InputRejected
from game_agent_lite.store import Stopped, read_json


def identity():
    return {"protocol": runtime.PROTOCOL, "framework_sha256": "f" * 64,
            "image_id": "sha256:" + "a" * 64}


def action(keys):
    return {"keys": keys, "duration_ms": 100, "pointer": None}


def test_html_preserves_playwright_keys_and_godot_normalizes(tmp_path, config):
    config.update(backend="codex")
    config["runtime"]["mode"] = "docker"
    html = runtime.DockerGame(config, tmp_path, tmp_path / "html")
    html._request = Mock()
    html.policy_act(action(["Backspace", "KeyW"]))
    assert html._request.call_args.kwargs["action"]["keys"] == ["Backspace", "KeyW"]
    assert html.input_capabilities()["engine"] == "html"
    godot = runtime.DockerGame({**config, "engine": "godot"}, tmp_path, tmp_path / "godot")
    godot._request = Mock()
    godot.policy_act(action(["KeyW", "W", "Digit1"]))
    assert godot._request.call_args.kwargs["action"]["keys"] == ["w", "1"]
    godot.act(action(["Backspace", "Delete", "Alt"]))
    assert godot._request.call_args.kwargs["action"]["keys"] == ["Backspace", "Delete", "Alt"]
    with pytest.raises(InputRejected):
        godot.act(action(["F13"]))
    assert godot._request.call_count == 2


@pytest.mark.parametrize("purpose,network", [("build", "bridge"), ("play", "none"),
                                              ("blender", "none"), ("media", "none")])
def test_network_only_enabled_for_explicit_build(tmp_path, config, monkeypatch, purpose, network):
    config.update(backend="codex")
    config["runtime"].update(mode="docker", build_network="bridge")
    source = tmp_path / "source"
    source.mkdir()
    task = runtime.RuntimeContainer(config, source, tmp_path / "output")
    monkeypatch.setattr(runtime, "runtime_identity", lambda _: identity())
    popen = Mock()
    monkeypatch.setattr(runtime.subprocess, "Popen", popen)
    task._request = Mock(return_value=identity())
    task._launch(purpose)
    argv = popen.call_args.args[0]
    assert argv[argv.index("--network") + 1] == network
    source_mount = next(argv[i + 1] for i, a in enumerate(argv) if a == "--mount" and "target=/source" in argv[i + 1])
    assert source_mount.endswith(",readonly") == (purpose != "build")
    assert read_json(task.output / "container-runtime.json")["network"] == network
    assert "--ipc=host" not in argv and "--shm-size" in argv
    assert not any(arg.startswith("LP_NUM_THREADS=") for arg in argv)  # HTML is not llvmpipe.


def test_blender_container_receipt_resume_and_no_host_fallback(tmp_path, config, monkeypatch):
    config.update(backend="codex", blender="/missing/host/blender")
    config["runtime"]["mode"] = "docker"
    source = tmp_path / "source"
    source.mkdir()
    (source / "build.py").write_text("# fixture")
    requested = {"id": "cube", "kind": "blender", "source": "build.py", "prompt": ""}
    client = Mock()
    runner = Mock()
    monkeypatch.setattr(runtime, "runtime_identity", lambda _: identity())
    task = Mock(side_effect=Stopped("fixture stop"))
    monkeypatch.setattr(runtime, "run_blender", task)
    assets = Assets(config, client=client, runner=runner)
    output = tmp_path / "assets"
    with pytest.raises(Stopped):
        assets.generate([requested], source, output)
    assert read_json(output / "cube/job.json")["runtime"] == identity()
    monkeypatch.setattr(runtime, "runtime_identity", lambda _: {**identity(), "image_id": "changed"})
    with pytest.raises(ValueError, match="runtime changed"):
        assets.generate([requested], source, output)
    assert task.call_count == 1
    monkeypatch.setattr(runtime, "runtime_identity", lambda _: identity())
    def complete(cfg, draft, folder, script, stop, *, output_format):
        assert output_format == "glb"
        assert cfg["runtime"]["image"] == identity()["image_id"]
        assert cfg["_runtime_identity"] == identity()
        assert script == "build.py" and draft == source
        folder.mkdir()
        produced = folder / "asset.glb"
        produced.write_bytes(b"glTF" + b"fixture")
        return produced, identity()
    task.side_effect = complete
    result = assets.generate([requested], source, output)
    # A completed artifact keeps its original provenance and is not regenerated.
    monkeypatch.setattr(runtime, "runtime_identity", Mock(side_effect=AssertionError("must reuse")))
    assert assets.generate([requested], source, output) == result
    assert task.call_count == 2
    assert not client.mock_calls and not runner.mock_calls
    assert read_json(output / "cube/job.json")["status"] == "completed"


def test_container_media_route_and_no_local_ffmpeg(tmp_path, config, monkeypatch):
    config.update(backend="codex", ffmpeg="/missing/host/ffmpeg")
    config["runtime"]["mode"] = "docker"
    output = tmp_path / "frames"
    video = tmp_path / "engine" / "play.mp4"
    def complete(cfg, source, destination, purpose, stop, **params):
        assert source == video.parent and purpose == "media"
        assert params == {"video": "play.mp4"}
        destination.mkdir()
        (destination / "frame-000001.png").write_bytes(b"fixture")
        return [{"time_seconds": 0.5, "image": {"path": "frame-000001.png"}}], identity()
    monkeypatch.setattr(runtime, "run_tool", complete)
    frames = policy._extract_frames(config, video, output, tmp_path, lambda: None)
    assert frames[0]["clock"] == "video" and frames[0]["time_seconds"] == 0.5
    assert frames[0]["image"]["path"] == "frames/frame-000001.png"
    monkeypatch.setattr(runtime, "run_tool", lambda *a, **kw: ([{"time_seconds": 0,
        "image": {"path": "../../outside.png"}}], identity()))
    with pytest.raises(ValueError, match="inside its workspace"):
        policy._extract_frames(config, video, output, tmp_path, lambda: None)


def test_failed_tool_disposes_owned_container(tmp_path, config, monkeypatch):
    task = Mock(identity=identity())
    task._request.side_effect = RuntimeError("tool failed")
    monkeypatch.setattr(runtime, "RuntimeContainer", Mock(return_value=task))
    with pytest.raises(RuntimeError, match="tool failed"):
        runtime.run_blender(config, tmp_path, tmp_path / "out", "make.py")
    task._dispose.assert_called_once_with(graceful=True)
    with pytest.raises(ValueError, match="Unsupported runtime tool"):
        runtime.run_tool(config, tmp_path, tmp_path, "shell")
