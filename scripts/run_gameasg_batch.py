"""Sequential, resumable GameASG batches; preparation and status never run agents."""
import argparse
from collections import Counter
from contextlib import contextmanager
from copy import deepcopy
import csv
from datetime import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time

from game_agent_lite.paths import load_saved_config, portable_config
from game_agent_lite.cli import load_config
from game_agent_lite.store import Store, atomic_json, file_hash, read_json, redact, safe_path, version
from game_agent_lite.types import CONFIG_SCHEMA, validate
from game_agent_lite.usage import reference as usage_reference, saved_reference

ROOT = Path(__file__).resolve().parents[1]
FORMAT = "gameasg-batch-v2"
BENCH_COMMIT = "6427cb6517ce11ff350ec45fcae17b67c5aec245"
DOCS = ("target.md", "game-spec.md", "tdd.md")
IMAGE = "gamebench-env:v2.0"
REPORTS = ("summary-test.json", "summary.json", "report-l1.json", "report-l2.json",
           "test.log", "l2-stderr.log")
NOTES = """[RSI execution environment; gameplay/interface requirements in the three official documents remain unchanged]
Implement the task through RSI's HTML development/play pipeline for one round. Do not change the genre or remove official requirements.
The three official documents are included in full below and also stored at the Game source workspace root. Read each, not just a design summary; identical copies need not be reread within the same session.
/envarena/workspace in the documents maps to Builder's actual Game source workspace.
The sole final entry is index.html; official evaluation copies only this file. Inline all first-party HTML/CSS/JS.
Do not depend on local external scripts, styles, JSON, images, audio or models. Embed required generated assets in HTML.
Read the framework-installed game_agent_bridge.js, inline its original implementation in an index.html script,
then call GameAgentBridge.register according to the framework contract. Do not reference it through external script src.
Also fully implement the official window.__gameTest; both interfaces must use the same actual game state.
GameAgentBridge.observe exposes player-visible state and clickable control positions; reset resets the real game.
Do not expose test-scenario solutions or hidden internal answers as Player observations. Player uses real keyboard/mouse inputs.
Implement valid scenarios, actions, state and rejection rules from the TDD; do not fabricate outcomes or create separate test-only state.
Player plans complementary goals from public requirements, covering the core loop, boundary recovery and UI, retaining coverage gaps.
This is not GameCraft mode and has no universal 20-second limit. End normal play on a clear terminal state or completed test goal.
Use only public documents for self-tests; do not search for official tests, test executors or past scores.
"""


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def git(path, *args):
    return subprocess.check_output(["git", "-C", str(path), *args], text=True).strip()


def code_identity():
    names = git(ROOT, "ls-files", "--cached", "--others", "--exclude-standard", "--",
                "src", "scripts", "docker", "pyproject.toml", "uv.lock").splitlines()
    files = {name: file_hash(ROOT / name) if (ROOT / name).is_file() else None
             for name in sorted(set(names))}
    return {"commit": git(ROOT, "rev-parse", "HEAD"), "files_sha256": digest(files)}


def benchmark_identity(bench):
    commit = git(bench, "rev-parse", "HEAD")
    if commit != BENCH_COMMIT or git(bench, "status", "--porcelain", "--untracked-files=no"):
        raise ValueError(f"GameASG 必须使用未修改的固定版本 {BENCH_COMMIT}")
    return commit


def discover(bench):
    names = sorted(p.name for p in (bench / "task").iterdir()
                   if p.is_dir() and all((p / name).is_file() for name in DOCS))
    if len(names) != 47:
        raise ValueError("GameASG 应有 47 题；任务数量变化，请核对版本")
    return names


def slug(value):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", value):
        raise ValueError(f"无效名称：{value}")
    return value


def paths(batch, manifest, task):
    work = batch / task["id"]
    run = (batch / manifest["runs"]).resolve() / task["id"]
    bench = (batch / manifest["benchmark"]["path"]).resolve()
    output = bench / "output" / manifest["exp_id"] / task["id"]
    return work, run, output


