"""Portable saved settings; absolute paths exist only at execution boundaries."""
from copy import deepcopy
import os
from pathlib import Path

from .types import CONFIG_SCHEMA, validate

TOOLS = ("codex", "harbor", "godot", "blender", "ffmpeg")


def resolve_paths(config, base):
    result = deepcopy(config)
    base = Path(base).resolve()
    def resolve(value):
        return str((base / Path(value).expanduser()).resolve())
    for name in ("source", "auth_file"):
        if result.get(name):
            result[name] = resolve(result[name])
    if result.get("assets", {}).get("env_file"):
        result["assets"]["env_file"] = resolve(result["assets"]["env_file"])
    if result.get("benchmark"):
        result["benchmark"]["path"] = resolve(result["benchmark"]["path"])
    for name in TOOLS:
        if "/" in result.get(name, ""):
            result[name] = resolve(result[name])
    return result


def portable_config(config, base):
    """Rebase resolved paths when saving, without persisting the host home path."""
    result = deepcopy(config)
    base = Path(base).resolve()
    def relative(value):
        path = Path(value).expanduser()
        return os.path.relpath(path, base) if path.is_absolute() else value
    for name in ("source", "auth_file"):
        if result.get(name):
            result[name] = relative(result[name])
    # The standard login belongs to the user of the new machine, not this run.
    if config.get("auth_file") and Path(config["auth_file"]).expanduser() == Path.home() / ".codex/auth.json":
        result["auth_file"] = "~/.codex/auth.json"
    if result.get("assets", {}).get("env_file"):
        result["assets"]["env_file"] = relative(result["assets"]["env_file"])
    if result.get("benchmark"):
        result["benchmark"]["path"] = relative(result["benchmark"]["path"])
    for name in TOOLS:
        if "/" in result.get(name, ""):
            result[name] = relative(result[name])
    return result


def load_saved_config(root):
    from .store import read_json
    root = Path(root).resolve()
    return resolve_paths(validate(read_json(root / "config.json"), CONFIG_SCHEMA), root)
