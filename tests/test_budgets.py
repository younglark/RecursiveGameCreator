"""Unlimited budgets use JSON null, while explicit caps and stopping still work."""
from pathlib import Path

import pytest
from jsonschema import ValidationError

from game_agent_lite.agent import MockAgent
from game_agent_lite.cli import load_config, main
from game_agent_lite.game import MockGame, PlaySession, valid_play
from game_agent_lite.loop import Loop
from game_agent_lite.store import Store, atomic_json, read_json


ASSET_KEYS = ("max_images", "max_models", "max_blender")
ACTION = {"keys": ["ArrowRight"], "duration_ms": 16, "pointer": None}


def assert_unlimited(config):
    assert config["rounds"] is None
    assert config["max_actions"] is None
    assert all(config["assets"][key] is None for key in ASSET_KEYS)


def test_prepare_persists_unlimited_defaults_without_calling_backend(tmp_path, monkeypatch):
    import game_agent_lite.cli as cli
    def forbidden(*args, **kwargs):
        pytest.fail("Preparing an unlimited run must not start a backend")
    monkeypatch.setattr(cli, "Loop", forbidden)
    cfg = tmp_path / "config.json"
    atomic_json(cfg, {"engine": "html", "brief": "test"})
    assert_unlimited(load_config(cfg))
    root = tmp_path / "run"
    assert main(["start", str(cfg), "--run-dir", str(root), "--prepare-only"]) == 0
    assert_unlimited(read_json(root / "config.json"))


@pytest.mark.parametrize("name", ["html.json", "card.json", "godot.json", "godot-3rounds.json"])
def test_live_examples_have_no_budget_overrides(name):
    assert_unlimited(load_config(Path(__file__).resolve().parents[1] / "examples" / name))


@pytest.mark.parametrize("limit", [None, 12345])
def test_null_and_explicit_large_budgets_load(tmp_path, limit):
    cfg = tmp_path / "config.json"
    atomic_json(cfg, {"engine": "html", "brief": "test", "rounds": limit,
        "max_actions": limit, "assets": {key: limit for key in ASSET_KEYS}})
    loaded = load_config(cfg)
    assert loaded["rounds"] == loaded["max_actions"] == limit
    assert all(loaded["assets"][key] == limit for key in ASSET_KEYS)


@pytest.mark.parametrize("key,bad", [("rounds", 0), ("max_actions", 0), ("max_images", -1),
                                    ("max_models", -1), ("max_blender", -1)])
def test_invalid_budgets_are_rejected(tmp_path, key, bad):
    cfg = tmp_path / "config.json"
    value = {"engine": "html", "brief": "test"}
    if key in ASSET_KEYS:
        value["assets"] = {key: bad}
    else:
        value[key] = bad
    atomic_json(cfg, value)
    with pytest.raises(ValidationError):
        load_config(cfg)


def test_unlimited_player_exceeds_action_and_observation_caps(tmp_path, config):
    class Player:
        def call(self, role, job, context, *, tick):
            assert context["max_actions"] is None
            for index in range(32):
                name = f"move-{index}"
                atomic_json(job / "io/requests" / f"{name}.json",
                            {"id": name, "method": "act", "action": ACTION})
                tick()
                assert "error" not in read_json(job / "io/responses" / f"{name}.json")
            for index in range(91):
                name = f"look-{index}"
                atomic_json(job / "io/requests" / f"{name}.json",
                            {"id": name, "method": "observe", "action": None})
                tick()
                assert "image" in read_json(job / "io/responses" / f"{name}.json")
            return {"experience": "simulated", "problems": [], "suggestions": []}
    job = tmp_path / "job"
    game = MockGame(config, tmp_path, job / "engine")
    session = PlaySession(game, job, "one", 42, None)
    report = session.run(Player())
    session.serve()  # Unlimited budgets do not re-execute duplicate request ids.
    assert len(report["steps"]) == game.x == 32
    assert len(session.served) == 123
    assert valid_play(report, job, "one", allow_mock=True)


def test_explicit_player_cap_is_preserved(tmp_path, config):
    job = tmp_path / "job"
    game = MockGame(config, tmp_path, job / "engine")
    game.start()
    session = PlaySession(game, job, "one", 42, 2)
    for index in range(3):
        atomic_json(job / "io/requests" / f"move-{index}.json",
                    {"id": f"move-{index}", "method": "act", "action": ACTION})
    session.serve()
    assert game.x == len(session.steps) == 2
    assert read_json(job / "io/responses/move-2.json")["error"] == "action limit reached"


def test_unlimited_loop_runs_past_three_rounds_and_can_be_stopped(tmp_path, config):
    config["rounds"] = None
    store = Store(tmp_path / "run")
    store.create(config)
    class StopAfterFourRounds(MockAgent):
        designs = 0
        def call(self, role, job, context, **kwargs):
            if role == "designer":
                assert all(context["asset_limits"][key] is None for key in ASSET_KEYS)
                self.designs += 1
                if self.designs == 5:
                    store.request_stop()
            return super().call(role, job, context, **kwargs)
    state = Loop(store, config, agent=StopAfterFourRounds()).run()
    assert state["status"] == "stopped"
    assert state["round"] == 4
    assert len(state["history"]) == 4
    assert store.load()["history"] == state["history"]
