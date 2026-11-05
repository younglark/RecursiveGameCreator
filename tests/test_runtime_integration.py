"""Opt-in real Linux/Xvfb runs with a fixed policy; no models or asset API.

GAME_AGENT_RUNTIME_IMAGE=<locally-built-image> pytest tests/test_runtime_integration.py -ra
"""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import threading

import pytest

from game_agent_lite.agent import MockAgent
from game_agent_lite.game import install_bridge, make_game, valid_play
from game_agent_lite.policy import PolicySession
from game_agent_lite.runtime import DockerGame, cleanup_runtimes
from game_agent_lite.store import Stopped, atomic_json, file_hash, read_json, snapshot, version

pytestmark = pytest.mark.skipif(not os.environ.get("GAME_AGENT_RUNTIME_IMAGE"),
                               reason="opt-in real Docker/Xvfb fixture")


@pytest.fixture
def setup_game(tmp_path, config):
    config.update(engine="godot", backend="codex", entry="project.godot")
    config["runtime"].update(mode="docker", image=os.environ["GAME_AGENT_RUNTIME_IMAGE"],
                             cli=os.environ.get("GAME_AGENT_CONTAINER_CLI", "docker"))
    config["player"].update(mode="policy", trajectories_per_version=3, parallel_episodes=3)
    # No Codex dependency in a fixed policy test: this same image has Python.
    config["harbor_image"] = config["runtime"]["image"]
    project = tmp_path / "source"
    snapshot(Path(__file__).parent / "fixtures/godot", project)
    install_bridge(project, "godot")
    script = project / "main.gd"
    script.write_text(script.read_text() + '''
var clicks := 0
func _input(event: InputEvent) -> void:
\tif event is InputEventMouseButton and event.pressed:
\t\tclicks += 1
\t\tvar save = FileAccess.open("user://clicks.txt", FileAccess.WRITE)
\t\tsave.store_string(str(clicks))
''')
    script.write_text(script.read_text().replace('return {"x": x}',
        'return {"x": x, "clicks": clicks, "seed": test_seed, "saved": FileAccess.file_exists("user://clicks.txt")}')
        .replace('var x := 20.0', 'var x := 20.0\nvar test_seed := 0')
        .replace('func agent_reset(_seed: int) -> void:', 'func agent_reset(_seed: int) -> void:\n\ttest_seed = _seed'))
    build = DockerGame(config, project, tmp_path / "build").build()
    assert build["passed"] and build["runtime"]["mode"] == "docker"
    return config, project


def action(keys=(), duration=150, pointer=None):
    return {"keys": list(keys), "duration_ms": duration, "pointer": pointer}


