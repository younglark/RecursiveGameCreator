"""Disposable HTML/Godot, Blender and media containers; no model calls."""
from copy import deepcopy
import json
import os
from pathlib import Path
import re
import select
import subprocess
import time
import uuid

from .agent import child_env, terminate
from .game import Engine, GodotGame, HTMLGame
from .store import atomic_json, read_json, redact, safe_path, version
from .types import DEFAULTS

PACKAGE = Path(__file__).parent
PROTOCOL = 2
OWNER_LABEL = "rsi-gamecreator.runtime-owner"


def options(config):
    return {**DEFAULTS["runtime"], **config.get("runtime", {})}


def _docker(args):
    return subprocess.run(["docker", *args], capture_output=True, text=True,
                          timeout=30, env=child_env())


def runtime_identity(config):
    """Resolve mutable tags before a batch and pin runs to the inspected image ID."""
    if config["backend"] == "mock" or options(config)["mode"] != "docker":
        return None
    image = options(config)["image"]
    result = _docker(["image", "inspect", image])
    if result.returncode:
        raise RuntimeError("Runtime image is unavailable; build docker/runtime.Dockerfile first. "
                           "No automatic pull or desktop fallback. " + redact(result.stderr))
    data = json.loads(result.stdout)[0]
    if data["Os"] != "linux" or not re.fullmatch(r"sha256:[a-f0-9]{64}", data["Id"]):
        raise ValueError("Runtime image must be a Linux image with a content-addressed ID")
    identity = {"mode": "docker", "protocol": PROTOCOL, "image_id": data["Id"],
            "os": data["Os"], "architecture": data["Architecture"],
            "framework_sha256": version(PACKAGE),
            "renderer": "mesa-software" if config["engine"] == "godot" else "chromium"}
    if config["engine"] == "godot":
        identity["render_threads"] = options(config)["render_threads"]
    return identity


def cleanup_owner(marker: Path, *, graceful=False):
    """Only remove a container whose unpredictable ownership label still matches."""
    if not marker.exists():
        return
    owner = read_json(marker)
    name, token = owner.get("container", ""), owner.get("owner", "")
    if not re.fullmatch(r"rsi-game-[a-f0-9]{32}", name) or not re.fullmatch(r"[a-f0-9]{32}", token):
        raise ValueError("Invalid runtime container ownership marker")
    result = _docker(["container", "inspect", name])
    if result.returncode:
        if "No such" not in result.stderr:
            raise RuntimeError("Cannot inspect owned runtime: " + redact(result.stderr))
    else:
        data = json.loads(result.stdout)[0]
        if (data["Config"].get("Labels") or {}).get(OWNER_LABEL) != token:
            raise ValueError("Runtime ownership mismatch; refusing to stop another container")
        if graceful and data["State"]["Running"]:
            result = _docker(["stop", "--time", "20", name])
            if result.returncode and "No such" not in result.stderr:
                raise RuntimeError("Cannot stop owned runtime: " + redact(result.stderr))
        for _ in range(50):
            result = _docker(["rm", "-f", name])
            if not result.returncode or "No such" in result.stderr:
                break
            if "removal" not in result.stderr or "already in progress" not in result.stderr:
                raise RuntimeError("Cannot clean owned runtime: " + redact(result.stderr))
            # --rm and explicit cleanup can race while Docker removes the mount.
            time.sleep(.1)
        else:
            raise RuntimeError("Owned runtime removal did not finish; ownership marker retained")
    marker.unlink(missing_ok=True)


def cleanup_runtimes(root: Path):
    for marker in root.rglob("*-runtime-owner.json"):
        cleanup_owner(marker, graceful=True)


