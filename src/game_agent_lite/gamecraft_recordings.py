"""Native Player evidence bundles. PNGs are canonical; MP4 is a timestamped preview.

No models, hidden rubrics or replay are used when packaging or validating evidence.
"""
import json
import math
from pathlib import Path
import re
import shutil
import struct

from .store import copy_file
from .store import (artifact, atomic_json, read_json, safe_path, snapshot,
                    verify_artifact, version)

FORMAT = "gamecraft-player-recordings-v1"
CAPTURE = {"width": 854, "height": 480, "fps": 2, "clock": "engine_input_wall_clock"}


def number(value):
    return type(value) in (int, float) and math.isfinite(value)


def checked_file(root, ref):
    path = ref.get("path") if isinstance(ref, dict) else None
    if (not isinstance(path, str) or not path or "\\" in path or
            Path(path).is_absolute() or any(p in (".", "..") for p in path.split("/"))):
        raise ValueError("Evidence must use relative paths inside the bundle")
    current = root
    for part in Path(path).parts:
        current /= part
        if current.is_symlink():
            raise ValueError("Evidence symlinks are not allowed")
    if not verify_artifact(ref, root):
        raise ValueError("Recording artifact missing or changed: " + path)
    return safe_path(root, path)


def check_frames(frames, duration, root):
    if not number(duration) or not 0 < duration <= 20 or not 1 <= len(frames) <= 40:
        raise ValueError("Recording requires 1..40 real frames within 20 seconds")
    previous, slots, paths = -1.0, set(), set()
    for frame in frames:
        slot, actual, planned = frame["slot"], frame["time_seconds"], frame["planned_seconds"]
        if (type(slot) is not int or not 0 <= slot < 40 or slot in slots or
                not number(actual) or not number(planned) or planned != slot / 2 or
                not previous < actual < duration or not planned <= actual < planned + .5):
            raise ValueError("Invalid capture order, slot, timestamp or duration")
        path = checked_file(root, frame["image"])
        if path in paths:
            raise ValueError("A captured frame cannot be reused to fill another slot")
        with path.open("rb") as stream:
            header = stream.read(24)
        if (len(header) != 24 or header[:8] != b"\x89PNG\r\n\x1a\n" or
                header[12:16] != b"IHDR" or struct.unpack(">II", header[16:24]) != (854, 480)):
            raise ValueError("Recording frames must be 854x480 PNGs")
        slots.add(slot)
        paths.add(path)
        previous = actual
    expected = set(range(min(40, math.ceil(duration * 2))))
    return {"missing_slots": sorted(expected - slots),
            "max_capture_delay_seconds": max(f["time_seconds"] - f["planned_seconds"] for f in frames)}


def finish_capture(game):
    """Finalize a stopped engine's PNG stream without adding any scoring frames."""
    receipt = read_json(game.frames / "episode.json")
    if any(receipt.get(k) != v for k, v in CAPTURE.items()):
        raise ValueError("Unsupported native recording capture settings")
    duration = receipt["duration_seconds"]
    rows = [json.loads(line) for line in (game.frames / "timing.jsonl").read_text().splitlines()]
    frames = []
    for row in rows:
        if not re.fullmatch(r"video-[0-9]{6}\.png", row["file"]):
            raise ValueError("Invalid captured frame filename")
        frames.append({k: row[k] for k in ("slot", "time_seconds", "planned_seconds")})
        frames[-1].update(clock=CAPTURE["clock"], image=artifact(game.frames / row["file"], game.output))
    quality = check_frames(frames, duration, game.output)
    video = game.output / "play.mp4"
    lines = []
    for i, frame in enumerate(frames):
        end = frames[i + 1]["time_seconds"] if i + 1 < len(frames) else duration
        lines += [f"file '{Path(frame['image']['path']).name}'", f"duration {end - frame['time_seconds']:.9f}"]
    lines += [f"file '{Path(frames[-1]['image']['path']).name}'"]
    (game.frames / "frames.txt").write_text("\n".join(lines) + "\n")
    # Preview holds the previous image across gaps. The judge receives ONLY real PNGs.
    game.runner.run([game.config["ffmpeg"], "-y", "-loglevel", "error", "-f", "concat",
        "-safe", "1", "-i", "frames.txt", "-fps_mode", "vfr", "-pix_fmt", "yuv420p",
        "-t", str(duration - frames[0]["time_seconds"]), str(video)], game.frames, game.output / "encode.log")
    return {"path": video, "mode": "native_sampled_2fps", "origin": "engine", "capture": CAPTURE,
            "raw_frames": frames, "capture_receipt": artifact(game.frames / "episode.json", game.output),
            "capture_timing": artifact(game.frames / "timing.jsonl", game.output),
            "duration_seconds": duration, "capture_quality": quality,
            "preview_origin_seconds": frames[0]["time_seconds"], "scoring_source": "raw_png"}


