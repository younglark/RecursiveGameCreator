"""Source-assisted Player: author once, execute episodes, then inspect evidence.

No model runs in the action loop. The host owns all game inputs and evidence.
"""
import ast
from copy import deepcopy
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
import json
import os
from pathlib import Path
import re
import select
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import warnings

from jsonschema import ValidationError

from .agent import Agent, CommandRunner, child_env, terminate
from .store import copy_file
from .store import (Stopped, artifact, atomic_json, file_hash, read_json, redact, safe_path,
                    inventory, snapshot, version, verify_artifact)
from .types import (ACTION_SCHEMA, DEFAULTS, PLAYER_SCHEMA, POLICY_PLAN_SCHEMA,
                    POLICY_REVIEW_SCHEMA, obj, role_schema, validate)

DECISION_SCHEMA = obj({"action": {"anyOf": [{"type": "null"}, ACTION_SCHEMA]},
                       "done": {"type": "boolean"}, "reason": {"type": "string", "minLength": 1}})


class PolicyWorker:
    """Small JSON-lines RPC. Docker exposes only policy and worker files."""
    def __init__(self, config, policy, output, stop=lambda: None):
        self.config, self.output, self.check_stop = config, output, stop
        self.timeout = config.get("player", {}).get("policy_call_timeout_seconds", 30)
        self.container = None
        self.buffer = b""
        worker = Path(__file__).with_name("policy_worker.py")
        output.mkdir(parents=True, exist_ok=True)
        if config["backend"] == "mock":
            argv = [sys.executable, "-I", "-S", "-u", str(worker), str(policy)]
        else:
            self.container = "rsi-policy-" + uuid.uuid4().hex
            atomic_json(output / "worker.json", {"container": self.container})
            argv = ["docker", "run", "--rm", "--pull=never", "--init", "-i",
                    "--name", self.container, "--network", "none", "--read-only",
                    "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                    "--user", "65534:65534", "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m",
                    "--mount", f"type=bind,source={policy.resolve()},target=/policy/policy.py,readonly",
                    "--mount", f"type=bind,source={worker.resolve()},target=/policy/worker.py,readonly",
                    "--workdir", "/tmp", "--entrypoint", "python3", config["harbor_image"],
                    "-I", "-S", "-u", "/policy/worker.py", "/policy/policy.py"]
        # Child stderr is streamed and redacted by the parent along with stdout.
        self.process = subprocess.Popen(argv, cwd=output, env=child_env(), stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        self.stderr_open = True

    @staticmethod
    def cleanup(marker):
        if marker.exists():
            name = read_json(marker).get("container", "")
            if not re.fullmatch(r"rsi-policy-[0-9a-f]{32}", name):
                raise ValueError("Invalid policy container ownership marker")
            result = subprocess.run(["docker", "rm", "-f", name], capture_output=True,
                                    text=True, timeout=15, env=child_env())
            if result.returncode and "No such container" not in result.stderr:
                raise RuntimeError("Cannot clean up policy container: " + redact(result.stderr))
            marker.unlink()

    def request(self, method, **params):
        self.check_stop()
        payload = json.dumps({"method": method, **params}, allow_nan=False).encode() + b"\n"
        # Nonblocking writes keep cancellation/timeout working even with a stuck policy.
        os.set_blocking(self.process.stdin.fileno(), False)
        until = time.monotonic() + self.timeout
        pending = memoryview(payload)
        with (self.output / "worker.log").open("a") as log:
            while True:
                self.check_stop()
                if time.monotonic() >= until:
                    raise TimeoutError(f"Policy {method} exceeded per-call health timeout ({self.timeout}s)")
                reads = [self.process.stdout]
                if self.stderr_open:
                    reads.append(self.process.stderr)
                ready, writable, _ = select.select(reads, [self.process.stdin] if pending else [], [], .05)
                if writable:
                    pending = pending[os.write(self.process.stdin.fileno(), pending):]
                for stream in ready:
                    data = os.read(stream.fileno(), 65536)
                    if stream is self.process.stderr:
                        if not data:
                            self.stderr_open = False
                        else:
                            log.write(redact(data.decode(errors="replace")))
                            log.flush()
                    else:
                        if not data:
                            raise RuntimeError("Policy worker exited; see worker.log")
                        self.buffer += data
                        if len(self.buffer) > 1024 * 1024:
                            raise ValueError("Policy response exceeds transport size limit")
                if b"\n" in self.buffer:
                    line, self.buffer = self.buffer.split(b"\n", 1)
                    reply = json.loads(line)
                    if reply.get("error"):
                        raise ValueError("Policy failed: " + redact(str(reply["error"])))
                    return reply["result"]

    def close(self):
        try:
            terminate(self.process)
        finally:
            for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
                stream.close()
            if self.container:
                self.cleanup(self.output / "worker.json")


def _state(game):
    state = game.observe_state()
    if not isinstance(state, dict):
        raise ValueError("Game state must be an object")
    # A detached, finite JSON value; never pass an engine object into a policy.
    return json.loads(json.dumps(state, allow_nan=False))


def _extract_frames(config, video, folder, root, stop):
    if config["backend"] != "mock" and config.get("runtime", {}).get("mode") == "docker":
        from .runtime import extract_frames
        return extract_frames(config, video, folder, root, stop)
    from .media import extract_frames
    return extract_frames(video, folder, root, stop, ffmpeg=config["ffmpeg"])



def _preparation(record, root):
    """Optional, hashed handoff for new sessions; old receipts remain readable."""
    if "preparation" not in record:
        return {}
    item = record["preparation"]
    if not verify_artifact(item, root):
        raise ValueError("Player preparation changed")
    data = read_json(safe_path(root, item["path"]))
    if data["version"] != record["version"]:
        raise ValueError("Player preparation version mismatch")
    if data.get("preflight"):
        item = data["preflight"]
        if not verify_artifact(item, root):
            raise ValueError("Player preflight changed")
        preflight = read_json(safe_path(root, item["path"]))
        if (preflight["version"] != record["version"] or preflight["status"] != "passed"
                or not verify_artifact(preflight["image"], root)):
            raise ValueError("Player preflight evidence changed")
    if data.get("parent"):
        if not all(verify_artifact(data["parent"][key], root) for key in ("policy", "plan", "feedback")):
            raise ValueError("Inherited policy reference changed")
    return data


class PolicySession:
    def __init__(self, config, project, job, expected_version, game_factory, stop=lambda: None,
                 *, context=None, parent=None, execution=None):
        self.benchmark = bool(config.get("benchmark")) and execution is None
        if self.benchmark:
            from .gamecraft_player import configure
            config, execution = configure(config)
        self.config, self.project, self.job = config, project.resolve(), job.resolve()
        self.context = {"game": {k: config[k] for k in ("engine", "brief", "entry")},
                        **deepcopy(context or {})}
        self.parent = parent
        self.native = config["engine"] == "godot"
        self.policy_filename = "policy.gd" if self.native else "policy.py"
        # Ordinary play has no deadline; benchmark profiles/preparation may supply one.
        self.execution = deepcopy(execution or {})
        self.native_recording = self.execution.get("evidence_source") == "player_recording"
        self.version, self.game_factory = expected_version, game_factory
        self._external_stop, self._cancel = stop, threading.Event()
        self.options = {**DEFAULTS["player"], **config.get("player", {})}
        if self.native:
            self.options["mode"] = "policy"
        # Concurrency is scheduling, not a change to the frozen policy/test plan.
        self.settings = {"seed": config["seed"], "max_actions": config["max_actions"],
                         "engine": config["engine"],
                         **{k: v for k, v in self.options.items() if k != "parallel_episodes"}}
        if self.native:
            self.settings.update(policy_backend="godot", runner_sha256=file_hash(
                Path(__file__).parent / "bridges/godot_policy_runner.gd"), bridge_sha256=file_hash(
                Path(__file__).parent / "bridges/game_agent_bridge.gd"))
        if self.execution:
            self.settings["execution"] = self.execution
        if self.benchmark:
            self.settings["benchmark"] = deepcopy(config["benchmark"])
        if config["backend"] != "mock" and config.get("runtime", {}).get("mode") == "docker":
            from .runtime import runtime_identity
            identity = runtime_identity(config)
            self.settings["runtime"] = identity
            self.config = deepcopy(config)
            self.config["runtime"]["image"] = identity["image_id"]
            self.config["_runtime_identity"] = identity
        job.mkdir(parents=True, exist_ok=True)

    def check_stop(self):
        if self._cancel.is_set():
            raise Stopped("Parallel Player batch interrupted")
        self._external_stop()

    def assert_source(self):
        if version(self.project) != self.version:
            raise ValueError("Game source changed during policy play")

    def preflight(self):
        """Probe a clean baseline before spending a model call on Policy authoring."""
        root = self.job / "preflight"
        receipt = root / "result.json"
        if receipt.exists():
            result = read_json(receipt)
            if result["version"] != self.version or result["settings"] != self.settings:
                raise ValueError("Preflight version/settings changed; use a new Player session")
            if result["status"] == "interrupted":
                archived = self.job / ("preflight-interrupted-" + uuid.uuid4().hex[:8])
                root.rename(archived)
                if result.get("image"):
                    result["image"] = artifact(archived / "initial.png", self.job)
                atomic_json(archived / "result.json", result)
            elif result["status"] != "passed":
                raise RuntimeError("Player preflight failed; inspect preflight/result.json and use a new session after fixing: "
                                   + result.get("error", "interrupted probe"))
            else:
                if not verify_artifact(result["image"], self.job):
                    raise ValueError("Preflight screenshot changed")
                return result
        self.assert_source()
        root.mkdir(parents=True, exist_ok=True)
        source = root / "source"
        snapshot(self.project, source)
        game = None
        result = {"version": self.version, "settings": self.settings, "status": "failed",
                  "stage": "start", "image": None, "formal_episodes_started": False,
                  "scope": "baseline startup/observation only; scenarios and gameplay are not verified"}
        interrupted = None
        started = time.monotonic()
        try:
            self.check_stop()
            game = self.game_factory(self.config, source, root / "engine", self.check_stop)
            if self.native:
                # Match game startup, but use diagnostic captures: no formal episode
                # has begun, so native recording metadata does not exist yet.
                game.configure_policy(None, fresh_launch=self.execution.get("fresh_launch", False),
                                      viewport=self.execution.get("viewport"),
                                      call_timeout=self.options["policy_call_timeout_seconds"])
            game.start()
            if not self.execution.get("fresh_launch"):
                result["stage"] = "reset"
                game.reset(self.config["seed"])
            result["stage"] = "observe"
            result["initial_state"] = _state(game)
            result["input_capabilities"] = game.input_info()
            observation = game.observe()
            image = root / "initial.png"
            copy_file(observation["image"], image)
            result["image"] = artifact(image, self.job)
            result.update(status="passed", stage="complete")
        except (Stopped, KeyboardInterrupt) as exc:
            interrupted = exc
            result.update(status="interrupted", error="stop requested")
        except Exception as exc:
            result["error"] = redact(f"{type(exc).__name__}: {exc}")
            try:
                image = root / "initial.png"
                if game is not None and game.capture_diagnostic(image):
                    result["image"] = artifact(image, self.job)
            except Exception:
                pass  # Original startup failure remains the primary diagnostic.
        finally:
            for cleanup in ((game.release_inputs, game.stop) if game is not None else ()):
                try:
                    cleanup()
                except Exception as exc:
                    result["cleanup_error"] = redact(f"{type(exc).__name__}: {exc}")
                    if result["status"] == "passed":
                        result.update(status="failed", error=result["cleanup_error"])
            result["elapsed_seconds"] = time.monotonic() - started
            shutil.rmtree(source, ignore_errors=True)
            atomic_json(receipt, redact(result))
        self.assert_source()
        if interrupted:
            raise interrupted
        if result["status"] != "passed":
            raise RuntimeError("Player preflight failed before Policy authoring; see preflight/result.json: "
                               + result.get("error", "probe failed"))
        return result

    def prepare(self):
        """Snapshot only the selected baseline's policy, plan and compact feedback."""
        path = self.job / "preparation.json"
        if not path.exists():
            data = {"version": self.version, "context": self.context, "parent": None}
            if (self.job / "preflight/result.json").is_file():
                data["preflight"] = artifact(self.job / "preflight/result.json", self.job)
            if self.parent:
                source = Path(self.parent["path"])
                report = read_json(source)
                # A legacy GUI report has no policy to inherit.
                if report.get("format") == "policy-v1":
                    if not valid_policy_play(report, source.parent, self.parent["version"],
                                             allow_mock=self.config["backend"] == "mock"):
                        raise ValueError("Baseline policy evidence is invalid")
                    inherited = self.job / "inherited"
                    inherited.mkdir(exist_ok=True)
                    parent = {"version": report["version"], "settings": report["settings"],
                              "source_report": self.parent["reference"],
                              "source_report_sha256": file_hash(source),
                              "context": _preparation(report, source.parent).get("context")}
                    for key, name in (("policy", Path(report["policy"]["path"]).name), ("plan", "plan.json")):
                        target = inherited / name
                        target.unlink(missing_ok=True)  # An interrupted preparation may have left a read-only copy.
                        copy_file(safe_path(source.parent, report[key]["path"]), target)
                        target.chmod(0o444)
                        parent[key] = artifact(target, self.job)
                    outcomes = [read_json(safe_path(source.parent, e["path"])) for e in report["episodes"]]
                    atomic_json(inherited / "feedback.json", {
                        "summary": report["summary"], "episodes": [
                            {k: e[k] for k in ("profile", "purpose", "seed", "status", "reason", "steps")}
                            for e in outcomes]})
                    parent["feedback"] = artifact(inherited / "feedback.json", self.job)
                    data["parent"] = parent
            atomic_json(path, data)
        item = artifact(path, self.job)
        return item, _preparation({"version": self.version, "preparation": item}, self.job)

    def freeze_policy(self, policy, plan, preparation, parent, *, author_called):
        validate(plan, POLICY_PLAN_SCHEMA)
        profiles = [e["profile"] for e in plan["episodes"]]
        if len(profiles) != self.options["trajectories_per_version"] or len(set(profiles)) != len(profiles):
            raise ValueError("Policy episode profiles must be distinct and match the configured count")
        if self.benchmark:
            if author_called or "coverage" in plan:
                validate(plan, role_schema("player", {"phase": "policy_author", "benchmark": True,
                         "trajectories_per_version": len(profiles)}))
            if "coverage" in plan:
                from .gamecraft_player import coverage_plan_warnings
                for message in coverage_plan_warnings(plan):
                    warnings.warn(message, UserWarning, stacklevel=2)
        if policy.is_symlink() or not policy.is_file():
            raise ValueError(f"Player must write a regular {self.policy_filename} in its shared directory")
        if self.native:
            if policy.suffix != ".gd":
                raise ValueError("Godot requires policy.gd; legacy Python policies must be adapted")
            # Godot parses and checks the script on the isolated runtime copy before play.
            if not policy.read_text().strip():
                raise ValueError("Empty GDScript Policy")
        else:
            ast.parse(policy.read_text())
        frozen = self.job / self.policy_filename
        frozen.unlink(missing_ok=True)
        copy_file(policy, frozen)
        frozen.chmod(0o444)
        atomic_json(self.job / "plan.json", plan)
        result = {"version": self.version, "settings": self.settings,
                  "policy": artifact(frozen, self.job), "plan": artifact(self.job / "plan.json", self.job),
                  "preparation": preparation, "inheritance": {
                      "mode": ("reused" if file_hash(frozen) == parent["policy"]["sha256"] else "adapted")
                              if parent else "new",
                      "author_called": author_called,
                      "source_version": parent["version"] if parent else None,
                      "source_report": parent["source_report"] if parent else None,
                      "source_policy_sha256": parent["policy"]["sha256"] if parent else None,
                      "summary": plan["summary"]}}
        atomic_json(self.job / "policy.json", result)
        return result

    def author(self, agent):
        saved = self.job / "policy.json"
        if saved.exists():
            result = read_json(saved)
            if (result["version"] != self.version or result.get("settings") != self.settings
                    or not verify_artifact(result["policy"], self.job)
                    or not verify_artifact(result["plan"], self.job)):
                raise ValueError("Saved Player policy/plan changed; refusing silent regeneration")
            _preparation(result, self.job)
            return result
        preflight = self.preflight()
        preparation, prepared = self.prepare()
        parent = prepared["parent"]
        if (parent and parent["version"] == self.version and parent["settings"] == self.settings
                and parent["context"] is not None and parent["context"] == prepared["context"]
                and (not self.benchmark or "coverage" in read_json(safe_path(self.job, parent["plan"]["path"])))):
            # Reuse code, not episodes: a new job still executes all configured games.
            return self.freeze_policy(safe_path(self.job, parent["policy"]["path"]),
                read_json(safe_path(self.job, parent["plan"]["path"])), preparation, parent, author_called=False)
        job = self.job / ("author-" + uuid.uuid4().hex[:10])
        # A filtered disposable copy keeps sealed games out of local Codex's write scope.
        source = job / "source"
        snapshot(self.project, source)
        for path in source.rglob("*"):
            if path.is_file():
                path.chmod(0o444)
        probe = job / "io/preflight"
        probe.mkdir(parents=True)
        copy_file(safe_path(self.job, preflight["image"]["path"]), probe / "initial.png")
        atomic_json(probe / "result.json", {k: v for k, v in preflight.items()
                    if k not in ("settings", "image")})
        prior = None
        if parent:
            folder = job / "io/prior_policy"
            folder.mkdir(parents=True)
            prior = {}
            for key, name in (("policy", Path(parent["policy"]["path"]).name), ("plan", "plan.json"), ("feedback", "feedback.json")):
                copy_file(safe_path(self.job, parent[key]["path"]), folder / name)
                (folder / name).chmod(0o444)
                prior[key] = "prior_policy/" + name
            prior_digest = version(folder)
        context = {"phase": "policy_author", "engine": self.config["engine"],
            "handoff": prepared["context"], "prior_policy": prior,
            "entry": self.config["entry"], "trajectories_per_version": self.options["trajectories_per_version"],
            "seed": self.config["seed"], "max_actions": self.config["max_actions"],
            "input_capabilities": preflight["input_capabilities"], "policy_file": self.policy_filename,
            "preflight": {"result": "preflight/result.json", "image": "preflight/initial.png"},
            "policy_language": "gdscript" if self.native else "python",
            "contract": "Policy.reset(seed, profile); Policy.act(state) -> {action, done, reason}",
            "action_schema": ACTION_SCHEMA,
            "source_access": "read_only; inspect agent_observe/GameAgentBridge to learn state fields"}
        if self.benchmark:
            from .gamecraft_player import context as benchmark_context
            context.update(benchmark=benchmark_context(self.config), execution=self.execution)
        plan = validate(agent.call("player", job, context, project=source, tick=self.check_stop),
                        role_schema("player", context))
        if version(source) != self.version:
            raise ValueError("Player modified its source copy")
        if prior and version(folder) != prior_digest:
            raise ValueError("Player modified its inherited reference copy")
        # Validate the literal path before resolving it so symlinks cannot become regular files.
        return self.freeze_policy(job / "io" / self.policy_filename, plan, preparation, parent, author_called=True)

    def _run_native(self, game, specification, policy, result):
        game.native_policy_start(seed=result["seed"], profile=specification["profile"],
            version=self.version, policy_sha256=policy["sha256"], max_actions=self.config["max_actions"],
            max_seconds=self.execution.get("max_seconds"),
            min_key_release_ms=self.execution.get("min_key_release_ms", 0)
                if self.execution.get("purpose") == "gamecraft_demo" else 0)
        while True:
            self.check_stop()
            progress = game.native_policy_status()
            if progress.get("status") and progress["status"] != "running":
                result.update({key: progress[key] for key in ("status", "reason", "steps", "elapsed_seconds")})
                return
            time.sleep(.1)

    def episode(self, index, specification, policy):
        directory = self.job / "episodes" / f"{index + 1:04d}"
        receipt = directory / "completed.json"
        if receipt.exists():
            item = read_json(receipt)
            if not verify_artifact(item, self.job):
                raise ValueError("Completed policy episode receipt changed")
            result = read_json(safe_path(self.job, item["path"]))
            if (not _episode_intact(result, self.job, self.version, policy["sha256"])
                    or result.get("runtime") != self.settings.get("runtime")):
                raise ValueError("Completed policy episode evidence changed")
            return item
        recorded = directory / "recorded.json"
        if recorded.exists():
            saved = read_json(recorded)
            if not verify_artifact(saved, self.job):
                raise ValueError("Recorded policy episode changed")
            result = read_json(safe_path(self.job, saved["path"]))
            if (not _episode_intact(result, self.job, self.version, policy["sha256"])
                    or result.get("runtime") != self.settings.get("runtime")):
                raise ValueError("Recorded policy episode evidence changed")
            return self.finish_episode(directory, safe_path(self.job, saved["path"]).parent, result)
        attempt = directory / ("attempt-" + uuid.uuid4().hex[:10])
        attempt.mkdir(parents=True)
        game = self.game_factory(self.config, self.project, attempt / "engine", self.check_stop)
        result = {"version": self.version, "policy_sha256": policy["sha256"], "index": index,
            **specification, "seed": self.config["seed"] + index, "steps": 0,
            "status": "failed", "reason": "not started", "frames": [], "video": None}
        if "runtime" in self.settings:
            result["runtime"] = self.settings["runtime"]
        trajectory = attempt / "trajectory.jsonl"
        worker = None
        started = time.monotonic()
        interrupted = None

        def append(stream, event):
            row = {"version": self.version, "policy_sha256": policy["sha256"],
                   "time_seconds": max(time.monotonic() - started, result.get("elapsed_seconds", 0)), **event}
            stream.write(json.dumps(redact(row), ensure_ascii=False, allow_nan=False) + "\n")
            stream.flush()

        def capture():
            observation = game.observe()
            result["frames"].append({"clock": "episode", "time_seconds": time.monotonic() - started,
                                      "image": artifact(observation["image"], self.job)})

        with trajectory.open("w") as trace:
            try:
                self.check_stop()
                if self.native:
                    scenario = self.execution.get("scenarios", {}).get(specification["profile"],
                        specification.get("scenario") if self.benchmark else None)
                    result.update(policy_backend="godot", scenario=scenario,
                                  fresh_launch=self.execution.get("fresh_launch", False))
                    capture_options = {"evidence_source": "player_recording"} if self.native_recording else {}
                    game.configure_policy((self.job / policy["path"]).read_text(), scenario=scenario,
                        fresh_launch=result["fresh_launch"], viewport=self.execution.get("viewport"),
                        call_timeout=self.options["policy_call_timeout_seconds"], **capture_options)
                else:
                    worker = PolicyWorker(self.config, self.job / policy["path"], attempt / "worker", self.check_stop)
                    worker.request("reset", seed=result["seed"], profile=specification["profile"])
                game.start()
                if not self.execution.get("fresh_launch"):
                    game.reset(result["seed"])
                started = time.monotonic()
                if not self.native_recording:
                    capture()
                if self.native:
                    self._run_native(game, specification, policy, result)
                else:
                    before = _state(game)
                    append(trace, {"type": "initial", "state": before})
                while not self.native:
                    self.check_stop()
                    decision = validate(worker.request("act", state=before), DECISION_SCHEMA)
                    if decision["done"]:
                        if decision["action"] is not None:
                            raise ValueError("Finished policy must not also request input")
                        result.update(status="completed", reason=decision["reason"])
                        break
                    limit = self.config["max_actions"]
                    if limit is not None and result["steps"] >= limit:
                        result.update(status="truncated", reason="configured action limit reached")
                        break
                    action = game.prepare_action(decision["action"])
                    action_start = time.monotonic() - started
                    # Record intent before input, including if the engine crashes during it.
                    append(trace, {"type": "input", "index": result["steps"],
                        "action": action, "before": before, "reason": decision["reason"]})
                    game.policy_act(action)
                    after = _state(game)
                    append(trace, {"type": "feedback", "index": result["steps"],
                        "action": action, "before": before, "after": after,
                        "action_start_seconds": action_start})
                    result["steps"] += 1
                    before = after
                if not self.native_recording:
                    capture()
            except (Stopped, KeyboardInterrupt) as exc:
                interrupted = exc
                result.update(status="interrupted", reason="stop requested")
            except Exception as exc:
                result.update(status="failed", reason=redact(f"{type(exc).__name__}: {exc}"))
            finally:
                result.setdefault("elapsed_seconds", time.monotonic() - started)
                if self.native and game.native_policy_trace().is_file():
                    # Keep the engine's actual input clock; host polling never invents actions.
                    try:
                        with game.native_policy_trace().open() as recorded_trace:
                            for line in recorded_trace:
                                row = json.loads(line)
                                if row["type"] != "end":
                                    trace.write(json.dumps(redact(row), ensure_ascii=False, allow_nan=False) + "\n")
                    except Exception as exc:
                        # A killed engine may leave a partial line. Preserve its raw file
                        # and still release inputs/containers instead of leaking them.
                        result.update(status="interrupted" if interrupted else "failed",
                                      reason=result["reason"] + redact(f"; invalid native trace: {exc}"))
                for cleanup in (game.release_inputs, worker.close if worker else lambda: None):
                    try:
                        cleanup()
                    except Exception as exc:
                        result.update(status="failed" if not interrupted else "interrupted",
                                      reason=result["reason"] + redact(f"; cleanup failed: {exc}"))
                try:
                    video = game.stop()
                    if video:
                        if self.native_recording:
                            result["frames"] = [{**f, "image": artifact(
                                safe_path(game.output, f["image"]["path"]), self.job)} for f in video["raw_frames"]]
                            result["capture_duration_seconds"] = video["duration_seconds"]
                            for key in ("capture_receipt", "capture_timing"):
                                video[key] = artifact(safe_path(game.output, video[key]["path"]), self.job)
                        result["video"] = {**{k: v for k, v in video.items() if k not in ("path", "raw_frames")},
                                           "artifact": artifact(video["path"], self.job)}
                    else:
                        raise ValueError("No recorded video")
                except Exception as exc:
                    result.update(status="failed" if not interrupted else "interrupted",
                                  reason=result["reason"] + redact(f"; recording failed: {exc}"))
                append(trace, {"type": "end", "status": result["status"], "reason": result["reason"],
                               **({"time_seconds": result["elapsed_seconds"]} if self.native_recording else {})})
                os.fsync(trace.fileno())
        result["trajectory"] = artifact(trajectory, self.job)
        if self.native and hasattr(game, "native_input_events"):
            result["input_events_required"] = True
            events = game.native_input_events()
            if events.is_file():
                saved_events = attempt / "input_events.jsonl"
                copy_file(events, saved_events)
                result["input_events"] = artifact(saved_events, self.job)
            else:
                result.update(status="interrupted" if interrupted else "failed",
                              reason=result["reason"] + "; missing native input events")
        # Save the episode before post-processing, preserving input and recording on stop.
        atomic_json(attempt / "recorded.json", result)
        if interrupted:
            raise interrupted
        atomic_json(recorded, artifact(attempt / "recorded.json", self.job))
        return self.finish_episode(directory, attempt, result)

    def finish_episode(self, directory, attempt, result):
        """Resume video post-processing without repeating completed game inputs."""
        if result["video"] and result["video"]["origin"] == "engine" and not self.native_recording:
            result["frames"] += _extract_frames(self.config,
                safe_path(self.job, result["video"]["artifact"]["path"]),
                attempt / "keyframes", self.job, self.check_stop)
        atomic_json(attempt / "result.json", result)
        item = artifact(attempt / "result.json", self.job)
        atomic_json(directory / "completed.json", item)  # Failed attempts count; never retry until success.
        self.assert_source()
        return item

    def run(self, agent):
        self.assert_source()
        from .runtime import cleanup_runtimes
        cleanup_runtimes(self.job)
        for marker in self.job.rglob("worker.json"):
            PolicyWorker.cleanup(marker)
        if self.config["backend"] == "harbor":
            for marker in self.job.rglob("harbor-owner.json"):
                Agent.cleanup_harbor(marker)
        policy = self.author(agent)
        plan = read_json(self.job / policy["plan"]["path"])
        if len(plan["episodes"]) != self.options["trajectories_per_version"]:
            raise ValueError("Cannot change episode count inside an existing Player session")
        episodes = self.run_episodes(plan["episodes"], policy["policy"])
        report = {"format": "policy-v1", **policy, "episodes": episodes,
                  "trajectories_per_version": len(episodes), "summary": None,
                  "analysis_evidence_valid": False}
        if self.benchmark:
            from .gamecraft_player import prepare_replay
            prepare_replay(self, report)
            report["gamecraft"] = artifact(self.job / "gamecraft.json", self.job)
        saved = self.job / "analysis.json"
        if saved.exists():
            analysis = read_json(saved)
        else:
            job = self.job / ("analysis-" + uuid.uuid4().hex[:10])
            target = job / "io/evidence"
            export_policy_bundle(report, self.job, target)
            digest = version(target)
            context = {
                "phase": "policy_analyze", "evidence": "evidence/summary.json",
                "handoff": _preparation(policy, self.job).get("context", {}),
                "policy_changes": {"mode": policy.get("inheritance", {}).get("mode", "legacy"),
                                   "summary": plan["summary"]},
                "source_access": "none; inspect host-recorded evidence only",
                "limitations": "source-assisted policies; completion is policy-reported, not human win rate; recording cadence is labelled per evidence source"}
            if self.benchmark:
                from .gamecraft_player import context as benchmark_context
                context["benchmark"] = benchmark_context(self.config)
            analysis = validate(agent.call("player", job, context,
                tick=self.check_stop), POLICY_REVIEW_SCHEMA)
            if version(target) != digest:
                raise ValueError("Player modified its analysis evidence copy")
            citations = analysis["evidence"]
            for citation in citations:
                if not citation.startswith("evidence/") or not safe_path(job / "io", citation).is_file():
                    raise ValueError("Player cited unavailable evidence")
            # Require visual inspection citations for every episode that has frames.
            for index, episode in enumerate(episodes):
                data = read_json(safe_path(self.job, episode["path"]))
                prefix = f"evidence/episodes/{index + 1:04d}/"
                if data["frames"] and not any(p.startswith(prefix) and p.endswith(".png") for p in citations):
                    raise ValueError("Player analysis must cite a real frame from every recorded episode")
            if self.benchmark:
                from .gamecraft_player import check_replay_citations, save_goal_assessment, save_coverage_assessment
                check_replay_citations(self.job, citations)
                save_goal_assessment(self.job, job / "io", report)
                save_coverage_assessment(self.job, job / "io", report)
            atomic_json(saved, analysis)
        validate(analysis, POLICY_REVIEW_SCHEMA)
        report["summary"] = {key: analysis[key] for key in PLAYER_SCHEMA["properties"]}
        report["analysis"] = artifact(saved, self.job)
        report["analysis_evidence_valid"] = True
        if self.benchmark:
            report["gamecraft_goals"] = artifact(self.job / "gamecraft-goals.json", self.job)
            if "coverage" in plan:
                report["gamecraft_coverage"] = artifact(self.job / "coverage-results.json", self.job)
        self.assert_source()
        atomic_json(self.job / "play.json", report)
        self.prune_workspaces(report)
        return report

    def prune_workspaces(self, report):
        if self.config.get("storage", {}).get("keep_workspaces", True):
            return
        # Failure workspaces stay available for diagnosis. Keep all frames, logs,
        # receipts, policies and candidate snapshots, including rejected candidates.
        outcomes = [read_json(safe_path(self.job, e["path"])) for e in report["episodes"]]
        if any(e["status"] not in ("completed", "truncated") for e in outcomes):
            return
        paths = list(self.job.glob("author-*/source"))
        paths += list(self.job.glob("preflight/engine/runtime-*/project"))
        paths += list(self.job.glob("preflight/engine/project"))
        for receipt in report["episodes"]:
            episode = safe_path(self.job, receipt["path"]).parent
            paths += list(episode.glob("engine/runtime-*/project"))
            paths += list(episode.glob("engine/project"))
        removed, warnings = [], []
        for path in paths:
            if path.is_symlink() or not path.resolve().is_relative_to(self.job.resolve()):
                continue
            try:
                shutil.rmtree(path)
                removed.append(path.relative_to(self.job).as_posix())
            except OSError:
                warnings.append(path.relative_to(self.job).as_posix())
        atomic_json(self.job / "workspace-cleanup.json", {"removed": removed, "not_removed": warnings,
                    "preserved": "candidate projects, policies, trajectories, images, videos, logs and receipts"})

    def run_episodes(self, specifications, policy):
        """Run independent engines concurrently; keep receipt order deterministic."""
        workers = min(self.options["parallel_episodes"], len(specifications))
        atomic_json(self.job / "execution.json", {
            "parallel_episodes": workers, "episode_count": len(specifications)})
        if workers == 1:
            return [self.episode(i, spec, policy) for i, spec in enumerate(specifications)]
        results = [None] * len(specifications)
        pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="player-episode")
        futures = {}
        try:
            futures = {pool.submit(self.episode, i, spec, policy): i
                       for i, spec in enumerate(specifications)}
            pending = set(futures)
            while pending:
                self.check_stop()
                finished, pending = wait(pending, timeout=.1, return_when=FIRST_COMPLETED)
                for future in finished:
                    results[futures[future]] = future.result()
            return results
        except BaseException:
            # Stop/interrupt/integrity failures cancel siblings. A recorded game
            # failure remains evidence and does not discard other episodes.
            self._cancel.set()
            for future in futures:
                future.cancel()
            raise
        finally:
            pool.shutdown(wait=True, cancel_futures=True)
            self._cancel.clear()


