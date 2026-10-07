"""Small Player hook: record once, export real inputs, replay upstream, retain evidence."""
from copy import deepcopy
from pathlib import Path
import math
import os
import re
import shutil
import uuid

from .agent import CommandRunner, PACKAGE
from .gamecraft import (MAX_SECONDS, _demo_time_limit, export_recorded_report,
                        validate_gamecraft_submission)
from .store import copy_file
from .store import (Stopped, artifact, atomic_json, file_hash, read_json,
                    safe_path, verify_artifact)
from .types import GAMECRAFT_COVERAGE_SCHEMA, GAMECRAFT_COVERAGE_RESULTS_SCHEMA, validate

EVAL_IMAGE = "rsi-gamecreator/gamecraft-eval:godot-4.6.2-v1"


def validate_profile(config):
    if not config.get("benchmark"):
        return
    if config["engine"] != "godot" or config.get("player", {}).get("mode", "policy") != "policy":
        raise ValueError("GameCraft Player requires Godot policy mode")
    if not 1 <= config.get("player", {}).get("trajectories_per_version", 3) <= 10:
        raise ValueError("GameCraft requires 1 to 10 Player episodes")
    if config.get("backend") != "mock" and config.get("runtime", {}).get("mode") != "docker":
        raise ValueError("GameCraft recording/replay requires Docker isolation")


def configure(config):
    validate_profile(config)
    config = deepcopy(config)
    config["runtime"]["image"] = config["benchmark"].get("image", EVAL_IMAGE)
    execution = {"purpose": "gamecraft_demo", "max_seconds": MAX_SECONDS,
                 "fresh_launch": True, "viewport": [1280, 720], "scenarios": {}}
    if config["benchmark"].get("evidence_source") == "player_recording":
        execution["evidence_source"] = "player_recording"
    return config, execution


def context(config):
    """Do not send benchmark paths, test rubrics or judge credentials to the role."""
    source = config["benchmark"].get("evidence_source", "official_replay")
    return {"name": "gamecraft", "task": config["benchmark"]["task"],
            "max_seconds": 20,
            "goal": "complementary demonstrations of the public brief; each at most 20 seconds; victory only where relevant",
            "viewport": [1280, 720], "replay_fps": None if source == "player_recording" else 30,
            "evidence_source": source}


def coverage_plan_warnings(plan):
    """Check references, not semantic completeness or whether a feature works."""
    rows = validate(plan["coverage"], GAMECRAFT_COVERAGE_SCHEMA)
    ids = [row["id"] for row in rows]
    if len(set(ids)) != len(ids):
        raise ValueError("GameCraft coverage requirement IDs must be unique")
    assignments = {i: set() for i in range(1, len(plan["episodes"]) + 1)}
    warnings = []
    for row in rows:
        if not row["episodes"]:
            if not row["omission_reason"].strip():
                raise ValueError("Unassigned GameCraft requirement needs an omission_reason")
            warnings.append(f"{row['id']}: requirement has no assigned episode")
        for number in row["episodes"]:
            if number not in assignments:
                raise ValueError("GameCraft coverage references an unavailable episode")
            assignments[number].add(row["id"])
    if any(not ids for ids in assignments.values()):
        raise ValueError("Every GameCraft episode needs a coverage assignment")
    for number, ids in assignments.items():
        for previous in range(1, number):
            if ids == assignments[previous]:
                warnings.append(f"Episodes {previous} and {number} have identical coverage assignments")
    return warnings