@pytest.mark.parametrize("logical_size,stretch_mode", [
    ((960, 540), "canvas_items"),
    ((1280, 720), "canvas_items"),
    ((640, 360), "canvas_items"),
    ((1280, 1024), "canvas_items"),  # Letterboxing adds an offset as well as scale.
    ((1280, 720), "viewport"),
])
def test_pointer_hits_button_across_viewport_sizes(tmp_path, config, logical_size, stretch_mode):
    """Check actual game/GUI coordinates, not just receipt of a mouse event."""
    config.update(engine="godot", backend="codex", entry="project.godot")
    config["runtime"].update(mode="docker", image=os.environ["GAME_AGENT_RUNTIME_IMAGE"],
                             cli=os.environ.get("GAME_AGENT_CONTAINER_CLI", "docker"))
    project = tmp_path / "pointer-source"
    snapshot(Path(__file__).parent / "fixtures/godot", project)
    install_bridge(project, "godot")
    width, height = logical_size
    with (project / "project.godot").open("a") as stream:
        stream.write(f'''\n[display]
window/size/viewport_width={width}
window/size/viewport_height={height}
window/stretch/mode="{stretch_mode}"
window/stretch/aspect="keep"
''')
    (project / "main.gd").write_text('''extends Node2D
var clicks := 0
var events: Array = []
var last_motion := Vector2.ZERO
var button: Button

func _ready() -> void:
    add_to_group("game_agent_target")
    button = Button.new()
    button.position = get_viewport_rect().size * Vector2(0.18, 0.66) - Vector2(20, 15)
    button.size = Vector2(40, 30)
    button.pressed.connect(func(): clicks += 1)
    add_child(button)

func _input(event: InputEvent) -> void:
    if event is InputEventMouseMotion:
        last_motion = event.position
    if event is InputEventMouseButton:
        events.append({"x": event.position.x, "y": event.position.y,
            "pressed": event.pressed, "button": event.button_index,
            "held": Input.is_mouse_button_pressed(event.button_index)})

func agent_reset(_seed: int) -> void:
    clicks = 0
    events.clear()

func agent_observe() -> Dictionary:
    return {"clicks": clicks, "events": events, "hovered": button.is_hovered(),
        "motion": [last_motion.x, last_motion.y],
        "viewport": [get_viewport_rect().size.x, get_viewport_rect().size.y],
        "window": [get_window().size.x, get_window().size.y],
        "left_held": Input.is_mouse_button_pressed(MOUSE_BUTTON_LEFT),
        "right_held": Input.is_mouse_button_pressed(MOUSE_BUTTON_RIGHT)}
''')
    expected = version(project)
    game = DockerGame(config, project, tmp_path / "pointer-play")
    target = [width * .18, height * .66]
    try:
        game.start()
        game.reset(42)
        game.policy_act(action(pointer={"x": .18, "y": .66, "button": "none"}))
        hover = game.observe_state()
        assert hover["viewport"] == [width, height]
        assert hover["window"] == [960, 540]
        assert hover["motion"] == pytest.approx(target, abs=.01)
        assert hover["hovered"] and hover["events"] == []
        for index, name in enumerate(("left", "right"), start=1):
            # Exercise both entry points; all three pointer events must agree.
            send = game.policy_act if name == "left" else game.act
            send(action(pointer={"x": .18, "y": .66, "button": name}))
            state = game.observe_state()
            assert state["clicks"] == 1
            assert not state["left_held"] and not state["right_held"]
            assert len(state["events"]) == index * 2
            for event, pressed in zip(state["events"][-2:], (True, False)):
                assert [event["x"], event["y"]] == pytest.approx(target, abs=.01)
                assert event["button"] == index
                assert event["pressed"] is pressed and event["held"] is pressed
        game.policy_act(action(pointer={"x": .9, "y": .1, "button": "left"}))
        outside = game.observe_state()
        assert outside["clicks"] == 1 and not outside["hovered"]
        assert outside["motion"] == pytest.approx([width * .9, height * .1], abs=.01)
    finally:
        game.stop()
    assert version(project) == expected