def prepare(bench, template, *, all_tasks=False, tasks=(), exp_id=None, env_file=None,
            image=IMAGE, dry_run=False):
    bench, template = bench.resolve(), template.resolve()
    commit = benchmark_identity(bench)
    available = discover(bench)
    selected = available if all_tasks else list(tasks)
    if ((all_tasks and tasks) or not selected or len(selected) != len(set(selected))
            or not set(selected) <= set(available)):
        raise ValueError("请选择 --all-tasks 或不重复的有效 --tasks")
    exp_id = slug(exp_id or "gameasg-" + datetime.now().strftime("%Y%m%d-%H%M%S"))
    batch = ROOT / "validation/gameasg" / exp_id
    runs = ROOT / "runs/gameasg" / exp_id
    if any(p.exists() for p in (batch, runs, bench / "output" / exp_id)):
        raise ValueError("批次目录已存在；继续请用 run，另做实验请换 --exp-id")
    config = load_config(template)
    if config["backend"] != "harbor" or config["engine"] != "html" or config["runtime"]["mode"] != "docker":
        raise ValueError("批量入口需要 Harbor + HTML Docker 配置")
    config.update(entry="index.html", rounds=1, benchmark=None,
                  model="gpt-6-astra", reasoning="high")
    config["player"].update(mode="policy", trajectories_per_version=3)
    if env_file is None:
        env_file = next((p for p in (ROOT / ".env.hunyuan", ROOT.parent / ".env.hunyuan")
                         if p.is_file()), None)
    if env_file is None or not env_file.expanduser().is_file():
        raise ValueError("请先配置 .env.hunyuan，或提供 --env-file；不会读取或保存密钥内容")
    env_file = env_file.expanduser().resolve()
    code = code_identity()
    shared = {"task_ids": selected, "benchmark_commit": commit, "code_sha256": code["files_sha256"],
              "template_sha256": file_hash(template), "model": config["model"],
              "reasoning": config["reasoning"], "rounds": 1, "player": config["player"], "image": image}
    manifest = {"format": FORMAT, "exp_id": exp_id,
                "created_at": time.time(), "code": code, **shared,
                "protocol_sha256": digest(shared), "runs": os.path.relpath(runs, batch),
                "benchmark": {"path": os.path.relpath(bench, batch), "commit": commit}, "tasks": []}
    prepared = []
    for name in selected:
        slug(name)
        if not all((bench / "tests" / name / file).is_file() for file in ("checks.json", "checks.js")):
            raise ValueError(f"{name}: 缺少官方测试文件")
        work = batch / name
        docs = {file: (bench / "task" / name / file).read_bytes() for file in DOCS}
        cfg = deepcopy(config)
        cfg["source"] = "task-docs"
        cfg["brief"] = NOTES + "\n\n" + "\n\n".join(f"[Official {file}, original text]\n{body.decode()}" for file, body in docs.items())
        cfg["assets"]["env_file"] = os.path.relpath(env_file, work)
        cfg = portable_config(cfg, work)
        validate(cfg, CONFIG_SCHEMA)
        item = {"id": name, "config_sha256": digest(cfg),
                "documents": {file: hashlib.sha256(body).hexdigest() for file, body in docs.items()}}
        manifest["tasks"].append(item)
        prepared.append((work, cfg, docs))
    if not dry_run:
        batch.mkdir(parents=True, exist_ok=False)
        for work, cfg, docs in prepared:
            (work / "task-docs").mkdir(parents=True)
            for file, body in docs.items():
                (work / "task-docs" / file).write_bytes(body)
            atomic_json(work / "config.local.json", cfg)
        atomic_json(batch / "batch.json", manifest)
        refresh(batch, manifest, persist=True)
    return batch, manifest


def load_manifest(batch):
    value = read_json(batch / "batch.json")
    if value.get("format") != FORMAT or value.get("rounds") != 1:
        raise ValueError("不是受支持的 GameASG 一轮批次")
    slug(value["exp_id"])
    names = [slug(t["id"]) for t in value["tasks"]]
    if not names or len(names) != len(set(names)):
        raise ValueError("批次任务为空或重复")
    return value


def active(path):
    if not path.exists():
        return False
    with path.open("r") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(stream, fcntl.LOCK_UN)
    return False


@contextmanager
def task_lock(path):
    # Pass the lease to children: a killed controller must not allow a second evaluator.
    with path.open("a+") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield stream.fileno()
        finally:
            # Closing, rather than explicitly unlocking, preserves a surviving child's lease.
            pass


