"""Engine I/O and host-owned gameplay evidence; the Player never supplies proof files."""
from copy import deepcopy
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import shutil
import socket
from string import ascii_lowercase, digits
import subprocess
import threading
import time
import uuid
from jsonschema import ValidationError

from .agent import CommandRunner, child_env, terminate
from .store import (artifact, atomic_json, file_hash, read_json, safe_path, snapshot, version, verify_artifact)
from .types import ACTION_SCHEMA, REQUEST_SCHEMA, validate

PACKAGE = Path(__file__).parent
VIDEO_WIDTH, VIDEO_HEIGHT, VIDEO_FPS = 640, 360, 2
VIDEO_FILTER = f"fps={VIDEO_FPS},scale={VIDEO_WIDTH}:{VIDEO_HEIGHT}:flags=bilinear,setsar=1"
GODOT_NAMED_KEYS = ("ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "Space",
                    "Escape", "Enter", "Shift", "Control", "Tab", "Alt", "Backspace", "Delete")
GODOT_KEY_ALIASES = {
    **{f"Digit{digit}": digit for digit in digits},
    **{f"Key{letter.upper()}": letter for letter in ascii_lowercase},
    " ": "Space", "Spacebar": "Space", "Ctrl": "Control", "Return": "Enter", "Esc": "Escape",
}


class InputRejected(ValueError):
    """Host preflight rejection: no key or pointer input has been sent."""


class Engine:
    def __init__(self, config, project, output, stop=lambda: None, runner=None):
        self.config, self.project, self.output, self.check_stop = config, project, output, stop
        self.runner = runner or CommandRunner()
        output.mkdir(parents=True, exist_ok=True)

    def input_capabilities(self):
        return {"engine": self.config["engine"],
                "key_format": "Engine-specific key names or single characters."}

    def prepare_action(self, action):
        validate(action, ACTION_SCHEMA)
        pointer = action["pointer"]
        if pointer and pointer.get("phase") in ("down", "up") and pointer["button"] == "none":
            raise InputRejected("Pointer down/up requires left or right button")
        return deepcopy(action)

    def input_info(self):
        return {**self.input_capabilities(), "pointer": {
            "coordinates": "normalized 0..1 relative to the interactive viewport, not recording size",
            "phases": ["click", "down", "move", "up"], "default_phase": "click",
            "max_held_buttons": 1, "null": "keeps the current pointer state"}}

    def capture_diagnostic(self, path):
        """Best-effort failure screenshot, even when the state bridge is broken."""
        return False

    def build(self):
        commands = []
        if self.config["build_command"]:
            commands.append(self.config["build_command"])
        if self.config["engine"] == "godot":
            if not (self.project / "project.godot").is_file():
                raise ValueError("Missing project.godot")
            commands.append([self.config["godot"], "--headless", "--editor", "--path", str(self.project), "--import"])
        elif not self.config["build_command"] and not safe_path(self.project, self.config["entry"]).is_file():
            raise ValueError("HTML entry does not exist")
        if self.config["check_command"]:
            commands.append(self.config["check_command"])
        for index, command in enumerate(commands):
            log = self.output / f"build-{index}.log"
            self.runner.run(command, self.project, log, tick=self.check_stop)
            if self.config["engine"] == "godot" and any(
                s in log.read_text(errors="replace") for s in ("SCRIPT ERROR:", "Parse Error:", "ERROR:")):
                raise ValueError("Godot logged errors even though its process exited successfully")
        if self.config["engine"] == "html" and not safe_path(self.project, self.config["entry"]).is_file():
            raise ValueError("HTML entry does not exist after build")
        return {"passed": True, "checks": len(commands), "scope": "build + configured checks; play bridge checked later"}


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


