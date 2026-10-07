"""Host input recovery against simulated engines; no rollout or engine process."""
from copy import deepcopy
from unittest.mock import Mock, call

import pytest

from game_agent_lite.game import GodotGame, HTMLGame, InputRejected, MockGame, PlaySession, valid_play
from game_agent_lite.store import atomic_json, read_json


ACTION = {"keys": ["Digit1"], "duration_ms": 100, "pointer": None}


class GodotFixture(GodotGame):
    """Use real host normalization with a simulated RPC and recording fixture."""
    start = MockGame.start
    reset = MockGame.reset
    observe = MockGame.observe
    stop = MockGame.stop

    def rpc(self, method, params):
        assert method == "act"
        # These inputs match names supported by the unchanged Godot bridge.
        assert params["keys"] == ["1"]
        self.x += 1
        return {"accepted": True}


def submit(job, tick, name, action):
    atomic_json(job / "io/requests" / f"{name}.json",
                {"id": name, "method": "act", "action": action})
    tick()
    return read_json(job / "io/responses" / f"{name}.json")


@pytest.mark.parametrize("keys,expected", [
    (["Digit1"], ["1"]), (["Digit0"], ["0"]), (["Digit9"], ["9"]),
    (["KeyW"], ["w"]), (["KeyA"], ["a"]), (["KeyZ"], ["z"]),
    (["Digit1", "1"], ["1"]), (["W", "KeyW", "w"], ["w"]),
    (["Shift", "KeyW"], ["Shift", "w"]),
    (["Ctrl", "Return", "Esc"], ["Control", "Enter", "Escape"]),
    ([" ", "Spacebar", "Space"], ["Space"]),
    (["ArrowLeft", "Tab", "?"], ["ArrowLeft", "Tab", "?"]),
])
def test_aliases_reach_rpc_without_changing_original(tmp_path, config, keys, expected):
    game = GodotGame(config, tmp_path, tmp_path / "engine")
    game.rpc = Mock()
    requested = {**ACTION, "keys": keys, "pointer": {"x": .2, "y": .4, "button": "left"}}
    original = deepcopy(requested)
    game.act(requested)
    game.rpc.assert_called_once_with("act", {**requested, "keys": expected})
    assert requested == original


@pytest.mark.parametrize("bad_key", ["Digit10", "KeyWW", "F12", "", "\n"])
def test_invalid_combination_sends_no_partial_keys_or_pointer(tmp_path, config, bad_key):
    game = GodotGame(config, tmp_path, tmp_path / "engine")
    game.rpc = Mock()
    with pytest.raises(InputRejected):
        game.act({**ACTION, "keys": ["Shift", "Digit1", bad_key],
                  "pointer": {"x": .5, "y": .5, "button": "left"}})
    game.rpc.assert_not_called()


def test_player_corrects_rejection_and_records_actual_input(tmp_path, config):
    config["engine"] = "godot"
    job = tmp_path / "job"
    game = GodotFixture(config, tmp_path, job / "engine")
    session = PlaySession(game, job, "sealed-version", 42, 1)

    class Player:
        def call(self, role, job, context, *, tick):
            assert context["input_capabilities"]["engine"] == "godot"
            assert context["input_capabilities"]["aliases"]["Digit1"] == "1"
            rejected = {**ACTION, "keys": ["Digit1", "NotAKey"]}
            error = submit(job, tick, "bad", rejected)
            assert error["executed"] is False and error["recoverable"] is True
            assert "NotAKey" in error["error"]
            assert "image" not in error and "version" not in error
            assert game.x == 0 and session.steps == []
            assert not (job / "steps.json").exists()
            tick()  # The rejected id must not be processed or recorded again.
            assert len(session.rejected_inputs) == 1
            response = submit(job, tick, "corrected", ACTION)
            assert response["executed_action"] == {**ACTION, "keys": ["1"]}
            assert response["public"]["x"] == 1 and "image" in response
            assert "error" not in response and "version" not in response
            tick()  # Successful requests must not be replayed either.
            assert game.x == 1
            return {"experience": "simulated correction", "problems": [], "suggestions": []}

    report = session.run(Player())
    step = report["steps"][0]
    assert step["requested_action"] == ACTION
    assert step["action"] == {**ACTION, "keys": ["1"]}
    assert step["before"]["public"]["x"] == 0 and step["after"]["public"]["x"] == 1
    assert report["steps"] == read_json(job / "steps.json")
    rejected = read_json(job / "rejected-inputs.json")
    assert report["rejected_inputs"] == rejected
    assert rejected[0]["version"] == "sealed-version" and rejected[0]["executed"] is False
    assert rejected[0]["requested_action"]["keys"] == ["Digit1", "NotAKey"]
    assert valid_play(report, job, "sealed-version", allow_mock=True)
    legacy = deepcopy(report)
    legacy.pop("rejected_inputs")
    legacy["steps"][0].pop("requested_action")
    assert valid_play(legacy, job, "sealed-version", allow_mock=True)