def _episode_intact(episode, root, expected_version, policy_hash):
    return (episode["version"] == expected_version and episode["policy_sha256"] == policy_hash
        and verify_artifact(episode["trajectory"], root)
        and (not episode.get("input_events_required") or "input_events" in episode)
        and ("input_events" not in episode or verify_artifact(episode["input_events"], root))
        and all(verify_artifact(frame["image"], root) for frame in episode["frames"])
        and (episode["video"] is None or (verify_artifact(episode["video"]["artifact"], root)
            and all(verify_artifact(episode["video"][k], root) for k in ("capture_receipt", "capture_timing")
                    if k in episode["video"]))))


def _relative_analysis(value, io, paths):
    """Repair only unambiguous legacy citations to known files; keep raw output."""
    if not isinstance(value, dict) or not isinstance(value.get("evidence"), list):
        raise ValueError("Player analysis must contain an evidence array")
    aliases = {alias: path for path in paths for alias in
               (path, "/workspace/io/" + path, str(io.resolve() / path))}
    citations = []
    for citation in value["evidence"]:
        if not isinstance(citation, str):
            raise ValueError("Player evidence must contain file paths")
        parts = re.split(r"：|:\s", citation.strip(), maxsplit=1)
        path = aliases.get(parts[0])
        if path is None or (len(parts) == 2 and "evidence/" in parts[1]):
            raise ValueError("Player cited unavailable or ambiguous evidence; use one relative path per item")
        citations.append(path)
    result = {**value, "evidence": list(dict.fromkeys(citations))}
    return validate(result, POLICY_REVIEW_SCHEMA)


