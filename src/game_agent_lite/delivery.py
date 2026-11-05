"""Portable source-and-assets export, without rerunning models or games."""
from pathlib import Path
from .host import path_inside
from .store import atomic_json, inventory, read_json, safe_path, snapshot, version


def export_game(run_dir, output, round_number=None):
    root, output = Path(run_dir).resolve(), Path(output).absolute()
    if output.exists() or output.is_symlink():
        raise FileExistsError("Export destination already exists")
    output = output.resolve()
    if path_inside(output, root) or path_inside(root, output):
        raise ValueError("Export must be outside the run")
    state = read_json(root / "state.json")
    if round_number is None:
        selected = state.get("best")
        if not selected:
            raise ValueError("No retained game is available")
        round_number = selected["round"]
    if type(round_number) is not int or round_number < 0:
        raise ValueError("Round must be a nonnegative integer")
    receipt = read_json(root / "rounds" / f"{round_number:04d}" / "check.json")
    source = safe_path(root, receipt["project"])
    if not receipt.get("passed") or version(source) != receipt["version"]:
        raise ValueError("Game has changed or did not pass its saved project checks")
    snapshot(source, output / "game")
    if version(output / "game") != receipt["version"] or version(source) != receipt["version"]:
        raise ValueError("Game changed during export; do not use this incomplete export")
    config = read_json(root / "config.json")
    atomic_json(output / "manifest.json", {"format": "rgc-game-export-v1", "round": round_number,
        "engine": config["engine"], "entry": "game/" + config["entry"],
        "version": receipt["version"], "files": inventory(output / "game")})
    return output
