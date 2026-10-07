"""Regression for five paid legacy images followed by an HTTP 402 rejection."""
import base64
import importlib.util
from pathlib import Path

import pytest

from game_agent_lite.assets import Assets, RejectedRequest, TokenHub
from game_agent_lite.store import Store, atomic_json, read_json, version

spec = importlib.util.spec_from_file_location("migration", Path(__file__).resolve().parents[1] /
                                            "scripts/migrate_hunyuan_v3.py")
migration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migration)
PNG = b"\x89PNG\r\n\x1a\nfixture"


class Client:
    def __init__(self, fail_after=None):
        self.calls, self.fail_after = [], fail_after

    def key(self):
        return "offline-fixture"

    def post(self, route, payload):
        self.calls.append((route, payload))
        if self.fail_after is not None and len(self.calls) > self.fail_after:
            raise RejectedRequest("Provider rejected request (HTTP 402)")
        return {"data": [{"b64_json": base64.b64encode(PNG).decode()}]}


@pytest.fixture
def legacy(tmp_path, config, monkeypatch):
    config["assets"]["image_model"] = "hy-image-v3.0"
    store = Store(tmp_path / "run")
    store.create(config)
    directory = store.round_dir(0)
    project = directory / "draft/project"
    project.mkdir(parents=True)
    (project / "index.html").write_text("<title>unchanged game</title>")
    requests = [{"id": f"image-{i}", "kind": "image", "prompt": f"art {i}", "source": None}
                for i in range(10)]
    atomic_json(directory / "design.json", {"assets": requests})
    atomic_json(directory / "draft.json", {
        "project": project.relative_to(store.root).as_posix(), "version": version(project)})
    with pytest.raises(RejectedRequest):
        Assets(config, client=Client(fail_after=5)).generate(requests, project, directory / "assets")
    store.save({**store.load(), "status": "failed", "phase": "assets", "error": "HTTP 402"})
    preserved = [store.root / "state.json", directory / "design.json", directory / "draft.json",
                 project / "index.html"]
    preserved += [p for i in range(5) for p in (directory / "assets" / f"image-{i}").iterdir()]
    before = {p: p.read_bytes() for p in preserved}
    def no_credentials(*args):
        raise AssertionError("Migration must not load provider credentials")
    monkeypatch.setattr(TokenHub, "key", no_credentials)
    return store, directory, project, requests, before


def test_migrate_preserves_five_images_and_resumes_only_remaining_five(legacy):
    store, directory, project, requests, before = legacy
    failed = (directory / "assets/image-5/job.json").read_text()
    result = migration.prepare(store.root)
    assert result["preserved_images"] == [f"image-{i}" for i in range(5)]
    assert result["retry_images"] == ["image-5"] and result["started"] is False
    assert migration.prepare(store.root) == result  # No second reset/submission.
    journal = read_json(store.root / "migrations/hy-image-v3-round-0000.json")
    assert journal["changes"][1]["before"] == failed
    config = read_json(store.root / "config.json")
    assert config["assets"]["image_model"] == "hy-image-v3"
    assert config["rounds"] == 2  # Migration never changes round/budget settings.
    client = Client()
    outputs = Assets(config, client=client).generate(requests, project, directory / "assets")
    assert len(client.calls) == 5
    assert all(route == "/wand/hunyuan-image/v3-generation" and
               payload["model"] == "hy-image-v3" and payload["size"] == "1024x1024"
               for route, payload in client.calls)
    assert [x["model"] for x in outputs] == ["hy-image-v3.0"] * 5 + ["hy-image-v3"] * 5
    assert all(path.read_bytes() == content for path, content in before.items())
    assert Assets(config, client=client).generate(requests, project, directory / "assets") == outputs
    assert len(client.calls) == 5
    with pytest.raises(ValueError, match="target changed"):
        migration.prepare(store.root)  # Cannot reset a now-completed retry.


@pytest.mark.parametrize("status", ["uncertain", "submitting", "download", "queued"])
def test_migration_refuses_pending_jobs_without_modifying_config(legacy, status):
    store, directory, *_ = legacy
    path = directory / "assets/image-5/job.json"
    atomic_json(path, {**read_json(path), "status": status})
    original = (store.root / "config.json").read_bytes()
    with pytest.raises(ValueError, match="reconcile/download"):
        migration.prepare(store.root)
    assert (store.root / "config.json").read_bytes() == original
    assert not (store.root / "migrations").exists()


def test_migration_retains_fingerprint_and_asset_integrity_checks(legacy):
    store, directory, project, requests, _ = legacy
    migration.prepare(store.root)
    assets = Assets(read_json(store.root / "config.json"), client=Client())
    root = directory / "assets"
    with pytest.raises(ValueError, match="parameters changed"):
        assets.generate([{**requests[0], "prompt": "changed art"}], project, root)
    (root / "image-0/asset.png").write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="missing or changed"):
        assets.generate([requests[0]], project, root)


def test_migration_refuses_tampered_artifact_before_any_changes(legacy):
    store, directory, *_ = legacy
    original = (store.root / "config.json").read_bytes()
    (directory / "assets/image-0/asset.png").write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="missing or changed"):
        migration.prepare(store.root)
    assert (store.root / "config.json").read_bytes() == original


def test_migration_recovers_interruption_between_config_and_job_writes(legacy, monkeypatch):
    store, directory, _, _, before = legacy
    original_write = migration.atomic_json
    def interrupt(path, *args, **kwargs):
        if path.name == "job.json":
            raise OSError("simulated interruption")
        return original_write(path, *args, **kwargs)
    monkeypatch.setattr(migration, "atomic_json", interrupt)
    with pytest.raises(OSError, match="interruption"):
        migration.prepare(store.root)
    assert read_json(store.root / "config.json")["assets"]["image_model"] == "hy-image-v3"
    assert read_json(directory / "assets/image-5/job.json")["status"] == "failed"
    monkeypatch.setattr(migration, "atomic_json", original_write)
    migration.prepare(store.root)
    assert read_json(directory / "assets/image-5/job.json")["status"] == "new"
    assert all(path.read_bytes() == content for path, content in before.items())


def test_migration_requires_inactive_controller_and_assets_stage(legacy):
    store, *_ = legacy
    with store.lock(), pytest.raises(RuntimeError, match="active controller"):
        migration.prepare(store.root)
    store.save({**store.load(), "phase": "integrate"})
    with pytest.raises(ValueError, match="assets stage"):
        migration.prepare(store.root)
