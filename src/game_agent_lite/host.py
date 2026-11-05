"""Process groups and file locks. POSIX behavior stays the default."""
import ctypes
import os
from pathlib import Path
import shutil
import stat
import queue
import signal
import subprocess
import sys
import threading
import time

WINDOWS = sys.platform == "win32"

ENV_ALLOW = {"PATH", "HOME", "USER", "LOGNAME", "TMPDIR", "SHELL", "LANG", "LC_ALL",
             "LC_CTYPE", "DISPLAY", "XDG_RUNTIME_DIR", "XDG_DATA_HOME", "XDG_CACHE_HOME",
             "LIBGL_ALWAYS_SOFTWARE", "LP_NUM_THREADS", "DOCKER_HOST", "DOCKER_CONTEXT",
             "DOCKER_CONFIG", "SYSTEMROOT", "PATHEXT", "USERPROFILE", "APPDATA", "LOCALAPPDATA"}


def child_env() -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key in ENV_ALLOW}
    if WINDOWS:
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
    return env


def popen_kwargs() -> dict:
    if WINDOWS:
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def remove_tree(path):
    """Delete a tree. Windows refuses unlink of the read-only copies Policy makes."""
    def onerror(func, name, _exc):
        os.chmod(name, stat.S_IWRITE)
        func(name)
    shutil.rmtree(path, onerror=onerror)


def path_inside(child, parent) -> bool:
    """False across Windows drive letters. pathlib raises ValueError there."""
    try:
        return Path(child).is_relative_to(parent)
    except ValueError:
        return False


def _kernel32():
    library = ctypes.WinDLL("kernel32", use_last_error=True)
    library.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
    library.CreateJobObjectW.restype = ctypes.c_void_p
    library.SetInformationJobObject.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
    library.SetInformationJobObject.restype = ctypes.c_int
    library.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    library.OpenProcess.restype = ctypes.c_void_p
    library.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    library.AssignProcessToJobObject.restype = ctypes.c_int
    library.TerminateJobObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    library.TerminateJobObject.restype = ctypes.c_int
    library.CloseHandle.argtypes = [ctypes.c_void_p]
    library.CloseHandle.restype = ctypes.c_int
    return library


def _assign_job(process):
    kernel32 = _kernel32()
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        return None

    class Basic(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", ctypes.c_uint32), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", ctypes.c_uint32),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", ctypes.c_uint32),
                    ("SchedulingClass", ctypes.c_uint32)]

    class Counters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_uint64) for name in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class Extended(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", Basic), ("IoInfo", Counters),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

    info = Extended()
    info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not kernel32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
        kernel32.CloseHandle(job)
        return None
    handle = kernel32.OpenProcess(0x1F0FFF, False, process.pid)
    if not handle or not kernel32.AssignProcessToJobObject(job, handle):
        if handle:
            kernel32.CloseHandle(handle)
        kernel32.CloseHandle(job)
        return None
    kernel32.CloseHandle(handle)
    return job


def _resume(process):
    ntdll = ctypes.WinDLL("ntdll")
    ntdll.NtResumeProcess.argtypes = [ctypes.c_void_p]
    ntdll.NtResumeProcess.restype = ctypes.c_long
    kernel32 = _kernel32()
    handle = kernel32.OpenProcess(0x0800, False, process.pid)
    if handle:
        ntdll.NtResumeProcess(handle)
        kernel32.CloseHandle(handle)


def popen(args, **kwargs):
    """Start a process in its own group. Windows keeps a job so grandchildren die with it."""
    if WINDOWS:
        flags = kwargs.pop("creationflags", 0) | subprocess.CREATE_NEW_PROCESS_GROUP | 0x4
        kwargs["creationflags"] = flags
    else:
        kwargs.update(popen_kwargs())
    process = subprocess.Popen(args, **kwargs)
    if not WINDOWS:
        return process
    try:
        process._rgc_job = _assign_job(process)
    finally:
        _resume(process)
    return process


def slot_owner() -> str:
    if WINDOWS:
        return os.environ.get("USERNAME") or "windows"
    return str(os.getuid())


def _ensure_lock_byte(stream):
    stream.seek(0, os.SEEK_END)
    if stream.tell() == 0:
        stream.write("\0")
        stream.flush()
    stream.seek(0)


def lock_file(stream, *, block=False):
    """Lock one byte at the start of an open file. Non-blocking raises BlockingIOError.

    Windows LK_LOCK stops after about ten attempts. A blocking wait retries
    LK_NBLCK until the holder leaves, matching POSIX flock.
    """
    if WINDOWS:
        import msvcrt
        _ensure_lock_byte(stream)
        while True:
            stream.seek(0)
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                return
            except OSError as exc:
                if not block:
                    raise BlockingIOError(str(exc)) from exc
                time.sleep(0.05)
    import fcntl
    flags = fcntl.LOCK_EX if block else fcntl.LOCK_EX | fcntl.LOCK_NB
    fcntl.flock(stream.fileno(), flags)


def unlock_file(stream):
    stream.seek(0)
    if WINDOWS:
        import msvcrt
        try:
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        return
    import fcntl
    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def terminate(process):
    if WINDOWS:
        job = getattr(process, "_rgc_job", None)
        kernel32 = _kernel32()
        if job:
            kernel32.TerminateJobObject(job, 1)
            kernel32.CloseHandle(job)
            process._rgc_job = None
        elif process.poll() is None:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(process.pid)],
                           capture_output=True, timeout=15)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        return
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