def recover_policy_analysis(job, expected_version, *, allow_mock=False):
    """Finish a fully recorded batch offline, preserving its historical runtime.

    No engines, policies or models run here. Incomplete batches still pass through
    the normal strict runtime-identity checks in PolicySession.author().
    """
    job = job.resolve()
    if not (job / "policy.json").is_file():
        return None
    policy = read_json(job / "policy.json")
    if (policy["version"] != expected_version or
            any(not verify_artifact(policy[name], job) for name in ("policy", "plan"))):
        raise ValueError("Saved Player policy/plan changed; refusing analysis recovery")
    _preparation(policy, job)
    plan = validate(read_json(safe_path(job, policy["plan"]["path"])), POLICY_PLAN_SCHEMA)
    receipts = [job / "episodes" / f"{i + 1:04d}" / "completed.json"
                for i in range(len(plan["episodes"]))]
    if not all(path.is_file() for path in receipts):
        return None
    episodes = [read_json(path) for path in receipts]
    for item in episodes:
        if not verify_artifact(item, job):
            raise ValueError("Completed policy episode receipt changed")
        episode = read_json(safe_path(job, item["path"]))
        if not _episode_intact(episode, job, expected_version, policy["policy"]["sha256"]):
            raise ValueError("Completed policy episode evidence changed")
    outputs = list(job.glob("analysis-*/io/result.json"))
    if not outputs:
        return None
    raw = max(outputs, key=lambda p: p.stat().st_mtime_ns)
    io = raw.parent
    report = {"format": "policy-v1", **policy, "episodes": episodes,
              "trajectories_per_version": len(episodes), "analysis_evidence_valid": True}
    if policy["settings"].get("benchmark"):
        if not (job / "gamecraft.json").exists():
            return None
        report["gamecraft"] = artifact(job / "gamecraft.json", job)
    with tempfile.TemporaryDirectory(prefix=".analysis-recovery-", dir=job) as temporary:
        staging = Path(temporary)
        expected = staging / "evidence"
        # Rebuild the exact bundle from hashed recordings, not model-supplied files.
        digest = export_policy_bundle({**report, "summary": None}, job, expected)
        if version(io / "evidence") != digest:
            raise ValueError("Saved Player analysis evidence copy changed; refusing recovery")
        paths = ["evidence/" + name for name in inventory(expected)]
        analysis = _relative_analysis(read_json(raw), io, paths)
        for index, item in enumerate(episodes):
            data = read_json(safe_path(job, item["path"]))
            prefix = f"evidence/episodes/{index + 1:04d}/"
            if data["frames"] and not any(p.startswith(prefix) and p.endswith(".png")
                                          for p in analysis["evidence"]):
                raise ValueError("Player analysis must cite a real frame from every recorded episode")
        atomic_json(staging / "analysis.json", analysis)
        report["summary"] = {key: analysis[key] for key in PLAYER_SCHEMA["properties"]}
        report["analysis"] = artifact(staging / "analysis.json", job)
        if policy["settings"].get("benchmark"):
            from .gamecraft_player import check_replay_citations, save_goal_assessment, save_coverage_assessment
            check_replay_citations(job, analysis["evidence"])
            report["gamecraft_goals"] = save_goal_assessment(job, io, report)
            coverage = save_coverage_assessment(job, io, report)
            if coverage:
                report["gamecraft_coverage"] = coverage
        if not valid_policy_play(report, job, expected_version, allow_mock=allow_mock):
            raise ValueError("Recorded Player batch failed integrity checks; refusing analysis recovery")
        atomic_json(job / "analysis-recovery.json", {
            "source": artifact(raw, job), "original_evidence": read_json(raw)["evidence"],
            "relative_evidence": analysis["evidence"], "replayed": False,
            "runtime": policy["settings"].get("runtime")})
        atomic_json(job / "analysis.json", analysis)
        report["analysis"] = artifact(job / "analysis.json", job)
        atomic_json(job / "play.json", report)
    return report