def check_task(batch, manifest, task):
    work, run, output = paths(batch, manifest, task)
    cfg_path = work / "config.local.json"
    if digest(read_json(cfg_path)) != task["config_sha256"]:
        raise ValueError("冻结配置已修改")
    for name, expected in task["documents"].items():
        if file_hash(work / "task-docs" / name) != expected:
            raise ValueError("冻结的公开文档已修改")
    config = load_config(cfg_path)
    if run.exists() and load_saved_config(run) != config:
        raise ValueError("运行配置与冻结配置不一致；不要搬动或改写正在续跑的批次")
    return config


def delivered(run):
    state = read_json(run / "state.json")
    if state["status"] != "completed" or state["round"] != 1 or [r["round"] for r in state["history"]] != [0]:
        raise ValueError("尚未完整完成 round 0")
    check = read_json(run / "rounds/0000/check.json")
    project = safe_path(run, check["project"])
    html = project / "index.html"
    if not check["passed"] or version(project) != check["version"]:
        raise ValueError("构建未通过或源游戏版本已改变")
    if html.is_symlink() or not html.is_file() or not html.stat().st_size:
        raise ValueError("缺少正常的 index.html")
    return html, check


def evaluation_metrics(directory, task):
    summary = read_json(directory / "summary-test.json")
    if any(summary.get(k) != v for k, v in {"game": task, "mode": "test-only", "test_phase": "all", "role": "target"}.items()):
        raise ValueError("官方报告的任务或评测模式不匹配")
    metrics = {"evaluation_time": summary.get("evaluation_time"), "strict_task_success": None,
               "strict_note": "test-only: not an official verified end-to-end result"}
    for phase in ("l1", "l2"):
        report = read_json(directory / f"report-{phase}.json")
        groups = ("tool_checks", "checks", "anti_patterns", "proximity_checks") if phase == "l1" else ("checks",)
        checks = [c for group in groups for c in report.get(group, [])]
        applicable = [c for c in checks if phase == "l1" or c.get("status") != "NOT_APPLICABLE"]
        counts = {"passed": sum(c.get("status") == "PASS" for c in applicable), "total": len(applicable)}
        item = summary[phase]
        if (report.get("error") or not checks or item.get("status") != "OK"
                or any(type(item.get(k)) is not int or item[k] != v for k, v in counts.items())):
            raise ValueError(f"{phase}: 官方报告不完整或计数不一致")
        metrics[phase] = counts
        if phase == "l2":
            metrics["priorities"] = {level: {
                "passed": sum(c.get("level") == level and c.get("status") == "PASS" for c in applicable),
                "total": sum(c.get("level") == level for c in applicable)} for level in ("P0", "P1", "P2")}
    return metrics


def inspect_task(batch, manifest, task):
    work, run, output = paths(batch, manifest, task)
    row = {"task": task["id"], "run": os.path.relpath(run, batch), "status": "pending", "command": "start",
           "model": manifest["model"], "reasoning": manifest["reasoning"], "usage": saved_reference(run)}
    try:
        if active(work / "task.lock") or active(run / "run.lock"):
            return {**row, "status": "active", "command": None}
        check_task(batch, manifest, task)
        if not run.exists():
            return row
        state = read_json(run / "state.json")
        row.update(status=state["status"], round=state["round"], phase=state["phase"], error=state.get("error"), command=None)
        if state["status"] in ("ready", "running", "failed", "stopped"):
            row["command"] = "resume"
        elif state["status"] == "completed":
            html, _ = delivered(run)
            row.update(status="ready_to_evaluate", command="evaluate")
            receipt = work / "evaluation.json"
            if receipt.exists():
                saved = read_json(receipt)
                if saved["html_sha256"] != file_hash(html) or saved["returncode"] != 0:
                    raise ValueError("评测回执与提交版本不一致")
                for name, expected in saved["artifacts"].items():
                    if file_hash(safe_path(work, name)) != expected:
                        raise ValueError("已保存评测报告被修改")
                metrics = evaluation_metrics(safe_path(work, saved["reports"]), task["id"])
                row.update(status="evaluated", command=None, metrics=metrics)
        elif state["status"] != "needs_reconciliation":
            raise ValueError("未知运行状态")
    except Exception as exc:
        row.update(status="invalid", command=None, error=redact(str(exc)))
    return row