class HTMLGame(Engine):
    def input_capabilities(self):
        return {"engine": "html", "key_format": "Playwright keyboard names or single characters.",
                "examples": ["1", "w", "Digit1", "KeyW", "ArrowLeft", "Enter", "Backspace"],
                "note": "HTML keeps Playwright key names; Godot's named-key list does not apply."}

    def start(self):
        from playwright.sync_api import sync_playwright
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), partial(QuietHandler, directory=str(self.project)))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.pw = sync_playwright().start()
        self.browser = self.pw.chromium.launch(headless=self.config["headless"])
        self.context = self.browser.new_context(viewport={"width": 960, "height": 540},
                                               record_video_dir=str(self.output / "video"),
                                               record_video_size={"width": VIDEO_WIDTH, "height": VIDEO_HEIGHT})
        self.page = self.context.new_page()
        self.errors = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        self.page.goto(f"http://127.0.0.1:{self.server.server_port}/{self.config['entry']}")
        self.page.wait_for_function("() => window.GameAgentBridge?.ready === true", timeout=15000)

    def reset(self, seed):
        self.release_inputs()
        self.page.evaluate("seed => window.GameAgentBridge.reset(seed)", seed)

    def input_info(self):
        return {**super().input_info(), "viewport": self._viewport(), "viewport_units": "CSS pixels"}

    def _viewport(self):
        size = self.page.viewport_size
        return size if isinstance(size, dict) else {"width": 960, "height": 540}

    def capture_diagnostic(self, path):
        if hasattr(self, "page"):
            self.page.screenshot(path=str(path))
            return True
        return False

    def _pointer(self, pointer):
        if pointer is None:
            return
        size = self._viewport()
        self.page.mouse.move(pointer["x"] * (size["width"] - 1), pointer["y"] * (size["height"] - 1))
        phase, button = pointer.get("phase", "click"), pointer["button"]
        held = getattr(self, "held_button", None)
        if phase in ("click", "down") and button != "none" and held is None:
            self.page.mouse.down(button=button)
            self.held_button = button
        elif phase == "up" and held == button:
            self.page.mouse.up(button=button)
            self.held_button = None

    def _release_mouse(self):
        if getattr(self, "held_button", None):
            self.page.mouse.up(button=self.held_button)
            self.held_button = None

    def observe(self):
        public = self.observe_state()
        image = self.output / f"frame-{uuid.uuid4().hex}.png"
        self.page.screenshot(path=str(image))
        if self.errors:
            raise ValueError("Browser runtime errors: " + self.errors[-1])
        return {"public": public, "image": image}

    def observe_state(self):
        public = self.page.evaluate("() => window.GameAgentBridge.observe()")
        if self.errors:
            raise ValueError("Browser runtime errors: " + self.errors[-1])
        return public

    def policy_act(self, action):
        """Persistent keys and optional pointer phases; legacy pointers are clicks."""
        action = self.prepare_action(action)
        pointer = action["pointer"]
        button = getattr(self, "held_button", None)
        if pointer and button and pointer["button"] != "none":
            phase = pointer.get("phase", "click")
            if phase == "click" or (phase == "down" and pointer["button"] != button):
                raise InputRejected("Release the held mouse button before another press/click")
        self.held_keys = list(getattr(self, "held_keys", []))
        desired = action["keys"]
        try:
            for key in self.held_keys[:]:
                if key not in desired:
                    self.page.keyboard.up(key)
                    self.held_keys.remove(key)
            for key in desired:
                if key not in self.held_keys:
                    self.held_keys.append(key)
                    self.page.keyboard.down(key)
            self._pointer(pointer)
            remaining = action["duration_ms"]
            while remaining:
                self.check_stop()
                span = min(remaining, 100)
                self.page.wait_for_timeout(span)
                remaining -= span
        except BaseException:
            self.release_inputs()
            raise
        finally:
            if pointer and pointer.get("phase", "click") == "click" and pointer["button"] != "none":
                self._release_mouse()

    def release_inputs(self):
        try:
            for key in getattr(self, "held_keys", set()):
                self.page.keyboard.up(key)
        finally:
            self.held_keys = set()
            self._release_mouse()

    def act(self, action):
        action = self.prepare_action(action)
        try:
            self.policy_act(action)
        finally:
            for key in getattr(self, "held_keys", set()):
                self.page.keyboard.up(key)
            self.held_keys = set()

    def stop(self):
        recording_path = None
        try:
            self.release_inputs()
            if hasattr(self, "context"):
                recording = self.page.video if hasattr(self, "page") else None
                self.context.close()
                if recording:
                    recording_path = Path(recording.path())
        finally:
            if hasattr(self, "browser"):
                self.browser.close()
            if hasattr(self, "pw"):
                self.pw.stop()
            if hasattr(self, "server"):
                self.server.shutdown()
                self.server.server_close()
                self.thread.join(timeout=2)
        if recording_path is None:
            return None
        # Playwright exposes video size, but not recording FPS. Normalize the
        # final artifact without changing gameplay speed or its viewport layout.
        video = self.output / "play.mp4"
        self.runner.run([self.config["ffmpeg"], "-y", "-loglevel", "error", "-i", str(recording_path),
                         "-vf", VIDEO_FILTER, "-pix_fmt", "yuv420p", "-fps_mode", "cfr", str(video)],
                        self.output, self.output / "encode.log")
        artifact(video, self.output)  # Do not silently fall back to native 25 FPS.
        recording_path.unlink()
        return {"path": video, "mode": "real_time_resampled_2fps", "origin": "engine",
                "capture": {"width": VIDEO_WIDTH, "height": VIDEO_HEIGHT, "fps": VIDEO_FPS,
                            "method": "Playwright recording resampled after play"}}


