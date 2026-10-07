"""Offline GameCraft input export and submission validation; never run games or judges."""
import hashlib
import json
import math
from pathlib import Path, PurePosixPath
import re
import shutil
import tempfile

from jsonschema import ValidationError

from .store import artifact, atomic_json, file_hash, read_json, safe_path, snapshot, verify_artifact, version
from .types import ACTION_SCHEMA, obj, validate

FPS, WIDTH, HEIGHT, MAX_SECONDS, MAX_DEMOS = 30, 1280, 720, 20, 10
_TIME_LIMIT_REASONS = {
    "benchmark demo time limit reached; target completion not established",
    "benchmark demo time limit reached",
}
_KEYS = {
    "ArrowUp": "UP", "ArrowDown": "DOWN", "ArrowLeft": "LEFT", "ArrowRight": "RIGHT",
    "Space": "SPACE", "Spacebar": "SPACE", " ": "SPACE", "Escape": "ESCAPE", "Esc": "ESCAPE",
    "Enter": "ENTER", "Return": "ENTER", "Shift": "SHIFT", "Control": "CTRL", "Ctrl": "CTRL",
    "Alt": "ALT", "Tab": "TAB", "Backspace": "BACKSPACE", "Delete": "DELETE",
}

_HASH = {"type": "string", "pattern": "^[0-9a-f]{64}$"}
_FRAME = {"type": "integer", "minimum": 0, "maximum": FPS * MAX_SECONDS}
_POINT = {"x": {"type": "integer", "minimum": 0, "maximum": WIDTH - 1},
          "y": {"type": "integer", "minimum": 0, "maximum": HEIGHT - 1}}
_TRACE_SCHEMA = obj({
    "duration_frames": {**_FRAME, "minimum": 1},
    "scenario": {"type": "string", "pattern": "^[A-Za-z0-9_-]+$"},
    "events": {"type": "array", "minItems": 1, "items": {"oneOf": [
        obj({"frame": _FRAME, "type": {"enum": ["key_press", "key_down", "key_up"]},
             "keycode": {"enum": sorted(set(_KEYS.values()) | set("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"))}}),
        obj({"frame": _FRAME, "type": {"enum": ["mouse_click", "mouse_down", "mouse_up"]},
             "button": {"enum": ["left", "right"]}, **_POINT}),
        obj({"frame": _FRAME, "type": {"const": "mouse_move"}, **_POINT}),
        obj({"frame": _FRAME, "type": {"const": "wait"}}),
    ]}},
}, required=["duration_frames", "events"])
_DEMO_SCHEMA = {"type": "object", "required": [
    "episode", "trace", "source_trajectory", "source_trajectory_sha256", "initial_state",
    "scenario", "source_elapsed_seconds", "source_steps", "exported_steps", "duration_frames", "clipped",
], "properties": {
    "episode": {"type": "integer", "minimum": 1},
    "trace": obj({"path": {"type": "string"}, "sha256": _HASH}),
    "source_trajectory": {"type": "string", "minLength": 1}, "source_trajectory_sha256": _HASH,
    "initial_state": {"type": "object"}, "scenario": {"type": ["string", "null"]},
    "source_elapsed_seconds": {"type": "number", "exclusiveMinimum": 0},
    "source_steps": {"type": "integer", "minimum": 1},
    "exported_steps": {"type": "integer", "minimum": 1},
    "duration_frames": {**_FRAME, "minimum": 1}, "clipped": {"type": "boolean"},
    "source_episode": {"type": "integer", "minimum": 1},
    "source_status": {"enum": ["completed", "truncated"]},
    "stop_reason": {"type": "string"}, "goal_verified": {"type": "null"},
    "demo_time_limit_seconds": {"enum": [None, MAX_SECONDS]},
    "input_source": {"enum": ["injected_events", "derived_actions"]},
    "source_input_events": obj({"path": {"type": "string", "minLength": 1}, "sha256": _HASH}),
    "input_event_map": {"type": "array", "minItems": 1, "items": {"type": "object"}},
}}
_SUBMISSION_SCHEMA = {"type": "object", "required": [
    "format", "engine", "round", "source_project", "source_version", "source_policy_sha256",
    "source_report_sha256", "exported_project_version", "fps", "viewport", "demos",
], "properties": {
    "format": {"const": "gamecraft-export-v1"}, "engine": {"const": "godot"},
    "round": {"type": "integer", "minimum": 0},
    "source_project": {"type": "string", "minLength": 1},
    **{name: _HASH for name in ("source_version", "source_policy_sha256", "source_report_sha256",
                               "exported_project_version")},
    "fps": {"const": FPS}, "viewport": {"const": [WIDTH, HEIGHT]},
    "demos": {"type": "array", "minItems": 1, "maxItems": MAX_DEMOS, "items": _DEMO_SCHEMA},
}}


