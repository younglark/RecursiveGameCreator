#!/usr/bin/env python3
"""Re-test only Player on saved rounds, optionally score its NEW GameCraft submission.

Never resumes the generation loop or changes the source run. Each invocation needs
a new output directory; completed scoring is never automatically retried.
"""
import argparse
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
import re
import subprocess
import sys
import uuid

from game_agent_lite.agent import Agent
from game_agent_lite.cli import load_config
from game_agent_lite.game import make_game
from game_agent_lite.gamecraft import validate_gamecraft_submission
from game_agent_lite.gamecraft_recordings import validate_recordings
from game_agent_lite.gamecraft_player import EVAL_IMAGE
from game_agent_lite.policy import PolicySession, valid_policy_play
from game_agent_lite.store import (atomic_json, file_hash, read_json, redact,
                                  safe_path, version)
from game_agent_lite.types import DEFAULTS
from game_agent_lite.usage import reference as usage_reference, refresh as refresh_usage

ROOT = Path(__file__).resolve().parents[1]


def previous_policy(run, number, expected):
    """Invalid/incomplete old reports must not prevent a fresh Player test."""
    receipt = run / f"rounds/{number:04d}/played-candidate.json"
    if receipt.is_file():
        try:
            saved = read_json(receipt)
            if saved.get("unavailable"):
                return None
            path = safe_path(run, saved["report"])
            if (file_hash(path) == saved["sha256"] and
                    valid_policy_play(read_json(path), path.parent, expected)):
                return {"path": path, "version": expected, "reference": saved["report"]}
        except (OSError, ValueError, KeyError, TypeError):
            pass
    return None


def prepare(args):
    run, benchmark = args.run_dir.resolve(), args.benchmark.resolve()
    output = (args.output or ROOT / "validation" / "gamecraft" / (
        f"player-{args.task}-{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}"
    )).resolve()
    if output.exists() or output.is_relative_to(run) or output.is_relative_to(benchmark):
        raise ValueError("Use a NEW output directory outside the original run and benchmark")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", args.task):
        raise ValueError("Invalid task name")
    if len(set(args.rounds)) != len(args.rounds) or any(n < 0 for n in args.rounds):
        raise ValueError("Round numbers must be nonnegative and unique")
    for path in (f"tasks/{args.task}/instruction.md", f"tasks/{args.task}/tests/rubric.json",
                 "gamecraft_bench/verifier/replay.py"):
        if not (benchmark / path).is_file():
            raise ValueError(f"Missing benchmark file: {path}")
    config = load_config(run / "config.json")
    if config["engine"] != "godot" or config["backend"] == "mock":
        raise ValueError("This entrypoint requires an existing real Godot run")
    if (config.get("benchmark") or {}).get("task", args.task) != args.task:
        raise ValueError("Task does not match the saved benchmark config")
    config.update(model="gpt-6-astra", reasoning="high")
    config["player"].update(mode="policy", trajectories_per_version=args.episodes,
                            parallel_episodes=args.parallel_episodes)
    config["runtime"].update(mode="docker", image=args.image, render_threads=args.render_threads)
    config["benchmark"] = {"name": "gamecraft", "task": args.task,
                           "path": str(benchmark), "image": args.image, "evidence_source": args.evidence_source}
    if args.score and not args.env_file.is_file():
        raise ValueError("Judge env file missing; configure it before using --score")
    selections = []
    for number in args.rounds:
        folder = run / f"rounds/{number:04d}"
        check = read_json(folder / "check.json")
        project = safe_path(run, check["project"])
        if not check.get("passed") or not (project / "project.godot").is_file():
            raise ValueError(f"Round {number} has no built Godot project")
        if version(project) != check["version"]:
            raise ValueError(f"Round {number} source no longer matches its build receipt")
        context = {"round": number, "version": check["version"], "plan": None,
                   "builder_summary": None}
        integrated = folder / "integrate.json"
        if integrated.is_file():
            data = read_json(integrated)
            if data.get("version") == check["version"] and data.get("project") == check["project"]:
                context["builder_summary"] = data.get("builder", {}).get("summary")
                if (folder / "design.json").is_file():
                    context["plan"] = read_json(folder / "design.json")
        selections.append({"round": number, "project": str(project), "version": check["version"],
                           "context": context})
    return run, output, config, selections