def test_parallel_games_inputs_saves_recording_and_cleanup(tmp_path, setup_game):
    config, project = setup_game
    expected = version(project)
    barrier = threading.Barrier(3)

    def episode(index):
        game = DockerGame(config, project, tmp_path / f"episode-{index}")
        try:
            game.start()
            game.reset(42 + index)
            initial = game.observe_state()
            assert initial["x"] == 20 and initial["clicks"] == 0 and not initial["saved"]
            data = read_json(game.output / "container-runtime.json")
            cli = config["runtime"]["cli"]
            inspect_cmd = (["wslc", "inspect", data["container"], "-f", "json"] if cli == "wslc"
                           else ["docker", "inspect", data["container"]])
            inspect = json.loads(subprocess.check_output(inspect_cmd, text=True))[0]
            assert inspect["HostConfig"]["NetworkMode"] == "none"
            if cli == "docker":
                assert inspect["HostConfig"]["ReadonlyRootfs"]
            def readonly(mount):
                if "RW" in mount:
                    return mount["RW"] is False
                return mount.get("ReadWrite") is False
            watched = ("/source", "/framework/game_agent_lite")
            assert all(readonly(m) for m in inspect["Mounts"] if m["Destination"] in watched)
            assert (inspect.get("NetworkSettings") or {}).get("Ports") in ({}, None)
            barrier.wait(timeout=45)
            game.policy_act(action(["ArrowRight"] if index == 0 else [], duration=600,
                                   pointer={"x": .5, "y": .5, "button": "left"} if index == 0 else None))
            game.release_inputs()
            state = game.observe_state()
            game.policy_act(action(duration=600))
            released = game.observe_state()
            assert abs(state["x"] - released["x"]) < 4
            if index == 0:
                assert state["x"] > 25 and state["clicks"] == 1 and state["saved"]
            else:
                assert state["x"] == 20 and state["clicks"] == 0 and not state["saved"]
            assert game.observe()["image"].is_file()
            runtime = read_json(game.output / "runtime.json")
        finally:
            video = game.stop()
        assert video and video["path"].is_file()
        probe_args = ["-v", "error", "-select_streams", "v:0",
                      "-show_entries", "stream=width,height,r_frame_rate", "-of", "json"]
        try:
            probe_out = subprocess.check_output(["ffprobe", *probe_args, str(video["path"])], text=True)
        except FileNotFoundError:
            cli = config["runtime"]["cli"]
            probe_out = subprocess.check_output([
                cli, "run", "--rm", "--pull=never", "--network", "none",
                "--mount", f"type=bind,source={video['path'].parent},target=/video,readonly",
                "--entrypoint", "ffprobe", config["runtime"]["image"],
                *probe_args, f"/video/{video['path'].name}"], text=True)
        probe = json.loads(probe_out)
        assert probe["streams"][0] == {"width": 640, "height": 360, "r_frame_rate": "2/1"}
        assert not game.owner_file.exists()
        cli = config["runtime"]["cli"]
        gone = (["wslc", "inspect", data["container"]] if cli == "wslc"
                else ["docker", "container", "inspect", data["container"]])
        assert subprocess.run(gone, capture_output=True).returncode
        return runtime["user_dir_name"]

    with ThreadPoolExecutor(max_workers=3) as pool:
        directories = list(pool.map(episode, range(3)))
    assert len(set(directories)) == 3
    assert version(project) == expected


def test_stopped_game_flushes_recording_and_resume_cleans_orphan(tmp_path, setup_game):
    config, project = setup_game
    cancel = threading.Event()
    def check():
        if cancel.is_set():
            raise Stopped("fixture stop")
    game = DockerGame(config, project, tmp_path / "stopped", check)
    try:
        game.start()
        game.policy_act(action(["ArrowRight"], 1600))
        cancel.set()
        with pytest.raises(Stopped):
            game.observe_state()
    finally:
        video = game.stop()
    assert video and video["path"].is_file()
    assert not game.owner_file.exists()

    orphan = DockerGame(config, project, tmp_path / "orphan")
    try:
        orphan.start()
        orphan.policy_act(action(duration=1600))
        cleanup_runtimes(tmp_path)
        assert not orphan.owner_file.exists()
        saved = read_json(orphan.output / "runtime-shutdown.json")
        assert saved["video"] and saved["error"] is None
    finally:
        orphan._dispose(graceful=True)


