"""Fixed recording format, legacy snapshots and offline media fixtures."""
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import shutil
import struct
import subprocess
from unittest.mock import Mock
import zlib

import pytest

import game_agent_lite.game as module
from game_agent_lite.agent import child_env
from game_agent_lite.game import GodotGame, HTMLGame, MockGame, PlaySession, install_bridge
from game_agent_lite.agent import MockAgent
from game_agent_lite.store import read_json, version


class EncodingRunner:
    def __init__(self):
        self.calls = []

    def run(self, argv, cwd, log, **kwargs):
        self.calls.append(argv)
        log.write_text("")
        if argv[-1] != "--import":
            Path(argv[-1]).write_bytes(b"mock encoded video")


def test_html_limits_recording_size_without_changing_pointer_viewport(tmp_path, config, monkeypatch):
    import playwright.sync_api
    pw = Mock()
    sync = Mock()
    sync.return_value.start.return_value = pw
    monkeypatch.setattr(playwright.sync_api, "sync_playwright", sync)
    monkeypatch.setattr(module, "ThreadingHTTPServer", Mock())
    monkeypatch.setattr(module.threading, "Thread", Mock())
    game = HTMLGame(config, tmp_path / "project", tmp_path / "out")
    game.start()
    options = pw.chromium.launch.return_value.new_context.call_args.kwargs
    assert options["record_video_size"] == {"width": 640, "height": 360}
    assert options["viewport"] == {"width": 960, "height": 540}


def html_recording(tmp_path, config, runner):
    game = HTMLGame(config, tmp_path / "project", tmp_path / "out", runner=runner)
    original = game.output / "native.webm"
    original.write_bytes(b"native video fixture")
    game.context, game.page, game.browser, game.pw = Mock(), Mock(), Mock(), Mock()
    game.page.video.path.return_value = str(original)
    return game, original


def test_html_normalizes_video_and_removes_native_only_after_success(tmp_path, config):
    runner = EncodingRunner()
    game, original = html_recording(tmp_path, config, runner)
    result = game.stop()
    game.context.close.assert_called_once()
    game.browser.close.assert_called_once()
    game.pw.stop.assert_called_once()
    assert result["path"].is_file() and not original.exists()
    assert result["capture"]["fps"] == 2
    assert result["capture"]["width"] == 640 and result["capture"]["height"] == 360
    assert result["mode"] == "real_time_resampled_2fps"
    command = runner.calls[0]
    assert "fps=2" in command[command.index("-vf") + 1]
    assert "scale=640:360" in command[command.index("-vf") + 1]
    assert command[command.index("-fps_mode") + 1] == "cfr"


def test_html_encoding_failure_preserves_raw_video_and_cleans_browser(tmp_path, config):
    runner = Mock()
    runner.run.side_effect = RuntimeError("encoder failed")
    game, original = html_recording(tmp_path, config, runner)
    with pytest.raises(RuntimeError, match="encoder failed"):
        game.stop()
    assert original.exists()
    game.browser.close.assert_called_once()
    game.pw.stop.assert_called_once()


def godot_project(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "project.godot").write_text('config_version=5\n[application]\nconfig/name="Recorder fixture"\n')
    (project / "main.gd").write_text("extends Node\n")
    (project / "game_agent_bridge.gd").write_text("extends Node\n# old recorder\n")
    (project / ".godot").mkdir()
    (project / ".godot/stale-cache").write_text("old cache")
    return project


def test_old_snapshot_runs_with_new_bridge_without_mutating_source(tmp_path, config):
    project = godot_project(tmp_path)
    before = version(project)
    runner = EncodingRunner()
    game = GodotGame(config, project, tmp_path / "out", runner=runner)
    runtime = game.prepare_runtime()
    assert runtime != project
    assert version(project) == before
    assert (runtime / "main.gd").read_bytes() == (project / "main.gd").read_bytes()
    assert (runtime / "game_agent_bridge.gd").read_bytes() == (module.PACKAGE / "bridges/game_agent_bridge.gd").read_bytes()
    assert not (runtime / ".godot/stale-cache").exists()
    assert runner.calls[0][-1] == "--import"
    (runtime / "main.gd").write_text("runtime write")
    assert version(project) == before  # no hard links into the sealed snapshot


def test_current_bridge_also_isolates_runtime_writes(tmp_path, config):
    project = godot_project(tmp_path)
    install_bridge(project, "godot")
    runner = EncodingRunner()
    game = GodotGame(config, project, tmp_path / "out", runner=runner)
    before = version(project)
    runtime = game.prepare_runtime()
    assert runtime != project
    (runtime / "saves").mkdir()
    (runtime / "saves/progress.json").write_text('{"level": 3}')
    assert not (project / "saves").exists()
    assert version(project) == before
    assert runner.calls[0][-1] == "--import"