def save_coverage_assessment(job, io, report):
    """Bind Player's coverage claims to the frozen plan and actual evidence files."""
    if not verify_artifact(report["plan"], job):
        raise ValueError("GameCraft coverage plan changed")
    plan = read_json(safe_path(job, report["plan"]["path"]))
    if "coverage" not in plan:  # Historical attempts remain recoverable without new model calls.
        return None
    warnings = coverage_plan_warnings(plan)
    rows = validate(read_json(io / "coverage-results.json"), GAMECRAFT_COVERAGE_RESULTS_SCHEMA)
    if [r["id"] for r in rows] != [r["id"] for r in plan["coverage"]]:
        raise ValueError("Coverage results must contain every planned requirement in order")
    rows = deepcopy(rows)
    native = report["settings"].get("benchmark", {}).get("evidence_source") == "player_recording"
    for requirement, row in zip(plan["coverage"], rows):
        for side, folder in (("recording", "episodes"), ("replay", "replay")):
            finding = row[side]
            refs = finding["evidence"]
            if native and side == "replay" and (finding["status"] != "unverified" or refs):
                raise ValueError("Native recording mode did not replay; replay evidence must be unverified/empty")
            if finding["status"] in ("shown", "partial") and not any(p.endswith(".png") for p in refs):
                raise ValueError("Shown/partial coverage requires an actual PNG from that execution")
            for path in refs:
                match = re.fullmatch(r"evidence/" + folder + r"/([0-9]{4})/.+", path)
                if (not match or ".." in Path(path).parts or int(match[1]) not in requirement["episodes"]
                        or not safe_path(io, path).is_file()):
                    raise ValueError("Coverage cited wrong side/episode or unavailable evidence")
            # Persist hashes of the inspected copies, with paths relative to the Player job.
            finding["evidence"] = [artifact(safe_path(io, path), job) for path in refs]
    result = {"version": report["version"], "plan_sha256": report["plan"]["sha256"],
              "assessment_source": "player_evidence_review", "independently_verified": False,
              "requirements": rows, "warnings": warnings}
    atomic_json(job / "coverage-results.json", result)
    return artifact(job / "coverage-results.json", job)


def check_coverage_assessment(report, root, plan):
    if "coverage" not in plan:
        return
    coverage_plan_warnings(plan)
    if not verify_artifact(report.get("gamecraft_coverage", {}), root):
        raise ValueError("GameCraft coverage assessment missing or changed")
    data = read_json(safe_path(root, report["gamecraft_coverage"]["path"]))
    if (data["version"] != report["version"] or data["plan_sha256"] != report["plan"]["sha256"]
            or [r["id"] for r in data["requirements"]] != [r["id"] for r in plan["coverage"]]):
        raise ValueError("GameCraft coverage assessment does not match the plan/version")
    for row in data["requirements"]:
        for side in ("recording", "replay"):
            if any(not verify_artifact(ref, root) for ref in row[side]["evidence"]):
                raise ValueError("GameCraft coverage evidence changed")


def replay_submission(config, submission, output, image_id, stop):
    """Use the recording image ID, no keys/network, official replay code read-only."""
    from .runtime import OWNER_LABEL, cleanup_owner, options
    benchmark = Path(config["benchmark"]["path"]).resolve()
    if not (benchmark / "gamecraft_bench/verifier/replay.py").is_file():
        raise ValueError("GameCraft replay source not found at benchmark.path")
    marker = output.with_name(output.name + "-runtime-owner.json")
    cleanup_owner(marker, graceful=True)
    token, name = uuid.uuid4().hex, "rsi-game-" + uuid.uuid4().hex
    output.mkdir(parents=True, exist_ok=True)
    argv = ["docker", "run", "--rm", "--pull=never", "--init", "--name", name,
            "--label", OWNER_LABEL + "=" + token, "--network", "none", "--read-only",
            "--user", f"{os.getuid()}:{os.getgid()}", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges", "--shm-size", "1g",
            "--tmpfs", "/tmp:rw,nosuid,nodev", "--workdir", "/output"]
    for path, target, readonly in ((submission, "/submission", True),
            (benchmark, "/opt/gamecraft-bench", True), (PACKAGE, "/framework/game_agent_lite", True),
            (output, "/output", False)):
        if "," in str(path):
            raise ValueError("Docker mount paths cannot contain commas")
        argv += ["--mount", f"type=bind,source={path.resolve()},target={target}" + (",readonly" if readonly else "")]
    render_threads = options(config)["render_threads"]
    for setting in ("PYTHONPATH=/framework:/opt/gamecraft-bench", "PYTHONDONTWRITEBYTECODE=1",
                    "HOME=/tmp", "XDG_RUNTIME_DIR=/tmp", "LIBGL_ALWAYS_SOFTWARE=1",
                    f"LP_NUM_THREADS={render_threads}"):
        argv += ["--env", setting]
    argv += ["--entrypoint", "python3", image_id, "-u", "-m", "game_agent_lite.gamecraft_replay_worker"]
    atomic_json(marker, {"container": name, "owner": token})
    try:
        atomic_json(output / "container-runtime.json", {"image_id": image_id,
                    "renderer": "mesa-software", "render_threads": render_threads})
        CommandRunner().run(argv, output, output / "process.log", tick=stop)
    finally:
        cleanup_owner(marker, graceful=True)