class RuntimeContainer(Engine):
    """Private transport shared by game instances and one-shot tool jobs."""
    def __init__(self, config, project, output, stop=lambda: None, runner=None):
        if config["engine"] not in ("html", "godot"):
            raise ValueError("Unsupported game engine")
        super().__init__(config, project.resolve(), output.resolve(), stop, runner)
        self.process = None
        self.broken = False
        self.buffer = b""
        self.stderr_open = True
        # Not exposed to the game through its /output mount.
        self.owner_file = self.output.with_name(self.output.name + "-runtime-owner.json")
        self.timeout = options(config)["health_timeout_seconds"]
        self.identity = None

    def _launch(self, purpose):
        self.check_stop()
        if os.getuid() == 0:
            raise ValueError("Run the controller as a non-root user for writable runtime bind mounts")
        cleanup_owner(self.owner_file, graceful=True)
        self.identity = runtime_identity(self.config)
        expected = self.config.get("_runtime_identity")
        if expected is not None and expected != self.identity:
            raise ValueError("Runtime identity changed during the Player batch")
        network = options(self.config)["build_network"] if purpose == "build" else "none"
        token = uuid.uuid4().hex
        name = "rsi-game-" + uuid.uuid4().hex
        mounts = [(self.project, "/source", purpose != "build"),
                  (self.output, "/output", False), (PACKAGE.resolve(), "/framework/game_agent_lite", True)]
        if any("," in str(path) for path, _, _ in mounts):
            raise ValueError("Runtime mount paths cannot contain commas")
        argv = ["docker", "run", "--rm", "--pull=never", "--init", "-i", "--name", name,
                "--label", OWNER_LABEL + "=" + token, "--network", network, "--read-only", "--shm-size", "512m",
                "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                "--user", f"{os.getuid()}:{os.getgid()}",
                "--tmpfs", "/tmp:rw,nosuid,nodev", "--workdir", "/output"]
        for path, target, readonly in mounts:
            # Docker's --mount CSV cannot represent arbitrary comma-containing paths here.
            argv += ["--mount", f"type=bind,source={path},target={target}" + (",readonly" if readonly else "")]
        for value in ("PYTHONPATH=/framework", "PYTHONDONTWRITEBYTECODE=1", "HOME=/output/home",
                      "XDG_DATA_HOME=/output/user-data", "XDG_CACHE_HOME=/tmp/cache",
                      "XDG_RUNTIME_DIR=/tmp/xdg-runtime", "LIBGL_ALWAYS_SOFTWARE=1", "DISPLAY=:99"):
            argv += ["--env", value]
        if self.config["engine"] == "godot":
            argv += ["--env", f"LP_NUM_THREADS={options(self.config)['render_threads']}"]
        argv += ["--entrypoint", "python3", self.identity["image_id"],
                 "-u", "-m", "game_agent_lite.runtime_worker"]
        atomic_json(self.owner_file, {"container": name, "owner": token})
        try:
            self.process = subprocess.Popen(argv, cwd=self.output, env=child_env(), stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
            # Pass engine settings only. Credentials, model settings and host tool paths stay outside.
            engine_config = {key: deepcopy(self.config[key]) for key in
                             ("engine", "entry", "build_command", "check_command", "headless")}
            ready = self._request("init", purpose=purpose, config=engine_config, identity=self.identity)
            if ready.get("protocol") != PROTOCOL or ready.get("framework_sha256") != self.identity["framework_sha256"]:
                raise ValueError("Runtime worker identity mismatch")
            atomic_json(self.output / "container-runtime.json", {
                **self.identity, "container": name, "purpose": purpose, "tools": ready,
                "source_version": version(self.project), "network": network, "desktop_attached": False})
        except BaseException:
            self._dispose(graceful=True)
            raise

    def _request(self, method, *, interruptible=True, **params):
        if self.broken or self.process is None:
            raise RuntimeError("Runtime transport unavailable; requests are never replayed")
        request_id = uuid.uuid4().hex
        payload = json.dumps({"id": request_id, "method": method, "params": params}, allow_nan=False).encode() + b"\n"
        pending = memoryview(payload)
        os.set_blocking(self.process.stdin.fileno(), False)
        deadline = time.monotonic() + self.timeout
        try:
            with (self.output / "container.log").open("a") as log:
                while True:
                    if interruptible:
                        self.check_stop()
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Container worker heartbeat lost; action was not retried")
                    streams = [self.process.stdout] + ([self.process.stderr] if self.stderr_open else [])
                    reads, writes, _ = select.select(streams, [self.process.stdin] if pending else [], [], .05)
                    if writes:
                        pending = pending[os.write(self.process.stdin.fileno(), pending):]
                    eof = False
                    for stream in reads:
                        data = os.read(stream.fileno(), 65536)
                        if stream is self.process.stderr:
                            if not data:
                                self.stderr_open = False
                            else:
                                log.write(redact(data.decode(errors="replace")))
                                log.flush()
                        else:
                            eof = not data
                            self.buffer += data
                            if len(self.buffer) > 10 * 1024 * 1024:
                                raise ValueError("Runtime response exceeds transport limit")
                    while b"\n" in self.buffer:
                        line, self.buffer = self.buffer.split(b"\n", 1)
                        reply = json.loads(line)
                        if reply.get("id") != request_id:
                            raise ValueError("Runtime response ID mismatch")
                        deadline = time.monotonic() + self.timeout
                        if reply.get("heartbeat"):
                            continue
                        if reply.get("error"):
                            raise RuntimeError("Container engine: " + redact(reply["error"]))
                        return reply["result"]
                    if eof:
                        raise RuntimeError("Runtime worker exited; see container.log; action was not retried")
        except BaseException:
            # A lost response is ambiguous: never send another action or assume success.
            self.broken = True
            raise

    def _dispose(self, *, graceful=False):
        try:
            cleanup_owner(self.owner_file, graceful=graceful)
        finally:
            if self.process is not None:
                try:
                    terminate(self.process)
                except PermissionError:
                    # macOS can return EPERM for an already-exited process group.
                    if self.process.poll() is None:
                        raise
                for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
                    stream.close()
                self.process = None


class DockerGame(RuntimeContainer):
    """Engine API with engine-specific input validation, no host engine startup."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        cls = HTMLGame if self.config["engine"] == "html" else GodotGame
        # Construction only: this adapter validates inputs and never starts on the host.
        self.input_adapter = cls(self.config, self.project, self.output, self.check_stop)

    def input_capabilities(self):
        return self.input_adapter.input_capabilities()

    def input_info(self):
        return self._request("input_info")

    def capture_diagnostic(self, path):
        if self.process is None or self.broken:
            return False
        result = self._request("capture_diagnostic")
        if result.get("path"):
            import shutil
            shutil.copyfile(safe_path(self.output, result["path"]), path)
            return True
        return False

    def prepare_action(self, action):
        return self.input_adapter.prepare_action(action)

    def build(self):
        try:
            self._launch("build")
            result = self._request("build")
            return {**result, "runtime": self.identity}
        finally:
            self._dispose(graceful=True)

    def start(self):
        self._launch("play")
        if hasattr(self, "native_policy_settings"):
            self._request("configure_policy", **self.native_policy_settings)
        self._request("start")

    def configure_policy(self, source, **settings):
        self.native_policy_settings = {"source": source, **settings}

    def native_policy_start(self, **settings):
        return self._request("native_policy_start", **settings)

    def native_policy_status(self):
        return self._request("native_policy_status")

    def native_policy_trace(self):
        return self.output / "policy-trajectory.jsonl"

    def native_input_events(self):
        return self.output / "input_events.jsonl"

    def reset(self, seed):
        self._request("reset", seed=seed)

    def observe_state(self):
        return self._request("observe_state")

    def observe(self):
        result = self._request("observe")
        return {"public": result["public"], "image": safe_path(self.output, result["image"])}

    def policy_act(self, action):
        self._request("policy_act", action=self.prepare_action(action))

    def act(self, action):
        self._request("act", action=self.prepare_action(action))

    def release_inputs(self):
        if self.process is not None and not self.broken:
            self._request("release_inputs", interruptible=False)

    def stop(self):
        result = None
        try:
            if self.process is not None and not self.broken:
                result = self._request("stop", interruptible=False)
        finally:
            self._dispose(graceful=True)
        if result is None and (self.output / "runtime-shutdown.json").exists():
            result = read_json(self.output / "runtime-shutdown.json")["video"]
        if result:
            return {**result, "path": safe_path(self.output, result["path"]), "runtime": self.identity}
        return None


def run_tool(config, source, output, purpose, stop=lambda: None, **params):
    """Run a bounded-purpose job with the same stop/ownership/identity protocol."""
    if purpose not in ("blender", "media"):
        raise ValueError("Unsupported runtime tool")
    runtime = RuntimeContainer(config, source, output, stop)
    try:
        runtime._launch(purpose)
        result = runtime._request(purpose, **params)
        return result, runtime.identity
    finally:
        runtime._dispose(graceful=True)


def run_blender(config, source, output, script, stop=lambda: None, *, output_format="glb"):
    result, identity = run_tool(config, source, output, "blender", stop,
                                script=script, output_format=output_format)
    return safe_path(output, result["path"]), identity


def extract_frames(config, video, output, root, stop=lambda: None):
    from .store import artifact
    result, _ = run_tool(config, video.parent, output, "media", stop, video=video.name)
    return [{"clock": "video", "time_seconds": frame["time_seconds"],
             "image": artifact(safe_path(output, frame["image"]["path"]), root)}
            for frame in result]