class GodotGame(Engine):
    def input_info(self):
        return {**super().input_info(), **self.rpc("input_info", {})}

    def capture_diagnostic(self, path):
        # Captures from a live bridge may survive a subsequent state/Policy failure.
        frames = sorted(self.output.glob("capture/*.png"), key=lambda p: p.stat().st_mtime)
        if frames:
            shutil.copyfile(frames[-1], path)
            return True
        return False

    def configure_policy(self, source, *, scenario=None, fresh_launch=False, viewport=None, call_timeout=30,
                         evidence_source="official_replay"):
        if hasattr(self, "process"):
            raise ValueError("Configure Policy before starting Godot")
        if scenario is not None:
            import re
            if not re.fullmatch(r"[A-Za-z0-9_-]+", scenario):
                raise ValueError("Invalid scenario name")
        self.native_policy_source = source
        self.policy_scenario = scenario
        self.policy_fresh_launch = fresh_launch
        self.policy_viewport = viewport or [960, 540]
        self.policy_health_timeout = call_timeout
        self.native_recording = evidence_source == "player_recording"

    def native_policy_start(self, **settings):
        return self.rpc("native_policy_start", {**settings, "aliases": GODOT_KEY_ALIASES})

    def native_policy_status(self):
        # Keep runtime errors visible even if the GDScript function aborts mid-call.
        self.observe_state()
        return self.rpc("native_policy_status", {})

    def native_policy_trace(self):
        return self.output / "policy-trajectory.jsonl"

    def native_input_events(self):
        return self.output / "input_events.jsonl"

    def prepare_runtime(self):
        """Always isolate imports, res:// writes and user:// saves per attempt."""
        runtime_id = uuid.uuid4().hex
        runtime = self.output / ("runtime-" + runtime_id) / "project"
        source_version = version(self.project)
        snapshot(self.project, runtime)
        if version(runtime) != source_version or version(self.project) != source_version:
            raise ValueError("Game source changed while preparing runtime copy")
        install_bridge(runtime, "godot")
        if getattr(self, "native_policy_source", None) is not None:
            (runtime / "_rsi_policy.gd").write_text(self.native_policy_source)
        # Preserve existing overrides, then override only the user-data location.
        # Godot creates this unique subdirectory in its platform app-data root.
        self.user_dir_name = "RSI-GameCreator/playtests/" + runtime_id
        settings = ('\n[application]\nconfig/use_custom_user_dir=true\n'
                    'config/custom_user_dir_name=' + json.dumps(self.user_dir_name) + '\n')
        # Editor import ignores override.cfg, so isolate both editor and game.
        for filename in ("project.godot", "override.cfg"):
            with (runtime / filename).open("a") as stream:
                stream.write(settings)
        self.runtime_info = {"source_version": source_version,
            "project": runtime.relative_to(self.output).as_posix(), "user_dir_name": self.user_dir_name,
            "user_data_dir": None}
        atomic_json(self.output / "runtime.json", self.runtime_info)
        log = self.output / "runtime-import.log"
        self.runner.run([self.config["godot"], "--headless", "--editor", "--path", str(runtime), "--import"],
                        runtime, log, tick=self.check_stop)
        if any(s in log.read_text(errors="replace") for s in ("SCRIPT ERROR:", "Parse Error:", "ERROR:")):
            raise RuntimeError("Godot runtime copy import failed; see runtime-import.log")
        return runtime

    def input_capabilities(self):
        return {"engine": "godot", "named_keys": list(GODOT_NAMED_KEYS),
                "key_format": "Printable single characters (e.g. 1, w) or one of named_keys.",
                "aliases": dict(GODOT_KEY_ALIASES),
                "note": "All keys are checked before input. Aliases for the same key are held once."}

    def prepare_action(self, action):
        prepared = super().prepare_action(action)
        keys = []
        for original in prepared["keys"]:
            key = GODOT_KEY_ALIASES.get(original, original)
            if key not in GODOT_NAMED_KEYS:
                if len(key) != 1 or not key.isprintable() or key.isspace():
                    raise InputRejected(f"Unsupported Godot key {original!r}; use a single character "
                                        "such as '1' or 'w', or a named key from input_capabilities.")
                if key in ascii_lowercase.upper():
                    key = key.lower()
            if key not in keys:
                keys.append(key)
        prepared["keys"] = keys
        return prepared

    def start(self):
        # Godot headless rendering does not provide real viewport screenshots.
        self.runtime_project = self.prepare_runtime()
        self.capture_bridge_hash = file_hash(self.runtime_project / "game_agent_bridge.gd")
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.port = probe.getsockname()[1]
        self.token = uuid.uuid4().hex
        env = child_env()
        self.frames = self.output / "capture"
        self.frames.mkdir(exist_ok=True)
        env.update({"GAME_AGENT_PORT": str(self.port), "GAME_AGENT_TOKEN": self.token,
                    "GAME_AGENT_CAPTURE": str(self.frames.resolve())})
        env["GAME_AGENT_RECORDING_MODE"] = "player_recording" if getattr(self, "native_recording", False) else ""
        self.log = (self.output / "godot.log").open("w")
        viewport = getattr(self, "policy_viewport", [960, 540])
        argv = [self.config["godot"], "--path", str(self.runtime_project),
                "--resolution", f"{viewport[0]}x{viewport[1]}"]
        if getattr(self, "policy_scenario", None):
            argv += ["--", "--scenario", self.policy_scenario]
        from .host import popen_kwargs
        self.process = subprocess.Popen(argv, cwd=self.runtime_project, env=env, stdin=subprocess.DEVNULL,
            stdout=self.log, stderr=subprocess.STDOUT, **popen_kwargs())
        until = time.monotonic() + 20  # Startup health check, not a rollout time limit.
        while True:
            self.check_stop()
            if self.process.poll() is not None:
                raise RuntimeError("Godot exited before its bridge became ready")
            try:
                ready = self.rpc("ready", {})
                break
            except (OSError, ValueError):
                if time.monotonic() > until:
                    raise RuntimeError("Godot bridge startup failed") from None
                time.sleep(.1)
        user_data_dir = ready.get("user_data_dir", "")
        self.runtime_info["user_data_dir"] = user_data_dir
        atomic_json(self.output / "runtime.json", self.runtime_info)
        if not isinstance(user_data_dir, str) or not user_data_dir.replace("\\", "/").endswith("/" + self.user_dir_name):
            raise RuntimeError("Godot user:// isolation was not applied; see runtime.json")

    def rpc(self, method, params):
        request_id = uuid.uuid4().hex
        with socket.create_connection(("127.0.0.1", self.port), timeout=5) as client:
            client.settimeout(getattr(self, "policy_health_timeout", 10))
            client.sendall((json.dumps({"id": request_id, "token": self.token,
                                       "method": method, "params": params}) + "\n").encode())
            with client.makefile("rb") as stream:
                line = stream.readline(10 * 1024 * 1024)
            response = json.loads(line)
        if response.get("id") != request_id or response.get("error"):
            raise ValueError("Godot bridge rejected request: " + str(response.get("error")))
        return response["result"]

    def reset(self, seed):
        self.rpc("reset", {"seed": seed})

    def observe_state(self):
        public = self.rpc("state", {})
        # Read only newly appended diagnostics during the fast control loop.
        path = self.output / "godot.log"
        if path.exists():
            with path.open(errors="replace") as stream:
                stream.seek(getattr(self, "diagnostic_offset", 0))
                lines = stream.read()
                self.diagnostic_offset = stream.tell()
            if "SCRIPT ERROR:" in lines or "ERROR:" in lines:
                raise ValueError("Godot runtime logged errors; see godot.log")
        return public

    def policy_act(self, action):
        self.rpc("policy_act", self.prepare_action(action))
        self.check_stop()

    def release_inputs(self):
        self.rpc("release", {})

    def observe(self):
        name = f"observe-{uuid.uuid4().hex}.png"
        public = self.rpc("observe", {"file": name})
        log_path = self.output / "godot.log"
        if log_path.exists():
            with log_path.open(errors="replace") as stream:
                if any("SCRIPT ERROR:" in line or "ERROR:" in line for line in stream):
                    raise ValueError("Godot runtime logged errors; see godot.log")
        image = self.frames / name
        if not image.is_file():
            raise ValueError("Godot did not capture a viewport image")
        return {"public": public, "image": image}

    def act(self, action):
        action = self.prepare_action(action)
        self.rpc("act", action)
        self.check_stop()

    def stop(self):
        video = None
        try:
            if hasattr(self, "process"):
                if self.process.poll() is None:
                    try:
                        self.rpc("stop", {})
                    except (OSError, ValueError):
                        pass
                terminate(self.process)
            if hasattr(self, "log"):
                self.log.close()
            if getattr(self, "native_recording", False):
                from .gamecraft_recordings import finish_capture
                return finish_capture(self)
            if hasattr(self, "frames") and (self.frames / "timing.jsonl").exists():
                timings = [json.loads(line) for line in (self.frames / "timing.jsonl").read_text().splitlines()]
                if len(timings) >= 2:
                    # Use actual capture intervals; never speed up a slow or stalled game.
                    lines = []
                    for first, second in zip(timings, timings[1:]):
                        lines += [f"file '{first['file']}'", f"duration {max(.001, second['time'] - first['time']):.6f}"]
                    lines += [f"file '{timings[-1]['file']}'", f"duration {1 / VIDEO_FPS:.6f}",
                              f"file '{timings[-1]['file']}'"]
                    (self.frames / "frames.txt").write_text("\n".join(lines) + "\n")
                    video = self.output / "play.mp4"
                    self.runner.run([self.config["ffmpeg"], "-y", "-loglevel", "error", "-f", "concat",
                        "-safe", "1", "-i", "frames.txt", "-vf", VIDEO_FILTER,
                        "-pix_fmt", "yuv420p", "-fps_mode", "cfr", str(video)], self.frames,
                        self.output / "encode.log")
                    artifact(video, self.output)
        finally:
            if hasattr(self, "process"):
                terminate(self.process)
        return {"path": video, "mode": "real_time_sampled_2fps", "origin": "engine",
                "capture": {"width": VIDEO_WIDTH, "height": VIDEO_HEIGHT, "fps": VIDEO_FPS,
                            "method": "viewport sampled at target 2 FPS; timestamps preserved",
                            "bridge_sha256": getattr(self, "capture_bridge_hash", None)}} if video else None