def test_runtime_attempts_have_independent_save_namespaces(tmp_path, config):
    project = godot_project(tmp_path)
    (project / "override.cfg").write_text('[rendering]\nrenderer/rendering_method="gl_compatibility"\n')
    first = GodotGame(config, project, tmp_path / "first", runner=EncodingRunner())
    second = GodotGame(config, project, tmp_path / "second", runner=EncodingRunner())
    a, b = first.prepare_runtime(), second.prepare_runtime()
    assert a != b and first.user_dir_name != second.user_dir_name
    assert 'renderer/rendering_method="gl_compatibility"' in (a / "override.cfg").read_text()
    assert first.user_dir_name in (a / "override.cfg").read_text()
    assert second.user_dir_name in (b / "override.cfg").read_text()
    (a / "save.json").write_text("first attempt only")
    assert not (b / "save.json").exists() and not (project / "save.json").exists()


def test_runtime_import_errors_are_not_silently_ignored(tmp_path, config):
    project = godot_project(tmp_path)
    before = version(project)
    runner = Mock()
    runner.run.side_effect = lambda argv, cwd, log, **kw: log.write_text("SCRIPT ERROR: broken fixture")
    with pytest.raises(RuntimeError, match="runtime copy import failed"):
        GodotGame(config, project, tmp_path / "out", runner=runner).prepare_runtime()
    assert version(project) == before


def test_real_godot_parallel_res_and_user_saves_are_isolated(tmp_path, config):
    godot = shutil.which("godot")
    if not godot:
        pytest.skip("Local Godot not installed")
    config["godot"] = godot
    project = godot_project(tmp_path)
    # A source override is preserved, but its shared user-data setting is replaced.
    (project / "override.cfg").write_text('[application]\nconfig/use_custom_user_dir=false\n')
    (project / "probe.gd").write_text('''extends SceneTree
func _initialize():
    var path = "user://counter.txt"
    var previous = int(FileAccess.get_file_as_string(path)) if FileAccess.file_exists(path) else 0
    var saved = FileAccess.open(path, FileAccess.WRITE)
    saved.store_string(str(previous + 1))
    saved.close()
    DirAccess.make_dir_recursive_absolute("res://saves")
    var local = FileAccess.open("res://saves/progress.json", FileAccess.WRITE)
    local.store_string("{\\"level\\":3}")
    local.close()
    print("SAVE_PROBE=" + JSON.stringify({"user_data_dir": OS.get_user_data_dir(), "previous": previous}))
    quit()
''')
    before = version(project)
    games = [GodotGame(config, project, tmp_path / f"episode-{i}") for i in range(3)]
    user_dirs = []
    def run_probe(runtime):
        result = subprocess.run([godot, "--headless", "--path", str(runtime), "--script", "res://probe.gd"],
            capture_output=True, text=True, timeout=30, env=child_env())
        assert result.returncode == 0 and "SCRIPT ERROR" not in result.stderr, result.stdout + result.stderr
        return json.loads(next(s.removeprefix("SAVE_PROBE=") for s in result.stdout.splitlines()
                               if s.startswith("SAVE_PROBE=")))
    try:
        with ThreadPoolExecutor(max_workers=3) as pool:
            runtimes = list(pool.map(lambda game: game.prepare_runtime(), games))
            first = list(pool.map(run_probe, runtimes))
        for game, data in zip(games, first):
            directory = Path(data["user_data_dir"])
            assert directory.as_posix().endswith("/" + game.user_dir_name)
            user_dirs.append(directory)
        assert len(set(user_dirs)) == 3
        assert [item["previous"] for item in first] == [0, 0, 0]
        assert run_probe(runtimes[0])["previous"] == 1  # Save/load within one attempt still works.
        assert all((runtime / "saves/progress.json").is_file() for runtime in runtimes)
        assert not (project / "saves").exists() and version(project) == before
    finally:
        for directory in user_dirs:
            shutil.rmtree(directory)


