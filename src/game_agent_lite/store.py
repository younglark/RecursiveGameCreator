"""Durable JSON, OS locks, immutable content identities and safe snapshots."""
import contextlib
import ctypes
import sys
from datetime import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import time
from typing import Iterator

SECRETS: set[str] = set()
# Reserved tooling/cache paths, not game source or evaluation evidence.
IGNORED = {".git", ".codex", ".godot", "node_modules", "__pycache__", ".venv",
           ".runtime", ".browsers", ".npm-cache", "test-results", "playwright-report",
           ".rgc-output"}


class Stopped(Exception):
    pass


class UncertainSubmission(Exception):
    pass


def redact(value):
    if isinstance(value, dict):
        return {k: ("[REDACTED]" if re.search(r"(?i)(api.?key|authorization|access_token|refresh_token|password)", k)
                    else redact(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    if not isinstance(value, str):
        return value
    for secret in SECRETS:
        if secret:
            value = value.replace(secret, "[REDACTED]")
    value = re.sub(r"(?i)(Bearer\s+)\S+", r"\1[REDACTED]", value)
    value = re.sub(r"\bsk-[A-Za-z0-9_-]+", "[REDACTED]", value)
    value = re.sub(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", "[REDACTED]", value)
    return re.sub(r"(?i)((?:api[_-]?key|HUNYUAN_API_KEY|access_token|refresh_token)\s*[=:]\s*)[^\s,\"}]+",
                  r"\1[REDACTED]", value)


def atomic_json(path: Path, value, *, private: bool = False):
    """Private provider receipts may contain signed URLs, never credentials."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value if private else redact(value), ensure_ascii=False,
                         indent=2, allow_nan=False) + "\n"
    fd, name = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(name).unlink(missing_ok=True)


def read_json(path: Path):
    return json.loads(path.read_text())


def safe_path(root: Path, relative: str) -> Path:
    candidate = (root / relative).resolve()
    if Path(relative).is_absolute() or not candidate.is_relative_to(root.resolve()):
        raise ValueError("Path must stay inside its workspace")
    return candidate


def ignored(path: Path) -> bool:
    return any(p in IGNORED or p.startswith(".env") for p in path.parts)


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def file_paths(root: Path) -> list[str]:
    if not root.is_dir():
        raise FileNotFoundError(f"Snapshot source directory is missing: {root}")
    files = []

    def fail(error):
        raise error

    # Prune before traversal: browser caches can be huge and contain native-library
    # symlinks. Still reject every symlink in the actual source/asset tree.
    for directory, dirs, names in os.walk(root, topdown=True, followlinks=False, onerror=fail):
        dirs[:] = sorted(d for d in dirs if not ignored((Path(directory) / d).relative_to(root)))
        for name in [*dirs, *sorted(names)]:
            path = Path(directory) / name
            relative = path.relative_to(root)
            if ignored(relative):
                continue
            if path.is_symlink():
                raise ValueError(f"Snapshot does not accept symlinks: {relative}")
            if path.is_file():
                files.append(relative.as_posix())
    return sorted(files)


def inventory(root: Path) -> dict[str, str]:
    return {name: file_hash(root / name) for name in file_paths(root)}


def version(root: Path) -> str:
    return hashlib.sha256(json.dumps(inventory(root), sort_keys=True).encode()).hexdigest()


def copy_file(source: Path, target: Path):
    """Independent file with shared blocks on APFS/Btrfs/XFS, portable copy elsewhere.

    No symlinks or hardlinks: writes to a role's view cannot mutate the original.
    Never replace an existing target with clonefile/ioctl; copy2 handles that case.
    """
    source, target = Path(source), Path(target)
    if not target.exists() and not target.is_symlink():
        if sys.platform == "darwin":
            library = ctypes.CDLL(None, use_errno=True)
            clone = getattr(library, "clonefile", None)
            if clone is not None:
                clone.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int]
                clone.restype = ctypes.c_int
                if clone(os.fsencode(source), os.fsencode(target), 0) == 0:
                    return target
        elif sys.platform.startswith("linux"):
            created = False
            try:
                with source.open("rb") as src, target.open("xb") as dst:
                    created = True
                    fcntl.ioctl(dst.fileno(), 0x40049409, src.fileno())  # FICLONE
                shutil.copystat(source, target)
                return target
            except OSError:
                if created:
                    target.unlink(missing_ok=True)
    return Path(shutil.copy2(source, target))


def snapshot(source: Path | None, target: Path):
    if target.exists():
        raise FileExistsError(target)
    if source is not None and target.resolve().is_relative_to(source.resolve()):
        raise ValueError("Snapshot destination cannot be inside source")
    target.mkdir(parents=True)
    if source is None:
        return
    for relative in file_paths(source):
        dest = target / relative
        dest.parent.mkdir(parents=True, exist_ok=True)
        copy_file(source / relative, dest)


def artifact(path: Path, root: Path) -> dict:
    if not path.is_file() or not path.stat().st_size:
        raise ValueError(f"Empty/missing evidence: {path.name}")
    return {"path": path.relative_to(root).as_posix(), "sha256": file_hash(path)}


def verify_artifact(item: dict, root: Path) -> bool:
    try:
        return artifact(safe_path(root, item["path"]), root) == item
    except (OSError, KeyError, TypeError, ValueError):
        return False


class Store:
    def __init__(self, root: Path):
        self.root = root.resolve()

    def create(self, config: dict):
        self.root.mkdir(parents=True, exist_ok=False)
        from .paths import portable_config
        atomic_json(self.root / "config.json", portable_config(config, self.root))
        if config.get("source"):
            staging = self.root / "seed-pending"
            snapshot(Path(config["source"]), staging)
            staging.rename(self.root / "seed")
        self.save({"schema_version": 1, "status": "ready", "round": 0,
                   "phase": "design", "best": None, "history": [], "error": None})

    def load(self) -> dict:
        state = read_json(self.root / "state.json")
        if state.get("schema_version") != 1:
            raise ValueError("Unsupported state schema")
        return state

    def save(self, state: dict):
        atomic_json(self.root / "state.json", state)

    @contextlib.contextmanager
    def lock(self) -> Iterator[None]:
        # Never unlink the lock inode: another process may already be waiting on it.
        with (self.root / "run.lock").open("a+") as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError("This run already has an active controller") from None
            try:
                stream.seek(0)
                stream.truncate()
                stream.write(str(os.getpid()))
                stream.flush()
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)

    def event(self, kind: str, **data):
        now = time.time()
        event = redact({"time": now,
                        "timestamp": datetime.fromtimestamp(now).astimezone().isoformat(
                            sep=" ", timespec="seconds"),
                        "kind": kind, **data})
        with (self.root / "events.jsonl").open("a") as stream:
            stream.write(json.dumps(event, ensure_ascii=False) + "\n")
            stream.flush()
        labels = {"stage_started": "开始", "stage_completed": "完成",
                  "stage_failed": "失败", "stage_stopped": "停止",
                  "stage_blocked": "等待核对"}
        if kind in labels:
            message = f"round={event.get('round', '?')} {event.get('phase', '?')} {labels[kind]}"
            if "duration_seconds" in event:
                seconds = event["duration_seconds"]
                hours, rest = divmod(int(seconds), 3600)
                minutes, remainder = divmod(rest, 60)
                duration = (f"{hours}时{minutes}分{remainder}秒" if hours else
                            f"{minutes}分{remainder}秒" if minutes else f"{seconds:.2f}秒")
                message += f"，本次耗时 {duration}"
            if event.get("error"):
                message += f"；{event['error']}"
        elif kind == "run_state":
            message = f"实验状态：{event['status']}"
            if event.get("error"):
                message += f"；{event['error']}"
        else:
            return
        # Losing the console must not turn a committed stage into a failure.
        with contextlib.suppress(OSError, ValueError):
            print(f"[{event['timestamp']}] {message}", flush=True)
        # Formatting/accounting errors must not change a committed stage's status.
        with contextlib.suppress(Exception):
            from .usage import describe
            usage = event.get("usage") or {}
            for key, label in (("stage", "本阶段累计"), ("round", "本轮累计"), ("total", "实验累计")):
                if key in usage:
                    print(f"[usage] {label}：{describe(usage[key])}", flush=True)

    def check_stop(self):
        if (self.root / "STOP").exists():
            raise Stopped("Stop requested; resume continues from the saved stage")

    def request_stop(self):
        (self.root / "STOP").touch()

    def round_dir(self, number: int) -> Path:
        path = self.root / "rounds" / f"{number:04d}"
        path.mkdir(parents=True, exist_ok=True)
        return path
