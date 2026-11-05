"""Manual entry points. No job starts at import or install time."""
import argparse
import copy
from datetime import datetime
import json
from pathlib import Path
import signal
import sys
import uuid

from .loop import Loop
from .store import Store, read_json, redact, safe_path
from .types import CONFIG_SCHEMA, DEFAULTS, validate
from .usage import refresh as refresh_usage, report_lines as usage_report_lines
from .paths import load_saved_config


def load_config(path: Path, *, prompt: str | None = None) -> dict:
    path = path.resolve()
    supplied = read_json(path)
    if prompt is not None:
        if not prompt.strip():
            raise ValueError("--prompt must contain a nonempty game request")
        if isinstance(supplied, dict):
            supplied = {**supplied, "brief": prompt}
    validate(supplied, CONFIG_SCHEMA)
    config = copy.deepcopy(DEFAULTS)
    config.update(supplied)
    config["assets"] = {**DEFAULTS["assets"], **supplied.get("assets", {})}
    config["player"] = {**DEFAULTS["player"], **supplied.get("player", {})}
    config["runtime"] = {**DEFAULTS["runtime"], **supplied.get("runtime", {})}
    for section in ("storage", "execution"):
        config[section] = {**DEFAULTS[section], **supplied.get(section, {})}
    if config.get("benchmark"):
        config["benchmark"]["path"] = str((path.parent / Path(config["benchmark"]["path"]).expanduser()).resolve())
        from .gamecraft_player import validate_profile
        validate_profile(config)
    if config["engine"] == "godot" and "entry" not in supplied:
        config["entry"] = "project.godot"
    for name in ("source", "auth_file", "grok_auth_file"):
        if config[name]:
            item = Path(config[name]).expanduser()
            config[name] = str((path.parent / item).resolve())
    env_file = Path(config["assets"]["env_file"]).expanduser()
    config["assets"]["env_file"] = str((path.parent / env_file).resolve())
    for name in ("codex", "harbor", "grok", "godot", "blender", "ffmpeg"):
        if "/" in config[name]:
            config[name] = str((path.parent / Path(config[name]).expanduser()).resolve())
    safe_path(path.parent, config["entry"])
    if config["source"] and not Path(config["source"]).is_dir():
        raise ValueError("Configured source directory does not exist")
    validate(config, CONFIG_SCHEMA)
    return config


def report(store: Store) -> Path:
    state = store.load()
    config = read_json(store.root / "config.json")
    mode = "模拟测试（没有调用模型或启动引擎）" if config["backend"] == "mock" else "正式后端"
    lines = ["# 游戏 Agent 运行报告", "", f"模式：{mode}", f"状态：{state['status']}",
             f"当前轮次：{state['round']}；阶段：{state['phase']}", "",
             "模型比较是乐趣的代理信号，不能替代真人试玩。初始基线不代表已经改进。", ""]
    if state["best"]:
        best = state["best"]
        lines += [f"最佳版本：`{best['version']}`", f"项目：[打开]({best['project']})", ""]
    if state["error"]:
        lines += [f"错误：{redact(state['error'])}", ""]
    lines += ["| 轮次 | 晋升 | 原因 |", "|---|---|---|"]
    for item in state["history"]:
        reason = item["reason"].replace("|", "\\|").replace("\n", " ")
        lines.append(f"| {item['round']} | {item['promoted']} | {reason} |")
    lines += ["", "各轮完整快照、角色调用、资产状态、输入反馈和比较结果保存在 rounds/。",
              "未知 token/费用保持 null；此版本不估算账单。"]
    lines += usage_report_lines(refresh_usage(store.root, config))
    destination = store.root / "report.md"
    destination.write_text("\n".join(lines) + "\n")
    return destination