def test_godot_video_preserves_gaps_and_uses_two_fps(tmp_path, config):
    runner = EncodingRunner()
    game = GodotGame(config, tmp_path / "project", tmp_path / "out", runner=runner)
    game.frames = game.output / "capture"
    game.frames.mkdir()
    rows = [{"file": f"video-{i:06d}.png", "time": t} for i, t in enumerate((10, 10.75, 12.5))]
    (game.frames / "timing.jsonl").write_text("\n".join(json.dumps(row) for row in rows))
    result = game.stop()
    durations = [line for line in (game.frames / "frames.txt").read_text().splitlines()
                 if line.startswith("duration ")]
    assert durations == ["duration 0.750000", "duration 1.750000", "duration 0.500000"]
    assert result["mode"] == "real_time_sampled_2fps"
    assert result["capture"]["fps"] == 2
    assert result["capture"]["width"] == 640 and result["capture"]["height"] == 360
    assert runner.calls[0][runner.calls[0].index("-fps_mode") + 1] == "cfr"


def test_capture_metadata_is_retained_in_play_report(tmp_path, config):
    class RecordedMock(MockGame):
        def stop(self):
            result = super().stop()
            result["capture"] = {"width": 640, "height": 360, "fps": 2, "bridge_sha256": "mock-bridge"}
            return result
    job = tmp_path / "job"
    report = PlaySession(RecordedMock(config, tmp_path, job / "engine"), job, "one", 42, 2).run(MockAgent())
    assert report["video"]["capture"]["bridge_sha256"] == "mock-bridge"
    assert read_json(job / "play.json")["video"]["capture"] == report["video"]["capture"]


def test_real_godot_resizes_5k_images_before_png_encoding(tmp_path):
    godot = shutil.which("godot") or "/Applications/Godot.app/Contents/MacOS/Godot"
    if not Path(godot).is_file():
        pytest.skip("Local Godot not installed; offline Image fixture only")
    project = tmp_path / "image-fixture"
    project.mkdir()
    (project / "project.godot").write_text('config_version=5\n')
    install_bridge(project, "godot")
    (project / "check.gd").write_text('''extends SceneTree
const Bridge = preload("res://game_agent_bridge.gd")
func _initialize():
    assert(Bridge.CAPTURE_FPS == 2.0)
    for size in [Vector2i(5120, 2880), Vector2i(960, 540), Vector2i(640, 360), Vector2i(800, 800)]:
        var image = Image.create(size.x, size.y, false, Image.FORMAT_RGB8)
        image.fill(Color.RED)
        image = Bridge.prepare_capture(image)
        assert(image.get_size() == Vector2i(640, 360))
        assert(image.get_pixel(320, 180).r > 0.9)
        assert(image.save_png("res://capture-%d.png" % size.x) == OK)
    assert(Bridge.prepare_capture(Image.new()).is_empty())
    print("CAPTURE_FIXTURE_OK")
    quit()
''')
    result = subprocess.run([godot, "--headless", "--path", str(project), "--script", "res://check.gd"],
                            capture_output=True, text=True, timeout=30, env=child_env())
    assert result.returncode == 0, result.stdout + result.stderr
    assert "CAPTURE_FIXTURE_OK" in result.stdout, result.stdout + result.stderr
    assert "SCRIPT ERROR" not in result.stderr
    for png in project.glob("capture-*.png"):
        assert struct.unpack(">II", png.read_bytes()[16:24]) == (640, 360)


def png_fixture(path, color):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 2, 8, 2, 0, 0, 0))
                     + chunk(b"IDAT", zlib.compress((b"\x00" + bytes(color) * 2) * 2)) + chunk(b"IEND", b""))


def test_real_ffmpeg_output_is_640_360_two_fps_without_speedup(tmp_path, config):
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        pytest.skip("Local FFmpeg not installed; offline media fixture only")
    config["ffmpeg"] = ffmpeg
    game = GodotGame(config, tmp_path / "project", tmp_path / "out")
    game.frames = game.output / "capture"
    game.frames.mkdir()
    rows = []
    for index, moment in enumerate((0, .5, 2.5)):
        name = f"video-{index:06d}.png"
        png_fixture(game.frames / name, (index * 100, 0, 0))
        rows.append({"file": name, "time": moment})
    (game.frames / "timing.jsonl").write_text("\n".join(json.dumps(row) for row in rows))
    result = game.stop()
    probe = subprocess.run([ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries",
                            "stream=width,height,r_frame_rate,avg_frame_rate:format=duration", "-of", "json",
                            str(result["path"])], capture_output=True, text=True, timeout=15, env=child_env())
    assert probe.returncode == 0, probe.stderr
    metadata = json.loads(probe.stdout)
    stream = metadata["streams"][0]
    assert (stream["width"], stream["height"]) == (640, 360)
    assert stream["r_frame_rate"] == stream["avg_frame_rate"] == "2/1"
    assert 3.0 <= float(metadata["format"]["duration"]) <= 3.5