def refresh(batch, manifest, *, persist=False):
    rows = [inspect_task(batch, manifest, task) for task in manifest["tasks"]]
    for row in rows:
        failure = batch / row["task"] / "last-attempt.json"
        if row["status"] not in ("evaluated", "active", "invalid") and failure.exists():
            row["last_attempt"] = read_json(failure)
    result = {"format": FORMAT, "exp_id": manifest["exp_id"], "updated_at": time.time(),
              "counts": dict(Counter(row["status"] for row in rows)), "tasks": rows}
    if persist:
        atomic_json(batch / "results.json", result)
        fields = ["task", "status", "model", "reasoning", "run", "l1_passed", "l1_total", "l2_passed", "l2_total",
                  "P0_passed", "P0_total", "P1_passed", "P1_total", "P2_passed", "P2_total", "evaluation_time",
                  "input_tokens", "cached_input_tokens", "output_tokens", "total_tokens", "known_total_tokens", "cost_usd", "usage_status"]
        fd, temporary = tempfile.mkstemp(prefix=".results-", dir=batch)
        try:
            with os.fdopen(fd, "w") as stream:
                writer = csv.DictWriter(stream, fieldnames=fields)
                writer.writeheader()
                for row in rows:
                    record = {k: row.get(k) for k in fields}
                    metrics = row.get("metrics", {})
                    record["evaluation_time"] = metrics.get("evaluation_time")
                    for group in ("l1", "l2", "P0", "P1", "P2"):
                        counts = metrics.get(group, metrics.get("priorities", {}).get(group, {}))
                        for key in ("passed", "total"):
                            record[f"{group}_{key}"] = counts.get(key)
                    record.update({k: v for k, v in row["usage"].items() if k in fields})
                    writer.writerow(record)
            os.replace(temporary, batch / "results.csv")
        finally:
            Path(temporary).unlink(missing_ok=True)
    return result


def preflight(batch, manifest, *, docker=False):
    bench = (batch / manifest["benchmark"]["path"]).resolve()
    benchmark_identity(bench)
    if code_identity()["files_sha256"] != manifest["code"]["files_sha256"]:
        raise ValueError("批次准备后的执行代码已改变；请使用匹配版本续跑，或准备新批次")
    configs = [check_task(batch, manifest, task) for task in manifest["tasks"]]
    if not docker:
        return
    for cfg in configs:
        if not Path(cfg["auth_file"]).is_file() or not Path(cfg["assets"]["env_file"]).is_file():
            raise ValueError("Codex 认证文件或混元 env 文件缺失")
        if not shutil.which(cfg["harbor"]):
            raise ValueError("找不到 Harbor；请检查 PATH")
    names = {manifest["image"]} | {c["harbor_image"] for c in configs} | {c["runtime"]["image"] for c in configs}
    images = {}
    for name in sorted(names):
        value = json.loads(subprocess.check_output(["docker", "image", "inspect", name], text=True))[0]
        images[name] = {"id": value["Id"], "architecture": value["Architecture"], "os": value["Os"]}
    target = batch / "environment.json"
    if target.exists() and read_json(target)["images"] != images:
        raise ValueError("镜像与首次运行不一致；恢复匹配镜像或使用新批次")
    if not target.exists():
        atomic_json(target, {"images": images, "recorded_at": time.time()})


def launch(command, *, cwd, log, batch, run, lease, generation=False, env=None):
    """Stream logs and watch STOP without an arbitrary model/experiment time limit."""
    with log.open("a") as output, log.open("r") as reader:
        reader.seek(0, os.SEEK_END)
        with subprocess.Popen(command, cwd=cwd, stdout=output, stderr=subprocess.STDOUT,
                              env=env, start_new_session=True, pass_fds=(lease,)) as process:
            interrupted = False
            while process.poll() is None:
                if (batch / "STOP").exists() and not interrupted:
                    interrupted = True
                    if generation:
                        # The CLI checks this after creating the run as well, avoiding an early-signal race.
                        print("停止请求已收到，等待当前生成保存状态。", flush=True)
                    else:
                        os.killpg(process.pid, signal.SIGTERM)
                if interrupted and generation and (run / "state.json").exists():
                    Store(run).request_stop()
                for line in reader:
                    print(line, end="", flush=True)
                try:
                    process.wait(timeout=0.5)
                except subprocess.TimeoutExpired:
                    pass
            for line in reader:
                print(line, end="", flush=True)
            return process.returncode


