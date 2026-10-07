"""Watch an existing controller and request stop when a round's select receipt lands.

For controllers already running without a round limit. This never launches a
rollout, signals unrelated processes, or changes game/evaluation state.
"""
import argparse
import fcntl
import os
from pathlib import Path
import time

from game_agent_lite.store import Store, atomic_json


def controller_active(store):
    with (store.root / "run.lock").open("a+") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(stream, fcntl.LOCK_UN)
        return False


def watch(root, target):
    store = Store(root)
    receipt = store.root / "rounds" / f"{target:04d}" / "select.json"
    output = store.root / "stop-after-round.json"
    with (store.root / "stop-after-round.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        record = {"pid": os.getpid(), "target_round": target, "status": "watching",
                  "started_at": time.time(), "stop_requested": False}
        atomic_json(output, record)
        try:
            while True:
                state = store.load()
                committed = any(item["round"] == target for item in state["history"])
                if not record["stop_requested"] and (receipt.is_file() or committed or state["round"] > target):
                    # select.json is committed before the controller finishes its
                    # final evidence validation and state update. STOP is consumed
                    # at the next stage boundary; selection itself can finish.
                    store.request_stop()
                    record.update(status="stop_requested", stop_requested=True, requested_at=time.time())
                    atomic_json(output, record)
                    store.event("round_stop_requested", target_round=target, watcher_pid=os.getpid())
                if not controller_active(store):
                    state = store.load()
                    committed = any(item["round"] == target for item in state["history"])
                    record.update(status="finished", ended_at=time.time(), target_committed=committed,
                                  run_status=state["status"], round=state["round"], phase=state["phase"],
                                  error=state["error"])
                    atomic_json(output, record)
                    return record
                time.sleep(.02)
        except Exception as exc:
            atomic_json(output, {**record, "status": "watcher_failed", "error": str(exc)})
            raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--after-round", type=int, required=True)
    args = parser.parse_args()
    if args.after_round < 0:
        parser.error("--after-round must be nonnegative")
    print(watch(args.run_dir, args.after_round), flush=True)