def valid_policy_play(report, root, expected_version, *, allow_mock=False):
    root = root.resolve()
    try:
        if report["version"] != expected_version or not report["analysis_evidence_valid"]:
            return False
        validate(report["summary"], PLAYER_SCHEMA)
        for name in ("policy", "plan", "analysis"):
            if not verify_artifact(report[name], root):
                return False
        _preparation(report, root)
        plan = validate(read_json(safe_path(root, report["plan"]["path"])), POLICY_PLAN_SCHEMA)
        analysis = validate(read_json(safe_path(root, report["analysis"]["path"])), POLICY_REVIEW_SCHEMA)
        if any(report["summary"][key] != analysis[key] for key in PLAYER_SCHEMA["properties"]):
            return False
        count = report["trajectories_per_version"]
        benchmark = report["settings"].get("benchmark")
        if benchmark:
            from .gamecraft_player import check_bundle, check_coverage_assessment
            if not all(verify_artifact(report[k], root) for k in ("gamecraft", "gamecraft_goals")):
                return False
            check_bundle(read_json(safe_path(root, report["gamecraft"]["path"])), root, report)
            check_coverage_assessment(report, root, plan)
        if count < 1 or len(report["episodes"]) != count or len(plan["episodes"]) != count:
            return False
        if len({e["profile"] for e in plan["episodes"]}) != count:
            return False
        for index, item in enumerate(report["episodes"]):
            if not verify_artifact(item, root):
                return False
            episode = read_json(safe_path(root, item["path"]))
            from .gamecraft import _demo_time_limit
            deadline_stop = bool(benchmark) and _demo_time_limit(episode, report["settings"])
            if (not _episode_intact(episode, root, expected_version, report["policy"]["sha256"])
                    or episode.get("runtime") != report["settings"].get("runtime")
                    or episode["index"] != index or (episode["status"] != "completed" and not deadline_stop)
                    or episode["profile"] != plan["episodes"][index]["profile"]
                    or episode["purpose"] != plan["episodes"][index]["purpose"]
                    or episode["seed"] != report["settings"]["seed"] + index
                    or episode["steps"] < 1 or len(episode["frames"]) < 2
                    or not episode["video"]
                    or (episode["video"]["origin"] != "engine" and not allow_mock)):
                return False
            actions, pending, state, ended, previous_time = 0, None, None, False, -1.0
            with safe_path(root, episode["trajectory"]["path"]).open() as stream:
                for line in stream:
                    row = json.loads(line)
                    if (ended or row["version"] != expected_version
                            or row["policy_sha256"] != report["policy"]["sha256"]
                            or row["time_seconds"] < previous_time):
                        return False
                    previous_time = row["time_seconds"]
                    if row["type"] == "initial":
                        if state is not None or not isinstance(row["state"], dict):
                            return False
                        state = row["state"]
                    elif row["type"] == "input":
                        validate(row["action"], ACTION_SCHEMA)
                        if pending or row["index"] != actions or row["before"] != state:
                            return False
                        pending = row
                    elif row["type"] == "feedback":
                        if (not pending or row["action"] != pending["action"]
                                or row["before"] != state or row["index"] != actions
                                or not isinstance(row["after"], dict)):
                            return False
                        actions += 1
                        state, pending = row["after"], None
                    elif row["type"] == "end":
                        ended = row["status"] == episode["status"] and pending is None
                        if not ended:
                            return False
                    else:
                        return False
            if actions != episode["steps"] or not ended:
                return False
        return True
    except (OSError, ValueError, KeyError, TypeError, ValidationError):
        return False