def _pipe_reader(stream, sink: queue.Queue):
    try:
        while True:
            data = os.read(stream.fileno(), 65536)
            sink.put(data)
            if not data:
                return
    except OSError as exc:
        sink.put(exc)


def ensure_windows_pipes(process):
    """One reader thread per standard stream. Safe to call again on the same process."""
    pipes = getattr(process, "_rgc_windows_pipes", None)
    if pipes is not None:
        return pipes
    pipes = {"out": queue.Queue(), "err": queue.Queue()}
    for stream, key in ((process.stdout, "out"), (process.stderr, "err")):
        threading.Thread(target=_pipe_reader, args=(stream, pipes[key]), daemon=True).start()
    process._rgc_windows_pipes = pipes
    return pipes


def exchange_windows(process, payload: bytes, *, buffer: bytearray, timeout: float, check_stop,
                     log, redact, max_bytes: int, interruptible: bool, on_line, eof_error: str,
                     limit_error: str = "Runtime response exceeds transport limit",
                     timeout_error: str = "Container worker heartbeat lost; action was not retried"):
    """Write one request and read response lines. Heartbeats return ('heartbeat', None)."""
    pipes = ensure_windows_pipes(process)
    deadline = time.monotonic() + timeout
    pending = payload
    written = threading.Event()
    write_error = []

    def write_stdin():
        try:
            view = pending
            while view:
                count = os.write(process.stdin.fileno(), view)
                view = view[count:]
            process.stdin.flush()
        except Exception as exc:
            write_error.append(exc)
        finally:
            written.set()

    threading.Thread(target=write_stdin, daemon=True).start()
    stderr_open = True
    while True:
        if interruptible:
            check_stop()
        if time.monotonic() >= deadline:
            raise TimeoutError(timeout_error)
        if written.is_set() and write_error:
            raise write_error[0]
        while stderr_open:
            try:
                err = pipes["err"].get_nowait()
            except queue.Empty:
                break
            if isinstance(err, Exception):
                raise err
            if not err:
                stderr_open = False
            else:
                log.write(redact(err.decode(errors="replace")))
                log.flush()
        try:
            chunk = pipes["out"].get(timeout=0.05)
        except queue.Empty:
            chunk = None
        if isinstance(chunk, Exception):
            raise chunk
        if chunk == b"":
            raise RuntimeError(eof_error)
        if chunk:
            buffer.extend(chunk)
            if len(buffer) > max_bytes:
                raise ValueError(limit_error)
        while b"\n" in buffer:
            newline = buffer.index(b"\n")
            line = bytes(buffer[:newline])
            del buffer[:newline + 1]
            kind, value = on_line(line)
            if kind == "heartbeat":
                deadline = time.monotonic() + timeout
                continue
            return value