def test_full_fixed_policy_batch_and_completed_episode_resume(tmp_path, setup_game):
    config, project = setup_game
    class FixedAgent(MockAgent):
        def call(self, role, job, context, **kwargs):
            result = super().call(role, job, context, **kwargs)
            if context["phase"] == "policy_author":
                (job / "io/policy.gd").write_text('''extends RefCounted
var steps := 0
func reset(_seed: int, _profile: String):
    steps = 0
func act(_state: Dictionary):
    if steps == 10:
        return {"action": null, "done": true, "reason": "fixed fixture finished"}
    steps += 1
    return {"action": {"keys": ["ArrowRight"] if steps < 5 else [], "duration_ms": 150, "pointer": null}, "done": false, "reason": "fixed fixture movement"}
''')
            return result
    play = PolicySession(config, project, tmp_path / "player", version(project), make_game)
    report = play.run(FixedAgent())
    assert valid_play(report, play.job, play.version)
    results = [read_json(play.job / item["path"]) for item in report["episodes"]]
    assert all(r["status"] == "completed" and r["steps"] == 10 for r in results)
    assert all(r["runtime"] == report["settings"]["runtime"] for r in results)
    before = list(play.job.glob("episodes/*/attempt-*"))
    resumed = PolicySession(config, project, play.job, play.version, make_game).run(FixedAgent())
    assert resumed == report
    assert list(play.job.glob("episodes/*/attempt-*")) == before
    assert not list(play.job.rglob("*-runtime-owner.json"))


def test_basic_3d_compatibility_rendering(tmp_path, config):
    """Rendered 3D fixture, not a generated game or a performance benchmark."""
    config.update(engine="godot", backend="codex", entry="project.godot")
    config["runtime"].update(mode="docker", image=os.environ["GAME_AGENT_RUNTIME_IMAGE"],
                             cli=os.environ.get("GAME_AGENT_CONTAINER_CLI", "docker"))
    project = tmp_path / "source-3d"
    project.mkdir()
    (project / "project.godot").write_text('''config_version=5
[application]
config/name="Container 3D fixture"
run/main_scene="res://main.tscn"
[autoload]
GameAgentBridge="*res://game_agent_bridge.gd"
[rendering]
renderer/rendering_method="gl_compatibility"
''')
    (project / "main.tscn").write_text('''[gd_scene load_steps=2 format=3]
[ext_resource type="Script" path="res://main.gd" id="1"]
[node name="World" type="Node3D"]
script = ExtResource("1")
''')
    (project / "main.gd").write_text('''extends Node3D
var box: MeshInstance3D
func _ready() -> void:
    add_to_group("game_agent_target")
    box = MeshInstance3D.new()
    box.mesh = BoxMesh.new()
    var material = StandardMaterial3D.new()
    material.albedo_color = Color.CORNFLOWER_BLUE
    box.material_override = material
    add_child(box)
    var camera = Camera3D.new()
    add_child(camera)
    camera.position = Vector3(2, 2, 5)
    camera.look_at(Vector3.ZERO)
    var light = DirectionalLight3D.new()
    light.rotation_degrees = Vector3(-45, -30, 0)
    add_child(light)
func _physics_process(delta: float) -> void:
    if Input.is_physical_key_pressed(KEY_RIGHT):
        box.position.x += delta
func agent_reset(_seed: int) -> void:
    box.position.x = 0
func agent_observe() -> Dictionary:
    return {"x": box.position.x}
''')
    install_bridge(project, "godot")
    assert DockerGame(config, project, tmp_path / "build-3d").build()["passed"]
    expected = version(project)
    game = DockerGame(config, project, tmp_path / "play-3d")
    try:
        game.start()
        game.reset(42)
        assert game.observe_state()["x"] == 0
        game.policy_act(action(["ArrowRight"], 600))
        game.release_inputs()
        assert game.observe_state()["x"] > .1
        assert game.observe()["image"].is_file()
        game.policy_act(action(duration=600))
    finally:
        video = game.stop()
    assert video and video["path"].is_file()
    assert version(project) == expected


