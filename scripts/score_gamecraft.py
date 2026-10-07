#!/usr/bin/env python3
"""Score replay JSON or native Player recordings in an isolated Linux container."""
import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import uuid

from game_agent_lite.gamecraft import validate_gamecraft_submission
from game_agent_lite.gamecraft_recordings import validate_recordings

ROOT = Path(__file__).resolve().parents[1]


def main(argv=None):
    local_benchmark = ROOT / "benchmarks/gamecraft-bench"
    if not local_benchmark.is_dir():
        local_benchmark = ROOT.parent / "game-agent-lite/benchmarks/gamecraft-bench"
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--project", type=Path, help="Completed Godot project with its own demo_outputs; mounted read-only")
    source.add_argument("--submission", type=Path, help="Export bundle containing manifest.json and game/; validate Player provenance before scoring")
    source.add_argument("--recordings", type=Path, help="Native Player PNG/MP4 bundle; validate and score without JSON replay")
    parser.add_argument("--task", required=True, help="Official task directory name, e.g. racing-drift-circuit")
    parser.add_argument("--benchmark", type=Path, default=local_benchmark)
    parser.add_argument("--output", type=Path, help="New output directory; default: validation/gamecraft/<unique-id>; never overwrites")
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env.gamecraft", help="Dedicated judge credentials, ignored for offline smoke tests")
    parser.add_argument("--image", help="Override the recorded replay image; legacy projects use the standard scoring image")
    parser.add_argument("--judge-max-tokens", type=int,
                        help="OpenAI judge output budget for this run only; omitted = upstream default (2048)")
    parser.add_argument("--smoke-test", action="store_true", help="Check the selected evidence pipeline with a stub judge and Docker --network none; not a real score")
    parser.add_argument("--dry-run", action="store_true", help="Validate project/submission and print the plan without launching Docker or making files")
    args = parser.parse_args(argv)
    if args.judge_max_tokens is not None and args.judge_max_tokens <= 0:
        parser.error("--judge-max-tokens must be positive")
    submission = (args.recordings or args.submission)
    submission = submission.resolve() if submission else None
    validator = validate_recordings if args.recordings else validate_gamecraft_submission
    manifest, manifest_hash = None, None
    if submission is not None:
        try:
            project, manifest, manifest_hash = validator(submission)
        except (OSError, ValueError) as exc:
            parser.error(f"Invalid {'--recordings' if args.recordings else '--submission'}: {exc}")
    else:
        project = args.project.resolve()
    recorded_image = ((manifest or {}).get("runtime" if args.recordings else "replay_environment") or {}).get("image_id")
    if recorded_image and not re.fullmatch(r"sha256:[0-9a-f]{64}", recorded_image):
        parser.error("Invalid replay image identity in submission")
    if manifest and manifest.get("task" if args.recordings else "benchmark_task", args.task) != args.task:
        parser.error("Submission benchmark task does not match --task")
    if args.image and recorded_image and args.image != recorded_image:
        print("WARNING: --image overrides the Player replay image; environment equivalence is not established.",
              file=sys.stderr, flush=True)
    args.image = args.image or recorded_image or "rsi-gamecreator/gamecraft-eval:godot-4.6.2-v1"
    benchmark = args.benchmark.resolve()
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", args.task):
        parser.error("Invalid task directory name")
    if not (project / "project.godot").is_file():
        parser.error("Missing project.godot")
    if not args.recordings and not list((project / "demo_outputs").glob("*.json")):
        parser.error("Missing demo_outputs/*.json")
    if not (benchmark / "tasks" / args.task / "tests/rubric.json").is_file():
        parser.error("Benchmark task/rubric not found; specify --benchmark")
    if not (benchmark / "gamecraft_bench/verifier/cli.py").is_file():
        parser.error("Benchmark verifier source not found")
    output = (args.output or ROOT / "validation" / "gamecraft" / (
        f"gamecraft-{args.task}-{'smoke' if args.smoke_test else 'score'}-"
        f"{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}"
    )).resolve()
    if output.exists() or output.is_relative_to(submission or project) or output.is_relative_to(benchmark):
        parser.error("Use a new output directory outside the submission, game and benchmark")
    if not args.smoke_test and not args.env_file.is_file() and not args.dry_run:
        parser.error("Create .env.gamecraft from docker/gamecraft/judge.env.example and fill in the judge API key")

    name = f"gamecraft-eval-{uuid.uuid4().hex[:12]}"
    command = ["docker", "run", "--rm", "--init", "--name", name, "--shm-size", "1g",
               "--mount", f"type=bind,source={project},target=/submission,readonly",
               "--mount", f"type=bind,source={benchmark},target=/opt/gamecraft-bench,readonly",
               "--mount", f"type=bind,source={output},target=/output"]
    if args.smoke_test:
        command += ["--network", "none"]
    else:
        command += ["--env-file", str(args.env_file.resolve())]
    if args.recordings:
        command += ["--mount", f"type=bind,source={submission},target=/recordings,readonly"]
    if args.recordings or args.judge_max_tokens is not None:
        command += ["--mount", f"type=bind,source={ROOT / 'src'},target=/framework,readonly",
                    "--mount", f"type=bind,source={ROOT / 'docker/gamecraft/entrypoint.py'},target=/opt/evaluator/entrypoint-current.py,readonly",
                    "--env", "PYTHONPATH=/framework:/opt/gamecraft-bench", "--entrypoint", "python",
                    args.image, "/opt/evaluator/entrypoint-current.py", "--task", args.task]
        if args.recordings:
            command += ["--recordings"]
    else:
        command += [args.image, "--task", args.task]
    if args.judge_max_tokens is not None:
        command += ["--judge-max-tokens", str(args.judge_max_tokens)]
    if args.smoke_test:
        command += ["--smoke-test"]
    provenance = None
    if manifest is not None:
        provenance = {"manifest": "submission-manifest.json", "manifest_sha256": manifest_hash,
                      "task": args.task, "round": manifest["round"],
                      "source_version": manifest["source_version"],
                      "exported_project_version": manifest.get("exported_project_version", manifest["source_version"]),
                      "episodes": [demo["episode"] for demo in manifest["demos"]],
                      "recorded_replay_image": recorded_image,
                      "goal_verified": None}
        print(f"Player submission: round={manifest['round']}; episodes={provenance['episodes']}; "
              f"demos={len(manifest['demos'])}", flush=True)
        if args.recordings:
            provenance.update(evidence_source="player_recording", replay_performed=False)
            print("Native Player PNG evidence; no JSON replay. Completion is not proof of victory.", flush=True)
        else:
            print("Starting-state alignment and replay outcome are unverified; export checks do not prove victory.",
                  file=sys.stderr, flush=True)
        if any(demo.get("clipped", False) for demo in manifest["demos"]):
            print("WARNING: this submission contains episode prefixes; later outcomes are not included.",
                  file=sys.stderr, flush=True)
    print(f"Output: {output}", flush=True)
    if args.dry_run:
        print(json.dumps({"dry_run": True, "command": command, "submission": provenance}, ensure_ascii=False, indent=2))
        return 0

    image_info = json.loads(subprocess.check_output([
        "docker", "image", "inspect", args.image, "--format", '{{json .}}'
    ], text=True))
    commit = subprocess.run(["git", "-C", str(benchmark), "rev-parse", "HEAD"], capture_output=True, text=True)
    hashes = {str(p.relative_to(benchmark)): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in sorted((benchmark / "gamecraft_bench").rglob("*.py"))}
    # Preserve the exact validated manifest, not a later read or a normalized rewrite.
    manifest_bytes = None
    if submission is not None:
        try:
            _, _, current_hash = validator(submission)
            manifest_bytes = (submission / "manifest.json").read_bytes()
        except (OSError, ValueError) as exc:
            parser.error(f"Submission changed during scoring setup: {exc}")
        if current_hash != manifest_hash or hashlib.sha256(manifest_bytes).hexdigest() != manifest_hash:
            parser.error("Submission manifest changed during scoring setup")
    output.mkdir(parents=True)
    metadata = {"source_project": str(project), "benchmark": str(benchmark),
                "benchmark_commit": commit.stdout.strip() if commit.returncode == 0 else None,
                "benchmark_python_sha256": hashes, "image_id": image_info["Id"],
                "architecture": image_info["Architecture"], "container": name,
                "smoke_test": args.smoke_test, "created_at": datetime.now().astimezone().isoformat()}
    metadata["judge_max_tokens_override"] = args.judge_max_tokens
    if args.recordings:
        metadata["evidence_source"] = "player_recording"
        metadata["adapter_sha256"] = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in [ROOT / "docker/gamecraft/entrypoint.py", *sorted((ROOT / "src/game_agent_lite").glob("gamecraft_record*.py"))]}
    if provenance is not None:
        (output / "submission-manifest.json").write_bytes(manifest_bytes)
        metadata["submission"] = provenance
    (output / "host-manifest.json").write_text(json.dumps(metadata, indent=2) + "\n")
    try:
        result = subprocess.run(command)
        return result.returncode
    except KeyboardInterrupt:
        subprocess.run(["docker", "stop", "--time", "5", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
