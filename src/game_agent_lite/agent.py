"""Role jobs plus cancellable process execution. No model SDK or background service."""
from collections import deque
import json
import os
from pathlib import Path
import queue
import signal
import subprocess
import threading
import tempfile

from .store import atomic_json, file_paths, read_json, redact
from .types import role_schema, validate
from .usage import capture

PACKAGE = Path(__file__).parent


def _result_event(line):
    """Last raw Grok CLI result. Invalid lines are not events."""
    try:
        event = json.loads(line)
    except ValueError:
        return None
    if isinstance(event, dict) and event.get("type") == "result":
        return event
    return None


def child_env() -> dict[str, str]:
    allowed = {"PATH", "HOME", "USER", "LOGNAME", "TMPDIR", "SHELL", "LANG", "LC_ALL",
               "LC_CTYPE", "DISPLAY", "XDG_RUNTIME_DIR", "XDG_DATA_HOME", "XDG_CACHE_HOME",
               "LIBGL_ALWAYS_SOFTWARE", "LP_NUM_THREADS", "DOCKER_HOST", "DOCKER_CONTEXT",
               "DOCKER_CONFIG", "SYSTEMROOT"}
    return {k: v for k, v in os.environ.items() if k in allowed}


def terminate(process):
    # The parent can exit while a grandchild still owns stdout or keeps running.
    # A dedicated process group belongs to this invocation even in that case.
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        pass
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()


class CommandRunner:
    def run(self, argv: list[str], cwd: Path, log: Path, *, env=None, tick=lambda: None,
            on_line=None):
        """Stream redacted output without buffering a whole rollout in memory.

        on_line sees each raw stdout line. process.log is the redacted copy and
        is not a parse source for role output.
        """
        log.parent.mkdir(parents=True, exist_ok=True)
        process = subprocess.Popen([str(a) for a in argv], cwd=cwd, env=env or child_env(),
                                   stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, text=True, errors="replace",
                                   start_new_session=True)
        lines: queue.Queue = queue.Queue(maxsize=256)
        finished = threading.Event()

        def pump():
            try:
                for line in process.stdout:
                    while not finished.is_set():
                        try:
                            lines.put(line, timeout=.1)
                            break
                        except queue.Full:
                            continue
            finally:
                process.stdout.close()

        reader = threading.Thread(target=pump, daemon=True)
        reader.start()
        try:
            with log.open("w") as stream:
                while process.poll() is None or reader.is_alive() or not lines.empty():
                    tick()
                    if process.poll() is not None:
                        terminate(process)
                    try:
                        line = lines.get(timeout=.05)
                    except queue.Empty:
                        pass
                    else:
                        if on_line is not None:
                            on_line(line)
                        stream.write(redact(line))
                        stream.flush()
            if process.returncode:
                raise RuntimeError(f"{Path(str(argv[0])).name} exited with {process.returncode}; see {log.name}")
        finally:
            finished.set()
            terminate(process)
            reader.join(timeout=3)


