"""Prepare a stopped assets stage for hy-image-v3, without any provider calls.

Explicitly retries only definitively failed legacy images on the next resume.
Successful receipts remain byte-for-byte intact. Run with the repository venv.
"""
import argparse
import copy
import hashlib
import json
from pathlib import Path

from game_agent_lite.assets import Assets
from game_agent_lite.paths import load_saved_config
from game_agent_lite.store import (Store, artifact, atomic_json, read_json, safe_path,
                                   verify_artifact, version)
from game_agent_lite.types import ASSET_SCHEMA, CONFIG_SCHEMA, validate

OLD, NEW = "hy-image-v3.0", "hy-image-v3"


def fingerprint(assets, request, project, model):
    payload = assets.payload(request, project, {}, model)
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def prepare(run_dir: Path) -> dict:
    store = Store(run_dir)
    with store.lock():
        state = store.load()
        if state["status"] not in ("failed", "stopped") or state["phase"] != "assets":
            raise ValueError("Migration requires a failed/stopped run at the assets stage")
        directory = store.round_dir(state["round"])
        if (directory / "assets.json").exists():
            raise ValueError("Asset stage is already committed; do not migrate its receipts")
        record = store.root / "migrations" / f"hy-image-v3-round-{state['round']:04d}.json"
        draft = read_json(directory / "draft.json")
        project = safe_path(store.root, draft["project"])
        if version(project) != draft["version"]:
            raise ValueError("Draft changed; migration must not alter or accept a changed game")
        if record.exists():
            plan = read_json(record)
        else:
            config = read_json(store.root / "config.json")
            validate(config, CONFIG_SCHEMA)
            if config["assets"]["image_model"] != OLD:
                raise ValueError("This migration only accepts the legacy hy-image-v3.0 config")
            assets = Assets(load_saved_config(store.root))  # Does not load credentials or call the provider.
            updated = copy.deepcopy(config)
            updated["assets"]["image_model"] = NEW
            plan = {"round": state["round"], "from_model": OLD, "to_model": NEW,
                    "preserved": [], "completed_images": [], "retry_images": [],
                    "changes": [{"path": "config.json",
                                 "before": (store.root / "config.json").read_text(),
                                 "after": updated}]}
            for path in (directory / "design.json", directory / "draft.json", store.root / "state.json"):
                plan["preserved"].append(artifact(path, store.root))
            for request in read_json(directory / "design.json")["assets"]:
                validate(request, ASSET_SCHEMA)
                if request["kind"] != "image":
                    continue
                path = safe_path(directory / "assets", request["id"]) / "job.json"
                if not path.exists():
                    continue  # Never submitted; resume will use the new config.
                job = read_json(path)
                if (job["id"], job["kind"], job["model"], job["fingerprint"]) != (
                        request["id"], "image", OLD, fingerprint(assets, request, project, OLD)):
                    raise ValueError("Image receipt does not match its original request")
                if job["status"] == "completed":
                    if not verify_artifact(job["artifact"], directory / "assets"):
                        raise ValueError("Completed asset file is missing or changed")
                    plan["preserved"].extend([artifact(path, store.root), artifact(
                        safe_path(directory / "assets", job["artifact"]["path"]), store.root)])
                    plan["completed_images"].append(job["id"])
                elif job["status"] == "failed" and not any(
                        key in job for key in ("data", "job_id", "artifact")):
                    plan["retry_images"].append(job["id"])
                    plan["changes"].append({"path": path.relative_to(store.root).as_posix(),
                        "before": path.read_text(), "after": {
                            "id": job["id"], "kind": "image", "model": NEW, "status": "new",
                            "fingerprint": fingerprint(assets, request, project, NEW)}})
                else:
                    raise ValueError("Only completed or definitively failed images can migrate; "
                                     "reconcile/download pending results first")

        # Validate everything before any writes. The immutable journal also allows
        # rerunning after interruption between config and failed-receipt writes.
        if not all(verify_artifact(item, store.root) for item in plan["preserved"]):
            raise ValueError("Preserved files changed since migration was prepared")
        for change in plan["changes"]:
            path = safe_path(store.root, change["path"])
            if path.read_text() != change["before"] and read_json(path) != change["after"]:
                raise ValueError("Migration target changed; refusing to overwrite it")
        if not record.exists():
            atomic_json(record, plan)
        for change in plan["changes"]:
            path = safe_path(store.root, change["path"])
            if read_json(path) != change["after"]:
                atomic_json(path, change["after"])
        store.event("image_model_migration_prepared", round=state["round"], model=NEW,
                    preserved_images=plan["completed_images"], retry_images=plan["retry_images"],
                    record=record.relative_to(store.root).as_posix())
        return {"model": NEW, "preserved_images": plan["completed_images"],
                "retry_images": plan["retry_images"], "started": False}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path)
    print(json.dumps(prepare(parser.parse_args().run_dir), ensure_ascii=False))
