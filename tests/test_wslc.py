"""WSLC command shape. No container engine is started."""
import json
from pathlib import Path
import subprocess
import threading
import time
from unittest.mock import Mock

import pytest

from game_agent_lite import containers, runtime
from game_agent_lite.agent import Agent
from game_agent_lite.host import lock_file, unlock_file
from game_agent_lite.runtime import same_runtime
from game_agent_lite.store import atomic_json


def test_blocking_lock_waits_until_the_holder_leaves(tmp_path):
    path = tmp_path / "lock"
    first = path.open("a+")
    second = path.open("a+")
    lock_file(first)
    started = time.monotonic()

    def release():
        time.sleep(0.3)
        unlock_file(first)

    threading.Thread(target=release).start()
    lock_file(second, block=True)
    assert time.monotonic() - started >= 0.2
    unlock_file(second)
    first.close()
    second.close()


def test_missing_runtime_cli_still_matches_docker():
    current = {"mode": "docker", "cli": "docker", "image_id": "sha256:" + "a" * 64}
    saved = {"mode": "docker", "image_id": current["image_id"]}
    assert same_runtime(saved, current)
    assert not same_runtime(saved, {**current, "cli": "wslc"})


def test_lock_excludes_a_second_holder(tmp_path):
    path = tmp_path / "lock"
    first = path.open("a+")
    second = path.open("a+")
    lock_file(first)
    try:
        with pytest.raises(BlockingIOError):
            lock_file(second)
        unlock_file(first)
        lock_file(second)
        unlock_file(second)
    finally:
        first.close()
        second.close()


def test_docker_run_keeps_isolation_and_wslc_drops_it():
    mounts = [(Path("D:/source"), "/source", True)]
    common = dict(name="rsi-game-" + "a" * 32, label="owner=b", network="none", shm="512m",
                  tmpfs="/tmp:rw,nosuid,nodev", workdir="/output", interactive=True,
                  read_only_root=True, mounts=mounts, envs=["DISPLAY=:99"], entrypoint="python3",
                  image="sha256:" + "c" * 64, command=["-m", "worker"])
    docker = containers.run_argv("docker", user="10:10", **common)
    wslc = containers.run_argv("wslc", user="1000:1000", **common)
    assert docker[:2] == ["docker", "run"]
    assert "--init" in docker and "--read-only" in docker and "--cap-drop" in docker
    assert "--security-opt" in docker and "10:10" in docker
    assert wslc[0] == "wslc"
    for flag in ("--init", "--read-only", "--cap-drop", "--security-opt"):
        assert flag not in wslc
    assert "1000:1000" in wslc and "--network" in wslc
    role = containers.run_argv(
        "wslc", name="rsi-role-abc", label="game-agent-lite.job=abc", network=None, user="runner",
        shm=None, tmpfs=None, workdir="/workspace/io", interactive=False, read_only_root=False,
        mounts=[(Path("D:/io"), "/workspace/io", False)], envs=["CODEX_HOME=/codex-home"],
        entrypoint="codex", image="sha256:" + "d" * 64, command=["exec", "--sandbox", "danger-full-access"])
    assert "--network" not in role and "danger-full-access" in role and "runner" in role


def test_wslc_image_inspect_uses_json_format(monkeypatch):
    def fake(args, **kwargs):
        assert args[0] == "wslc" and args[-2:] == ["-f", "json"]
        return json.dumps([{"Id": "sha256:" + "a" * 64, "Os": "linux", "Architecture": "amd64"}])
    monkeypatch.setattr(containers.subprocess, "check_output", fake)
    assert containers.validated_image("wslc", "runtime")["Architecture"] == "amd64"


def test_wslc_cleanup_accepts_localized_missing_container(tmp_path, monkeypatch):
    marker = tmp_path / "engine-runtime-owner.json"
    atomic_json(marker, {"container": "rsi-game-" + "a" * 32, "owner": "b" * 32, "cli": "wslc"})

    def fake(binary, args, timeout=30):
        assert binary == "wslc"
        return subprocess.CompletedProcess(args, 1, "", "找不到容器\nWSLC_E_CONTAINER_NOT_FOUND")

    monkeypatch.setattr(runtime, "capture", fake)
    runtime.cleanup_owner(marker)
    assert not marker.exists()


def test_wslc_role_runs_codex_without_harbor(tmp_path, config, monkeypatch):
    config["backend"] = "harbor"
    config["runtime"]["cli"] = "wslc"
    Path(config["auth_file"]).write_text("{}\n")
    monkeypatch.setattr("game_agent_lite.agent.validated_image",
                        lambda *_: {"Id": "sha256:" + "a" * 64, "Os": "linux"})

    def run(argv, **kwargs):
        if len(argv) > 1 and argv[1] == "ps":
            return Mock(returncode=0, stdout="", stderr="")
        return Mock(returncode=0, stdout="codex-cli 0.153.4\n", stderr="")

    monkeypatch.setattr("game_agent_lite.agent.subprocess.run", run)
    seen = {}

    class Runner:
        def run(self, argv, cwd, log, **kwargs):
            seen["argv"] = argv
            atomic_json(cwd / "result.json", {"summary": "fixture", "checks": [], "remaining": []})
            log.write_text('{"type":"turn.completed","usage":null}\n')

    job = tmp_path / "job"
    project = tmp_path / "project"
    project.mkdir()
    Agent(config, Runner()).call("builder", job, {"stage": "draft"}, project=project)
    argv = seen["argv"]
    assert argv[0] == "wslc" and "--network" not in argv
    assert "danger-full-access" in argv and "runner" in argv
    assert "/workspace/project" in " ".join(argv)
    home = next(item for item in argv if "target=/codex-home" in item)
    assert not home.endswith(",readonly")
    assert not (job / "task/environment/docker-compose.yaml").exists()
    assert not (job / ".codex-home").exists()