def archive_reports(source, destination):
    destination.mkdir(parents=True, exist_ok=True)
    for name in REPORTS:
        path = source / name
        if path.is_symlink():
            raise ValueError("评测报告不能是符号链接")
        if path.is_file():
            shutil.copy2(path, destination / name)


def evaluate(batch, manifest, task, lease):
    work, run, output = paths(batch, manifest, task)
    html, check = delivered(run)
    sha = file_hash(html)
    submission = work / "submission.json"
    if submission.exists():
        saved = read_json(submission)
        if saved["html_sha256"] != sha or saved["version"] != check["version"]:
            raise ValueError("已有提交回执与游戏不一致")
    elif output.exists():
        raise ValueError("官方输出目录已有未知内容；不覆盖")
    else:
        atomic_json(submission, {"task": task["id"], "round": 0,
            "run": os.path.relpath(run, work), "source_project": os.path.relpath(html.parent, work),
            "version": check["version"], "html_sha256": sha,
            "evaluation_directory": os.path.relpath(output, work), "model": manifest["model"],
            "reasoning": manifest["reasoning"], "documents": task["documents"],
            "generation": "RSI-GameCreator; official evaluator test-only entrypoint"})
    output.mkdir(parents=True, exist_ok=True)
    target = output / "index.html"
    if target.is_symlink() or (target.exists() and file_hash(target) != sha):
        raise ValueError("官方评测副本已改变；不覆盖")
    if not target.exists():
        # Atomic delivery allows recovery from a crash while copying a large embedded asset.
        temporary = output / ".index-pending"
        shutil.copyfile(html, temporary)
        os.replace(temporary, target)
    attempts = work / "evaluations"
    attempts.mkdir(exist_ok=True)
    number = max((int(p.name) for p in attempts.iterdir() if p.name.isdigit()), default=0) + 1
    attempt = attempts / f"{number:04d}"
    attempt.mkdir()
    archive_reports(output, attempt / "previous")
    # Stale reports cannot masquerade as outputs of an interrupted new attempt.
    for name in REPORTS:
        (output / name).unlink(missing_ok=True)
    started = time.time()
    atomic_json(attempt / "started.json", {"started_at": started, "html_sha256": sha})
    rc = None
    try:
        bench = (batch / manifest["benchmark"]["path"]).resolve()
        rc = launch(["bash", "docker_test.sh", task["id"], "--exp-id", manifest["exp_id"], "--phase", "all"],
                    cwd=bench, log=attempt / "runner.log", batch=batch, run=run, lease=lease,
                    env={**os.environ, "DOCKER_IMAGE": manifest["image"]})
        archive_reports(output, attempt / "reports")
        if rc != 0 or (batch / "STOP").exists():
            raise ValueError(f"评测未完成（returncode={rc}）；可续跑，仅重新评测")
        if file_hash(target) != sha or file_hash(html) != sha:
            raise ValueError("评测期间提交文件已改变")
        metrics = evaluation_metrics(attempt / "reports", task["id"])
        receipt = {"html_sha256": sha, "returncode": rc, "reports": os.path.relpath(attempt / "reports", work),
                   "artifacts": {os.path.relpath(p, work): file_hash(p) for p in (attempt / "reports").iterdir()},
                   "metrics": metrics, "finished_at": time.time()}
        atomic_json(work / "evaluation.json", receipt)
    finally:
        archive_reports(output, attempt / "reports")
        atomic_json(attempt / "exit.json", {"returncode": rc, "started_at": started, "finished_at": time.time()})


def run_task(batch, manifest, task):
    work, run, _ = paths(batch, manifest, task)
    row = inspect_task(batch, manifest, task)
    if row["command"] is None:
        print(f"[{task['id']}] 跳过：{row['status']}", flush=True)
        return
    with task_lock(work / "task.lock") as lease:
        if active(run / "run.lock"):
            return
        if row["command"] in ("start", "resume"):
            command = [sys.executable, "-u", "-m", "game_agent_lite.cli", row["command"]]
            command += [str(work / "config.local.json"), "--run-dir", str(run)] if row["command"] == "start" else [str(run)]
            print(f"[{task['id']}] {row['command']} round 0；日志：{work / 'generation.log'}", flush=True)
            rc = launch(command, cwd=ROOT, log=work / "generation.log", batch=batch, run=run, lease=lease, generation=True)
            if rc != 0 or (batch / "STOP").exists():
                raise ValueError(f"生成未完成（returncode={rc}），保留阶段供续跑")
        delivered(run)  # A stopped CLI can return 0; it must never trigger evaluation.
        print(f"[{task['id']}] 官方 L1/L2 开始", flush=True)
        evaluate(batch, manifest, task, lease)


