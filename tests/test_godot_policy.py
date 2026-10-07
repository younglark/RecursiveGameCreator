"""Native GDScript fixtures: no models, generated games or network services."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from game_agent_lite.agent import MockAgent, child_env
from game_agent_lite.game import MockGame, install_bridge, valid_play
from game_agent_lite.policy import PolicySession
from game_agent_lite.store import Store, artifact, atomic_json, read_json, version


def test_godot_selects_gd_and_reuses_completed_evidence(tmp_path, config):
    config.update(engine="godot", entry="project.godot")
    project = tmp_path / "game"
    project.mkdir()
    (project / "project.godot").write_text("config_version=5\n")
    session = PolicySession(config, project, tmp_path / "player", version(project), MockGame)
    assert session.native and session.policy_filename == "policy.gd"
    agent = MockAgent()
    report = session.run(agent)
    assert valid_play(report, session.job, session.version, allow_mock=True)
    assert report["policy"]["path"] == "policy.gd"
    assert report["settings"]["policy_backend"] == "godot"
    assert "execution" not in report["settings"]  # No implicit 20s cap.
    attempts = sorted(session.job.glob("episodes/*/attempt-*"))
    assert session.run(agent) == report
    assert sorted(session.job.glob("episodes/*/attempt-*")) == attempts
    assert agent.calls == ["player", "player"]
    inherited = PolicySession(config, project, tmp_path / "next", version(project), MockGame,
        parent={"path": session.job / "play.json", "version": session.version, "reference": "play.json"})
    result = inherited.run(agent)
    assert result["inheritance"]["mode"] == "reused" and not result["inheritance"]["author_called"]


POLICY = '''extends RefCounted
var steps := 0
func reset(_seed: int, _profile: String): steps = 0
func act(_state: Dictionary):
    steps += 1
    if steps > 3:
        return {"action": null, "done": true, "reason": "fixture covered"}
    return {"action": {"keys": ["ArrowRight"] if steps < 3 else [], "duration_ms": 40,
        "pointer": {"x": 0.5, "y": 0.5, "button": "left"} if steps == 1 else null},
        "done": false, "reason": "fixture input"}
'''


@pytest.mark.parametrize("mode", ["normal", "long_normal", "scene_change", "deadline", "invalid", "stop", "stop_pointer"])
def test_native_godot_runner_real_inputs_and_clock(tmp_path, mode):
    binary = shutil.which("godot")
    if binary is None:
        pytest.skip("Local Godot not installed")
    project = tmp_path / "fixture"
    project.mkdir()
    (project / "capture").mkdir()
    (project / "project.godot").write_text("config_version=5\n")
    install_bridge(project, "godot")
    policy = POLICY
    if mode in ("deadline", "stop"):
        policy = policy.replace("steps > 3", "steps > 100000")
    if mode == "stop_pointer":
        policy = policy.replace('"duration_ms": 40', '"duration_ms": 1000')
    if mode == "invalid":
        policy = policy.replace('"ArrowRight"', '"UnknownKey"')
    (project / "_rsi_policy.gd").write_text(policy)
    script = '''extends SceneTree
const Bridge = preload("res://game_agent_bridge.gd")
const Runner = preload("res://godot_policy_runner.gd")
class Target extends Node:
    var x := 0
    var downs := 0
    var ups := 0
    var clicks := 0
    func _physics_process(_dt):
        if Input.is_key_pressed(KEY_RIGHT): x += 1
    func _input(event):
        if event is InputEventKey and event.keycode == KEY_RIGHT:
            if event.pressed: downs += 1
            else: ups += 1
        if event is InputEventMouseButton and event.pressed: clicks += 1
    func agent_observe(): return {"x": x, "downs": downs, "ups": ups, "clicks": clicks}
func _initialize():
    create_timer(5).timeout.connect(func(): quit(2))
    run.call_deferred()
func run():
    var target = Target.new()
    root.add_child(target)
    target.add_to_group("game_agent_target")
    var bridge = Bridge.new()
    root.add_child(bridge)
    bridge.capture_dir = "res://capture"
    var runner = Runner.new()
    root.add_child(runner)
    runner.begin(bridge, {"version": "fixture", "policy_sha256": "fixture", "seed": 1,
        "profile": "test", "max_actions": null, "max_seconds": LIMIT, "aliases": {}})
    # Simulate elapsed recording time only; do not wait 21 seconds in a unit test.
    if "MODE" == "long_normal":
        runner.started_usec -= 21000000
        bridge.input_origin_usec = runner.started_usec
    if "MODE" == "scene_change":
        create_timer(0.05).timeout.connect(func():
            target.remove_from_group("game_agent_target")
            target.queue_free()
            var replacement = Target.new()
            root.add_child(replacement)
            replacement.add_to_group("game_agent_target")
        )
    if "MODE" in ["stop", "stop_pointer"]:
        create_timer(0.1).timeout.connect(func(): runner.finish("interrupted", "fixture stop"))
    while runner.running:
        await process_frame
    assert(not Input.is_key_pressed(KEY_RIGHT))
    assert(not Input.is_mouse_button_pressed(MOUSE_BUTTON_LEFT))
    var active = get_first_node_in_group("game_agent_target")
    print("RESULT=" + JSON.stringify({"receipt": runner.receipt, "state": active.agent_observe()}))
    quit()
'''.replace("LIMIT", "0.15" if mode == "deadline" else "null").replace("MODE", mode)
    (project / "test.gd").write_text(script)
    proc = subprocess.run([binary, "--headless", "--path", str(project), "--script", "res://test.gd"],
                          capture_output=True, text=True, env=child_env(), timeout=12)
    assert proc.returncode == 0 and "SCRIPT ERROR" not in proc.stderr, proc.stdout + proc.stderr
    result = json.loads(next(line[7:] for line in proc.stdout.splitlines() if line.startswith("RESULT=")))
    expected = {"normal": "completed", "long_normal": "completed", "scene_change": "completed", "deadline": "truncated",
                "invalid": "failed", "stop": "interrupted", "stop_pointer": "interrupted"}[mode]
    assert result["receipt"]["status"] == expected
    rows = [json.loads(line) for line in (project / "policy-trajectory.jsonl").read_text().splitlines()]
    assert rows[-1]["status"] == expected
    injected = [json.loads(line) for line in (project / "input_events.jsonl").read_text().splitlines()]
    assert injected[0]["type"] == "start" and injected[-1]["status"] == expected
    assert [r["sequence"] for r in injected] == list(range(len(injected)))
    assert [r["time_seconds"] for r in injected] == sorted(r["time_seconds"] for r in injected)
    assert injected[-1]["time_seconds"] <= result["receipt"]["elapsed_seconds"]
    times = [row["time_seconds"] for row in rows]
    assert times == sorted(times)
    if mode == "normal":
        assert result["receipt"]["steps"] == 3
        assert result["state"]["x"] > 0
        assert result["state"]["downs"] == result["state"]["ups"] == 1
        assert result["state"]["clicks"] == 1
        feedback = [r for r in rows if r["type"] == "feedback"]
        assert feedback[0]["action_start_seconds"] <= feedback[0]["pointer_release_seconds"] <= feedback[0]["time_seconds"]
    if mode == "invalid":
        assert result["state"]["x"] == result["state"]["clicks"] == 0
    if mode == "stop_pointer":
        assert [r["type"] for r in injected if r["type"] in ("mouse_down", "mouse_up")] == ["mouse_down", "mouse_up"]
        assert next(r for r in injected if r["type"] == "mouse_up")["action_index"] == -1
    if mode == "long_normal":
        assert result["receipt"]["elapsed_seconds"] > 21 and result["receipt"]["steps"] == 3


@pytest.mark.parametrize("payload", ["preview", "nan", "inf", "negative_inf", "node", "resource", "integer_key", "vector"])
def test_native_godot_preview_observation_contract(tmp_path, payload):
    binary = shutil.which("godot")
    if binary is None:
        pytest.skip("Local Godot not installed")
    project = tmp_path / "fixture"
    project.mkdir()
    (project / "capture").mkdir()
    (project / "project.godot").write_text("config_version=5\n")
    install_bridge(project, "godot")
    (project / "_rsi_policy.gd").write_text('''extends RefCounted
var steps := 0
var normalized := false
func reset(_seed: int, _profile: String): steps = 0
func act(observation: Dictionary):
    steps += 1
    if steps == 3:
        normalized = observation.keys().all(func(key): return key is String) and observation.preview.action is String
        observation.preview.board.turn = 999
        return {"action": null, "done": true, "reason": "preview observed"}
    return {"action": {"keys": [] if steps == 1 else ["Shift", "Space"], "duration_ms": 40,
        "pointer": {"x": 0.5, "y": 0.5, "button": "left"} if steps == 1 else null},
        "done": false, "reason": "start then preview"}
''')
    (project / "test.gd").write_text('''extends SceneTree
const Bridge = preload("res://game_agent_bridge.gd")
const Runner = preload("res://godot_policy_runner.gd")
class Target extends Node:
    var screen := "title"
    var preview: Dictionary = {}
    func _input(event):
        if event is InputEventMouseButton and event.pressed: screen = "play"
        if screen == "play" and event is InputEventKey and event.pressed and event.keycode == KEY_SPACE and Input.is_key_pressed(KEY_SHIFT):
            preview = {"action": &"wait", "board": {"turn": 1}}
            match "PAYLOAD":
                "nan": preview.invalid = NAN
                "inf": preview.invalid = INF
                "negative_inf": preview.invalid = -INF
                "node": preview.invalid = self
                "resource": preview.invalid = Resource.new()
                "integer_key": preview[1] = "invalid key"
                "vector": preview.invalid = Vector2.ONE
    func agent_observe():
        var out = {"screen": screen}
        if not preview.is_empty(): out.preview = preview
        return out
func _initialize():
    create_timer(5).timeout.connect(func(): quit(2))
    run.call_deferred()
func run():
    var target = Target.new()
    root.add_child(target)
    target.add_to_group("game_agent_target")
    var bridge = Bridge.new()
    root.add_child(bridge)
    bridge.capture_dir = "res://capture"
    var runner = Runner.new()
    root.add_child(runner)
    runner.begin(bridge, {"version": "fixture", "policy_sha256": "fixture", "seed": 1,
        "profile": "preview", "max_actions": null, "max_seconds": null, "aliases": {}})
    while runner.running:
        await process_frame
    print("RESULT=" + JSON.stringify({"receipt": runner.receipt, "normalized": runner.policy.normalized,
        "turn": target.preview.board.turn, "string_name_key": target.agent_observe().keys().any(func(key): return key is StringName),
        "released": not Input.is_key_pressed(KEY_SHIFT) and not Input.is_key_pressed(KEY_SPACE) and not Input.is_mouse_button_pressed(MOUSE_BUTTON_LEFT)}))
    quit()
'''.replace("PAYLOAD", payload))
    proc = subprocess.run([binary, "--headless", "--path", str(project), "--script", "res://test.gd"],
                          capture_output=True, text=True, env=child_env(), timeout=12)
    assert proc.returncode == 0 and "SCRIPT ERROR" not in proc.stderr, proc.stdout + proc.stderr
    result = json.loads(next(line[7:] for line in proc.stdout.splitlines() if line.startswith("RESULT=")))
    rows = [json.loads(line) for line in (project / "policy-trajectory.jsonl").read_text().splitlines()]
    feedback = [row for row in rows if row["type"] == "feedback"]
    assert rows[0]["state"]["screen"] == "title"
    assert feedback[0]["after"]["screen"] == "play"
    assert result["string_name_key"] and result["released"]
    assert result["turn"] == 1  # The Policy only receives a detached copy.
    if payload == "preview":
        assert result["receipt"]["status"] == rows[-1]["status"] == "completed"
        assert result["normalized"] and result["receipt"]["steps"] == 2
        assert feedback[1]["after"]["preview"] == {"action": "wait", "board": {"turn": 1}}
    else:
        assert result["receipt"]["status"] == rows[-1]["status"] == "failed"
        assert result["receipt"]["reason"] == "agent_observe must return finite JSON data"
        assert result["receipt"]["steps"] == len(feedback) == 1


def test_partial_native_trace_still_releases_and_stops(tmp_path, config):
    class BrokenTrace(MockGame):
        released = stopped = 0
        def native_policy_start(self, **settings):
            super().native_policy_start(**settings)
            with self.native_policy_trace().open("a") as stream:
                stream.write('{"partial":')
        def release_inputs(self):
            type(self).released += 1
        def stop(self):
            type(self).stopped += 1
            return super().stop()
    config.update(engine="godot", entry="project.godot")
    project = tmp_path / "source"
    project.mkdir()
    (project / "project.godot").write_text("config_version=5\n")
    session = PolicySession(config, project, tmp_path / "player", version(project), BrokenTrace)
    report = session.run(MockAgent())
    assert BrokenTrace.released == BrokenTrace.stopped == 4
    assert all(read_json(session.job / item["path"])["status"] == "failed" for item in report["episodes"])
    assert not valid_play(report, session.job, session.version, allow_mock=True)


def test_loop_replaces_incomplete_python_job_without_losing_it(tmp_path, config, monkeypatch):
    from game_agent_lite.loop import Loop
    config.update(engine="godot", entry="project.godot")
    store = Store(tmp_path / "run")
    store.create(config)
    directory = store.round_dir(0)
    project = directory / "integrate-fixture/project"
    project.mkdir(parents=True)
    (project / "project.godot").write_text("config_version=5\n")
    candidate = {"version": version(project), "project": project.relative_to(store.root).as_posix()}
    old = directory / "jobs/player-candidate-old"
    old.mkdir(parents=True)
    (old / "policy.py").write_text("# preserved legacy policy\n")
    atomic_json(old / "plan.json", {"summary": "old plan", "episodes": [
        {"profile": "old", "purpose": "coverage"}]})
    atomic_json(old / "policy.json", {"version": candidate["version"], "settings": {"engine": "godot"},
        "policy": artifact(old / "policy.py", old), "plan": artifact(old / "plan.json", old)})
    marker = directory / "player-session-candidate.json"
    atomic_json(marker, {"job": old.name, "version": candidate["version"]})
    before = {p.name: p.read_bytes() for p in old.iterdir()}
    cleanup = []
    monkeypatch.setattr("game_agent_lite.runtime.cleanup_runtimes", lambda path: cleanup.append(path))
    result = Loop(store, config, agent=MockAgent()).play(directory, candidate, "candidate")
    assert old in cleanup and read_json(marker)["previous_job"] == old.name
    assert {p.name: p.read_bytes() for p in old.iterdir()} == before
    assert read_json(store.root / result["report"])["policy"]["path"] == "policy.gd"


@pytest.mark.parametrize("backend", ["native", "rpc"])
def test_shared_inputs_modifiers_repress_pointer_and_cleanup(tmp_path, backend):
    binary = shutil.which("godot")
    if not binary:
        pytest.skip("Local Godot not installed")
    project = tmp_path / "shared"
    project.mkdir()
    (project / "capture").mkdir()
    (project / "project.godot").write_text("config_version=5\n")
    install_bridge(project, "godot")
    steps = [
        {"keys": keys, "duration_ms": 80, "pointer": pointer}
        for keys, pointer in [(["ArrowRight"], None), (["ArrowRight"], None), ([], None),
            (["ArrowRight", "Shift"], None), ([], None), (["ArrowRight"], None),
            (["Control"], {"x": .25, "y": .5, "button": "left"}),
            (["Control"], {"x": .1, "y": .2, "button": "left", "phase": "down"}),
            (["Control"], None),
            (["Control"], {"x": .7, "y": .8, "button": "none", "phase": "move"}),
            (["Control"], {"x": .7, "y": .8, "button": "left", "phase": "up"}),
            (["Control"], {"x": .4, "y": .6, "button": "left", "phase": "down"})]]
    (project / "_rsi_policy.gd").write_text('''extends RefCounted
var index := 0
var actions = JSON.parse_string('ACTIONS')
func reset(_seed: int, _profile: String): index = 0
func act(_state: Dictionary):
    if index == actions.size(): return {"action": null, "done": true, "reason": "fixture done"}
    var action = actions[index]
    index += 1
    return {"action": action, "done": false, "reason": "fixture"}
'''.replace("ACTIONS", json.dumps(steps)))
    (project / "test.gd").write_text('''extends SceneTree
const Bridge = preload("res://game_agent_bridge.gd")
const Runner = preload("res://godot_policy_runner.gd")
class Target extends Node:
    var rights: Array = []
    var mouse: Array = []
    var drags: Array = []
    func agent_reset(_seed): pass
    func agent_observe(): return {}
    func _input(event):
        if event is InputEventKey and event.keycode == KEY_RIGHT and event.pressed:
            rights.append([event.shift_pressed, Input.is_key_pressed(KEY_SHIFT), event.echo])
        if event is InputEventMouseButton:
            mouse.append([event.pressed, event.ctrl_pressed, Input.is_mouse_button_pressed(MOUSE_BUTTON_LEFT)])
        if event is InputEventMouseMotion and event.button_mask == MOUSE_BUTTON_MASK_LEFT:
            drags.append([event.relative.x, event.relative.y])
func _initialize():
    create_timer(6).timeout.connect(func(): quit(2))
    run.call_deferred()
func run():
    var target = Target.new()
    root.add_child(target)
    target.add_to_group("game_agent_target")
    var bridge = Bridge.new()
    root.add_child(bridge)
    bridge.capture_dir = "res://capture"
    if "BACKEND" == "native":
        var runner = Runner.new()
        root.add_child(runner)
        runner.begin(bridge, {"version": "fixture", "policy_sha256": "fixture", "seed": 1,
            "profile": "fixture", "max_actions": null, "max_seconds": null, "aliases": {}})
        while runner.running: await process_frame
        assert(runner.receipt.status == "completed")
    else:
        assert(bridge.begin_input_recording(Time.get_ticks_usec(), {"version": "fixture", "policy_sha256": "fixture"}))
        var actions = JSON.parse_string('ACTIONS')
        for i in range(actions.size()):
            bridge.input_action_index = i
            var result = await bridge._dispatch({"id": i, "token": bridge.token, "method": "policy_act", "params": actions[i]})
            assert(not result.has("error"))
        bridge.input_action_index = -1
        bridge._release_inputs()
        bridge.end_input_recording("completed", "fixture done")
    assert(bridge.held_codes.is_empty() and bridge.held_pointer.is_empty())
    assert(not Input.is_key_pressed(KEY_CTRL) and not Input.is_key_pressed(KEY_RIGHT))
    print("RESULT=" + JSON.stringify({"rights": target.rights, "mouse": target.mouse, "drags": target.drags}))
    quit()
'''.replace("ACTIONS", json.dumps(steps)).replace("BACKEND", backend))
    proc = subprocess.run([binary, "--headless", "--path", str(project), "--script", "res://test.gd"],
        capture_output=True, text=True, timeout=12, env=child_env())
    assert proc.returncode == 0 and "SCRIPT ERROR" not in proc.stderr, proc.stdout + proc.stderr
    result = json.loads(next(line[7:] for line in proc.stdout.splitlines() if line.startswith("RESULT=")))
    assert result["rights"] == [[False, False, False], [True, True, False], [False, False, False]]
    assert result["mouse"] == [[True, True, True], [False, True, False]] * 2 + [[True, True, True], [False, False, False]]
    assert any(dx > 0 and dy > 0 for dx, dy in result["drags"])
    rows = [json.loads(line) for line in (project / "input_events.jsonl").read_text().splitlines()]
    assert [r["action_index"] for r in rows if r.get("key") == "Right" and r["type"] == "key_down"] == [0, 3, 5]
    assert [r["key"] for r in rows if r["type"] == "key_down"] == ["Right", "Shift", "Right", "Right", "Ctrl"]
    assert any(row["type"] == "key_up" and row.get("key") == "Ctrl" and row["action_index"] == -1 for row in rows)
    mouse = [row for row in rows if row["type"] in ("mouse_down", "mouse_up")]
    assert [row["type"] for row in mouse] == ["mouse_down", "mouse_up"] * 3
    for row, position in zip(mouse, ([.25, .5], [.25, .5], [.1, .2], [.7, .8], [.4, .6], [.4, .6])):
        assert [row["x"], row["y"]] == pytest.approx(position)
    assert mouse[-1]["action_index"] == -1  # Session cleanup releases an unfinished drag.


@pytest.mark.parametrize("release_ms", [0, 120])
def test_optional_benchmark_release_duration_is_actually_executed(tmp_path, release_ms):
    binary = shutil.which("godot")
    if not binary:
        pytest.skip("Local Godot not installed")
    project = tmp_path / "paced"
    project.mkdir()
    (project / "capture").mkdir()
    (project / "project.godot").write_text("config_version=5\n")
    install_bridge(project, "godot")
    (project / "_rsi_policy.gd").write_text('''extends RefCounted
var index := 0
var keys = [["ArrowRight"], ["ArrowRight"], [], [], ["ArrowRight"]]
func reset(_seed, _profile): index = 0
func act(_state):
    if index == keys.size(): return {"action": null, "done": true, "reason": "done"}
    var action = {"keys": keys[index], "duration_ms": 32, "pointer": null}
    index += 1
    return {"action": action, "done": false, "reason": "input"}
''')
    (project / "test.gd").write_text('''extends SceneTree
const Bridge = preload("res://game_agent_bridge.gd")
const Runner = preload("res://godot_policy_runner.gd")
class Target extends Node:
    var steps := 0
    func agent_observe(): return {"steps": steps}
    func _input(event):
        if event is InputEventKey and event.pressed and event.keycode == KEY_RIGHT: steps += 1
func _initialize():
    create_timer(5).timeout.connect(func(): quit(2))
    run.call_deferred()
func run():
    var target = Target.new()
    root.add_child(target)
    target.add_to_group("game_agent_target")
    var bridge = Bridge.new()
    root.add_child(bridge)
    bridge.capture_dir = "res://capture"
    var runner = Runner.new()
    root.add_child(runner)
    runner.begin(bridge, {"version": "fixture", "policy_sha256": "fixture", "seed": 1,
        "profile": "test", "max_actions": null, "max_seconds": null, "aliases": {}, "min_key_release_ms": RELEASE_MS})
    while runner.running: await process_frame
    assert(target.steps == 2 and runner.receipt.status == "completed")
    quit()
'''.replace("RELEASE_MS", str(release_ms)))
    proc = subprocess.run([binary, "--headless", "--path", str(project), "--script", "res://test.gd"],
        capture_output=True, text=True, timeout=10, env=child_env())
    assert proc.returncode == 0 and "SCRIPT ERROR" not in proc.stderr, proc.stdout + proc.stderr
    rows = [json.loads(line) for line in (project / "policy-trajectory.jsonl").read_text().splitlines()]
    inputs = [r for r in rows if r["type"] == "input"]
    assert [r["requested_duration_ms"] for r in inputs] == [32] * 5
    assert [r["action"]["duration_ms"] for r in inputs] == [32, 32, max(32, release_ms), 32, 32]
    feedback = next(r for r in rows if r["type"] == "feedback" and r["index"] == 2)
    assert feedback["time_seconds"] - feedback["action_start_seconds"] >= max(32, release_ms) / 1000