def parser():
    root = argparse.ArgumentParser(prog="game-agent", description="Lightweight game evolution; commands run only when explicitly invoked")
    commands = root.add_subparsers(dest="command", required=True)
    start = commands.add_parser("start", help="Create and run a new experiment")
    start.add_argument("config", type=Path)
    start.add_argument("--prompt", help="Game request; replaces the config brief for this new run")
    start.add_argument("--run-dir", type=Path, help="Override the default runs/ directory (runs/gamecraft/ for GameCraft)")
    start.add_argument("--prepare-only", action="store_true", help="Save validated config and seed without invoking any backend")
    resume = commands.add_parser("resume", help="Resume the saved stage")
    resume.add_argument("run_dir", type=Path)
    resume.add_argument("--resolve-job", action="append", default=[], metavar="ASSET_ID=JOB_ID",
                        help="Attach an externally verified 3D job id to an uncertain submission")
    resume.add_argument("--resolve-file", action="append", default=[], metavar="ASSET_ID=FILE",
                        help="Attach an externally recovered image/GLB to an uncertain submission")
    for command in ("status", "stop", "report"):
        commands.add_parser(command).add_argument("run_dir", type=Path)
    deliver = commands.add_parser("export-game", help="Export a validated game without experiment logs or credentials")
    deliver.add_argument("run_dir", type=Path)
    deliver.add_argument("--output", type=Path, required=True)
    deliver.add_argument("--round", type=int, dest="round_number")
    export = commands.add_parser("export-gamecraft", help="Export saved Godot policy inputs; no rollout or evaluation")
    export.add_argument("run_dir", type=Path)
    export.add_argument("--output", type=Path, required=True, help="New submission directory outside the run")
    export.add_argument("--round", type=int, dest="round_number", help="Export this round's candidate; default: best version")
    export.add_argument("--prepared-dir", type=Path,
                        help="Read existing prepare-gamecraft evidence without playing again; default round comes from preparation.json")
    export.add_argument("--episode", type=int, action="append", dest="episodes",
                        help="One-based episode number, repeatable; default: all (maximum 10)")
    export.add_argument("--clip-long", action="store_true", help="Explicitly export only the first 20 seconds of long episodes")
    export.add_argument("--scenario", action="append", default=[], metavar="EPISODE=NAME",
                        help="Existing game scenario matching an episode's initial state; does not create scenarios")
    prepare = commands.add_parser("prepare-gamecraft", help="Record <=20s demos with a saved Godot Policy; no model or judge calls")
    prepare.add_argument("run_dir", type=Path)
    prepare.add_argument("--output", type=Path, required=True, help="New directory outside the run for evidence and submission/")
    prepare.add_argument("--round", type=int, dest="round_number")
    prepare.add_argument("--episode", type=int, action="append", dest="episodes")
    prepare.add_argument("--scenario", action="append", default=[], metavar="EPISODE=NAME")
    prepare.add_argument("--min-key-release-ms", type=int, default=0,
                         help="Optional 0..2000ms minimum duration for key-release actions during NEW benchmark recording; never retime old JSON")
    prepare.add_argument("--dry-run", action="store_true", help="Validate and print the plan; no files, engines or Docker")
    return root


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "export-game":
            from .delivery import export_game
            path = export_game(args.run_dir, args.output, args.round_number)
            print(f"Game export: {path}")
            return 0
        if args.command == "prepare-gamecraft":
            from .gamecraft_prepare import prepare_gamecraft
            def interrupted(*_):
                raise KeyboardInterrupt()
            previous = signal.signal(signal.SIGTERM, interrupted)
            try:
                value = prepare_gamecraft(args.run_dir, args.output, round_number=args.round_number,
                    episodes=args.episodes, scenarios=args.scenario, min_key_release_ms=args.min_key_release_ms,
                    dry_run=args.dry_run)
            finally:
                signal.signal(signal.SIGTERM, previous)
            print(json.dumps(value, ensure_ascii=False, indent=2))
            return 0
        if args.command == "export-gamecraft":
            from .gamecraft import export_gamecraft
            destination = export_gamecraft(args.run_dir, args.output, round_number=args.round_number,
                episodes=args.episodes, clip_long=args.clip_long, scenarios=args.scenario,
                prepared_dir=args.prepared_dir)
            manifest = read_json(destination / "manifest.json")
            print(f"GameCraft submission: {destination / 'game'}")
            print(f"Demos: {len(manifest['demos'])}; replay_verified=false; no evaluator was run")
            if any(demo["clipped"] for demo in manifest["demos"]):
                print("Export window ends at 20 seconds; later inputs and outcomes are not included.")
            if any(demo.get("source_status") == "truncated" for demo in manifest["demos"]):
                print("Benchmark time-limit stops retained as truncated; Policy completion and victory are not established.")
            print("Match the game's fresh-launch/scenario state to the recorded reset state before official replay.")
            return 0
        if args.command == "start":
            config = load_config(args.config, prompt=args.prompt)
            name = datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8]
            root = Path.cwd() / "runs"
            if (config.get("benchmark") or {}).get("name") == "gamecraft":
                root /= "gamecraft"
            store = Store(args.run_dir or root / name)
            if config["source"] and store.root.is_relative_to(Path(config["source"])):
                raise ValueError("Run directory must be outside the source project")
            store.create(config)
            print(f"Run directory: {store.root}")
            if args.prepare_only:
                print("Prepared only; no model, asset service, or engine was invoked.")
                return 0
        else:
            store = Store(args.run_dir)
            if args.command == "status":
                print(json.dumps(redact(store.load()), ensure_ascii=False, indent=2))
                return 0
            if args.command == "stop":
                store.load()
                store.request_stop()
                print("Stop requested. The controller will save its current stage.")
                return 0
            if args.command == "report":
                print(report(store))
                return 0
            config = load_saved_config(store.root)
        previous = signal.signal(signal.SIGTERM, lambda *_: store.request_stop())
        try:
            state = Loop(store, config).run(resume=args.command == "resume",
                resolve_jobs=getattr(args, "resolve_job", []), resolve_files=getattr(args, "resolve_file", []))
        finally:
            signal.signal(signal.SIGTERM, previous)
        print(json.dumps(redact({"status": state["status"], "round": state["round"],
                                "phase": state["phase"], "error": state["error"]}), ensure_ascii=False))
        report(store)
        return 0 if state["status"] in ("completed", "stopped") else 1
    except (Exception, KeyboardInterrupt) as exc:
        print(redact(f"{type(exc).__name__}: {exc}"), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
