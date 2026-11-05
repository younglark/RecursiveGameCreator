"""Best-effort, local accounting. Never used to control an Agent or evaluation.

One receipt per call; summaries are rebuilt, not incremented on resume. Only
host-owned job locations are inspected, never copied evidence or game projects.
"""
from collections import defaultdict
import csv
from datetime import datetime, timezone
import io
import json
import math
import os
from pathlib import Path
import re
import tempfile

from .store import atomic_json, read_json
from .types import PHASES

FORMAT = "agent-usage-v1"
FIELDS = ("input_tokens", "cached_input_tokens", "output_tokens", "total_tokens",
          "reasoning_output_tokens", "cost_usd")
ALIASES = {
    "input_tokens": ("input_tokens", "n_input_tokens", "total_prompt_tokens"),
    "cached_input_tokens": ("cached_input_tokens", "n_cache_tokens", "total_cached_tokens"),
    "output_tokens": ("output_tokens", "n_output_tokens", "total_completion_tokens"),
    "reasoning_output_tokens": ("reasoning_output_tokens",),
    "cost_usd": ("cost_usd",),
}


def _json(path):
    try:
        value = read_json(path)
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _number(value, *, money=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value < 0 or (not money and int(value) != value):
        return None
    return float(value) if money else int(value)


def normalize(value):
    value = value if isinstance(value, dict) else {}
    result = {key: next((_number(value[name], money=key == "cost_usd")
                        for name in names if name in value), None)
              for key, names in ALIASES.items()}
    inputs, cached, outputs = result["input_tokens"], result["cached_input_tokens"], result["output_tokens"]
    if inputs is not None and cached is not None and cached > inputs:
        result["cached_input_tokens"] = None
    # Cached input and reasoning output are subsets, never added a second time.
    result["total_tokens"] = inputs + outputs if inputs is not None and outputs is not None else None
    return result


def aggregate(records):
    rows = [r["tokens"] for r in records]
    result = {"calls": len(rows), "known_calls": sum(r["total_tokens"] is not None for r in rows),
              "unknown_calls": sum(r.get("usage_status") == "unknown" for r in records)}
    result["partial_calls"] = sum(r.get("usage_status") == "partial" for r in records)
    for key in FIELDS:
        known = [r[key] for r in rows if r.get(key) is not None]
        result[key] = sum(known) if len(known) == len(rows) else None
        result["known_" + key] = sum(known)
    result["usage_status"] = ("complete" if all(r.get("usage_status") == "complete" for r in records)
                              else "partial" if any(r.get(k) is not None for r in rows for k in FIELDS[:3])
                              else "unknown")
    return result


def _status(tokens):
    if tokens.get("_incomplete"):
        return "partial" if any(tokens[k] is not None for k in FIELDS[:3]) else "unknown"
    if all(tokens[k] is not None for k in ("input_tokens", "cached_input_tokens", "output_tokens")):
        return "complete"
    return "partial" if any(tokens[k] is not None for k in FIELDS[:3]) else "unknown"


def _combine(values):
    if not values:
        return normalize({})
    result = {key: sum(row[key] for row in values if row[key] is not None)
              if any(row[key] is not None for row in values) else None for key in FIELDS}
    if any(row["cost_usd"] is None for row in values):
        result["cost_usd"] = None
    result["_incomplete"] = any(_status(row) != "complete" for row in values)
    return result


def _events(path):
    try:
        with path.open(errors="replace") as stream:
            for line in stream:
                try:
                    event = json.loads(line)
                    if isinstance(event, dict):
                        yield event
                except ValueError:
                    continue
    except OSError:
        return


def shape_grok_usage(event):
    """Map Grok's uncached prompt buckets onto the host ledger.

    input_tokens from Grok excludes cache. cache_read and cache_creation are
    separate from that number and from each other. The host ledger stores the
    full prompt in input_tokens and cache as a subset. A missing bucket stays
    unknown instead of being summed as zero. An incomplete or partial all-zero
    bill is not recorded as a real zero. usage_is_incomplete is not a complete
    receipt; read_tokens marks those calls partial.
    """
    if not isinstance(event, dict) or not isinstance(event.get("usage"), dict):
        return None
    usage = event["usage"]
    uncached = _number(usage.get("input_tokens"))
    cache_read = _number(usage.get("cache_read_input_tokens"))
    cache_create = _number(usage.get("cache_creation_input_tokens"))
    output = _number(usage.get("output_tokens"))
    known = [item for item in (uncached, cache_read, cache_create, output) if item is not None]
    flagged = bool(event.get("usage_is_incomplete") or event.get("cost_is_partial"))
    if flagged and (not known or all(item == 0 for item in known)):
        return None
    shaped = {}
    buckets = (uncached, cache_read, cache_create)
    if all(item is not None for item in buckets):
        shaped["input_tokens"] = uncached + cache_read + cache_create
        shaped["cached_input_tokens"] = cache_read + cache_create
    if output is not None:
        shaped["output_tokens"] = output
    reasoning = _number(usage.get("reasoning_tokens"))
    if reasoning is not None:
        shaped["reasoning_output_tokens"] = reasoning
    if not event.get("usage_is_incomplete") and not event.get("cost_is_partial"):
        cost = _number(event.get("total_cost_usd"), money=True)
        if cost is not None:
            shaped["cost_usd"] = cost
    if not any(shaped.get(key) is not None for key in ("input_tokens", "cached_input_tokens", "output_tokens")):
        return None
    return shaped


def read_tokens(job, backend):
    """Use one authoritative representation per trial/session, never sum copies."""
    sources, values = [], []
    if backend == "grok":
        event = _json(job / "grok-result.json")
        shaped = shape_grok_usage(event)
        tokens = normalize(shaped)
        if event.get("usage_is_incomplete") and any(tokens[key] is not None for key in FIELDS[:3]):
            tokens["_incomplete"] = True
        if any(tokens[key] is not None for key in FIELDS):
            return tokens, ["grok-result.json"]
        return tokens, []
    if backend == "harbor" or (job / "jobs/role").is_dir():
        seen = set()
        for trial in sorted((job / "jobs/role").glob("*/")):
            result = _json(trial / "result.json")
            identity = result.get("id") or trial.name
            if identity in seen:
                continue
            seen.add(identity)
            tokens = normalize(result.get("agent_result"))
            source = trial / "result.json"
            if tokens["total_tokens"] is None:
                trajectory = _json(trial / "agent/trajectory.json")
                metrics = trajectory.get("final_metrics") or {}
                fallback = normalize({**metrics, **(metrics.get("extra") or {})})
                if fallback["total_tokens"] is not None:
                    tokens, source = fallback, trial / "agent/trajectory.json"
                else:
                    # Failed calls may have session snapshots but no final result.
                    sessions = {}
                    for log in sorted((trial / "agent/sessions").rglob("*.jsonl")):
                        session_id = str(log.relative_to(trial))
                        for event in _events(log):
                            payload = event.get("payload") or {}
                            if event.get("type") == "session_meta":
                                session_id = payload.get("id") or session_id
                            info = payload.get("info") or {}
                            if payload.get("type") == "token_count" and info.get("total_token_usage"):
                                sessions[session_id] = (normalize(info["total_token_usage"]), log)
                    if sessions:
                        tokens = _combine([value[0] for value in sessions.values()])
                        sources.extend(str(value[1].relative_to(job)) for value in sessions.values())
                        source = None
            values.append(tokens)
            if source is not None:
                sources.append(str(source.relative_to(job)))
    else:
        # Codex JSON output reports per-turn usage. Session cumulative snapshots
        # are deliberately not also added to these turn totals.
        for event in _events(job / "process.log"):
            if event.get("type") == "turn.completed":
                values.append(normalize(event.get("usage")))
        if values:
            sources.append("process.log")
    return _combine(values), sorted(set(sources))


def _location(job):
    for folder in job.parents:
        if folder.name.isdigit() and folder.parent.name == "rounds":
            return folder.parent.parent, int(folder.name), "generation"
        if re.fullmatch(r"round-\d+", folder.name):
            return folder.parent, int(folder.name[6:]), "player_retest"
    return None, None, "standalone"


def call_record(job, config=None, *, role=None, context=None, status=None):
    config = config or {}
    request = _json(job / "request.json")
    context = context if context is not None else _json(job / "io/input.json")
    root, number, scope = _location(job)
    role = role or request.get("role")
    phase = context.get("stage") if role == "builder" else {
        "designer": "design", "player": "play", "reviewer": "review"}.get(role)
    backend = config.get("backend") or ("harbor" if (job / "jobs/role").is_dir() else "codex")
    tokens, sources = read_tokens(job, backend)
    status = status or ("completed" if (job / "result.json").is_file() else "incomplete")
    availability = _status(tokens)
    if status != "completed" and availability == "complete":
        availability = "partial"
    return {"format": FORMAT, "call_id": str(job.relative_to(root)) if root else job.name,
            "round": number, "phase": phase or "unknown", "role": role,
            "subphase": context.get("phase"), "scope": scope,
            "model": request.get("model") or config.get("model"),
            "reasoning": config.get("reasoning"), "backend": backend,
            "call_status": status, "tokens": tokens, "usage_status": availability,
            "sources": sources}


def _warn():
    # Do not print raw exceptions (paths/logs can contain credentials).
    try:
        print("[usage] 用量统计暂不可用；不影响 Agent 执行，原始记录保留。", flush=True)
    except Exception:
        pass


def capture(job, config, role, context, status):
    """Called in Agent.call's finally block; never replaces its result/error."""
    try:
        if not (job / "request.json").is_file():
            return
        record = call_record(job, config, role=role, context=context, status=status)
        atomic_json(job / "usage.json", record)
        print(f"[usage] {role}/{record['subphase'] or record['phase']}：" +
              describe(aggregate([record])), flush=True)
    except Exception:
        _warn()


def _jobs(root):
    # Explicit locations exclude inherited policies, source trees, io/evidence,
    # copied reviewer bundles, and Harbor's own request/result representations.
    parents = list(root.glob("rounds/*/jobs/*")) + list(root.glob("round-*/player"))
    if (root / "request.json").is_file() or list(root.glob("author-*/request.json")):
        parents.append(root)
    if (root / "player").is_dir():
        parents.append(root / "player")
    found = set()
    for parent in parents:
        for job in [parent, *parent.glob("author-*"), *parent.glob("analysis-*")]:
            if (job / "request.json").is_file() and not job.is_symlink():
                found.add(job)
    return sorted(found)


def summarize(root, config=None, *, scope="generation"):
    root = Path(root)
    config = config or _json(root / "config.json")
    calls, unreadable = [], []
    for job in _jobs(root):
        record = _json(job / "usage.json")
        if record.get("format") != FORMAT:
            try:
                record = call_record(job, config)
            except Exception:
                record = {"tokens": normalize({}), "usage_status": "unknown", "phase": "unknown",
                          "round": _location(job)[1], "model": config.get("model"),
                          "reasoning": config.get("reasoning"), "backend": config.get("backend")}
                unreadable.append(str(job.relative_to(root)))
        record = {**record, "call_id": str(job.relative_to(root)), "scope": scope}
        calls.append(record)
    stages = defaultdict(list)
    for call in calls:
        stages[(call["round"], call["phase"])].append(call)
    # Include stages actually entered, even when no language model was invoked.
    for event in _events(root / "events.jsonl"):
        if event.get("kind", "").startswith("stage_") and event.get("phase") in PHASES:
            stages.setdefault((event.get("round"), event["phase"]), [])
    rounds = defaultdict(list)
    stage_rows = []
    for (number, phase), records in sorted(stages.items(), key=lambda item: (
            -1 if item[0][0] is None else item[0][0],
            PHASES.index(item[0][1]) if item[0][1] in PHASES else len(PHASES))):
        rounds[number].extend(records)
        stage_rows.append({"round": number, "phase": phase, **aggregate(records),
                           "by_model": _models(records)})
    return {"format": FORMAT, "scope": scope, "updated_at": datetime.now(timezone.utc).isoformat(),
            "excludes": ["asset_service_usage", "external_judge_usage", "compute_cost"],
            "unreadable_calls": unreadable, "calls": calls, "total": aggregate(calls),
            "by_model": _models(calls), "stages": stage_rows,
            "rounds": [{"round": n, **aggregate(records), "by_model": _models(records)}
                       for n, records in rounds.items()]}


def _models(records):
    grouped = defaultdict(list)
    for record in records:
        grouped[(record.get("model"), record.get("reasoning"), record.get("backend"))].append(record)
    return [{"model": key[0], "reasoning": key[1], "backend": key[2], **aggregate(rows)}
            for key, rows in sorted(grouped.items(), key=lambda item: str(item[0]))]


def _csv(path, rows, *, stages=False):
    columns = ["round"] + (["phase"] if stages else []) + ["models", "calls", *FIELDS,
               "known_input_tokens", "known_cached_input_tokens", "known_output_tokens", "known_total_tokens",
               "known_calls", "unknown_calls", "partial_calls", "usage_status"]
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({**row, "models": json.dumps([
            {k: m[k] for k in ("model", "reasoning", "backend")} for m in row["by_model"]], ensure_ascii=False)})
    # Atomic replacement also avoids truncated CSVs if the controller stops.
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="", dir=path.parent,
                                         prefix=".usage-", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(output.getvalue())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def refresh(root, config=None, *, scope="generation"):
    try:
        if not Path(root).is_dir():
            return None
        result = summarize(root, config, scope=scope)
        atomic_json(Path(root) / "usage.json", result)
        _csv(Path(root) / "usage-stages.csv", result["stages"], stages=True)
        _csv(Path(root) / "usage-rounds.csv", result["rounds"])
        return result
    except Exception:
        _warn()
        return None


def reference(root, config=None, *, scope="generation"):
    """Compact wrapper result, with paths relative to its usage directory."""
    result = refresh(root, config, scope=scope)
    if result is None:
        return {"usage_status": "unknown", "total_tokens": None}
    return {**result["total"], "scope": scope, "updated_at": result["updated_at"], "report": "usage.json",
            "stages_csv": "usage-stages.csv", "rounds_csv": "usage-rounds.csv"}


def saved_reference(root):
    """Read-only summary for batch status/dry-run; never scan or rewrite jobs."""
    try:
        data = _json(Path(root) / "usage.json")
        if data.get("format") == FORMAT and isinstance(data.get("total"), dict):
            return {**data["total"], "scope": data["scope"], "updated_at": data["updated_at"],
                    "report": "usage.json", "stages_csv": "usage-stages.csv", "rounds_csv": "usage-rounds.csv"}
    except Exception:
        pass
    return {"usage_status": "unknown", "total_tokens": None}


def event_usage(root, config, stage=None):
    result = refresh(root, config)
    if result is None:
        return None
    value = {"total": result["total"]}
    if stage:
        value["stage"] = next((r for r in result["stages"] if r["round"] == stage["round"]
                               and r["phase"] == stage["phase"]), aggregate([]))
        value["round"] = next((r for r in result["rounds"] if r["round"] == stage["round"]), aggregate([]))
    return value


def describe(summary):
    def value(key):
        return f"{summary[key]:,}" if summary.get(key) is not None else "未知"
    label = "总计" if summary.get("usage_status") == "complete" else "已知小计"
    total = value("total_tokens" if label == "总计" else "known_total_tokens")
    text = f"输入{value('input_tokens')}，缓存{value('cached_input_tokens')}，输出{value('output_tokens')}，{label}{total} token"
    if summary.get("usage_status") != "complete":
        text += f"（用量不完整；未知调用{summary.get('unknown_calls', 0)}，部分调用{summary.get('partial_calls', 0)}）"
    return text


def report_lines(summary):
    if summary is None:
        return ["", "Token 统计暂不可用；不代表消耗为零。"]
    lines = ["", "## 模型用量", "", describe(summary["total"]), "",
             "仅统计本次 Agent 调用；缓存已包含在输入中，推理 token 已包含在输出中。",
             "资产服务、外部裁判和计算费用未包含；未知费用保持 null，不估算订阅账单。", "",
             "| 轮次 | 阶段 | 调用 | 输入 | 缓存输入 | 输出 | 总计/已知小计 | 完整性 |",
             "|---|---|---:|---:|---:|---:|---:|---|"]
    for row in [*summary["stages"], *({**r, "phase": "轮次合计"} for r in summary["rounds"])]:
        def fmt(key):
            return f"{row[key]:,}" if row.get(key) is not None else "未知"
        total = fmt("total_tokens" if row["usage_status"] == "complete" else "known_total_tokens")
        lines.append(f"| {row['round']} | {row['phase']} | {row['calls']} | {fmt('input_tokens')} | "
                     f"{fmt('cached_input_tokens')} | {fmt('output_tokens')} | {total} | {row['usage_status']} |")
    lines += ["", "明细：[usage.json](usage.json)；[阶段表](usage-stages.csv)；[轮次表](usage-rounds.csv)。"]
    return lines
