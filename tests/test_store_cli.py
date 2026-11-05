import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from jsonschema import ValidationError

from game_agent_lite import store as module
from game_agent_lite.agent import child_env, CommandRunner
from game_agent_lite.cli import load_config, main
from game_agent_lite.store import (SECRETS, Stopped, Store, atomic_json, read_json,
                                  redact, snapshot, version)


def test_atomic_write_survives_failed_replace(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    atomic_json(path, {"old": 1})
    def fail(*args):
        raise OSError("crash before commit")
    monkeypatch.setattr(module.os, "replace", fail)
    with pytest.raises(OSError):
        atomic_json(path, {"new": 2})
    assert read_json(path) == {"old": 1}
    assert not list(tmp_path.glob(".write-*"))


def test_live_lock_and_crash_release(tmp_path, config):
    store = Store(tmp_path / "run")
    store.create(config)
    with store.lock():
        with pytest.raises(RuntimeError, match="active controller"):
            with Store(store.root).lock():
                pass
    with store.lock():
        pass
    assert (store.root / "run.lock").exists()


def test_snapshot_identity_and_credentials(tmp_path):
    from tests.conftest import require_symlinks
    require_symlinks(tmp_path)
    source = tmp_path / "source"
    source.mkdir()
    (source / "index.html").write_text("test")
    (source / ".env.hunyuan").write_text("secret")
    (source / ".codex").mkdir()
    (source / ".codex" / "auth.json").write_text("secret")
    target = tmp_path / "copy"
    snapshot(source, target)
    assert sorted(p.name for p in target.iterdir()) == ["index.html"]
    assert version(source) == version(target)
    (target / "index.html").write_text("changed")
    assert version(source) != version(target)
    (source / "leak").symlink_to(source / ".env.hunyuan")
    with pytest.raises(ValueError, match="symlinks"):
        snapshot(source, tmp_path / "reject")


def test_snapshot_prunes_browser_caches_and_keeps_authored_files(tmp_path, monkeypatch):
    from tests.conftest import require_symlinks
    require_symlinks(tmp_path)
    source = tmp_path / "source"
    source.mkdir()
    authored = {"index.html": "game", "assets/car.glb": "model", "assets/card.png": "image",
                "tests/browser.cjs": "test source", "package-lock.json": "lock",
                ".gitignore": "assets/\n"}  # Arbitrary .gitignore must not hide game assets.
    for name, body in authored.items():
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
    original_version = version(source)
    for name in (".runtime", ".browsers", ".npm-cache", "test-results", "playwright-report"):
        cache = source / name
        cache.mkdir()
        (cache / "lib.so.1").symlink_to(tmp_path / "missing-native-library")
        (cache / "external").symlink_to(tmp_path, target_is_directory=True)
        (cache / "download.bin").write_bytes(b"temporary dependency")
    (source / "node_modules").symlink_to(tmp_path, target_is_directory=True)
    scandir = module.os.scandir

    def guarded_scan(path):
        assert not module.ignored(Path(path).relative_to(source)), "cache must never be traversed"
        return scandir(path)

    with monkeypatch.context() as patch:
        patch.setattr(module.os, "scandir", guarded_scan)
        assert version(source) == original_version
        snapshot(source, tmp_path / "copy")
    assert set(module.inventory(tmp_path / "copy")) == set(authored)
    assert version(tmp_path / "copy") == original_version
    (source / "assets/car.glb").write_text("improved model")
    assert version(source) != original_version


@pytest.mark.parametrize("link_type", ["file", "directory", "dangling"])
def test_authored_asset_symlinks_remain_rejected(tmp_path, link_type):
    from tests.conftest import require_symlinks
    require_symlinks(tmp_path)
    source = tmp_path / "source"
    assets = source / "assets"
    assets.mkdir(parents=True)
    external = tmp_path / "external"
    if link_type == "directory":
        external.mkdir()
    elif link_type == "file":
        external.write_text("not a cache")
    (assets / "model").symlink_to(external, target_is_directory=link_type == "directory")
    with pytest.raises(ValueError, match="symlinks: assets/model"):
        version(source)


def test_missing_snapshot_source_is_not_an_empty_version(tmp_path):
    with pytest.raises(FileNotFoundError):
        version(tmp_path / "missing")


def test_redaction_and_child_env(tmp_path, monkeypatch):
    secret = "unit-test-credential-value"
    SECRETS.add(secret)
    monkeypatch.setenv("HUNYUAN_API_KEY", secret)
    monkeypatch.setenv("OPENAI_API_KEY", secret)
    assert "HUNYUAN_API_KEY" not in child_env()
    assert "OPENAI_API_KEY" not in child_env()
    value = {"Authorization": secret, "error": f"Bearer {secret}", "nested": [secret]}
    assert secret not in json.dumps(redact(value))
    path = tmp_path / "redacted.json"
    atomic_json(path, value)
    assert secret not in path.read_text()


def test_config_defaults_paths_and_rejection(tmp_path):
    path = tmp_path / "config.json"
    atomic_json(path, {"engine": "godot", "brief": "test"})
    cfg = load_config(path)
    assert cfg["rounds"] is None
    assert cfg["model"] == "gpt-6-astra"
    assert cfg["assets"]["model_3d"] == "hy-3d-3.1"
    assert cfg["entry"] == "project.godot"
    atomic_json(path, {"engine": "godot", "brief": "test", "typo": 1})
    with pytest.raises(ValidationError):
        load_config(path)
    atomic_json(path, {"engine": "html", "brief": "test", "entry": "../secret"})
    with pytest.raises(ValueError):
        load_config(path)


def test_cli_prepare_status_stop_report_never_calls_backend(tmp_path, monkeypatch, capsys):
    import game_agent_lite.cli as cli
    def forbidden(*args, **kwargs):
        pytest.fail("prepare/status/stop/report must not instantiate Loop")
    monkeypatch.setattr(cli, "Loop", forbidden)
    cfg = tmp_path / "config.json"
    atomic_json(cfg, {"engine": "html", "brief": "test"})
    root = tmp_path / "run"
    assert main(["start", str(cfg), "--run-dir", str(root), "--prepare-only"]) == 0
    assert main(["status", str(root)]) == 0
    assert main(["stop", str(root)]) == 0
    assert (root / "STOP").exists()
    assert main(["report", str(root)]) == 0
    assert (root / "report.md").exists()
    assert "Prepared only" in capsys.readouterr().out


def test_cli_invalid_and_duplicate_dir(tmp_path, capsys):
    assert main(["status", str(tmp_path / "missing")]) == 1
    cfg = tmp_path / "bad.json"
    atomic_json(cfg, {"engine": "bad", "brief": "test"})
    assert main(["start", str(cfg), "--prepare-only"]) == 1
    assert "ValidationError" in capsys.readouterr().err


@pytest.mark.parametrize("original_brief", ["Old elaborate game requirements", None])
def test_cli_prompt_replaces_and_persists_request_without_launch(tmp_path, monkeypatch, original_brief):
    import game_agent_lite.cli as cli
    def forbidden(*args, **kwargs):
        pytest.fail("--prepare-only must not invoke any backend")
    monkeypatch.setattr(cli, "Loop", forbidden)
    cfg = tmp_path / "config.json"
    original = {"engine": "html"}
    if original_brief is not None:
        original["brief"] = original_brief
    atomic_json(cfg, original)
    prompt = '制作一个好玩的卡牌游戏。\nLiteral text: $(never-execute) "quote"'
    root = tmp_path / "run"
    assert main(["start", str(cfg), "--prompt", prompt, "--run-dir", str(root), "--prepare-only"]) == 0
    assert read_json(root / "config.json")["brief"] == prompt
    assert read_json(cfg) == original


@pytest.mark.parametrize("prompt", ["", " \n\t"])
def test_empty_cli_prompt_rejected_before_run_creation(tmp_path, prompt):
    cfg = tmp_path / "config.json"
    atomic_json(cfg, {"engine": "html", "brief": "fallback must not be used"})
    root = tmp_path / "run"
    assert main(["start", str(cfg), "--prompt", prompt, "--run-dir", str(root), "--prepare-only"]) == 1
    assert not root.exists()


def test_command_redacts_stream_and_cancels_process(tmp_path):
    log = tmp_path / "process.log"
    CommandRunner().run([sys.executable, "-c", "print('Authorization: Bearer abc123')"], tmp_path, log)
    assert "abc123" not in log.read_text(encoding="utf-8")
    wide = tmp_path / "wide.log"
    CommandRunner().run([sys.executable, "-c", "import sys; sys.stdout.buffer.write('🙂\\n'.encode())"], tmp_path, wide)
    assert "🙂" in wide.read_text(encoding="utf-8")
    calls = 0
    def stop():
        nonlocal calls
        calls += 1
        if calls > 3:
            raise Stopped()
    with pytest.raises(Stopped):
        CommandRunner().run([sys.executable, "-c", "import time; time.sleep(30)"], tmp_path, log, tick=stop)


def test_command_nonzero(tmp_path):
    with pytest.raises(RuntimeError, match="exited with 7"):
        CommandRunner().run([sys.executable, "-c", "raise SystemExit(7)"], tmp_path, tmp_path / "fail.log")


def test_exited_parent_does_not_leave_stdout_holding_grandchild(tmp_path):
    import time
    began = time.monotonic()
    script = "import subprocess,sys; subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'])"
    CommandRunner().run([sys.executable, "-c", script], tmp_path, tmp_path / "child.log")
    assert time.monotonic() - began < 5
