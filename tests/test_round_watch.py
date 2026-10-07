from pathlib import Path
import subprocess
import sys
import time

import pytest

from game_agent_lite.agent import child_env
from game_agent_lite.store import Store, atomic_json, read_json


@pytest.mark.parametrize("outcome", ["selected", "already_committed", "failed"])
def test_round_watcher_keeps_current_work_then_stops_at_selection(tmp_path, config, outcome):
    store = Store(tmp_path / "run")
    store.create(config)
    state = store.load()
    state.update(status="running", round=2, phase="review")
    store.save(state)
    script = Path(__file__).resolve().parents[1] / "scripts/stop_after_round.py"
    process = None
    try:
        with store.lock():
            process = subprocess.Popen([sys.executable, str(script), str(store.root), "--after-round", "2"],
                                       env=child_env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            deadline = time.monotonic() + 5
            output = store.root / "stop-after-round.json"
            while not output.exists() and time.monotonic() < deadline:
                assert process.poll() is None
                time.sleep(.01)
            assert read_json(output)["status"] == "watching"
            atomic_json(store.round_dir(2) / "review.json", {"reviews": []})
            time.sleep(.05)
            assert not (store.root / "STOP").exists()
            if outcome == "selected":
                atomic_json(store.round_dir(2) / "select.json", {"promoted": False})
            elif outcome == "already_committed":
                state.update(round=3, phase="design", history=[{"round": 2}])
                store.save(state)
            if outcome != "failed":
                while not (store.root / "STOP").exists() and time.monotonic() < deadline:
                    time.sleep(.01)
                assert (store.root / "STOP").exists()
                state.update(status="stopped", round=3, phase="design", history=[{"round": 2}])
            else:
                state.update(status="failed", error="test failure")
            store.save(state)
        stdout, stderr = process.communicate(timeout=5)
        assert process.returncode == 0, stdout + stderr
        result = read_json(output)
        assert result["status"] == "finished"
        assert result["target_committed"] == (outcome != "failed")
        assert result["stop_requested"] == (outcome != "failed")
        assert store.load() == state  # Watcher cannot forge stage completion or promotion.
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait()
