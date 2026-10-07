"""Score canonical Player PNGs with upstream judges/formula; never replay inputs.

The upstream package is mounted read-only. This adapter replaces evidence acquisition,
not the judge prompt, requirement aggregation, retries or score formula.
"""
import argparse
from dataclasses import replace
import math
from pathlib import Path
import shutil
import time

from .gamecraft_recordings import validate_recordings
from .store import atomic_json, read_json


def score_recordings(bundle, rubric_path, output, judge):
    from gamecraft_bench.verifier import score as upstream
    from gamecraft_bench.verifier.judges.base import JudgeError, JudgeRequest, RequirementSpec

    _, manifest, _ = validate_recordings(bundle)
    rubric = read_json(rubric_path)
    if len(manifest["demos"]) > int(rubric.get("max_demos", 10)):
        raise ValueError("Recording count exceeds task limit; no silent selection")
    if any(d["duration_seconds"] > float(rubric.get("max_demo_seconds", 20)) for d in manifest["demos"]):
        raise ValueError("Recording duration exceeds task limit")
    output.mkdir(parents=True, exist_ok=True)
    build_ok, build_log = upstream._run_build_check(rubric["build_check"], output)
    requirements = rubric["requirements"]
    scores = {r["id"]: upstream.RequirementScore(r["id"], r["description"], {}, 0.0,
                                                upstream._validate_agg(r)) for r in requirements}
    errors, artifacts, logs = [], [], []
    specs = [RequirementSpec(id=r["id"], description=r["description"]) for r in requirements]
    if build_ok:
        for demo in manifest["demos"]:
            demo_id = f"{demo['episode']:04d}"
            dest = output / "demos" / demo_id
            (dest / "frames").mkdir(parents=True, exist_ok=False)
            video = dest / "play.mp4"
            shutil.copyfile(bundle / demo["video"]["path"], video)
            frames = []
            for i, frame in enumerate(demo["frames"]):
                path = dest / "frames" / f"frame_{i + 1:04d}.png"
                shutil.copyfile(bundle / frame["image"]["path"], path)
                frames.append(path)
            # This is a recording receipt, not a synthetic replay trace.
            reference = dest / "recording.json"
            atomic_json(reference, demo)
            artifacts.append(upstream.DemoArtifacts(demo_id, reference, video, frames, demo["duration_seconds"]))
            request = JudgeRequest(demo_id=demo_id, video_path=video, frame_paths=frames, requirements=specs)
            began, response, last = time.monotonic(), None, None
            for attempt in range(upstream._JUDGE_MAX_ATTEMPTS):
                if attempt:
                    time.sleep(5 * attempt)
                try:
                    response = judge.score(request)
                    break
                except JudgeError as exc:
                    last = exc
            if response is None:
                errors.append(f"judge failed on {demo_id}: {last}")
            for index, spec in enumerate(specs):
                raw = response.scores.get(spec.id, 0.0) if response else 0.0
                try:
                    value = float(raw)
                    if not math.isfinite(value):
                        raise ValueError("non-finite")
                except (ValueError, TypeError):
                    value = 0.0
                    errors.append(f"judge returned non-numeric score for {demo_id}/{spec.id}")
                value = max(0.0, min(1.0, value))
                old = scores[spec.id]
                per_demo = {**old.per_demo, demo_id: value}
                scores[spec.id] = replace(old, per_demo=per_demo, aggregated=upstream._aggregate(old.agg, per_demo))
                logs.append({"demo_id": demo_id, "requirement_id": spec.id, "score": value,
                    "rationale": response.rationales.get(spec.id, "") if response else "",
                    "raw": response.raw if response and index == 0 else "",
                    "latency_seconds": round(time.monotonic() - began, 3)})
    variables = {rubric["build_check"].get("id", "BUILD"): float(build_ok),
                 **{key: item.aggregated for key, item in scores.items()}}
    try:
        reward = max(0.0, min(1.0, upstream._safe_eval_formula(rubric["score_formula"], variables)))
    except upstream.FormulaError as exc:
        errors.append(f"score_formula evaluation failed: {exc}")
        reward = 0.0
    result = upstream.ScoreResult(reward, build_ok, build_log, rubric["score_formula"],
        list(scores.values()), artifacts, type(judge).__name__, judge.model, errors)
    upstream._write_artifacts(output, result, logs, variables)
    breakdown = read_json(output / "breakdown.json")
    breakdown.update(evidence_source="player_recording", replay_performed=False,
                     capture=manifest["capture"])
    for demo in breakdown["demos"]:
        demo["recording"] = demo.pop("trace")
    atomic_json(output / "breakdown.json", breakdown)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--recordings", type=Path, required=True)
    parser.add_argument("--rubric", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--smoke-test", action="store_true")
    args = parser.parse_args()
    from gamecraft_bench.verifier.judges import get_judge
    judge = get_judge("stub", "0") if args.smoke_test else get_judge()
    result = score_recordings(args.recordings, args.rubric, args.output, judge)
    print(f"[verifier] evidence_source=player_recording; replay_performed=False; build_ok={result.build_ok}", flush=True)
    print(f"[verifier] demos={len(result.demos)}; reward={result.reward}; errors={len(result.errors)}", flush=True)
    for item in result.requirements:
        print(f"[verifier] {item.requirement_id} {item.aggregated:.3f} {item.per_demo}", flush=True)
    return 2 if result.errors else (0 if result.build_ok else 1)


if __name__ == "__main__":
    raise SystemExit(main())