class Agent:
    def __init__(self, config: dict, runner=None):
        self.config = config
        self.runner = runner or CommandRunner()

    def call(self, role: str, job: Path, context: dict, *, project: Path | None = None,
             tick=lambda: None) -> dict:
        status = "failed"
        try:
            value = self._call(role, job, context, project=project, tick=tick)
            status = "completed"
            return value
        finally:
            # Separate host metadata; never changes prompts, return values or errors.
            capture(job, self.config, role, context, status)

    def _call(self, role: str, job: Path, context: dict, *, project: Path | None = None,
              tick=lambda: None) -> dict:
        io = job / "io"
        io.mkdir(parents=True, exist_ok=True)
        prefixes = (("A", "B") if role == "reviewer" else
                    ("evidence",) if role == "player" and context.get("phase") == "policy_analyze" else ())
        if prefixes:
            paths = sorted(f"{prefix}/{name}" for prefix in prefixes if (io / prefix).is_dir()
                           for name in file_paths(io / prefix))
            if paths:
                context = {**context, "evidence_paths": paths}
        if role == "builder":
            (io / "validation").mkdir(exist_ok=True)
            context = {**context, "validation_output": "/workspace/io/validation" if self.config["backend"] == "harbor"
                       else str((io / "validation").resolve())}
        atomic_json(io / "input.json", context)
        schema = role_schema(role, context)
        atomic_json(io / "schema.json", schema)
        remote = self.config["backend"] == "harbor"
        io_path = "/workspace/io" if remote else str(io.resolve())
        project_path = "/workspace/project" if remote else str(project.resolve()) if project else None
        prompt = (PACKAGE / "prompts" / f"{role}.md").read_text()
        if role == "player" and (context.get("benchmark") or {}).get("name") == "gamecraft":
            prompt += "\n\n" + (PACKAGE / "prompts/player_gamecraft.md").read_text()
        prompt += (f"\nRead {io_path}/input.json and {io_path}/schema.json. "
                   f"Your shared directory is {io_path}. "
                   "Return only the final JSON matching the schema.\n")
        if project is not None:
            prompt += f"Game source workspace: {project_path}.\n"
        if self.config["backend"] == "grok":
            self.run_grok(role, job, io, prompt, schema, tick)
        else:
            atomic_json(job / "request.json", {"role": role, "model": self.config["model"],
                                              "prompt": prompt, "usage": None})
            if remote:
                argv, env = self.harbor_command(job, prompt, project, role)
                try:
                    self.runner.run(argv, io, job / "process.log", env=env, tick=tick)
                finally:
                    self.cleanup_harbor(job / "harbor-owner.json")
                self.check_harbor(job / "jobs")
            else:
                argv = [self.config["codex"], "exec", "--json", "--ephemeral",
                        "--skip-git-repo-check", "--model", self.config["model"],
                        "-c", f'model_reasoning_effort="{self.config["reasoning"]}"',
                        "--sandbox", "workspace-write", "--cd", str(io.resolve()),
                        "--output-schema", str((io / "schema.json").resolve()),
                        "--output-last-message", str((io / "result.json").resolve())]
                if project is not None and role == "builder":
                    argv += ["--add-dir", str(project.resolve())]
                argv += [prompt]
                env = child_env()
                auth = Path(self.config["auth_file"]).expanduser()
                if not auth.is_file():
                    raise ValueError("Local Codex login file is missing; run codex login first")
                # Use the selected saved login without importing arbitrary user MCPs or API keys.
                with tempfile.TemporaryDirectory(prefix="game-agent-codex-") as home:
                    (Path(home) / "auth.json").symlink_to(auth.resolve())
                    env["CODEX_HOME"] = home
                    self.runner.run(argv, io, job / "process.log", env=env, tick=tick)
        value = validate(read_json(io / "result.json"), schema)
        # Usage not reported by a backend stays unknown, never an invented zero.
        # Grok stores the provider usage object here. The ledger remaps it.
        atomic_json(job / "result.json", {"role": role, "model": self.config["model"],
                                         "result": value, "usage": self.provider_usage(job)})
        return value

    @staticmethod
    def usage(log: Path):
        result = None
        with log.open(errors="replace") as stream:
            for line in stream:
                try:
                    event = json.loads(line)
                    if event.get("type") == "turn.completed":
                        result = event.get("usage")
                except (ValueError, AttributeError):
                    pass
        return result

    def provider_usage(self, job: Path):
        """Raw provider usage for the job receipt. Ledger shaping lives in usage.py."""
        if self.config["backend"] == "grok":
            event = read_json(job / "grok-result.json") if (job / "grok-result.json").is_file() else {}
            usage = event.get("usage") if isinstance(event, dict) else None
            return usage if isinstance(usage, dict) else None
        return self.usage(job / "process.log")

    def run_grok(self, role: str, job: Path, io: Path, prompt: str, schema: dict, tick):
        """One headless Grok call. The role result comes from the raw result event."""
        prompt += ("Write files with tools. The final answer is only the JSON object "
                   "matching the schema.\n")
        atomic_json(job / "request.json", {"role": role, "model": self.config["model"],
                                          "prompt": prompt, "usage": None})
        prompt_path = job / "prompt.txt"
        prompt_path.write_text(prompt)
        seen = {}

        def on_line(line):
            event = _result_event(line)
            if event is not None:
                seen["event"] = event

        with tempfile.TemporaryDirectory(prefix="game-agent-grok-") as home:
            argv, env = self.grok_command(Path(home), io, prompt_path, schema)
            self.runner.run(argv, io, job / "process.log", env=env, tick=tick, on_line=on_line)
        self.finish_grok(job, seen.get("event"))

    def grok_command(self, home: Path, io: Path, prompt_path: Path, schema: dict):
        """One fresh headless Grok process. Its home holds only this call's login.

        `--sandbox off` avoids bubblewrap user namespaces on hosts that block them.
        """
        home = home.resolve()
        env = child_env()
        env["GROK_HOME"] = str(home)
        env["GROK_MEMORY"] = "0"
        env["GROK_DISABLE_AUTOUPDATER"] = "1"
        key = os.environ.get("XAI_API_KEY")
        if key:
            env["XAI_API_KEY"] = key
        else:
            auth = Path(self.config["grok_auth_file"]).expanduser()
            if not auth.is_file():
                raise ValueError("Grok login is missing; run grok login or set XAI_API_KEY")
            (home / "auth.json").symlink_to(auth.resolve())
        argv = [self.config["grok"], "--prompt-file", str(prompt_path.resolve()),
                "--output-format", "streaming-messages-json",
                "--json-schema", json.dumps(schema, separators=(",", ":"), ensure_ascii=False),
                "--cwd", str(io.resolve()), "-m", self.config["model"],
                "--effort", self.config["reasoning"], "--sandbox", "off", "--yolo",
                "--verbatim", "--no-plan", "--no-subagents", "--disable-web-search",
                "--no-auto-update", "--leader-socket", str(home / "leader.sock"),
                "--tools", "read_file,grep,list_dir,search_replace,write,run_terminal_cmd"]
        return argv, env

    def finish_grok(self, job: Path, event):
        """Host-owned result.json. A file the model wrote is not the role result.

        event is the last raw CLI result, captured before log redaction.
        """
        if isinstance(event, dict):
            billing = {key: event[key] for key in (
                "type", "usage", "usage_is_incomplete", "cost_is_partial", "total_cost_usd")
                if key in event}
            atomic_json(job / "grok-result.json", billing)
        structured = event.get("structured_output") if isinstance(event, dict) else None
        if not isinstance(structured, dict):
            raise RuntimeError("Grok finished without structured output; see process.log")
        # Keep the model object intact. process.log is redacted and is not this source.
        atomic_json(job / "io" / "result.json", structured, private=True)

    def harbor_command(self, job: Path, prompt: str, project: Path | None, role: str):
        config = self.config
        auth = Path(config["auth_file"]).expanduser()
        if not auth.is_file():
            raise ValueError("Local Codex login file is missing; run codex login first")
        task = job / "task"
        (task / "environment").mkdir(parents=True, exist_ok=True)
        (task / "tests").mkdir(exist_ok=True)
        owner = str(job.resolve())
        atomic_json(job / "harbor-owner.json", {"label": owner, "active": True})
        # JSON is valid YAML; label only this role's main container for stop/crash cleanup.
        atomic_json(task / "environment" / "docker-compose.yaml", {
            "services": {"main": {"pull_policy": "never", "labels": {"game-agent-lite.job": owner}}}})
        (task / "instruction.md").write_text(prompt)
        (task / "task.toml").write_text(
            'schema_version = "1.4"\n[task]\nname = "local/game-agent-lite-role"\n'
            'version = "0.1.0"\n[agent]\nuser = "runner"\n'
            '[verifier]\ntimeout_sec = inf\n[environment]\n'
            f'docker_image = {json.dumps(config["harbor_image"])}\nbuild_timeout_sec = inf\n')
        # Role completion is checked again by the host, separate from game checks.
        (task / "tests" / "test.sh").write_text(
            '#!/bin/sh\nmkdir -p /logs/verifier\n'
            'if python3 -m json.tool /workspace/io/result.json >/dev/null; then\n'
            '  echo 1 > /logs/verifier/reward.txt\nelse\n'
            '  echo 0 > /logs/verifier/reward.txt\nfi\n')
        mounts = [{"type": "bind", "source": str((job / "io").resolve()), "target": "/workspace/io"}]
        if project is not None:
            mounts.append({"type": "bind", "source": str(project.resolve()),
                           "target": "/workspace/project", "read_only": role != "builder"})
        argv = [config["harbor"], "run", "-p", str(task.resolve()),
                "--agent-import-path", "preinstalled_codex:PreinstalledCodex",
                "-m", "openai/" + config["model"], "--ak", "version=" + config["codex_version"],
                "--ak", "reasoning_effort=" + config["reasoning"],
                # Harbor's --delete also runs `down --rmi all`, deleting the shared image tag.
                # --no-delete still tears down containers/networks; preserve the prebuilt image.
                "--agent-setup-timeout-multiplier", "inf", "--env", "docker", "--no-delete",
                "-n", "1", "-k", "1", "--max-retries", "0", "--jobs-dir",
                str((job / "jobs").resolve()), "--job-name", "role", "--mounts", json.dumps(mounts), "-y"]
        env = child_env()
        env.update({"CODEX_AUTH_JSON_PATH": str(auth.resolve()),
                    "PYTHONPATH": str(PACKAGE / "harbor_support"),
                    "LITELLM_LOCAL_MODEL_COST_MAP": "True"})
        return argv, env

    @staticmethod
    def cleanup_harbor(marker: Path):
        if not marker.exists():
            return
        owner = read_json(marker)
        if not owner.get("active"):
            return
        # A label unique to the role is required; never stop unrelated Docker work.
        result = subprocess.run(["docker", "ps", "-aq", "--filter", "label=game-agent-lite.job=" + owner["label"]],
            capture_output=True, text=True, check=True, timeout=15, env=child_env())
        ids = result.stdout.split()
        if ids:
            subprocess.run(["docker", "rm", "-f", *ids], capture_output=True, check=True, timeout=15, env=child_env())
        atomic_json(marker, {**owner, "active": False})

    @staticmethod
    def check_harbor(jobs: Path):
        trials = [(p, read_json(p)) for p in jobs.rglob("result.json")]
        trials = [(p, t) for p, t in trials if "agent_result" in t and "verifier_result" in t]
        if len(trials) != 1:
            raise RuntimeError("Harbor did not produce exactly one trial result")
        result_path, trial = trials[0]
        if trial.get("exception_info"):
            error = trial["exception_info"]
            message = str(error.get("exception_message") or "")
            # Docker errors put the actionable daemon message after a long compose argv.
            detail = message.rsplit("Error response from daemon:", 1)[-1] if "Error response from daemon:" in message else message
            detail = " ".join(redact(detail).split())[:1200]
            raise RuntimeError(f"Harbor role failed: {error.get('exception_type')}: {detail}; "
                               f"see {result_path.parent / 'exception.txt'}")
        rewards = (trial.get("verifier_result") or {}).get("rewards") or {}
        if rewards.get("reward") != 1:
            raise RuntimeError("Harbor role output verification failed")