def stop(batch):
    manifest = load_manifest(batch)
    (batch / "STOP").touch()
    for task in manifest["tasks"]:
        _, run, _ = paths(batch, manifest, task)
        if (run / "state.json").exists() and active(run / "run.lock"):
            Store(run).request_stop()


def run_batch(batch, *, dry_run=False):
    manifest = load_manifest(batch)
    preflight(batch, manifest)
    if dry_run:
        return refresh(batch, manifest)
    with Store(batch).lock():
        preflight(batch, manifest, docker=True)
        (batch / "STOP").unlink(missing_ok=True)
        def interrupted(*_):
            (batch / "STOP").touch()
        handlers = {sig: signal.signal(sig, interrupted) for sig in (signal.SIGINT, signal.SIGTERM)}
        try:
            for task in manifest["tasks"]:
                if (batch / "STOP").exists():
                    break
                # Do not silently mix code or image revisions halfway through a long batch.
                preflight(batch, manifest, docker=True)
                work, run, _ = paths(batch, manifest, task)
                attempt = {"started_at": time.time()}
                try:
                    run_task(batch, manifest, task)
                    attempt["status"] = "returned"
                except Exception as exc:
                    attempt.update(status="failed", error=redact(str(exc)))
                    print(f"[{task['id']}] {attempt['error']}", flush=True)
                finally:
                    attempt["finished_at"] = time.time()
                    atomic_json(work / "last-attempt.json", attempt)
                    if not active(work / "task.lock") and not active(run / "run.lock"):
                        usage_reference(run)
                    refresh(batch, manifest, persist=True)
        finally:
            for sig, previous in handlers.items():
                signal.signal(sig, previous)
        return refresh(batch, manifest, persist=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prep = commands.add_parser("prepare", help="冻结任务与公开文档；不调用模型、Docker 或评测")
    selection = prep.add_mutually_exclusive_group(required=True)
    selection.add_argument("--all-tasks", action="store_true", help="运行全部 47 个公开任务")
    selection.add_argument("--tasks", nargs="+")
    prep.add_argument("--bench", type=Path, default=ROOT / "benchmarks/GameASG-Bench")
    prep.add_argument("--config", type=Path, default=ROOT / "examples/html-docker.json")
    prep.add_argument("--env-file", type=Path)
    prep.add_argument("--image", default=IMAGE)
    prep.add_argument("--exp-id")
    prep.add_argument("--dry-run", action="store_true", help="只校验，不写文件")
    for name in ("run", "status", "stop"):
        sub = commands.add_parser(name)
        sub.add_argument("batch", type=Path)
        if name == "run":
            sub.add_argument("--dry-run", action="store_true", help="只读检查，无模型、Docker 或评测")
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            batch, manifest = prepare(args.bench, args.config, all_tasks=args.all_tasks, tasks=args.tasks or (),
                exp_id=args.exp_id, env_file=args.env_file, image=args.image, dry_run=args.dry_run)
            print(json.dumps({"batch": str(batch), "tasks": [t["id"] for t in manifest["tasks"]],
                "rounds": 1, "model": manifest["model"], "reasoning": manifest["reasoning"], "player": manifest["player"],
                "protocol_sha256": manifest["protocol_sha256"], "dry_run": args.dry_run}, ensure_ascii=False, indent=2))
            return 0
        batch = args.batch.resolve()
        if args.command == "stop":
            stop(batch)
            print("已请求停止派发，并保存当前生成阶段；评测中断后可单独重试。")
            return 0
        result = run_batch(batch, dry_run=args.dry_run) if args.command == "run" else refresh(batch, load_manifest(batch))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if args.command == "status":
            return 0
        if args.dry_run:
            return int(any(r["status"] == "invalid" for r in result["tasks"]))
        return int(any(r["status"] != "evaluated" for r in result["tasks"]))
    except Exception as exc:
        print(redact(f"{type(exc).__name__}: {exc}"), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