def export_policy_bundle(report, root, target):
    """Adapt Player evidence for the existing Reviewer without exposing source/order."""
    root = root.resolve()
    target.mkdir(parents=True)
    episodes = []
    for index, item in enumerate(report["episodes"]):
        result = read_json(safe_path(root, item["path"]))
        folder = target / "episodes" / f"{index + 1:04d}"
        folder.mkdir(parents=True)

        def copy(item, name):
            dest = folder / name
            copy_file(safe_path(root, item["path"]), dest)
            return dest.relative_to(target).as_posix()

        frames = [{**{k: v for k, v in f.items() if k != "image"},
                   "image": copy(f["image"], f"frame-{i:04d}.png")}
                  for i, f in enumerate(result["frames"])]
        video = result["video"]
        video_path = copy(video["artifact"], "play" + Path(video["artifact"]["path"]).suffix) if video else None
        trajectory = folder / "trajectory.jsonl"
        with trajectory.open("w") as dest, safe_path(root, result["trajectory"]["path"]).open() as source:
            for line in source:
                row = json.loads(line)
                row.pop("version", None)
                row.pop("policy_sha256", None)
                dest.write(json.dumps(row, ensure_ascii=False) + "\n")
        episodes.append({key: result[key] for key in
                         ("profile", "purpose", "seed", "steps", "status", "reason", "elapsed_seconds")})
        if "goal" in result:
            episodes[-1].update(goal=result["goal"], scenario=result.get("scenario"))
        episodes[-1].update(frames=frames, video=video_path, video_mode=video["mode"] if video else None,
                            trajectory=trajectory.relative_to(target).as_posix())
    atomic_json(target / "summary.json", {"player": report["summary"], "mode": "policy",
        "episodes": episodes, "limitations": [
            "Source-assisted automated tests; policy completion is not a verified game win.",
            "Profiles may converge to the same behaviour; do not infer diversity from names.",
            "Each episode has its own trace, frames and video. Inspect all episodes.",
            "Frames use episode-relative or video-relative clocks as labelled; their origins may differ.",
            "2 FPS recordings do not establish smooth frame pacing or precise input latency.",
            "Policies are generated independently per version; this is not a fixed-skill human difficulty comparison."]})
    if "gamecraft" in report:
        from .gamecraft_player import export_evidence
        export_evidence(report, root, target)
    return version(target)
