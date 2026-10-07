"""Explicit, model-free GameCraft demo recording. Normal Player has no time cap."""
from copy import deepcopy
from pathlib import Path

from .game import make_game
from .gamecraft import MAX_DEMOS, MAX_SECONDS, _checked, _demo_time_limit, _scenario_map, export_gamecraft
from .policy import PolicySession
from .store import atomic_json, read_json, safe_path, version
from .types import CONFIG_SCHEMA, DEFAULTS, validate


def prepare_gamecraft(run_dir, output, *, round_number=None, episodes=None,
                      scenarios=None, min_key_release_ms=0, dry_run=False, stop=lambda: None):
    """Reuse an existing .gd Policy without a model call or source mutation.

    Profiles retain their original semantics. A long/mismatched profile may hit
    the deadline; retain truncated status and export checked inputs, never claim victory.
    """
    root, output = Path(run_dir).resolve(), Path(output).absolute()
    if type(min_key_release_ms) is not int or not 0 <= min_key_release_ms <= 2000:
        raise ValueError("min_key_release_ms must be an integer in 0..2000")
    if output.exists() or output.is_symlink():
        raise FileExistsError("Preparation output exists; choose a new directory")
    output = output.resolve()
    if output.is_relative_to(root) or root.is_relative_to(output):
        raise ValueError("Preparation output must be outside the source run")
    from .paths import load_saved_config
    config = load_saved_config(root)
    if config["engine"] != "godot" or config["backend"] == "mock":
        raise ValueError("GameCraft preparation requires a real Godot run")
    if round_number is None:
        best = read_json(root / "state.json").get("best")
        if not best:
            raise ValueError("No best version; select --round N")
        round_number = best["round"]
    if type(round_number) is not int or round_number < 0:
        raise ValueError("Round must be non-negative")
    directory = safe_path(root, f"rounds/{round_number:04d}")
    candidate = read_json(directory / "check.json")
    source = safe_path(root, candidate["project"])
    if not candidate.get("passed") or version(source) != candidate["version"]:
        raise ValueError("Game changed or did not pass its build check")
    receipt = read_json(directory / "played-candidate.json")
    report_path = _checked({"path": receipt["report"], "sha256": receipt["sha256"]}, root)
    report = read_json(report_path)
    job = report_path.parent
    if report.get("format") != "policy-v1" or report["version"] != candidate["version"]:
        raise ValueError("Saved Player report does not match this game")
    policy = _checked(report["policy"], job)
    if policy.suffix != ".gd" or report["settings"].get("policy_backend") != "godot":
        raise ValueError("Preparation needs an existing Godot policy.gd; legacy Python is not silently converted")
    plan = read_json(_checked(report["plan"], job))
    selected = list(range(1, len(plan["episodes"]) + 1)) if episodes is None else list(episodes)
    if (not 1 <= len(selected) <= MAX_DEMOS or len(set(selected)) != len(selected)
            or any(type(n) is not int or not 1 <= n <= len(plan["episodes"]) for n in selected)):
        raise ValueError("Select 1 to 10 distinct existing episodes")
    selected.sort()
    scenarios = _scenario_map(scenarios, selected)
    specs = [plan["episodes"][n - 1] for n in selected]
    execution = {"purpose": "gamecraft_demo", "max_seconds": MAX_SECONDS,
                 "fresh_launch": True, "viewport": [1280, 720],
                 "scenarios": {plan["episodes"][n - 1]["profile"]: name for n, name in scenarios.items()}}
    if min_key_release_ms:
        execution["min_key_release_ms"] = min_key_release_ms
    preview = {"round": round_number, "source_version": candidate["version"],
               "source_policy_sha256": report["policy"]["sha256"], "source_episodes": selected,
               "execution": execution, "model_calls": False, "judge_calls": False}
    if dry_run:
        return preview
    output.mkdir(parents=True)
    atomic_json(output / "preparation.json", preview)
    # Keep the recording environment used by the run. Do not rewrite run/config.json.
    config["player"] = {**DEFAULTS["player"], **config.get("player", {}),
                        "mode": "policy", "trajectories_per_version": len(specs)}
    session = PolicySession(config, source, output / "player", candidate["version"], make_game,
                            stop, execution=execution)
    preparation, _ = session.prepare()
    frozen = session.freeze_policy(policy, {"summary": "Reuse recorded Policy for GameCraft demos",
        "episodes": specs}, preparation, None, author_called=False)
    if frozen["policy"]["sha256"] != report["policy"]["sha256"]:
        raise ValueError("Source Policy changed while preparing demos")
    attempts = session.run_episodes(specs, frozen["policy"])
    prepared = {"format": "policy-v1", **frozen, "episodes": attempts,
                "trajectories_per_version": len(specs), "summary": None, "analysis_evidence_valid": False}
    atomic_json(session.job / "play.json", prepared)
    outcomes = [read_json(_checked(item, session.job)) for item in attempts]
    ready = all((e["status"] == "completed" and 0 < e["elapsed_seconds"] <= MAX_SECONDS)
                or _demo_time_limit(e, prepared["settings"]) for e in outcomes)
    status = {"ready_for_export": False, "replay_verified": False, "goal_verified": None,
        "episodes": [{k: e[k] for k in ("profile", "status", "reason", "elapsed_seconds")} for e in outcomes]}
    atomic_json(output / "status.json", status)
    if not ready:
        raise ValueError("Demo execution failed, was interrupted, or has no supported completion/time-limit stop; evidence retained in "
                         + str(output) + ". No automatic retry or success claim.")
    destination = export_gamecraft(root, output / "submission", round_number=round_number,
                                  prepared_dir=output)
    status["ready_for_export"] = True
    atomic_json(output / "status.json", status)
    return {**preview, "submission": str(destination), "replay_verified": False}