def run_round(args, run, output, config, item):
    number, expected = item["round"], item["version"]
    project = Path(item["project"])
    folder = output / f"round-{number:04d}"
    folder.mkdir()
    job = folder / "player"
    result = {"round": number, "version": expected, "status": "failed", "official_score": None}
    try:
        print(f"round={number} Player 开始：{args.episodes} 条新轨迹，"
              f"并发上限 {args.parallel_episodes}，渲染线程 {args.render_threads}，每条最多 20 秒", flush=True)
        parent = previous_policy(run, number, expected)
        session = PolicySession(deepcopy(config), project, job, expected, make_game,
                                context=item["context"], parent=parent)
        report = session.run(Agent(config))
        if not valid_policy_play(report, job, expected):
            raise ValueError("Player evidence did not pass validation; recordings retained; scoring skipped")
        replay = read_json(job / "gamecraft.json")
        native = args.evidence_source == "player_recording"
        if replay["status"] != ("recorded" if native else "completed"):
            raise ValueError(("Native recording is incomplete" if native else "Official offline replay did not complete") + "; scoring skipped")
        submission = job / ("gamecraft/recordings" if native else "gamecraft/submission")
        _, manifest, _ = (validate_recordings if native else validate_gamecraft_submission)(submission)
        if manifest["source_version"] != expected or manifest["round"] != number:
            raise ValueError("Submission does not match the selected round/version")
        result.update(status="player_completed", submission=str(submission.relative_to(output)), evidence_source=args.evidence_source)
        print(f"round={number} " + ("Player 原生录像与截图已保存，未执行 JSON 重放" if native else "Player 与官方离线重放完成") +
              "；目标是否达成见 player/gamecraft-goals.json", flush=True)
        if args.score:
            if version(project) != expected:
                raise ValueError("Original game changed; scoring skipped")
            print(f"round={number} 开始付费裁判评分，证据来源：{args.evidence_source}", flush=True)
            command = [sys.executable, "-u", str(ROOT / "scripts/score_gamecraft.py"),
                       "--task", args.task, "--recordings" if native else "--submission", str(submission),
                       "--benchmark", config["benchmark"]["path"],
                       "--env-file", str(args.env_file.resolve()), "--output", str(folder / "score")]
            completed = subprocess.run(command, cwd=ROOT)
            if completed.returncode:
                raise RuntimeError(f"Scorer exited with {completed.returncode}; see round-{number:04d}/score/")
            evaluation = read_json(folder / "score/evaluation.json")
            score_key = "recording_score" if native else "official_score"
            if evaluation.get(score_key) is None:
                raise ValueError(f"Scorer returned no valid {score_key}; inspect score/evaluation.json")
            result.update(status="scoring_completed", **{score_key: evaluation[score_key]})
    except Exception as exc:
        result.update(status="failed", error=redact(f"{type(exc).__name__}: {exc}"))
    finally:
        if version(project) != expected:
            result.update(status="failed", error="Original game version changed during re-test")
        result["usage"] = usage_reference(folder, config, scope="player_retest")
        atomic_json(folder / "result.json", result)
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--task", required=True)
    parser.add_argument("--rounds", type=int, nargs="+", required=True)
    parser.add_argument("--output", type=Path, help="New output directory; default: validation/gamecraft/<unique-id>")
    parser.add_argument("--episodes", type=int, choices=range(1, 11), default=3)
    parser.add_argument("--parallel-episodes", type=int, choices=range(1, 11),
                        default=DEFAULTS["player"]["parallel_episodes"],
                        help="Concurrent episodes per round (default: 1, serial); does not change episode count")
    parser.add_argument("--render-threads", type=int, default=DEFAULTS["runtime"]["render_threads"],
                        help="Godot software rendering threads (default: 4)")
    parser.add_argument("--benchmark", type=Path, default=ROOT / "benchmarks/gamecraft-bench")
    parser.add_argument("--image", default=EVAL_IMAGE)
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env.gamecraft")
    parser.add_argument("--score", action="store_true", help="After Player/replay validation, run the real paid judge")
    parser.add_argument("--evidence-source", choices=("player_recording", "official_replay"), default="player_recording",
                        help="Default: native 2 FPS Player frames; official_replay retains the JSON replay path")
    parser.add_argument("--dry-run", action="store_true", help="Read-only validation; no models, Docker, games, judge or output files")
    args = parser.parse_args(argv)
    if args.render_threads < 1:
        parser.error("--render-threads must be a positive integer")
    try:
        run, output, config, selections = prepare(args)
    except (OSError, ValueError, KeyError) as exc:
        parser.error(redact(str(exc)))
    plan = {"run": str(run), "task": args.task, "rounds": args.rounds,
            "episodes_per_round": args.episodes, "parallel_episodes": args.parallel_episodes,
            "render_threads": args.render_threads, "model": config["model"],
            "reasoning": config["reasoning"], "max_demo_seconds": 20,
            "image": args.image, "score": args.score, "output": str(output),
            "evidence_source": args.evidence_source,
            "selections": selections}
    print(json.dumps({k: v for k, v in plan.items() if k != "selections"}, ensure_ascii=False, indent=2), flush=True)
    if args.dry_run:
        print("DRY RUN OK — 未启动 Player、Docker 或评分，未写入文件", flush=True)
        return 0
    output.mkdir(parents=True, exist_ok=False)
    atomic_json(output / "plan.json", plan)
    results = []
    for item in selections:
        results.append(run_round(args, run, output, config, item))
        atomic_json(output / "results.json", results)
        refresh_usage(output, config, scope="player_retest")
    return int(any(item["status"] == "failed" for item in results))


if __name__ == "__main__":
    raise SystemExit(main())