@pytest.mark.parametrize("input_case", ["hold", "edges"])
def test_native_gamecraft_preparation_and_optional_upstream_replay(tmp_path, setup_game, input_case):
    """Fixed fixture only: saved .gd -> new short recording -> official trace format.

    GAME_AGENT_GAMECRAFT_IMAGE additionally exercises upstream's unchanged replayer,
    without reading a rubric, using a judge, credentials or network.
    """
    from game_agent_lite.gamecraft_prepare import prepare_gamecraft
    from game_agent_lite.gamecraft import validate_gamecraft_submission
    config, source = setup_game
    config["player"]["trajectories_per_version"] = 1
    root = tmp_path / "run"
    directory = root / "rounds/0000"
    project = directory / "integrate-fixture/project"
    snapshot(source, project)
    script = project / "main.gd"
    script.write_text(script.read_text().replace('add_to_group("game_agent_target")',
        'add_to_group("game_agent_target")\n\tif "short" in OS.get_cmdline_user_args():\n\t\tx = 80.0\n\t\tprint("FIXTURE_SCENARIO_SHORT")')
        + '''
var goal_logged := false
func _physics_process(_dt):
\tif x > 110.0 and not goal_logged:
\t\tgoal_logged = true
\t\tprint("FIXTURE_REACHED_GOAL")
''')
    if input_case == "edges":
        script.write_text(script.read_text().replace('func _input(event: InputEvent) -> void:',
            'func _input(event: InputEvent) -> void:\n\t_count_edges(event)') + '''
var rights: Array = []
var mouse: Array = []
func _count_edges(event: InputEvent) -> void:
    if event is InputEventKey and event.keycode == KEY_RIGHT and event.pressed:
        rights.append([event.shift_pressed, Input.is_key_pressed(KEY_SHIFT), event.echo])
    if event is InputEventMouseButton:
        mouse.append([event.pressed, event.ctrl_pressed, Input.is_mouse_button_pressed(MOUSE_BUTTON_LEFT),
            roundi(event.position.x), roundi(event.position.y)])
    if event is InputEventKey and event.keycode == KEY_CTRL and not event.pressed:
        print("INPUT_RESULT=" + JSON.stringify({"rights": rights, "mouse": mouse}))
''')
    script.write_text(script.read_text().expandtabs(4))
    assert DockerGame(config, project, tmp_path / "build-native-export").build()["passed"]
    identity = version(project)
    job = directory / "jobs/player-fixture"
    play = PolicySession(config, project, job, identity, make_game)
    prepared, _ = play.prepare()
    source_policy = tmp_path / "fixed.gd"
    source_policy.write_text('''extends RefCounted
var steps := 0
func reset(_seed: int, _profile: String): steps = 0
func act(_state: Dictionary):
    if steps == 12: return {"action": null, "done": true, "reason": "fixed recording ended"}
    steps += 1
    return {"action": {"keys": ["ArrowRight"], "duration_ms": 100, "pointer": null},
            "done": false, "reason": "fixture movement"}
''')
    if input_case == "edges":
        actions = [action(keys, duration=100, pointer=pointer) for keys, pointer in [
            (["ArrowRight"], None), (["ArrowRight"], None), ([], None),
            (["ArrowRight", "Shift"], None), ([], None), (["ArrowRight"], None),
            (["Control"], {"x": .25, "y": .5, "button": "left"})]]
        # Keep the tiny fixture visible long enough for two 2 FPS video samples.
        actions = [action(duration=800), *actions, action(duration=800)]
        source_policy.write_text('''extends RefCounted
var index := 0
var actions = JSON.parse_string('ACTIONS')
func reset(_seed: int, _profile: String): index = 0
func act(_state: Dictionary):
    if index == actions.size(): return {"action": null, "done": true, "reason": "fixture done"}
    var action = actions[index]
    index += 1
    return {"action": action, "done": false, "reason": "fixture inputs"}
'''.replace("ACTIONS", json.dumps(actions)))
    plan = {"summary": "fixed fixture", "episodes": [{"profile": "core", "purpose": "reach marker"}]}
    frozen = play.freeze_policy(source_policy, plan, prepared, None, author_called=False)
    report = {"format": "policy-v1", **frozen, "episodes": play.run_episodes(plan["episodes"], frozen["policy"])}
    atomic_json(job / "play.json", report)
    atomic_json(root / "config.json", config)
    atomic_json(directory / "check.json", {"passed": True, "version": identity,
        "project": project.relative_to(root).as_posix()})
    atomic_json(directory / "played-candidate.json", {
        "report": (job / "play.json").relative_to(root).as_posix(), "sha256": file_hash(job / "play.json")})
    before = {p.relative_to(root): file_hash(p) for p in root.rglob("*") if p.is_file()}
    output = tmp_path / "demos"
    result = prepare_gamecraft(root, output, round_number=0, episodes=[1], scenarios=["1=short"])
    game, manifest, _ = validate_gamecraft_submission(result["submission"])
    assert manifest["demos"][0]["initial_state"]["x"] == 80  # No agent_reset overwrite.
    assert manifest["demos"][0]["initial_state"]["seed"] == 0
    assert manifest["demos"][0]["scenario"] == "short"
    assert manifest["demos"][0]["duration_frames"] <= 600 and not manifest["demos"][0]["clipped"]
    assert before == {p.relative_to(root): file_hash(p) for p in root.rglob("*") if p.is_file()}
    assert not list(output.rglob("*-runtime-owner.json"))
    logs = list((output / "player").glob("episodes/*/attempt-*/engine/godot.log"))
    assert len(logs) == 1
    if input_case == "hold": assert "FIXTURE_REACHED_GOAL" in logs[0].read_text()
    assert manifest["demos"][0]["input_source"] == "injected_events"
    assert list((output / "player").glob("episodes/*/attempt-*/input_events.jsonl"))
    def input_result(text):
        return json.loads(next(line.split("INPUT_RESULT=", 1)[1] for line in text.splitlines() if line.startswith("INPUT_RESULT=")))
    if input_case == "edges":
        native_result = input_result(logs[0].read_text())
        assert native_result == {"rights": [[False, False, False], [True, True, False], [False, False, False]],
                                 "mouse": [[True, True, True, 320, 360], [False, True, False, 320, 360]]}
    evaluator = os.environ.get("GAME_AGENT_GAMECRAFT_IMAGE")
    if evaluator:
        benchmark = Path(os.environ["GAME_AGENT_GAMECRAFT_BENCHMARK"]).resolve()
        assert (benchmark / "gamecraft_bench/verifier/replay.py").is_file()
        replay_output = tmp_path / "official-replay"
        replay_output.mkdir()
        code = '''from pathlib import Path
import shutil, subprocess
from gamecraft_bench.verifier.replay import replay_trace
shutil.copytree("/submission", "/tmp/game")
subprocess.run(["godot", "--headless", "--editor", "--path", "/tmp/game", "--import"], check=True, timeout=60)
replay_trace(project_dir=Path("/tmp/game"), trace_path=Path("/tmp/game/demo_outputs/0001.json"),
             output_mp4=Path("/output/replay.mp4"), godot_bin="godot", log_dir=Path("/output"))
'''
        command = ["docker", "run", "--rm", "--network", "none", "--entrypoint", "python",
            "-e", "PYTHONPATH=/opt/gamecraft-bench",
            "--mount", f"type=bind,source={benchmark},target=/opt/gamecraft-bench,readonly",
            "--mount", f"type=bind,source={game},target=/submission,readonly",
            "--mount", f"type=bind,source={replay_output},target=/output", evaluator, "-c", code]
        run = subprocess.run(command, capture_output=True, text=True, timeout=120)
        assert run.returncode == 0, run.stdout + run.stderr
        assert (replay_output / "replay.mp4").stat().st_size > 1000
        text = (replay_output / "godot.log").read_text()
        assert "FIXTURE_SCENARIO_SHORT" in text
        if input_case == "hold": assert "FIXTURE_REACHED_GOAL" in text
        else: assert input_result(text) == native_result
        assert "SCRIPT ERROR" not in text
        validate_gamecraft_submission(result["submission"])