def prepare_replay(session, report):
    """Persist before analysis; restart never silently replays a completed attempt."""
    receipt = session.job / "gamecraft.json"
    if receipt.exists():
        data = read_json(receipt)
        check_bundle(data, session.job, report)
        return data
    if session.config["benchmark"].get("evidence_source") == "player_recording":
        from .gamecraft_recordings import export_recordings, validate_recordings
        atomic_json(session.job / "recording.json", report)
        folder = session.job / "gamecraft/recordings"
        if folder.exists() and not (folder / "manifest.json").is_file():
            folder.rename(folder.with_name("interrupted-recordings-" + uuid.uuid4().hex[:10]))
        if not folder.exists():
            export_recordings(session, report, folder)
        validate_recordings(folder)
        result = {"version": session.version, "task": session.config["benchmark"]["task"],
                  "status": "recorded", "evidence_source": "player_recording", "replay_performed": False,
                  "reason": "Native Player frames retained; no JSON replay performed",
                  "recordings": artifact(folder / "manifest.json", session.job),
                  "submission": None, "episodes": [], "goal_verified": None, "official_score": None}
        atomic_json(receipt, result)
        check_bundle(result, session.job, report)
        return result
    folder = session.job / "gamecraft"
    folder.mkdir(exist_ok=True)
    result = {"version": session.version, "task": session.config["benchmark"]["task"],
              "status": "unavailable", "submission": None, "episodes": [],
              "goal_verified": None, "official_score": None}
    outcomes = [read_json(safe_path(session.job, e["path"])) for e in report["episodes"]]
    # Keep failures in the play report; never silently select only successful recordings.
    eligible = session.config["backend"] != "mock" and all((e["status"] == "completed" and 0 < e["elapsed_seconds"] <= 20)
                   or _demo_time_limit(e, report["settings"]) for e in outcomes)
    if not eligible:
        result["reason"] = "Recording failed or exceeded the supported deadline; all evidence retained"
    else:
        recording = session.job / "recording.json"
        if recording.exists() and read_json(recording) != report:
            raise ValueError("Recording changed before replay")
        atomic_json(recording, report)
        submission = folder / "submission"
        if not submission.exists():
            specs = read_json(safe_path(session.job, report["plan"]["path"]))["episodes"]
            preparation = {"source_policy_sha256": report["policy"]["sha256"],
                           "execution": report["settings"]["execution"]}
            export_recorded_report(session.project, recording, submission, expected=session.version,
                round_number=session.context.get("round", 0), source_project=os.path.relpath(session.project, session.job),
                preparation=preparation, source_episodes=list(range(1, len(specs) + 1)), source_specs=specs)
        _, manifest, _ = validate_gamecraft_submission(submission)
        if manifest["source_report_sha256"] != file_hash(recording):
            raise ValueError("Submission does not match this recording")
        result["submission"] = artifact(submission / "manifest.json", session.job)
        attempt = folder / "replay"
        if not (attempt / "results.json").exists():
            # Interrupted work is retained separately, not mixed with the next capture.
            if attempt.exists():
                attempt.rename(folder / ("interrupted-replay-" + uuid.uuid4().hex[:10]))
            try:
                replay_submission(session.config, submission, attempt,
                                  report["settings"]["runtime"]["image_id"], session.check_stop)
            except (Stopped, KeyboardInterrupt):
                raise
            except (OSError, RuntimeError) as exc:
                result["reason"] = f"Replay infrastructure failed: {type(exc).__name__}: {exc}"
        if (attempt / "results.json").exists():
            raw = read_json(attempt / "results.json")
            result["godot"] = raw["godot"]
            if len(raw["episodes"]) != len(manifest["demos"]):
                raise ValueError("Replay episode count changed")
            for ref, demo in zip(raw["episodes"], manifest["demos"]):
                if not verify_artifact(ref, attempt):
                    raise ValueError("Replay receipt changed")
                item = read_json(safe_path(attempt, ref["path"]))
                if item["episode"] != demo["episode"] or item["trace_sha256"] != demo["trace"]["sha256"]:
                    raise ValueError("Replay trace does not match submission")
                for key in ("frames", "logs", "video"):
                    values = ([item[key]] if item.get(key) else []) if key == "video" else item[key]
                    if any(not verify_artifact(v, attempt) for v in values):
                        raise ValueError("Replay artifacts changed")
                    mapped = [artifact(safe_path(attempt, v["path"]), session.job) for v in values]
                    item[key] = (mapped[0] if mapped else None) if key == "video" else mapped
                result["episodes"].append(item)
            result["status"] = "completed" if all(e["status"] == "completed" for e in result["episodes"]) else "failed"
            result["reason"] = "Official replay results; victory requires separate evidence analysis"
    atomic_json(receipt, result)
    return result