def validate_gamecraft_submission(submission: Path) -> tuple[Path, dict, str]:
    """Check a portable export bundle, returning game, manifest and manifest SHA-256.

    Source-run references are provenance only; never open them on the scoring host.
    This verifies integrity and format, not starting-state alignment or victory.
    Older v1 exports without the additive range/start-check fields remain supported.
    """
    root = Path(submission).resolve()
    manifest_path = root / "manifest.json"
    if manifest_path.is_symlink():
        raise ValueError("Submission manifest must not be a symlink")
    raw = manifest_path.read_bytes()
    manifest = json.loads(raw, parse_constant=_finite_json)
    try:
        validate(manifest, _SUBMISSION_SCHEMA)
        game = root / "game"
        if game.is_symlink() or not (game / "project.godot").is_file():
            raise ValueError("Submission is missing a regular game/project.godot project")
        if version(game) != manifest["exported_project_version"]:
            raise ValueError("Exported game or demo files changed after export")
        expected_files, episodes = set(), set()
        for demo in manifest["demos"]:
            relative = PurePosixPath(demo["trace"]["path"])
            if (relative.as_posix() != demo["trace"]["path"] or "\\" in str(relative)
                    or relative.parent != PurePosixPath("game/demo_outputs") or relative.suffix != ".json"):
                raise ValueError("Demo path must be relative to the bundle inside game/demo_outputs")
            if relative.name in expected_files or demo["episode"] in episodes:
                raise ValueError("Submission contains duplicate demo files or episodes")
            expected_files.add(relative.name)
            episodes.add(demo["episode"])
            path = _checked(demo["trace"], root)
            trace = json.loads(path.read_bytes(), parse_constant=_finite_json)
            validate(trace, _TRACE_SCHEMA)
            frames = [event["frame"] for event in trace["events"]]
            if frames != sorted(frames) or frames[-1] > trace["duration_frames"]:
                raise ValueError("Demo events must be ordered and within duration_frames")
            elapsed = _seconds(demo["source_elapsed_seconds"])
            if demo.get("source_status") == "truncated":
                if (manifest.get("source_evidence_scope") != "prepared_player"
                        or demo.get("stop_reason") not in _TIME_LIMIT_REASONS
                        or demo.get("demo_time_limit_seconds") != MAX_SECONDS
                        or "goal_verified" not in demo or elapsed < MAX_SECONDS):
                    raise ValueError("Truncated demo must retain its benchmark time-limit provenance")
            elif demo.get("demo_time_limit_seconds") is not None:
                raise ValueError("Only a time-limited truncated demo may declare a stop deadline")
            clipped = elapsed > MAX_SECONDS
            end = min(elapsed, MAX_SECONDS)
            export_range = {"kind": "prefix" if clipped else "full_episode",
                            "start_seconds": 0.0, "end_seconds": end}
            if (trace["duration_frames"] != demo["duration_frames"]
                    or trace.get("scenario") != demo["scenario"]
                    or demo["duration_frames"] != max(1, math.ceil(end * FPS))
                    or demo["clipped"] != clipped or demo["exported_steps"] > demo["source_steps"]
                    or (not clipped and demo["exported_steps"] != demo["source_steps"])
                    or demo.get("export_range", export_range) != export_range):
                raise ValueError("Demo range, scenario or duration does not match its manifest")
        actual_files = {path.name for path in (game / "demo_outputs").glob("*.json")}
        if actual_files != expected_files:
            raise ValueError("demo_outputs files do not match the submission manifest")
    except ValidationError as exc:
        location = "/".join(str(part) for part in exc.absolute_path) or "root"
        raise ValueError(f"Invalid GameCraft submission schema at {location}") from exc
    return game, manifest, hashlib.sha256(raw).hexdigest()


def _key(value):
    if value in _KEYS:
        return _KEYS[value]
    if re.fullmatch(r"[A-Za-z0-9]", value):
        return value.upper()
    if re.fullmatch(r"Key[A-Z]|Digit[0-9]", value):
        return value[-1].upper()
    if value in _KEYS.values():
        return value
    raise ValueError(f"GameCraft does not support key: {value!r}")