class MockAgent:
    """Offline fixture producer, not a model or a claim of game quality."""
    def __init__(self, winners=None):
        self.winners = deque(winners or [])
        self.calls = []

    def call(self, role, job, context, *, project=None, tick=lambda: None):
        self.calls.append(role)
        job.mkdir(parents=True, exist_ok=True)
        tick()
        if role == "designer":
            return {"hypothesis": "mock change", "changes": ["mock"],
                    "acceptance": ["mock feedback"], "assets": []}
        if role == "builder":
            (project / "mock-artifact.txt").write_text(str(context["round"]) + context["stage"])
            return {"summary": "mock builder"}
        if role == "player":
            if context.get("phase") == "policy_author":
                io = job / "io"
                io.mkdir(parents=True, exist_ok=True)
                (io / "policy.py").write_text('''class Policy:
    def reset(self, seed, profile):
        self.steps = 0
        self.duration = 16 + seed % 3
    def act(self, state):
        self.steps += 1
        if self.steps > 2:
            return {"action": None, "done": True, "reason": "fixture coverage complete"}
        return {"action": {"keys": ["ArrowRight"], "duration_ms": self.duration,
                           "pointer": None}, "done": False, "reason": "move fixture"}
''')
                if context.get("policy_language") == "gdscript":
                    (io / "policy.py").unlink()
                    (io / "policy.gd").write_text('''extends RefCounted
var steps := 0
func reset(_seed: int, _profile: String) -> void:
    steps = 0
func act(_state: Dictionary) -> Dictionary:
    steps += 1
    if steps > 2:
        return {"action": null, "done": true, "reason": "fixture complete"}
    return {"action": {"keys": ["ArrowRight"], "duration_ms": 300, "pointer": null},
            "done": false, "reason": "fixed fixture movement"}
''')
                plan = {"summary": "Mock code policy; simulated evidence only", "episodes": [
                    {"profile": f"profile-{i}", "purpose": f"fixture scenario {i}",
                     **({"goal": "fixture goal", "scenario": None} if context.get("benchmark") else {})}
                    for i in range(context["trajectories_per_version"])]}
                if context.get("benchmark"):
                    plan["coverage"] = [{"id": f"R{i + 1}", "requirement": f"Fixture requirement {i + 1}",
                        "episodes": [i + 1], "expected": "Synthetic fixture feedback", "omission_reason": ""}
                        for i in range(context["trajectories_per_version"])]
                return plan
            if context.get("phase") == "policy_analyze":
                frames = sorted((job / "io/evidence").rglob("*.png"))
                frames += sorted((job / "io/evidence").rglob("trajectory.jsonl"))
                if context.get("benchmark"):
                    from .store import read_json
                    summary = read_json(job / "io/evidence/summary.json")
                    if "coverage" in summary["gamecraft"]:
                        atomic_json(job / "io/coverage-results.json", [{"id": r["id"], **{
                            side: {"status": "unverified", "reason": "Synthetic fixture, no real visual assessment",
                                   "evidence": []} for side in ("recording", "replay")}}
                            for r in summary["gamecraft"]["coverage"]])
                return {"experience": "simulated policy play", "problems": [], "suggestions": [],
                        "evidence": [p.relative_to(job / "io").as_posix() for p in frames]}
            requests = job / "io" / "requests"
            requests.mkdir(parents=True, exist_ok=True)
            atomic_json(requests / "move.json", {"id": "move", "method": "act", "action": {
                "keys": ["ArrowRight"], "duration_ms": 100, "pointer": None}})
            tick()
            return {"experience": "simulated", "problems": [], "suggestions": []}
        return {"winner": self.winners.popleft() if self.winners else "tie",
                "reason": "mock comparison", "evidence": ["A/summary.json", "B/summary.json"]}