def check_bundle(data, root, report):
    if data["version"] != report["version"]:
        raise ValueError("Replay game version mismatch")
    native = report["settings"].get("benchmark", {}).get("evidence_source") == "player_recording"
    if native or data.get("evidence_source") == "player_recording":
        from .gamecraft_recordings import validate_recordings
        if (not native or data.get("replay_performed") is not False or data.get("episodes") or
                data.get("submission") or data.get("status") != "recorded" or
                not verify_artifact(data.get("recordings", {}), root)):
            raise ValueError("Invalid native recording receipt")
        _, manifest, _ = validate_recordings(safe_path(root, data["recordings"]["path"]).parent)
        if (manifest["source_version"] != report["version"] or manifest["policy"]["sha256"] != report["policy"]["sha256"] or
                manifest["plan"]["sha256"] != report["plan"]["sha256"] or
                [d["record"]["sha256"] for d in manifest["demos"]] != [r["sha256"] for r in report["episodes"]]):
            raise ValueError("Native recording provenance mismatch")
        return
    if data.get("submission"):
        if not verify_artifact(data["submission"], root):
            raise ValueError("Submission manifest changed")
        _, manifest, _ = validate_gamecraft_submission(safe_path(root, data["submission"]["path"]).parent)
        if manifest["source_version"] != report["version"] or manifest["source_policy_sha256"] != report["policy"]["sha256"]:
            raise ValueError("Submission provenance mismatch")
        if manifest["source_report_sha256"] != file_hash(root / "recording.json"):
            raise ValueError("Submission recording changed")
        if data["episodes"] and [(e["episode"], e["trace_sha256"]) for e in data["episodes"]] != [
                (d["episode"], d["trace"]["sha256"]) for d in manifest["demos"]]:
            raise ValueError("Replay episode mapping changed")
    elif data["episodes"]:
        raise ValueError("Replay evidence without a submission")
    for item in data["episodes"]:
        for ref in item["frames"] + item["logs"] + ([item["video"]] if item["video"] else []):
            if not verify_artifact(ref, root):
                raise ValueError("Replay evidence changed")


def export_evidence(report, root, target):
    """Add replay evidence to the existing Player/Reviewer bundle; no game code."""
    if not verify_artifact(report["gamecraft"], root):
        raise ValueError("Replay receipt changed")
    data = read_json(safe_path(root, report["gamecraft"]["path"]))
    check_bundle(data, root, report)
    public = {k: data[k] for k in ("status", "reason", "goal_verified")}
    # Preserve historical replay summaries byte-for-byte for analysis recovery.
    native = data.get("evidence_source") == "player_recording"
    if native:
        public.update(evidence_source="player_recording", replay_performed=False)
    public["episodes"] = []
    for item in data["episodes"]:
        folder = target / "replay" / f"{item['episode']:04d}"
        folder.mkdir(parents=True, exist_ok=True)
        row = {k: item[k] for k in ("episode", "status", "reason")}
        # Keep runtime logs in the private receipt, not in anonymous A/B bundles.
        for key in ("frames", "video"):
            refs = ([item[key]] if item[key] else []) if key == "video" else item[key]
            paths = []
            for index, ref in enumerate(refs):
                src = safe_path(root, ref["path"])
                dest = folder / f"{key}-{index:04d}{src.suffix}"
                copy_file(src, dest)
                paths.append(dest.relative_to(target).as_posix())
            row[key] = (paths[0] if paths else None) if key == "video" else paths
        public["episodes"].append(row)
    summary = read_json(target / "summary.json")
    plan = read_json(safe_path(root, report["plan"]["path"]))
    if "coverage" in plan:
        public["coverage"] = plan["coverage"]
        public["coverage_warnings"] = coverage_plan_warnings(plan)
    summary["gamecraft"] = public
    summary["limitations"].append(
        "Native Player frames only; no official replay was performed. Process completion does not prove victory."
        if native else "Original Policy play and official replay are separate evidence; neither process completion proves victory.")
    atomic_json(target / "summary.json", summary)