def _seconds(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError("Trajectory timestamps must be finite non-negative numbers")
    return float(value)


def _finite_json(value):
    raise ValueError(f"Non-finite JSON value: {value}")


def _demo_time_limit(episode, settings):
    """Only the dedicated native benchmark recorder may export a deadline stop."""
    execution = settings.get("execution", {})
    return (episode.get("status") == "truncated"
            and episode.get("reason") in _TIME_LIMIT_REASONS
            and settings.get("policy_backend") == episode.get("policy_backend") == "godot"
            and execution.get("purpose") == "gamecraft_demo"
            and execution.get("max_seconds") == MAX_SECONDS
            and execution.get("fresh_launch") is True and episode.get("fresh_launch") is True
            and execution.get("viewport") == [WIDTH, HEIGHT]
            and _seconds(episode["elapsed_seconds"]) >= MAX_SECONDS)


def _read_actions(path, episode, expected_version, policy_hash, *, time_limited=False):
    """Accept paired executed inputs only, never unacknowledged input intent."""
    state, pending, initial, ended = None, None, None, False
    actions, previous, last_feedback = [], -1.0, 0.0
    elapsed = _seconds(episode["elapsed_seconds"])
    with path.open() as stream:
        for line in stream:
            row = json.loads(line, parse_constant=_finite_json)
            now = _seconds(row["time_seconds"])
            if (ended or now < previous or row["version"] != expected_version
                    or row["policy_sha256"] != policy_hash):
                raise ValueError("Trajectory order or version does not match the saved episode")
            previous = now
            kind = row["type"]
            if kind == "initial":
                if initial is not None or not isinstance(row["state"], dict):
                    raise ValueError("Invalid initial trajectory state")
                initial = state = row["state"]
                last_feedback = now
            elif kind == "input":
                validate(row["action"], ACTION_SCHEMA)
                if initial is None or pending is not None or row["index"] != len(actions) or row["before"] != state:
                    raise ValueError("Invalid input sequence")
                pending = row
            elif kind == "feedback":
                if (pending is None or row["index"] != len(actions) or row["action"] != pending["action"]
                        or row["before"] != state or not isinstance(row["after"], dict)):
                    raise ValueError("Executed feedback does not match input intent")
                start = _seconds(row["action_start_seconds"])
                if not last_feedback <= start <= pending["time_seconds"] <= now <= elapsed:
                    raise ValueError("Invalid executed-action timing")
                if time_limited and start >= MAX_SECONDS:
                    raise ValueError("Time-limited demo contains an input started after the deadline")
                # Validate even inputs beyond a requested prefix; never hide unsupported controls.
                action = row["action"]
                keys = list(dict.fromkeys(_key(key) for key in action["keys"]))
                release = row.get("pointer_release_seconds")
                if release is not None and not start <= _seconds(release) <= now:
                    raise ValueError("Invalid recorded mouse release time")
                actions.append({"start": start, "finish": now, "action": action, "keys": keys,
                                "pointer_release_seconds": release})
                pending, state, last_feedback = None, row["after"], now
            elif kind == "end":
                expected_status = "truncated" if time_limited else "completed"
                if (pending is not None or row["status"] != expected_status
                        or row["status"] != episode["status"] or now < elapsed
                        or (time_limited and row.get("reason") != episode["reason"])):
                    raise ValueError("Cannot export incomplete or failed input execution")
                ended = True
            else:
                raise ValueError(f"Unknown trajectory event: {kind}")
    if not ended or initial is None or not actions or len(actions) != episode["steps"]:
        raise ValueError("Trajectory is incomplete or action count does not match")
    return actions, initial


def _convert(actions, elapsed, *, clip_long):
    if elapsed > MAX_SECONDS and not clip_long:
        raise ValueError("Episode exceeds GameCraft's 20-second limit; select another episode or use --clip-long")
    duration = min(elapsed, MAX_SECONDS)
    frames = max(1, math.ceil(duration * FPS))
    events, held, selected_steps = [], [], 0

    def emit(at, kind, **fields):
        # Frame numbers are replay-clock offsets, not the 2 FPS evidence-video clock.
        frame = min(frames, math.floor(at * FPS + 0.5))
        events.append({"frame": frame, "type": kind, **fields})

    for step in actions:
        start, action, keys = step["start"], step["action"], step["keys"]
        if start >= duration:
            break
        selected_steps += 1
        for key in held:
            if key not in keys:
                emit(start, "key_up", keycode=key)
        for key in keys:
            if key not in held:
                emit(start, "key_down", keycode=key)
        held = keys
        pointer = action["pointer"]
        if pointer is not None:
            if pointer.get("phase", "click") != "click":
                raise ValueError("Persistent pointer actions require recorded input_events; legacy inference is unsafe")
            # Godot's bridge holds a mouse button for duration_ms, unlike an instant click.
            point = {"x": min(WIDTH - 1, int(pointer["x"] * WIDTH)),
                     "y": min(HEIGHT - 1, int(pointer["y"] * HEIGHT))}
            button = pointer["button"]
            if button == "none":
                emit(start, "mouse_move", **point)
            else:
                emit(start, "mouse_down", button=button, **point)
                recorded_release = step.get("pointer_release_seconds")
                release = min(recorded_release if recorded_release is not None else
                              start + action["duration_ms"] / 1000, step["finish"], duration)
                emit(release, "mouse_up", button=button, **point)
    if not selected_steps:
        raise ValueError("No executed inputs fall inside the 20-second export window")
    for key in held:
        # Cleanup's exact key-up time was not recorded; elapsed_seconds is its boundary.
        events.append({"frame": frames, "type": "key_up", "keycode": key})
    events.append({"frame": frames, "type": "wait"})
    # Stable sort preserves release/re-press ordering when timestamps quantize to one frame.
    events.sort(key=lambda event: event["frame"])
    return {"duration_frames": frames, "events": events}, selected_steps


def _checked(item, root):
    if not verify_artifact(item, root):
        raise ValueError("Saved artifact is missing, changed or outside its workspace")
    return safe_path(root, item["path"])


def _read_input_events(path, episode, actions):
    """Validate the injection journal on the trajectory clock, including cleanup."""
    rows = [json.loads(line, parse_constant=_finite_json) for line in path.read_text().splitlines()]
    if (len(rows) < 2 or rows[0].get("type") != "start" or rows[-1].get("type") != "end"
            or rows[0].get("format") != "godot-input-events-v1"
            or rows[0].get("clock") != "engine_input_wall_clock"
            or rows[0].get("time_seconds") != 0
            or rows[-1].get("status") != episode["status"]
            or rows[-1].get("reason") != episode["reason"]):
        raise ValueError("Input event journal is incomplete or does not match its episode")
    previous, held, buttons, result = 0.0, set(), set(), []
    modifiers = {"SHIFT": "shift", "CTRL": "ctrl", "ALT": "alt", "META": "meta"}
    for sequence, row in enumerate(rows):
        now = _seconds(row["time_seconds"])
        index = row.get("action_index")
        if (type(row.get("sequence")) is not int or row["sequence"] != sequence
                or row.get("version") != episode["version"]
                or row.get("policy_sha256") != episode["policy_sha256"]
                or not previous <= now <= _seconds(episode["elapsed_seconds"])
                or type(index) is not int or index < -1 or index >= len(actions)):
            raise ValueError("Input event order, clock or version does not match its episode")
        previous = now
        if sequence in (0, len(rows) - 1):
            if index != -1:
                raise ValueError("Input journal boundaries cannot belong to an action")
            continue
        if ((index >= 0 and not actions[index]["start"] <= now <= actions[index]["finish"])
                or (index == -1 and now < actions[-1]["finish"])):
            raise ValueError("Input event is outside its recorded action/cleanup interval")
        event = {"type": row["type"], "time_seconds": now, "source_sequence": sequence}
        kind = row["type"]
        if kind in ("key_down", "key_up"):
            # OS.get_keycode_string uses Right/Left rather than browser ArrowRight/ArrowLeft.
            name = row["key"]
            code = _key("Arrow" + name if name in ("Up", "Down", "Left", "Right") else name)
            if (kind == "key_down") == (code in held):
                raise ValueError("Input journal contains an unbalanced key transition")
            held.add(code) if kind == "key_down" else held.remove(code)
            event["keycode"] = code
        elif kind in ("mouse_move", "mouse_down", "mouse_up"):
            if any(_seconds(row[axis]) > 1 for axis in ("x", "y")):
                raise ValueError("Input mouse coordinates must be normalized window coordinates")
            event.update(x=min(WIDTH - 1, int(row["x"] * WIDTH)),
                         y=min(HEIGHT - 1, int(row["y"] * HEIGHT)))
            if kind != "mouse_move":
                button = row.get("button")
                if button not in ("left", "right") or (kind == "mouse_down") == (button in buttons):
                    raise ValueError("Input journal contains an invalid mouse transition")
                buttons.add(button) if kind == "mouse_down" else buttons.remove(button)
                event["button"] = button
        else:
            raise ValueError(f"Unknown injected input event: {kind}")
        flags = row.get("modifiers", {})
        if (set(flags) != set(modifiers.values()) or any(type(v) is not bool for v in flags.values())
                or any(flags[name] != (key in held) for key, name in modifiers.items())):
            raise ValueError("Input modifier flags do not match injected key state")
        result.append(event)
    if held or buttons:
        raise ValueError("Input journal is missing final input releases")
    return result


def _convert_input_events(recorded, actions, elapsed, *, clip_long):
    if elapsed > MAX_SECONDS and not clip_long:
        raise ValueError("Episode exceeds GameCraft's 20-second limit; select another episode or use --clip-long")
    duration = min(elapsed, MAX_SECONDS)
    frames = max(1, math.ceil(duration * FPS))
    events, mapping, held, buttons = [], [], [], {}
    for row in recorded:
        if row["time_seconds"] > duration:
            break
        event = {key: value for key, value in row.items() if key not in ("time_seconds", "source_sequence")}
        event["frame"] = min(frames, math.floor(row["time_seconds"] * FPS + .5))
        events.append(event)
        mapping.append({"event_index": len(events) - 1, "source_sequence": row["source_sequence"],
                        "source_time_seconds": row["time_seconds"], "origin": "injected"})
        if row["type"] == "key_down": held.append(row["keycode"])
        if row["type"] == "key_up": held.remove(row["keycode"])
        if row["type"] == "mouse_down": buttons[row["button"]] = {k: row[k] for k in ("x", "y", "button")}
        if row["type"] == "mouse_up": buttons.pop(row["button"])
    # Only an explicit prefix/time-limit export needs synthetic boundary releases.
    # Do not claim these were observed injections, or change/re-time original events.
    boundary = [{"type": "key_up", "keycode": key} for key in held]
    boundary += [{"type": "mouse_up", **point} for point in buttons.values()]
    for event in boundary + [{"type": "wait"}]:
        events.append({"frame": frames, **event})
        mapping.append({"event_index": len(events) - 1, "source_sequence": None,
                        "origin": "end_marker" if event["type"] == "wait" else "prefix_release"})
    steps = sum(action["start"] < duration for action in actions)
    if not steps:
        raise ValueError("No executed inputs fall inside the export window")
    return {"duration_frames": frames, "events": events}, steps, mapping


def _scenario_map(values, selected):
    result = {}
    for value in values or []:
        number, sep, name = value.partition("=")
        if not sep or not number.isdecimal() or not re.fullmatch(r"[A-Za-z0-9_-]+", name):
            raise ValueError("Use --scenario EPISODE=NAME with a simple existing scenario name")
        index = int(number)
        if index not in selected or index in result:
            raise ValueError("Scenario must name a selected episode exactly once")
        result[index] = name
    return result


def export_gamecraft(run_dir: Path, output: Path, *, round_number=None, episodes=None,
                     clip_long=False, scenarios=None, prepared_dir=None) -> Path:
    """Export best or a round's candidate to a NEW game/ + manifest.json bundle.

    This is a format adapter only: no automatic scenario creation, policy execution,
    official replay, rubric access or judge call. Sources and run state are read-only.
    """
    root = Path(run_dir).resolve()
    requested_output = Path(output).absolute()
    if requested_output.exists() or requested_output.is_symlink():
        raise FileExistsError("Export output already exists; choose a new directory")
    output = requested_output.resolve()
    if output.is_relative_to(root) or root.is_relative_to(output):
        raise ValueError("Export output must be outside the run directory")
    preparation, prepared_root, preparation_path = None, None, None
    if prepared_dir is not None:
        prepared_root = Path(prepared_dir).resolve()
        preparation_path = safe_path(prepared_root, "preparation.json")
        preparation = read_json(preparation_path)
        if not isinstance(preparation, dict):
            raise ValueError("Invalid preparation metadata")
        if output.is_relative_to(prepared_root / "player"):
            raise ValueError("Export output must be outside the prepared Player evidence directory")
        if round_number is None:
            round_number = preparation.get("round")
        if type(round_number) is not int or preparation.get("round") != round_number:
            raise ValueError("Prepared round does not match --round")
    config = read_json(root / "config.json")
    if config.get("engine") != "godot" or config.get("backend") == "mock":
        raise ValueError("GameCraft export requires a real Godot run (HTML and mock runs are unsupported)")
    best = None
    if round_number is None:
        best = read_json(root / "state.json").get("best")
        if not best:
            raise ValueError("No best version is saved yet; use --round N for a completed candidate")
        round_number = best["round"]
    if type(round_number) is not int or round_number < 0:
        raise ValueError("Round must be a non-negative integer")
    directory = safe_path(root, f"rounds/{round_number:04d}")
    candidate = read_json(directory / "check.json")
    if not candidate.get("passed"):
        raise ValueError("Candidate did not pass its saved build check")
    source = safe_path(root, candidate["project"])
    expected = candidate["version"]
    if not (source / "project.godot").is_file() or version(source) != expected:
        raise ValueError("Game project is missing or changed after play")
    receipt = read_json(directory / "played-candidate.json")
    report_path = _checked({"path": receipt["report"], "sha256": receipt["sha256"]}, root)
    if best and (best["project"] != candidate["project"] or best["version"] != expected or best["play"] != receipt["report"]):
        raise ValueError("Best-version pointer does not match the candidate evidence")
    source_report = read_json(report_path)
    extra_watched = []
    source_episodes = None
    if preparation is not None:
        source_plan_path = _checked(source_report["plan"], report_path.parent)
        source_policy_path = _checked(source_report["policy"], report_path.parent)
        source_specs = read_json(source_plan_path)["episodes"]
        source_episodes = preparation.get("source_episodes")
        if (preparation.get("source_version") != expected or source_report.get("version") != expected
                or preparation.get("source_policy_sha256") != source_report["policy"]["sha256"]
                or source_policy_path.suffix != ".gd"
                or not isinstance(source_episodes, list) or not 1 <= len(source_episodes) <= MAX_DEMOS
                or any(type(n) is not int or not 1 <= n <= len(source_specs) for n in source_episodes)
                or source_episodes != sorted(set(source_episodes))):
            raise ValueError("Prepared evidence does not match the source version, Policy or episode mapping")
        extra_watched = [(preparation_path, file_hash(preparation_path)),
                         (report_path, receipt["sha256"]),
                         (source_plan_path, source_report["plan"]["sha256"]),
                         (source_policy_path, source_report["policy"]["sha256"])]
        report_path = safe_path(prepared_root, "player/play.json")
        receipt = {"sha256": file_hash(report_path)}
    return export_recorded_report(source, report_path, output, expected=expected,
        round_number=round_number, source_project=candidate["project"], evidence_root=root,
        episodes=episodes, clip_long=clip_long, scenarios=scenarios, preparation=preparation,
        source_episodes=source_episodes, source_specs=source_specs if preparation else None,
        extra_watched=extra_watched)


def export_recorded_report(source, report_path, output, *, expected, round_number,
                           source_project, evidence_root=None, episodes=None, clip_long=False,
                           scenarios=None, preparation=None, source_episodes=None,
                           source_specs=None, extra_watched=()):
    """Shared serializer for completed runs and an in-progress Player recording.

    Only recorded evidence is consumed; no game, model or judge is run here.
    """
    source, report_path, output = Path(source).resolve(), Path(report_path).resolve(), Path(output).absolute()
    if output.exists() or output.is_symlink():
        raise FileExistsError("Export output already exists")
    if output.resolve().is_relative_to(source) or source.is_relative_to(output.resolve()):
        raise ValueError("Export output must be outside the game project")
    if version(source) != expected or not (source / "project.godot").is_file():
        raise ValueError("Game source changed before export")
    job = report_path.parent
    evidence_root = job if preparation is not None or evidence_root is None else evidence_root
    extra_watched = list(extra_watched)
    receipt = {"sha256": file_hash(report_path)}
    report = read_json(report_path)
    if report.get("format") != "policy-v1" or report.get("settings", {}).get("engine") != "godot":
        raise ValueError("Export requires saved policy-v1 Godot evidence, not legacy GUI records")
    if report["version"] != expected:
        raise ValueError("Play report version does not match the project")
    policy_hash = report["policy"]["sha256"]
    _checked(report["policy"], job)
    plan = read_json(_checked(report["plan"], job))
    if preparation is None and report["settings"].get("benchmark"):
        preparation = {"source_policy_sha256": policy_hash, "execution": report["settings"].get("execution", {})}
        source_specs = plan["episodes"]
        source_episodes = list(range(1, len(source_specs) + 1))
        evidence_root = job
    if preparation is not None:
        execution = preparation.get("execution", {})
        if (report["policy"]["sha256"] != preparation["source_policy_sha256"]
                or plan["episodes"] != [source_specs[n - 1] for n in source_episodes]
                or report["settings"].get("policy_backend") != "godot"
                or report["settings"].get("execution") != execution
                or execution.get("purpose") != "gamecraft_demo"
                or execution.get("max_seconds") != MAX_SECONDS
                or execution.get("fresh_launch") is not True
                or execution.get("viewport") != [WIDTH, HEIGHT]):
            raise ValueError("Prepared report, plan or execution settings do not match preparation")
    items = report["episodes"]
    if len(plan["episodes"]) != len(items) or report["trajectories_per_version"] != len(items):
        raise ValueError("Episode count does not match the saved plan")
    selected = list(range(1, len(items) + 1)) if episodes is None else list(episodes)
    if (not 1 <= len(selected) <= MAX_DEMOS or len(set(selected)) != len(selected)
            or any(type(n) is not int or not 1 <= n <= len(items) for n in selected)):
        raise ValueError("Select 1 to 10 distinct existing one-based episodes with --episode")
    selected.sort()
    scenarios = _scenario_map(scenarios, selected)
    demos = []
    watched = extra_watched + [(report_path, receipt["sha256"]), (_checked(report["policy"], job), policy_hash),
               (_checked(report["plan"], job), report["plan"]["sha256"])]
    for number in selected:
        episode_path = _checked(items[number - 1], job)
        episode = read_json(episode_path)
        specification = plan["episodes"][number - 1]
        time_limited = preparation is not None and _demo_time_limit(episode, report["settings"])
        if (episode["version"] != expected or episode["policy_sha256"] != policy_hash
                or episode["index"] != number - 1 or (episode["status"] != "completed" and not time_limited)
                or episode["profile"] != specification["profile"] or episode["purpose"] != specification["purpose"]
                or episode["seed"] != report["settings"]["seed"] + number - 1
                or not episode.get("video") or episode["video"]["origin"] != "engine"):
            raise ValueError("Selected episode is incomplete, simulated or does not match its version/plan")
        if preparation is not None and (episode.get("policy_backend") != "godot"
                or episode.get("fresh_launch") is not True
                or episode.get("scenario") != execution.get("scenarios", {}).get(specification["profile"], specification.get("scenario"))):
            raise ValueError("Prepared episode does not match its recorded launch/scenario")
        trace_path = _checked(episode["trajectory"], job)
        video_path = _checked(episode["video"].get("artifact", {}), job)
        actions, initial = _read_actions(trace_path, episode, expected, policy_hash, time_limited=time_limited)
        input_metadata = {"input_source": "derived_actions"}
        if episode.get("input_events_required") or "input_events" in episode:
            if "input_events" not in episode:
                raise ValueError("Required input event journal is missing; refusing legacy fallback")
            input_path = _checked(episode["input_events"], job)
            recorded = _read_input_events(input_path, episode, actions)
            trace, steps, mapping = _convert_input_events(recorded, actions, episode["elapsed_seconds"],
                                                         clip_long=clip_long or time_limited)
            input_metadata = {"input_source": "injected_events", "input_event_map": mapping,
                              "source_input_events": artifact(input_path, evidence_root)}
            watched.append((input_path, episode["input_events"]["sha256"]))
        else:
            trace, steps = _convert(actions, episode["elapsed_seconds"], clip_long=clip_long or time_limited)
        recorded_scenario = episode.get("scenario")
        if episode.get("fresh_launch"):
            if number in scenarios and scenarios[number] != recorded_scenario:
                raise ValueError("Cannot relabel a freshly recorded scenario")
            if recorded_scenario is not None:
                scenarios[number] = recorded_scenario
        if number in scenarios:
            trace["scenario"] = scenarios[number]
        demos.append((trace, {**input_metadata, "episode": number, "profile": episode["profile"], "seed": episode["seed"],
            "source_episode": source_episodes[number - 1] if preparation is not None else number,
            "source_status": episode["status"], "stop_reason": episode["reason"], "goal_verified": None,
            "demo_time_limit_seconds": MAX_SECONDS if time_limited else None,
            "source_trajectory": trace_path.relative_to(evidence_root).as_posix(),
            "source_trajectory_sha256": episode["trajectory"]["sha256"],
            "source_video": artifact(video_path, evidence_root),
            "initial_state": initial, "scenario": scenarios.get(number),
            "initial_state_check": "fresh_launch_recorded" if episode.get("fresh_launch") else "unverified",
            "source_elapsed_seconds": episode["elapsed_seconds"], "source_steps": episode["steps"],
            "exported_steps": steps, "duration_frames": trace["duration_frames"],
            "clipped": episode["elapsed_seconds"] > MAX_SECONDS,
            "export_range": {"kind": "prefix" if episode["elapsed_seconds"] > MAX_SECONDS else "full_episode",
                             "start_seconds": 0.0, "end_seconds": min(episode["elapsed_seconds"], MAX_SECONDS)}}))
        watched.extend([(episode_path, items[number - 1]["sha256"]), (trace_path, episode["trajectory"]["sha256"]),
                        (video_path, episode["video"]["artifact"]["sha256"])])

    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".gamecraft-export-", dir=output.parent))
    try:
        game = staging / "game"
        snapshot(source, game)
        if version(game) != expected or version(source) != expected:
            raise ValueError("Source project changed while exporting")
        # Exclude prior demos only in the COPY so official first-10 ordering is unambiguous.
        demo_dir = game / "demo_outputs"
        had_demos = demo_dir.exists()
        if demo_dir.is_dir():
            shutil.rmtree(demo_dir)
        elif demo_dir.exists():
            demo_dir.unlink()
        demo_dir.mkdir()
        entries = []
        for trace, metadata in demos:
            target = demo_dir / f"{metadata['episode']:04d}.json"
            atomic_json(target, trace)
            entries.append({**metadata, "trace": artifact(target, staging)})
        manifest = {"format": "gamecraft-export-v1", "engine": "godot", "round": round_number,
            "source_project": source_project, "source_version": expected,
            "source_policy_sha256": policy_hash, "source_report_sha256": receipt["sha256"],
            "exported_project_version": version(game), "fps": FPS, "viewport": [WIDTH, HEIGHT],
            "time_basis": "engine_input_wall_clock" if report["settings"].get("policy_backend") == "godot" else "wall_clock_since_agent_reset",
            "source_evidence_scope": "prepared_player" if preparation is not None else "run",
            "replaced_demo_outputs_in_copy": had_demos, "demos": entries,
            "replay_verified": False, "official_score": None,
            "limitations": [
                "Format export only; no official replay, score or task compliance has been verified.",
                "Normal episodes follow agent_reset(seed); prepared demos use a fresh launch/scenario. Startup timing still needs replay verification.",
                "Seeds and initial states are metadata only. Existing scenarios must reproduce them; none are generated.",
                "Wall-clock action starts are rounded to 30 FPS; physics frames are diagnostic, not the official replay clock.",
                "Injected journals preserve actual transitions; legacy exports derive edges from actions. Prefix cleanup is synthetic and marked separately.",
                "Coordinates scale to 1280x720; UI layout changes may require new traces.",
                "Source-assisted policies were closed-loop; exported input traces cannot correct replay drift.",
                "Clipped traces contain only an episode prefix, not its later outcome.",
                "A benchmark time-limit stop is exportable evidence, not proof of Policy completion or victory.",
            ]}
        if preparation is not None:
            manifest["source_preparation_sha256"] = extra_watched[0][1] if extra_watched else None
        if report["settings"].get("benchmark"):
            manifest["replay_environment"] = report["settings"].get("runtime")
            manifest["benchmark_task"] = report["settings"]["benchmark"]["task"]
        atomic_json(staging / "manifest.json", manifest)
        # Detect changes to source evidence before publishing the new bundle.
        if any(file_hash(path) != digest for path, digest in watched):
            raise ValueError("Saved evidence changed while exporting")
        if output.exists() or output.is_symlink():
            raise FileExistsError("Export output appeared while exporting; choose a new directory")
        staging.rename(output)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return output
