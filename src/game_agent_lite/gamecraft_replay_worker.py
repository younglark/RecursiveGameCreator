"""Offline container worker: upstream replay + sampling, never a rubric or judge."""
from pathlib import Path
import os
import shutil
import subprocess

from .store import artifact, atomic_json, read_json, redact, snapshot


def main(submission=Path("/submission"), output=Path("/output"), temporary=Path("/tmp")):
    from gamecraft_bench.verifier.replay import replay_trace
    from gamecraft_bench.verifier.score import _sample_frames

    manifest = read_json(submission / "manifest.json")
    results = []
    # Like the official scorer, demos restart Godot but share this disposable
    # project and user directory for the batch. Do not invent per-demo resets.
    project = temporary / "game"
    os.environ["XDG_DATA_HOME"] = str(temporary / "user-data")
    snapshot(submission / "game", project)
    for demo in manifest["demos"]:
        folder = output / f"{demo['episode']:04d}"
        receipt = folder / "result.json"
        folder.mkdir(parents=True, exist_ok=True)
        result = {"episode": demo["episode"], "status": "failed", "video": None,
                  "frames": [], "goal_verified": None, "trace_sha256": demo["trace"]["sha256"]}
        try:
            with (folder / "import.log").open("w") as log:
                subprocess.run(["godot", "--headless", "--editor", "--path", str(project), "--import"],
                               stdout=log, stderr=subprocess.STDOUT, check=True, timeout=180)
            replay = replay_trace(project_dir=project,
                trace_path=submission / demo["trace"]["path"],
                output_mp4=folder / "play.mp4", viewport=(1280, 720),
                record_size=(854, 480), fps=30, godot_bin="godot", log_dir=folder / "logs")
            frames = _sample_frames(folder / "play.mp4", folder / "frames",
                duration_seconds=demo["duration_frames"] / 30, interval_seconds=.5,
                max_window_seconds=20, seed=str(demo["episode"]))
            result.update(status="completed" if frames else "failed",
                video=artifact(folder / "play.mp4", output),
                frames=[artifact(p, output) for p in frames],
                reason="Replay and sampling completed; victory not verified" if frames else "No replay frames",
                duration_seconds=replay.duration_seconds)
        except Exception as exc:
            result["reason"] = redact(f"{type(exc).__name__}: {exc}")
        result["logs"] = [artifact(p, output) for p in sorted(folder.rglob("*.log")) if p.stat().st_size]
        atomic_json(receipt, result)
        results.append(artifact(receipt, output))
    atomic_json(output / "results.json", {"episodes": results,
        "godot": subprocess.check_output(["godot", "--version"], text=True).strip(),
        "judge_calls": False, "official_score": None})
    shutil.rmtree(project, ignore_errors=True)


if __name__ == "__main__":
    main()