def save_goal_assessment(job, io, report):
    """Player evidence judgment, explicitly NOT an independent automatic win oracle."""
    count = len(report["episodes"])
    path = io / "goal_results.json"
    rows = read_json(path) if path.is_file() else [{"episode": i + 1,
        "recording": {"outcome": "unverified", "time_seconds": None, "evidence": []},
        "replay": {"outcome": "unverified", "time_seconds": None, "evidence": []}} for i in range(count)]
    if (not isinstance(rows, list) or any(not isinstance(r, dict) or type(r.get("episode")) is not int for r in rows)
            or [r["episode"] for r in rows] != list(range(1, count + 1))):
        raise ValueError("Goal assessments must cover every episode in order")
    replay = read_json(job / "gamecraft.json")
    native = replay.get("evidence_source") == "player_recording"
    replay_episodes = {e["episode"]: e for e in replay["episodes"]}
    for row in rows:
        for side, prefix in (("recording", "episodes"), ("replay", "replay")):
            value = row.get(side)
            if not isinstance(value, dict) or not {"outcome", "time_seconds", "evidence"} <= value.keys():
                raise ValueError("Goal assessment requires outcome, time_seconds and evidence for each side")
            if value["outcome"] not in ("won", "not_won", "unverified"):
                raise ValueError("Invalid goal outcome")
            t = value.get("time_seconds")
            if t is not None and (type(t) not in (int, float) or not math.isfinite(t) or not 0 <= t <= 20):
                raise ValueError("Goal time must be within the 20-second demo")
            refs = value["evidence"]
            if native and side == "replay" and (value["outcome"] != "unverified" or t is not None or refs):
                raise ValueError("Native recording mode has no replay goal evidence")
            if not isinstance(refs, list) or any(not isinstance(p, str) or
                    not p.startswith(f"evidence/{prefix}/{row['episode']:04d}/") or
                    not safe_path(io, p).is_file() for p in refs):
                raise ValueError("Goal assessment cited wrong episode or unavailable evidence")
            if value["outcome"] == "won" and (t is None or not refs):
                raise ValueError("Victory requires a time and actual evidence")
            if value["outcome"] == "won":
                episode = (read_json(safe_path(job, report["episodes"][row["episode"] - 1]["path"]))
                           if side == "recording" else replay_episodes.get(row["episode"], {}))
                duration = episode.get("capture_duration_seconds", episode.get("elapsed_seconds", 0)) if side == "recording" else episode.get("duration_seconds", 0)
                if episode.get("status") not in ("completed", "truncated") or t > min(duration, 20):
                    raise ValueError("Victory is outside the available successful execution evidence")
    result = {"assessment_source": "player_evidence_review", "independently_verified": False,
              "episodes": rows, "all_goals_supported": all(r[s]["outcome"] == "won" for r in rows
                  for s in (("recording",) if native else ("recording", "replay")))}
    atomic_json(job / "gamecraft-goals.json", result)
    return artifact(job / "gamecraft-goals.json", job)


def check_replay_citations(job, citations):
    for episode in read_json(job / "gamecraft.json")["episodes"]:
        prefix = f"evidence/replay/{episode['episode']:04d}/"
        if episode["frames"] and not any(p.startswith(prefix) and p.endswith(".png") for p in citations):
            raise ValueError("Player analysis must cite every replay with frames")
