"""Container-side JSON-lines engine worker. No provider keys or Docker socket."""
from copy import deepcopy
import contextlib
import json
import os
from pathlib import Path
import queue
import shutil
import signal
import subprocess
import sys
import threading
import time

from .agent import CommandRunner, child_env, terminate
from .game import GodotGame, HTMLGame
from .store import Stopped, atomic_json, read_json, redact, safe_path, snapshot, version
from .types import DEFAULTS

PROTOCOL = 2
OUTPUT = Path("/output")
SOURCE = Path("/source")
PACKAGE = Path(__file__).parent


class RuntimeWorker:
    def __init__(self, stop):
        self.check_stop = stop
        self.game = None
        self.display = None
        self.display_log = None
        self.purpose = None
        self.started = False
        self.closed = False
        self.video = None

    def encode(self, value):
        if isinstance(value, Path):
            return value.resolve().relative_to(OUTPUT.resolve()).as_posix()
        if isinstance(value, dict):
            return {k: self.encode(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.encode(v) for v in value]
        return value

    def start_display(self):
        Path("/tmp/.X11-unix").mkdir(mode=0o1777, exist_ok=True)
        self.display_log = (OUTPUT / "xvfb.log").open("w")
        viewport = getattr(self.game, "policy_viewport", [960, 540])
        self.display = subprocess.Popen(["Xvfb", ":99", "-screen", "0", f"{viewport[0]}x{viewport[1]}x24", "-nolisten", "tcp", "-ac"],
            stdin=subprocess.DEVNULL, stdout=self.display_log, stderr=subprocess.STDOUT,
            env=child_env(), start_new_session=True)
        deadline = time.monotonic() + 15
        while not Path("/tmp/.X11-unix/X99").exists():
            self.check_stop()
            if self.display.poll() is not None or time.monotonic() >= deadline:
                raise RuntimeError("Virtual display failed to start; see xvfb.log")
            time.sleep(.05)

    def copy_source(self):
        project = OUTPUT / "project"
        expected = version(SOURCE)
        snapshot(SOURCE, project)
        if version(project) != expected or version(SOURCE) != expected:
            raise ValueError("Source changed while preparing runtime copy")
        return project

    def dispatch(self, method, params):
        if method == "init":
            if self.purpose is not None or params["purpose"] not in ("build", "play", "blender", "media"):
                raise ValueError("Invalid runtime initialization")
            supplied = params["config"]
            allowed = {"engine", "entry", "build_command", "check_command", "headless"}
            if set(supplied) != allowed or supplied["engine"] not in ("html", "godot"):
                raise ValueError("Only HTML/Godot engine settings are accepted by this worker")
            framework = version(PACKAGE)
            if params["identity"]["protocol"] != PROTOCOL or params["identity"]["framework_sha256"] != framework:
                raise ValueError("Host/worker framework identity mismatch")
            for path in (Path(os.environ["HOME"]), Path(os.environ["XDG_RUNTIME_DIR"]), Path(os.environ["XDG_DATA_HOME"])):
                path.mkdir(parents=True, exist_ok=True)
            Path(os.environ["XDG_RUNTIME_DIR"]).chmod(0o700)
            self.purpose = params["purpose"]
            self.config = {**deepcopy(DEFAULTS), **supplied, "godot": "godot", "ffmpeg": "ffmpeg"}
            tools = {"protocol": PROTOCOL, "framework_sha256": framework,
                     "python_version": sys.version.split()[0], "purpose": self.purpose}
            if self.purpose == "blender":
                tools["blender_version"] = subprocess.check_output(
                    ["blender", "--version"], text=True, timeout=20, env=child_env()).splitlines()[0]
            elif self.purpose == "media":
                tools["ffmpeg_version"] = subprocess.check_output(
                    ["ffmpeg", "-version"], text=True, timeout=20, env=child_env()).splitlines()[0]
            elif supplied["engine"] == "godot":
                binary = shutil.which("godot")
                if binary is None:
                    raise RuntimeError("Godot is not installed in the runtime image")
                launcher = OUTPUT / "godot-runtime"
                launcher.write_text("#!/usr/bin/env python3\nimport os, sys\n"
                    f"os.execv({binary!r}, [{binary!r}, '--audio-driver', 'Dummy', *sys.argv[1:]])\n")
                launcher.chmod(0o755)
                self.config["godot"] = str(launcher)
                self.game = GodotGame(self.config, SOURCE, OUTPUT, self.check_stop)
                tools.update(godot_version=subprocess.check_output(["godot", "--version"],
                    text=True, timeout=20, env=child_env()).strip(), display=":99", audio_driver="Dummy")
            else:
                from importlib.metadata import version as package_version
                project = self.copy_source() if self.purpose == "play" else SOURCE
                self.game = HTMLGame(self.config, project, OUTPUT, self.check_stop)
                tools.update(playwright_version=package_version("playwright"),
                             display=None if supplied["headless"] else ":99")
                if self.purpose == "play":
                    atomic_json(OUTPUT / "runtime.json", {"project": project.relative_to(OUTPUT).as_posix(),
                        "source_version": version(SOURCE), "browser_profile": "isolated_context"})
            return tools
        if self.purpose is None or self.closed:
            raise ValueError("Runtime has not initialized or is closed")
        if method == "stop":
            return self.close()
        if method == "blender" and self.purpose == "blender":
            # Scripts may write relative temporary files, but never to the sealed source.
            project = self.copy_source()
            script = safe_path(project, params["script"])
            if not script.is_file() or script.suffix != ".py":
                raise ValueError("Blender requires an existing relative .py script")
            output_format = params.get("output_format", "glb")
            if output_format not in ("glb", "png"):
                raise ValueError("Unsupported Blender output format")
            destination = OUTPUT / ("asset." + output_format)
            CommandRunner().run(["blender", "--background", "--factory-startup", "--python-exit-code", "1",
                "--python", str(script), "--", "--output", str(destination)], project,
                OUTPUT / "blender.log", tick=self.check_stop)
            magic = b"glTF" if output_format == "glb" else b"\x89PNG\r\n\x1a\n"
            if not destination.read_bytes().startswith(magic):
                raise ValueError("Invalid Blender output: " + output_format)
            return {"path": destination}
        if method == "media" and self.purpose == "media":
            from .media import extract_frames
            video = safe_path(SOURCE, params["video"])
            return extract_frames(video, OUTPUT, OUTPUT, self.check_stop)
        if self.game is None:
            raise ValueError("Unsupported command for runtime tool")
        if method == "configure_policy" and self.purpose == "play" and not self.started and self.config["engine"] == "godot":
            self.game.configure_policy(**params)
            return {"configured": True}
        if method == "build" and self.purpose == "build":
            return self.game.build()
        if method == "start" and self.purpose == "play" and not self.started:
            if self.config["engine"] == "godot" or not self.config["headless"]:
                self.start_display()
            self.started = True  # Includes partially started engines for cleanup.
            self.game.start()
            if self.config["engine"] == "html":
                saved = read_json(OUTPUT / "runtime.json")
                atomic_json(OUTPUT / "runtime.json", {**saved, "browser_version": self.game.browser.version,
                    "headless": self.config["headless"]})
            return {"ready": True}
        if not self.started:
            raise ValueError("Game is not running")
        if method == "capture_diagnostic":
            path = OUTPUT / "diagnostic.png"
            return {"path": path if self.game.capture_diagnostic(path) else None}
        if method == "reset":
            return self.game.reset(params["seed"])
        if method == "native_policy_start" and self.config["engine"] == "godot":
            return self.game.native_policy_start(**params)
        if method == "native_policy_status" and self.config["engine"] == "godot":
            return self.game.native_policy_status()
        if method in ("observe", "observe_state", "release_inputs", "input_info"):
            return getattr(self.game, method)()
        if method in ("act", "policy_act"):
            return getattr(self.game, method)(params["action"])
        raise ValueError("Unsupported runtime command")

    def close(self):
        if self.closed:
            return self.video
        self.closed = True
        error = None
        try:
            if self.started:
                try:
                    self.game.release_inputs()
                except Exception:
                    pass
                self.video = self.encode(self.game.stop())
        except Exception as exc:
            error = redact(f"{type(exc).__name__}: {exc}")
        finally:
            if self.display is not None:
                terminate(self.display)
            if self.display_log is not None:
                self.display_log.close()
            atomic_json(OUTPUT / "runtime-shutdown.json", {"video": self.video, "error": error})
        if error:
            raise RuntimeError(error)
        return self.video


def main():
    wire = sys.stdout
    lock = threading.Lock()
    cancelled = threading.Event()
    inbox = queue.Queue()

    def emit(value):
        with lock:
            wire.write(json.dumps(value, allow_nan=False) + "\n")
            wire.flush()

    def read():
        try:
            for line in sys.stdin:
                inbox.put(line)
        finally:
            inbox.put(None)
            cancelled.set()

    def check_stop():
        if cancelled.is_set():
            raise Stopped("Container runtime stopped")

    signal.signal(signal.SIGTERM, lambda *_: cancelled.set())
    signal.signal(signal.SIGINT, lambda *_: cancelled.set())
    threading.Thread(target=read, daemon=True).start()
    worker = RuntimeWorker(check_stop)
    try:
        while not cancelled.is_set():
            try:
                line = inbox.get(timeout=.1)
            except queue.Empty:
                continue
            if line is None:
                break
            request = json.loads(line)
            request_id = request["id"]
            finished = threading.Event()

            def heartbeat():
                while not finished.wait(1):
                    try:
                        emit({"id": request_id, "heartbeat": True})
                    except (OSError, ValueError):
                        cancelled.set()
                        break

            pulse = threading.Thread(target=heartbeat, daemon=True)
            pulse.start()
            try:
                with contextlib.redirect_stdout(sys.stderr):
                    result = worker.encode(worker.dispatch(request["method"], request["params"]))
                reply = {"id": request_id, "result": result}
            except Exception as exc:
                reply = {"id": request_id, "error": redact(f"{type(exc).__name__}: {exc}")}
            finally:
                finished.set()
                pulse.join()
            emit(reply)
            if request["method"] == "stop":
                break
    finally:
        with contextlib.redirect_stdout(sys.stderr):
            worker.close()


if __name__ == "__main__":
    main()