def export_recordings(session, report, output):
    """Export every planned episode, including failures; never select by victory."""
    root = session.job
    output.mkdir(parents=True, exist_ok=False)
    snapshot(session.project, output / "game")
    if version(output / "game") != session.version or version(session.project) != session.version:
        raise ValueError("Recording game source changed")

    def copy(ref, dest):
        source = checked_file(root, ref)
        path = output / dest
        path.parent.mkdir(parents=True, exist_ok=True)
        copy_file(source, path)
        return artifact(path, output)

    plan = read_json(checked_file(root, report["plan"]))
    data = {"format": FORMAT, "evidence_source": "player_recording", "replay_performed": False,
            "task": session.config["benchmark"]["task"], "round": session.context.get("round", 0),
            "source_version": session.version, "capture": CAPTURE, "viewport": [1280, 720],
            "runtime": report["settings"].get("runtime"), "policy": copy(report["policy"], "policy.gd"),
            "plan": copy(report["plan"], "plan.json"), "demos": []}
    if len(report["episodes"]) != len(plan["episodes"]):
        raise ValueError("Recording count does not match the frozen plan")
    for i, ref in enumerate(report["episodes"], 1):
        result = read_json(checked_file(root, ref))
        if result["version"] != session.version or result["policy_sha256"] != report["policy"]["sha256"]:
            raise ValueError("Episode belongs to a different game or policy")
        prefix = f"episodes/{i:04d}"
        demo = {"episode": i, **{k: result[k] for k in ("profile", "purpose", "goal", "scenario", "seed", "status", "reason")},
                "duration_seconds": result.get("capture_duration_seconds"), "frames": [],
                "record": copy(ref, prefix + "/record.json"),
                "trajectory": copy(result["trajectory"], prefix + "/trajectory.jsonl"),
                "input_events": copy(result["input_events"], prefix + "/input_events.jsonl")}
        for frame in result["frames"]:
            demo["frames"].append({**frame, "image": copy(frame["image"], prefix + f"/frames/{frame['slot']:04d}.png")})
        video = result["video"]
        if not video or video.get("mode") != "native_sampled_2fps" or video.get("capture") != CAPTURE:
            raise ValueError("Episode has no native 854x480 recording; re-record with the new mode")
        demo.update(video=copy(video["artifact"], prefix + "/play.mp4"),
                    capture_receipt=copy(video["capture_receipt"], prefix + "/capture.json"),
                    capture_timing=copy(video["capture_timing"], prefix + "/timing.jsonl"))
        demo["capture_quality"] = check_frames(demo["frames"], demo["duration_seconds"], output)
        data["demos"].append(demo)
    atomic_json(output / "manifest.json", data)
    validate_recordings(output)
    return data


def validate_recordings(root):
    """Read-only validation; no engine, decoder, model or paid API is started."""
    root = Path(root).resolve()
    data = read_json(root / "manifest.json")
    try:
        if (data["format"] != FORMAT or data["evidence_source"] != "player_recording" or
                data["replay_performed"] is not False or data["capture"] != CAPTURE or
                data["viewport"] != [1280, 720] or type(data["round"]) is not int or data["round"] < 0 or
                not re.fullmatch(r"[a-z0-9][a-z0-9-]*", data["task"])):
            raise ValueError("Unsupported recording manifest")
        if not (root / "game/project.godot").is_file() or version(root / "game") != data["source_version"]:
            raise ValueError("Recording game version changed")
        checked_file(root, data["policy"])
        plan = read_json(checked_file(root, data["plan"]))
        demos = data["demos"]
        if not 1 <= len(demos) <= 10 or len(demos) != len(plan["episodes"]):
            raise ValueError("Recording bundle must include every planned episode (1..10)")
        for i, (demo, spec) in enumerate(zip(demos, plan["episodes"]), 1):
            if type(demo["episode"]) is not int or demo["episode"] != i or any(demo[k] != spec[k] for k in ("profile", "purpose", "goal", "scenario")):
                raise ValueError("Episode does not match the frozen plan")
            for key in ("record", "trajectory", "input_events", "video", "capture_receipt", "capture_timing"):
                checked_file(root, demo[key])
            record = read_json(root / demo["record"]["path"])
            if (record["version"] != data["source_version"] or record["policy_sha256"] != data["policy"]["sha256"] or
                    record["index"] != i - 1 or any(record[k] != demo[k] for k in ("profile", "purpose", "goal", "scenario", "seed", "status", "reason")) or
                    record.get("capture_duration_seconds") != demo["duration_seconds"]):
                raise ValueError("Recording source receipt mismatch")
            for key in ("trajectory", "input_events"):
                if record[key]["sha256"] != demo[key]["sha256"]:
                    raise ValueError("Recording input evidence changed")
            if record["video"]["artifact"]["sha256"] != demo["video"]["sha256"]:
                raise ValueError("Recording preview changed")
            if (record["video"]["mode"] != "native_sampled_2fps" or record["video"]["capture"] != CAPTURE or
                    any(record["video"][key]["sha256"] != demo[key]["sha256"] for key in ("capture_receipt", "capture_timing"))):
                raise ValueError("Recording capture provenance changed")
            receipt = read_json(root / demo["capture_receipt"]["path"])
            if receipt["duration_seconds"] != demo["duration_seconds"] or any(receipt[k] != v for k, v in CAPTURE.items()):
                raise ValueError("Capture metadata mismatch")
            quality = check_frames(demo["frames"], demo["duration_seconds"], root)
            timing = [json.loads(line) for line in (root / demo["capture_timing"]["path"]).read_text().splitlines()]
            if len(timing) != len(demo["frames"]) or len(record["frames"]) != len(timing):
                raise ValueError("Recording frames were dropped or added")
            for frame, row, original in zip(demo["frames"], timing, record["frames"]):
                if (any(frame[k] != row[k] or frame[k] != original[k] for k in ("slot", "time_seconds", "planned_seconds")) or
                        frame["image"]["sha256"] != original["image"]["sha256"] or
                        abs(row["time"] - receipt["origin_usec"] / 1e6 - row["time_seconds"]) > .00001):
                    raise ValueError("Frame no longer matches its recorded time or source")
            if demo["capture_quality"] != quality:
                raise ValueError("Capture quality metadata changed")
        return root / "game", data, artifact(root / "manifest.json", root)["sha256"]
    except (KeyError, TypeError, IndexError) as exc:
        raise ValueError("Malformed recording manifest/evidence") from exc
