"""Prepare a disposable game copy; score upstream replay or native Player evidence."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


def openai_bootstrap(module, max_tokens=None):
    """Process-local configuration; never rewrite the pinned benchmark source."""
    budget = ""
    if max_tokens is not None:
        budget = ("import gamecraft_bench.verifier.judges.openai_gpt as judge; "
                  f"judge._MAX_TOKENS = {max_tokens!r}; "
                  f"print('[verifier] OpenAI max_tokens={max_tokens}', flush=True); ")
    return (
        "import functools, httpx, openai, runpy; "
        "openai.OpenAI = functools.partial(openai.OpenAI, "
        "timeout=httpx.Timeout(600.0, connect=30.0)); "
        "print('[verifier] OpenAI timeout: connect=30s, read/write/pool=600s', flush=True); "
        + budget + f"runpy.run_module({module!r}, run_name='__main__')"
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--task", required=True)
    parser.add_argument("--smoke-test", action="store_true")
    parser.add_argument("--recordings", action="store_true", help="Use /recordings canonical PNG bundle; no replay")
    parser.add_argument("--judge-max-tokens", type=int)
    args = parser.parse_args()
    benchmark = Path("/opt/gamecraft-bench")
    rubric = benchmark / "tasks" / args.task / "tests/rubric.json"
    if not rubric.is_file() or not rubric.resolve().is_relative_to(benchmark / "tasks"):
        parser.error("Task rubric not found")
    backend = os.environ.get("GAMECRAFT_BENCH_JUDGE", "openai").strip().lower()
    if args.judge_max_tokens is not None and (args.judge_max_tokens <= 0 or backend != "openai"):
        parser.error("--judge-max-tokens requires a positive OpenAI output budget")
    if not args.smoke_test:
        key_names = {
            "openai": ("GAMECRAFT_BENCH_JUDGE_OPENAI_API_KEY", "OPENAI_API_KEY"),
            "claude": ("GAMECRAFT_BENCH_JUDGE_ANTHROPIC_API_KEY", "GAMECRAFT_BENCH_JUDGE_ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"),
            "opus": ("GAMECRAFT_BENCH_JUDGE_ANTHROPIC_API_KEY", "GAMECRAFT_BENCH_JUDGE_ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"),
            "kimi": ("GAMECRAFT_BENCH_JUDGE_MOONSHOT_API_KEY", "MOONSHOT_API_KEY"),
            "gemini": ("GAMECRAFT_BENCH_JUDGE_GEMINI_API_KEY", "GAMECRAFT_BENCH_JUDGE_GOOGLE_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"),
        }
        if backend not in key_names or not any(os.environ.get(k) for k in key_names[backend]):
            parser.error("Configure a supported real judge and its API key in the dedicated env file; use --smoke-test for offline checks")

    output = Path("/output")
    project = Path("/workspace/game")
    shutil.copytree("/submission", project, ignore=shutil.ignore_patterns(".godot", ".git", "__pycache__", ".env", ".env.*"))
    files = {str(p.relative_to(project)): hashlib.sha256(p.read_bytes()).hexdigest()
             for p in sorted(project.rglob("*")) if p.is_file()}
    (output / "source-files.sha256.json").write_text(json.dumps(files, indent=2) + "\n")
    demos = sorted((project / "demo_outputs").glob("*.json"))
    if args.recordings:
        from game_agent_lite.gamecraft_recordings import validate_recordings
        _, recordings, _ = validate_recordings(Path("/recordings"))
        if recordings["task"] != args.task:
            parser.error("Recording task mismatch")
        demos = recordings["demos"]
    if not demos:
        parser.error("No demo_outputs/*.json traces in this project")
    manifest = {
        "mode": "offline-smoke-test" if args.smoke_test else "gamecraft-scoring",
        "official_score": None,
        "task": args.task,
        "godot": subprocess.check_output(["godot", "--version"], text=True).strip(),
        "judge": "stub" if args.smoke_test else backend,
        "judge_model": "0" if args.smoke_test else os.environ.get("GAMECRAFT_BENCH_JUDGE_MODEL"),
        "demo_count": len(demos),
        "rubric_sha256": hashlib.sha256(rubric.read_bytes()).hexdigest(),
        "input_viewport": [1280, 720], "replay_fps": 30,
        "record_size": [854, 480], "frame_interval_seconds": 0.5,
        "judge_max_tokens_override": args.judge_max_tokens,
    }
    if args.recordings:
        manifest.update(mode="native-recording-smoke-test" if args.smoke_test else "gamecraft-player-recording-scoring",
                        evidence_source="player_recording", replay_performed=False, replayed_demos=0,
                        recording_score=None, capture=recordings["capture"], replay_fps=None,
                        capture_quality=[d["capture_quality"] for d in demos])
    def save():
        (output / "evaluation.json").write_text(json.dumps(manifest, indent=2) + "\n")
    save()
    shutil.copy("/opt/evaluator/installed-packages.txt", output)
    print(f"Mode: {manifest['mode']}; Godot: {manifest['godot']}; demos: {len(demos)}", flush=True)
    if args.smoke_test:
        print("OFFLINE CHECK ONLY: stub values are not game scores; network is disabled by the launcher.", flush=True)
    with (output / "import.log").open("w") as log:
        imported = subprocess.run(["godot", "--headless", "--editor", "--path", str(project), "--import"], stdout=log, stderr=subprocess.STDOUT, timeout=180)
    if imported.returncode:
        raise RuntimeError("Godot import failed; inspect import.log")

    command = [sys.executable, "-m", "gamecraft_bench.verifier", "--project", str(project),
               "--rubric", str(rubric), "--output", str(output)]
    module = "gamecraft_bench.verifier"
    if args.recordings:
        module = "game_agent_lite.gamecraft_recording_score"
        command = [sys.executable, "-m", module, "--recordings", "/recordings",
                   "--rubric", str(rubric), "--output", str(output)]
    if args.smoke_test:
        command += ["--smoke-test"] if args.recordings else ["--judge", "stub", "--judge-model", "0"]
    elif backend == "openai":
        # Apply the timeout inside the scoring child; keep upstream source unchanged.
        command[1:3] = ["-c", openai_bootstrap(module, args.judge_max_tokens)]
    # Save the upstream CLI's complete output without copying rubric text to the terminal.
    with (output / "verifier.log").open("w") as log:
        result = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT)
    manifest["verifier_exit_code"] = result.returncode
    breakdown = output / "breakdown.json"
    if not breakdown.is_file():
        save()
        raise RuntimeError("Verifier did not finish; inspect verifier.log")
    data = json.loads(breakdown.read_text())
    manifest.update(build_ok=data["build_ok"], replayed_demos=len(data["demos"]), errors=data["errors"])
    if args.recordings:
        manifest.update(replayed_demos=0, scored_demos=len(data["demos"]))
    manifest["judge_model"] = data["judge"]["model"]
    manifest["artifacts"] = [{"demo_id": d["demo_id"],
                              "video": str(Path(d["mp4"]).relative_to(output)),
                              "frames": [str(Path(p).relative_to(output)) for p in d["frames"]]}
                             for d in data["demos"]]
    if args.smoke_test:
        # Prevent automated consumers from mistaking the fixed zero for a measured score.
        if (output / "reward.txt").exists():
            (output / "reward.txt").rename(output / "stub-reward.NOT-A-SCORE.txt")
        manifest["smoke_passed"] = bool(data["build_ok"] and data["demos"] and not data["errors"])
    elif not data["errors"]:
        manifest["recording_score" if args.recordings else "official_score"] = data["reward"]
    save()
    print(json.dumps({k: manifest[k] for k in ("mode", "build_ok", "replayed_demos", "official_score") +
                     (("recording_score", "scored_demos") if args.recordings else ())}, ensure_ascii=False), flush=True)
    if args.smoke_test:
        print(f"Offline pipeline check passed: {manifest['smoke_passed']} (no real judge used)", flush=True)
    if data["errors"]:
        print("Verifier reported errors; inspect evaluation.json and verifier.log before using any score.", flush=True)
        return 2
    return 0 if data["build_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