class MockGame(Engine):
    """Protocol fixture; its evidence is explicitly marked simulated."""
    def configure_policy(self, source, **settings):
        self.native_settings = settings

    def native_policy_trace(self):
        return self.output / "policy-trajectory.jsonl"

    def native_policy_start(self, **settings):
        # Fixed fixture only, not a GDScript interpreter. Never used for real Godot.
        action = {"keys": ["ArrowRight"], "duration_ms": 16, "pointer": None}
        rows = [{"type": "initial", "state": {"x": self.x}, "time_seconds": 0}]
        for i in range(2):
            before = {"x": self.x}
            rows.append({"type": "input", "index": i, "before": before, "action": action,
                         "reason": "mock", "time_seconds": .001 + i * .001})
            self.policy_act(action)
            rows.append({"type": "feedback", "index": i, "before": before, "after": {"x": self.x},
                         "action": action, "action_start_seconds": .001 + i * .001,
                         "time_seconds": .001 + i * .001})
        rows.append({"type": "end", "status": "completed", "reason": "mock", "time_seconds": .003})
        with self.native_policy_trace().open("w") as stream:
            for row in rows:
                stream.write(json.dumps({"version": settings["version"], "policy_sha256": settings["policy_sha256"], **row}) + "\n")

    def native_policy_status(self):
        return {"status": "completed", "reason": "mock", "steps": 2, "elapsed_seconds": .003}

    def build(self):
        return {"passed": True, "checks": 1, "scope": "mock"}

    def start(self):
        self.x = 0

    def reset(self, seed):
        self.x = 0

    def observe(self):
        image = self.output / f"mock-{uuid.uuid4().hex}.png"
        # Fixture bytes are never presented as real gameplay or rendered video.
        image.write_bytes(b"mock observation " + str(self.x).encode())
        return {"public": {"x": self.x}, "image": image}

    def observe_state(self):
        return {"x": self.x}

    def policy_act(self, action):
        self.act(action)

    def release_inputs(self):
        pass

    def act(self, action):
        validate(action, ACTION_SCHEMA)
        self.x += int("ArrowRight" in action["keys"])

    def stop(self):
        video = self.output / "mock-video.txt"
        video.write_text("SIMULATED EVIDENCE; no engine was launched")
        return {"path": video, "mode": "simulated", "origin": "mock"}