@pytest.mark.parametrize("failure", [TimeoutError("timed out"), RuntimeError("engine crashed"),
                                   ValueError("Godot bridge rejected request: unsupported key"),
                                   InputRejected("unexpected rejection after dispatch")])
def test_errors_after_preflight_are_not_mislabeled_as_unexecuted(tmp_path, config, failure):
    job = tmp_path / "job"
    game = GodotFixture(config, tmp_path, job / "engine")
    game.rpc = Mock(side_effect=failure)
    session = PlaySession(game, job, "one", 42, None)

    class Player:
        def call(self, role, job, context, *, tick):
            submit(job, tick, "move", ACTION)

    with pytest.raises(type(failure), match=str(failure)):
        session.run(Player())
    assert game.rpc.call_count == 1
    assert session.rejected_inputs == []
    assert not (job / "io/responses/move.json").exists()
    assert not (job / "play.json").exists()


def test_capture_failure_after_input_is_not_hidden(tmp_path, config):
    job = tmp_path / "job"
    game = GodotFixture(config, tmp_path, job / "engine")
    observe = game.observe
    game.observe = Mock(side_effect=lambda: observe())
    session = PlaySession(game, job, "one", 42, None)

    class Player:
        def call(self, role, job, context, *, tick):
            # The initial observation is complete. Fail after the next input.
            game.observe.side_effect = [observe(), ValueError("missing screenshot")]
            submit(job, tick, "move", ACTION)

    with pytest.raises(ValueError, match="missing screenshot"):
        session.run(Player())
    assert game.x == 1
    assert not session.steps and not session.rejected_inputs
    assert not (job / "io/responses/move.json").exists()


def test_only_rejected_requests_cannot_supply_play_evidence(tmp_path, config):
    job = tmp_path / "job"
    game = GodotFixture(config, tmp_path, job / "engine")

    class Player:
        def call(self, role, job, context, *, tick):
            submit(job, tick, "bad", {**ACTION, "keys": ["NotAKey"]})
            return {"experience": "no gameplay", "problems": [], "suggestions": []}

    report = PlaySession(game, job, "one", 42, None).run(Player())
    assert len(report["rejected_inputs"]) == 1 and report["steps"] == []
    assert not valid_play(report, job, "one", allow_mock=True)


def test_html_retains_playwright_keys_and_capabilities(tmp_path, config):
    game = HTMLGame(config, tmp_path, tmp_path / "engine")
    game.page = Mock()
    keys = ["Digit1", "KeyW", "Backspace", "F12"]
    game.act({**ACTION, "keys": keys})
    assert game.page.keyboard.down.call_args_list == [call(k) for k in keys]
    assert game.page.keyboard.up.call_args_list == [call(k) for k in keys]
    capabilities = game.input_capabilities()
    assert capabilities["engine"] == "html"
    assert "named_keys" not in capabilities
    assert "Playwright" in capabilities["key_format"]
