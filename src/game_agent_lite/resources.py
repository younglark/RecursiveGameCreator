"""Per-user host game limit; OS leases are released when a process exits."""
from contextlib import contextmanager
import json
from pathlib import Path
import tempfile
import time
import uuid

from .host import lock_file, slot_owner, unlock_file


class GameLease:
    def __init__(self, limit, stop=lambda: None, root=None):
        self.limit, self.stop = limit, stop
        self.root = Path(root) if root else Path(tempfile.gettempdir()) / f"rgc-game-slots-{slot_owner()}"
        self.file = self.path = None

    @contextmanager
    def gate(self):
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (self.root / "gate.lock").open("a+") as lock:
            while True:
                try:
                    lock_file(lock)
                    break
                except BlockingIOError:
                    self.stop()
                    time.sleep(.05)
            try:
                yield
            finally:
                unlock_file(lock)

    def acquire(self):
        if self.file is not None:
            return
        while True:
            self.stop()
            with self.gate():
                limits = []
                stale = []
                for path in self.root.glob("slot-*.lock"):
                    with path.open("r+") as active:
                        try:
                            lock_file(active)
                        except BlockingIOError:
                            parts = path.name.split("-")
                            if len(parts) >= 3 and parts[1].isdigit():
                                limits.append(int(parts[1]))
                        else:
                            unlock_file(active)
                            stale.append(path)
                for path in stale:
                    path.unlink(missing_ok=True)
                # Respect the most restrictive active run, even across batches.
                if len(limits) < min([self.limit, *limits]):
                    self.path = self.root / f"slot-{int(self.limit)}-{uuid.uuid4().hex}.lock"
                    self.file = self.path.open("x+")
                    lock_file(self.file, block=True)
                    json.dump({"limit": self.limit}, self.file)
                    self.file.flush()
                    return
            time.sleep(.1)

    def release(self):
        if self.file is not None:
            # Closing releases the lease even after cancellation. Stale filenames
            # are reclaimed under the gate by the next acquirer.
            self.file.close()
            self.file = None


def limit_game(game, config, stop):
    limit = config.get("runtime", {}).get("max_game_instances")
    if limit is None or config["backend"] == "mock":
        return game
    lease = GameLease(limit, stop)
    original_start, original_stop = game.start, game.stop
    def start():
        lease.acquire()
        try:
            return original_start()
        except BaseException:
            try:
                original_stop()
            finally:
                lease.release()
            raise
    def finish():
        try:
            return original_stop()
        finally:
            lease.release()
    game.start, game.stop = start, finish
    return game