def make_game(config, project, output, stop=lambda: None):
    if config["backend"] != "mock" and config.get("runtime", {}).get("mode") == "docker":
        from .runtime import DockerGame
        game = DockerGame(config, project, output, stop)
    else:
        cls = MockGame if config["backend"] == "mock" else HTMLGame if config["engine"] == "html" else GodotGame
        game = cls(config, project, output, stop)
    from .resources import limit_game
    return limit_game(game, config, stop)


def install_bridge(project: Path, engine: str):
    source = PACKAGE / "bridges" / ("game_agent_bridge.js" if engine == "html" else "game_agent_bridge.gd")
    shutil.copy2(source, project / source.name)
    if engine == "godot":
        shutil.copy2(PACKAGE / "bridges/godot_policy_runner.gd", project / "godot_policy_runner.gd")


class PlaySession:
    def __init__(self, game, job, version, seed, max_actions, stop=lambda: None):
        self.game, self.job, self.version, self.seed = game, job, version, seed
        self.io = job / "io"
        self.evidence = job / "evidence"
        self.max_actions, self.check_stop = max_actions, stop
        self.steps = []
        self.rejected_inputs = []
        self.served = set()
        for path in (self.io / "requests", self.io / "responses", self.io / "frames", self.evidence):
            path.mkdir(parents=True, exist_ok=True)

    def observe(self):
        observation = self.game.observe()
        if not isinstance(observation["public"], dict):
            raise ValueError("Public observation must be an object")
        name = f"{uuid.uuid4().hex}.png"
        private = self.evidence / name
        shutil.copy2(observation["image"], private)
        shutil.copy2(private, self.io / "frames" / name)
        evidence = {"public": observation["public"], "image": artifact(private, self.job)}
        exposed = {"public": observation["public"], "image": "frames/" + name}
        return evidence, exposed

    def serve(self):
        self.check_stop()
        for path in sorted((self.io / "requests").glob("*.json")):
            if path.name in self.served:
                continue
            request = validate(read_json(path), REQUEST_SCHEMA)
            if path.stem != request["id"]:
                raise ValueError("Request id must match its filename")
            self.served.add(path.name)
            if self.max_actions is not None and len(self.served) > self.max_actions * 3:
                raise ValueError("Player exceeded observation request limit")
            if request["method"] == "act":
                if request["action"] is None:
                    raise ValueError("Act requires an action")
                if self.max_actions is not None and len(self.steps) >= self.max_actions:
                    atomic_json(self.io / "responses" / path.name, {"id": request["id"], "error": "action limit reached"})
                    continue
                # Only preflight errors are recoverable. Runtime/capture/transport
                # failures may follow real input and must not be blindly retried.
                try:
                    action = self.game.prepare_action(request["action"])
                except InputRejected as exc:
                    response = {"id": request["id"], "error": str(exc),
                                "recoverable": True, "executed": False,
                                "input_capabilities": self.game.input_capabilities(),
                                "hint": "Correct the input and send a new request id; this action was not executed."}
                    self.rejected_inputs.append({"id": request["id"], "version": self.version,
                        "requested_action": request["action"], "error": str(exc), "executed": False})
                    atomic_json(self.job / "rejected-inputs.json", self.rejected_inputs)
                    atomic_json(self.io / "responses" / path.name, response)
                    continue
                before, _ = self.observe()
                self.game.act(action)
                after, exposed = self.observe()
                self.steps.append({"id": request["id"], "version": self.version,
                    "requested_action": request["action"], "action": action,
                    "before": before, "after": after})
                atomic_json(self.job / "steps.json", self.steps)
                exposed["executed_action"] = action
            else:
                _, exposed = self.observe()
            atomic_json(self.io / "responses" / path.name, {"id": request["id"], **exposed})

    def run(self, agent):
        video = None
        try:
            self.game.start()
            self.game.reset(self.seed)
            initial, exposed = self.observe()
            atomic_json(self.io / "initial.json", exposed)
            summary = agent.call("player", self.job, {"max_actions": self.max_actions,
                "initial": "initial.json", "input_capabilities": self.game.input_capabilities(),
                "protocol": "requests/<id>.json -> responses/<id>.json"}, tick=self.serve)
            self.serve()
        finally:
            video = self.game.stop()
        if video is None:
            raise ValueError("No playable video evidence captured")
        report = {"version": self.version, "seed": self.seed, "initial": initial,
            "steps": self.steps, "rejected_inputs": self.rejected_inputs,
            "summary": summary, "video": {"artifact": artifact(video["path"], self.job),
            "mode": video["mode"], "origin": video["origin"]}}
        if "capture" in video:
            report["video"]["capture"] = video["capture"]
        atomic_json(self.job / "play.json", report)
        return report


def valid_play(report, root, expected_version, *, allow_mock=False):
    if report.get("format") == "policy-v1":
        from .policy import valid_policy_play
        return valid_policy_play(report, root, expected_version, allow_mock=allow_mock)
    try:
        if report["version"] != expected_version or not report["steps"]:
            return False
        if not report.get("summary") or not verify_artifact(report["initial"]["image"], root):
            return False
        if report["video"]["origin"] != "engine" and not allow_mock:
            return False
        if not verify_artifact(report["video"]["artifact"], root):
            return False
        for step in report["steps"]:
            validate(step["action"], ACTION_SCHEMA)
            if "requested_action" in step:
                validate(step["requested_action"], ACTION_SCHEMA)
            if step["version"] != expected_version:
                return False
            if not all(verify_artifact(step[side]["image"], root) for side in ("before", "after")):
                return False
        return True
    except (KeyError, TypeError, ValueError, ValidationError):
        return False
