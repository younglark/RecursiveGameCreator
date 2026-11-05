"""Offline policy subprocesses, engine contracts, durable episodes and evidence."""
import json
from pathlib import Path
import shutil
import subprocess
import threading
import time
from unittest.mock import Mock

import pytest
from jsonschema import ValidationError

import game_agent_lite.policy as module
from game_agent_lite.agent import Agent, MockAgent
from game_agent_lite.cli import load_config
from game_agent_lite.game import HTMLGame, GodotGame, MockGame, valid_play
from game_agent_lite.loop import Loop
from game_agent_lite.policy import PolicySession, PolicyWorker
from game_agent_lite.store import Store, Stopped, artifact, atomic_json, read_json, version
from game_agent_lite.types import DEFAULTS


@pytest.fixture
def policy_config(config):
    config["player"]["mode"] = "policy"
    # Existing recovery fixtures deliberately exercise a known serial ordering.
    # Parallel scheduling and real worker overlap are covered explicitly below.
    config["player"]["parallel_episodes"] = 1
    return config


def session(tmp_path, config, factory=MockGame, stop=lambda: None):
    project = tmp_path / "game"
    project.mkdir()
    (project / "index.html").write_text("offline test fixture")
    return PolicySession(config, project, tmp_path / "player", version(project), factory, stop)


def episodes(report, root):
    return [read_json(root / item["path"]) for item in report["episodes"]]


def test_three_actual_policy_episodes_then_analysis(tmp_path, policy_config):
    class TrackingGame(MockGame):
        starts = captures = inputs = 0
        def start(self):
            type(self).starts += 1
            super().start()
        def observe(self):
            type(self).captures += 1
            return super().observe()
        def act(self, action):
            type(self).inputs += 1
            return super().act(action)

    class Phases(MockAgent):
        phases = []
        def call(self, role, job, context, **kwargs):
            self.phases.append(context["phase"])
            if context["phase"] == "policy_analyze":
                assert TrackingGame.starts == 4 and TrackingGame.inputs == 6
                assert kwargs.get("project") is None
            else:
                assert kwargs["project"].is_dir()
            return super().call(role, job, context, **kwargs)

    play = session(tmp_path, policy_config, TrackingGame)
    agent = Phases()
    report = play.run(agent)
    assert agent.phases == ["policy_author", "policy_analyze"]
    assert TrackingGame.captures == 7  # One preflight frame plus six episode endpoints.
    results = episodes(report, play.job)
    assert [r["seed"] for r in results] == [42, 43, 44]
    assert len({r["profile"] for r in results}) == 3
    assert all(r["steps"] == 2 and r["status"] == "completed" for r in results)
    trace = [json.loads(line) for line in (play.job / results[0]["trajectory"]["path"]).read_text().splitlines()]
    assert [r["type"] for r in trace] == ["initial", "input", "feedback", "input", "feedback", "end"]
    assert trace[2]["before"] == {"x": 0} and trace[2]["after"] == {"x": 1}
    assert valid_play(report, play.job, play.version, allow_mock=True)
    assert not valid_play(report, play.job, play.version)
    assert not valid_play(report, play.job, "another-version", allow_mock=True)
    assert version(play.project) == play.version


def test_policy_round_trip_and_anonymous_comparison(tmp_path, policy_config):
    store = Store(tmp_path / "run")
    store.create(policy_config)
    state = Loop(store, policy_config, agent=MockAgent(["B", "A"])).run()
    assert state["status"] == "completed", state["error"]
    assert [r["promoted"] for r in state["history"]] == [True, True]
    assert state["best"]["round"] == 1
    bundles = list(store.root.glob("rounds/0001/jobs/reviewer-*/io/*/summary.json"))
    assert len(bundles) == 4
    for path in bundles:
        data = read_json(path)
        assert len(data["episodes"]) == 3
        assert '"version"' not in path.read_text()
        assert not list(path.parent.rglob("policy.py"))
        for e in data["episodes"]:
            assert (path.parent / e["video"]).is_file()
            trace = (path.parent / e["trajectory"]).read_text()
            assert "policy_sha256" not in trace and '"version"' not in trace
    feedback = next(store.root.glob("rounds/0001/jobs/designer-*/io/evolution_feedback.json"))
    assert read_json(feedback)["rounds"][0]["plays"]["candidate"]["evidence_valid"]


def test_resume_keeps_completed_episode_and_policy(tmp_path, policy_config):
    class Interrupt(MockGame):
        starts = 0
        stopped = False
        def start(self):
            type(self).starts += 1
            super().start()
        def act(self, action):
            if type(self).starts == 3 and not type(self).stopped:
                type(self).stopped = True
                raise Stopped()
            super().act(action)
    play = session(tmp_path, policy_config, Interrupt)
    agent = MockAgent()
    with pytest.raises(Stopped):
        play.run(agent)
    first = read_json(play.job / "episodes/0001/completed.json")
    resumed = PolicySession(policy_config, play.project, play.job, play.version, Interrupt)
    report = resumed.run(agent)
    assert report["episodes"][0] == first
    assert agent.calls == ["player", "player"]  # No author regeneration on resume.
    assert Interrupt.starts == 5  # Preflight + episode 1 + interrupted/resumed 2 + episode 3.
    assert len(list((play.job / "episodes/0002").glob("attempt-*"))) == 2
    assert valid_play(report, play.job, play.version, allow_mock=True)


def test_resume_after_analysis_interrupt_does_not_replay(tmp_path, policy_config):
    class StopAnalysis(MockAgent):
        interrupted = False
        def call(self, role, job, context, **kwargs):
            if context.get("phase") == "policy_analyze" and not self.interrupted:
                self.interrupted = True
                raise Stopped()
            return super().call(role, job, context, **kwargs)
    play = session(tmp_path, policy_config)
    agent = StopAnalysis()
    with pytest.raises(Stopped):
        play.run(agent)
    receipts = sorted(play.job.glob("episodes/*/completed.json"))
    saved = [p.read_bytes() for p in receipts]
    report = play.run(agent)
    assert [p.read_bytes() for p in receipts] == saved
    assert len(list(play.job.glob("episodes/*/attempt-*"))) == 3
    assert valid_play(report, play.job, play.version, allow_mock=True)


@pytest.mark.parametrize("corruption", ["policy", "video", "frame", "trajectory", "episode", "analysis"])
def test_missing_or_changed_evidence_rejected(tmp_path, policy_config, corruption):
    play = session(tmp_path, policy_config)
    report = play.run(MockAgent())
    episode = episodes(report, play.job)[0]
    item = {"policy": report["policy"], "analysis": report["analysis"],
            "video": episode["video"]["artifact"], "frame": episode["frames"][0]["image"],
            "trajectory": episode["trajectory"], "episode": report["episodes"][0]}[corruption]
    path = play.job / item["path"]
    path.chmod(0o644)
    path.write_bytes(b"changed")
    assert not valid_play(report, play.job, play.version, allow_mock=True)


def test_consistent_hashes_do_not_hide_cross_version_steps(tmp_path, policy_config):
    play = session(tmp_path, policy_config)
    report = play.run(MockAgent())
    result_path = play.job / report["episodes"][0]["path"]
    data = read_json(result_path)
    trace = play.job / data["trajectory"]["path"]
    rows = [json.loads(line) for line in trace.read_text().splitlines()]
    rows[2]["version"] = "other game"
    trace.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    data["trajectory"] = artifact(trace, play.job)
    atomic_json(result_path, data)
    report["episodes"][0] = artifact(result_path, play.job)
    assert not valid_play(report, play.job, play.version, allow_mock=True)


def test_policy_runtime_failure_is_recorded_not_retried(tmp_path, policy_config):
    class Broken(MockAgent):
        def call(self, role, job, context, **kwargs):
            answer = super().call(role, job, context, **kwargs)
            if context.get("phase") == "policy_author":
                (job / "io/policy.py").write_text('''class Policy:
    def reset(self, seed, profile): pass
    def act(self, state): raise RuntimeError("fixture policy failed")
''')
            return answer
    play = session(tmp_path, policy_config)
    report = play.run(Broken())
    assert all(r["status"] == "failed" and "fixture policy failed" in r["reason"]
               for r in episodes(report, play.job))
    assert not valid_play(report, play.job, play.version, allow_mock=True)
    same = play.run(MockAgent())
    assert same["episodes"] == report["episodes"]
    assert len(list(play.job.glob("episodes/*/attempt-*"))) == 3


def test_configured_action_cap_is_truncated_not_a_win(tmp_path, policy_config):
    policy_config["max_actions"] = 1
    play = session(tmp_path, policy_config)
    report = play.run(MockAgent())
    assert all(r["status"] == "truncated" and r["steps"] == 1 for r in episodes(report, play.job))
    assert not valid_play(report, play.job, play.version, allow_mock=True)


def test_policy_no_input_cannot_promote(tmp_path, policy_config):
    class NoInput(MockAgent):
        def call(self, role, job, context, **kwargs):
            answer = super().call(role, job, context, **kwargs)
            if context.get("phase") == "policy_author":
                (job / "io/policy.py").write_text('''class Policy:
    def reset(self, seed, profile): pass
    def act(self, state): return {"action": None, "done": True, "reason": "nothing tested"}
''')
            return answer
    play = session(tmp_path, policy_config)
    report = play.run(NoInput())
    assert not valid_play(report, play.job, play.version, allow_mock=True)


@pytest.mark.parametrize("tamper", ["source", "evidence", "citation", "profile"])
def test_player_cannot_modify_source_or_proof(tmp_path, policy_config, tamper):
    class Tamper(MockAgent):
        def call(self, role, job, context, **kwargs):
            answer = super().call(role, job, context, **kwargs)
            if context["phase"] == "policy_author":
                if tamper == "source":
                    p = kwargs["project"] / "index.html"
                    p.chmod(0o644)
                    p.write_text("modified")
                if tamper == "profile":
                    answer["episodes"][1]["profile"] = answer["episodes"][0]["profile"]
            elif tamper == "evidence":
                next((job / "io/evidence").rglob("*.png")).write_bytes(b"fake")
            elif tamper == "citation":
                answer["evidence"] = ["../../policy.py"]
            return answer
    play = session(tmp_path, policy_config)
    with pytest.raises(ValidationError if tamper == "citation" else ValueError):
        play.run(Tamper())
    assert version(play.project) == play.version


def test_policy_worker_timeout_and_process_cleanup(tmp_path, policy_config):
    policy_config["player"]["policy_call_timeout_seconds"] = .3
    policy = tmp_path / "policy.py"
    policy.write_text('''class Policy:
    def reset(self, seed, profile): pass
    def act(self, state):
        while True: pass
''')
    worker = PolicyWorker(policy_config, policy, tmp_path / "worker")
    try:
        worker.request("reset", seed=42, profile="test")
        with pytest.raises(TimeoutError, match="per-call health timeout"):
            worker.request("act", state={})
    finally:
        worker.close()
    assert worker.process.poll() is not None


def test_policy_worker_stdout_is_not_protocol_and_secrets_are_not_inherited(tmp_path, policy_config, monkeypatch):
    monkeypatch.setenv("HUNYUAN_API_KEY", "not-for-policy")
    policy = tmp_path / "policy.py"
    policy.write_text('''import os
print("hello from import")
class Policy:
    def reset(self, seed, profile): print("hello from reset")
    def act(self, state): return {"secret": os.getenv("HUNYUAN_API_KEY"), "value": state["x"]}
''')
    worker = PolicyWorker(policy_config, policy, tmp_path / "worker")
    try:
        worker.request("reset", seed=42, profile="test")
        assert worker.request("act", state={"x": 7}) == {"secret": None, "value": 7}
    finally:
        worker.close()
    assert "not-for-policy" not in (tmp_path / "worker/worker.log").read_text()


def test_real_backend_worker_has_only_readonly_code_mounts(tmp_path, policy_config, monkeypatch):
    policy_config["backend"] = "codex"
    policy = tmp_path / "policy.py"
    policy.write_text("class Policy: pass")
    popen = Mock()
    monkeypatch.setattr(module.subprocess, "Popen", popen)
    worker = PolicyWorker(policy_config, policy, tmp_path / "worker")
    argv = popen.call_args.args[0]
    assert argv[:2] == ["docker", "run"]
    assert "--pull=never" in argv and "--read-only" in argv
    assert argv[argv.index("--network") + 1] == "none"
    mounts = [argv[i + 1] for i, word in enumerate(argv) if word == "--mount"]
    assert len(mounts) == 2 and all(m.endswith(",readonly") for m in mounts)
    assert not any(s in " ".join(argv) for s in ("auth.json", "docker.sock", "HUNYUAN"))
    assert read_json(tmp_path / "worker/worker.json")["container"] == worker.container


def test_state_reads_skip_screenshots_and_keys_remain_held(tmp_path, policy_config):
    html = HTMLGame(policy_config, tmp_path, tmp_path / "html")
    html.page, html.errors = Mock(), []
    html.page.evaluate.return_value = {"x": 1}
    assert html.observe_state() == {"x": 1}
    html.page.screenshot.assert_not_called()
    action = {"keys": ["ArrowRight"], "duration_ms": 16, "pointer": None}
    html.policy_act(action)
    html.policy_act(action)
    html.page.keyboard.down.assert_called_once_with("ArrowRight")
    html.page.keyboard.up.assert_not_called()
    html.policy_act({**action, "keys": []})
    html.page.keyboard.up.assert_called_once_with("ArrowRight")
    html.release_inputs()
    assert html.page.keyboard.up.call_count == 1
    godot = GodotGame(policy_config, tmp_path, tmp_path / "godot")
    godot.rpc = Mock(return_value={"x": 2})
    assert godot.observe_state() == {"x": 2}
    godot.rpc.assert_called_once_with("state", {})
    godot.policy_act({**action, "keys": ["KeyW"]})
    godot.rpc.assert_called_with("policy_act", {**action, "keys": ["w"]})
    godot.release_inputs()
    godot.rpc.assert_called_with("release", {})


def test_local_player_source_does_not_receive_write_scope(tmp_path, policy_config):
    policy_config["backend"] = "codex"
    Path(policy_config["auth_file"]).write_text("{}")
    class Runner:
        def run(self, argv, cwd, log, **kwargs):
            assert "--add-dir" not in argv
            schema = read_json(cwd / "schema.json")
            assert schema["properties"]["episodes"]["minItems"] == 4
            atomic_json(cwd / "result.json", {"summary": "plan", "episodes": [
                {"profile": f"p-{i}", "purpose": "test"} for i in range(4)]})
            log.write_text("")
    Agent(policy_config, Runner()).call("player", tmp_path / "job",
        {"phase": "policy_author", "trajectories_per_version": 4}, project=tmp_path / "source")


def test_player_defaults_and_partial_config(tmp_path):
    path = tmp_path / "config.json"
    atomic_json(path, {"engine": "html", "brief": "fixture", "player": {"trajectories_per_version": 5}})
    config = load_config(path)
    assert config["player"] == {**DEFAULTS["player"], "trajectories_per_version": 5}
    atomic_json(path, {"engine": "html", "brief": "fixture", "player": {"mode": "gui"}})
    assert load_config(path)["player"]["mode"] == "gui"
    assert DEFAULTS["player"]["parallel_episodes"] == 1
    for options in ({"trajectories_per_version": 0}, {"mode": "unknown"},
                    {"parallel_episodes": 0}, {"parallel_episodes": 1.5},
                    {"policy_call_timeout_seconds": 0}):
        atomic_json(path, {"engine": "html", "brief": "fixture", "player": options})
        with pytest.raises(ValidationError):
            load_config(path)


def test_default_runs_all_three_episodes_serially(tmp_path, policy_config, monkeypatch):
    policy_config["player"].pop("parallel_episodes")
    play = session(tmp_path, policy_config)
    events = []
    def episode(index, spec, policy):
        assert threading.current_thread() is threading.main_thread()
        events.append(index)
        return {"path": str(index)}
    monkeypatch.setattr(play, "episode", episode)
    monkeypatch.setattr(module, "ThreadPoolExecutor", Mock(side_effect=AssertionError("Default must be serial")))
    assert play.options["trajectories_per_version"] == 3
    assert play.run_episodes([{}, {}, {}], {}) == [{"path": str(i)} for i in range(3)]
    assert events == [0, 1, 2]
    assert read_json(play.job / "execution.json") == {"parallel_episodes": 1, "episode_count": 3}


def test_three_policy_workers_overlap_with_ordered_evidence(tmp_path, policy_config):
    policy_config["player"]["parallel_episodes"] = 3
    barrier = threading.Barrier(3, timeout=10)
    class ConcurrentGame(MockGame):
        def start(self):
            super().start()
            if "preflight" not in self.output.parts:
                barrier.wait()  # Formal episodes must still overlap.
    play = session(tmp_path, policy_config, ConcurrentGame)
    report = play.run(MockAgent())
    data = episodes(report, play.job)
    assert [e["index"] for e in data] == [0, 1, 2]
    assert [e["seed"] for e in data] == [42, 43, 44]
    assert all(e["steps"] == 2 and e["status"] == "completed" for e in data)
    assert len({e["video"]["artifact"]["path"] for e in data}) == 3
    assert read_json(play.job / "execution.json")["parallel_episodes"] == 3
    assert valid_play(report, play.job, play.version, allow_mock=True)


def test_parallel_results_keep_plan_order_not_completion_order(tmp_path, policy_config, monkeypatch):
    policy_config["player"]["parallel_episodes"] = 3
    play = session(tmp_path, policy_config)
    barrier = threading.Barrier(3, timeout=5)
    released = [threading.Event() for _ in range(3)]
    completed = []
    def episode(index, spec, policy):
        barrier.wait()
        if index < 2:
            assert released[index + 1].wait(5)
        completed.append(index)
        released[index].set()
        return {"path": str(index)}
    monkeypatch.setattr(play, "episode", episode)
    assert play.run_episodes([{}, {}, {}], {}) == [{"path": "0"}, {"path": "1"}, {"path": "2"}]
    assert completed == [2, 1, 0]


def test_parallelism_is_bounded_for_larger_batches(tmp_path, policy_config, monkeypatch):
    policy_config["player"]["parallel_episodes"] = 2
    play = session(tmp_path, policy_config)
    lock = threading.Lock()
    two_started = threading.Event()
    active = maximum = 0
    def episode(index, spec, policy):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
            if active == 2:
                two_started.set()
        assert two_started.wait(5)
        time.sleep(.01)
        with lock:
            active -= 1
        return index
    monkeypatch.setattr(play, "episode", episode)
    assert play.run_episodes([{}] * 5, {}) == list(range(5))
    assert maximum == 2 and active == 0


def test_parallel_resume_reuses_legacy_policy_and_completed_episode(tmp_path, policy_config):
    play = session(tmp_path, policy_config)
    agent = MockAgent()
    policy = play.author(agent)
    assert "parallel_episodes" not in policy["settings"]
    plan = read_json(play.job / "plan.json")
    first = play.episode(0, plan["episodes"][0], policy["policy"])
    receipt = (play.job / "episodes/0001/completed.json").read_bytes()
    policy_config["player"]["parallel_episodes"] = 3
    resumed = PolicySession(policy_config, play.project, play.job, play.version, MockGame)
    report = resumed.run(agent)
    assert report["episodes"][0] == first
    assert (play.job / "episodes/0001/completed.json").read_bytes() == receipt
    assert len(list(play.job.glob("episodes/*/attempt-*"))) == 3
    assert agent.calls == ["player", "player"]  # Author once, analyze once.
    assert valid_play(report, play.job, play.version, allow_mock=True)


def test_parallel_stop_cleans_up_all_workers_and_preserves_attempts(tmp_path, policy_config, monkeypatch):
    policy_config["player"]["parallel_episodes"] = 3
    barrier = threading.Barrier(3, timeout=10)
    workers = []
    real_worker = module.PolicyWorker
    class TrackingWorker(real_worker):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            workers.append(self)
    class InterruptGame(MockGame):
        def reset(self, seed):
            self.seed = seed
        def act(self, action):
            barrier.wait()
            if self.seed == 42:
                raise Stopped()
            while True:
                self.check_stop()
                time.sleep(.01)
    monkeypatch.setattr(module, "PolicyWorker", TrackingWorker)
    play = session(tmp_path, policy_config, InterruptGame)
    with pytest.raises(Stopped):
        play.run(MockAgent())
    assert len(workers) == 3 and all(w.process.poll() is not None for w in workers)
    assert len(list(play.job.glob("episodes/*/attempt-*/recorded.json"))) == 3
    assert not list(play.job.glob("episodes/*/completed.json"))
    assert not (play.job / "analysis.json").exists()


def test_one_failed_parallel_game_does_not_discard_other_episodes(tmp_path, policy_config):
    policy_config["player"]["parallel_episodes"] = 3
    class OneFailure(MockGame):
        def reset(self, seed):
            self.seed = seed
        def act(self, action):
            if self.seed == 43:
                raise RuntimeError("fixture engine crash")
            super().act(action)
    play = session(tmp_path, policy_config, OneFailure)
    report = play.run(MockAgent())
    assert [e["status"] for e in episodes(report, play.job)] == ["completed", "failed", "completed"]
    assert not valid_play(report, play.job, play.version, allow_mock=True)


def test_parallel_play_still_detects_actual_candidate_mutation(tmp_path, policy_config):
    policy_config["player"]["parallel_episodes"] = 3
    class Mutation(MockGame):
        def reset(self, seed):
            if seed == 42:
                (self.project / "index.html").write_text("mutated game code")
    play = session(tmp_path, policy_config, Mutation)
    with pytest.raises(ValueError, match="Game source changed"):
        play.run(MockAgent())


def test_old_saved_config_without_player_uses_gui(tmp_path, config):
    config.pop("player")
    config["rounds"] = 1
    store = Store(tmp_path / "run")
    store.create(config)
    state = Loop(store, config).run()
    assert state["status"] == "completed"
    report = read_json(store.root / state["best"]["play"])
    assert "format" not in report and len(report["steps"]) == 1


def test_postprocessing_resume_does_not_reexecute_game(tmp_path, policy_config, monkeypatch):
    class RecordedGame(MockGame):
        starts = 0
        def start(self):
            type(self).starts += 1
            super().start()
        def stop(self):
            video = super().stop()
            video["origin"] = "engine"  # Stub only; never presented as real media.
            return video
    calls = []
    def extract(config, video, folder, root, stop):
        calls.append(str(video))
        if len(calls) == 1:
            raise Stopped()
        return []
    monkeypatch.setattr(module, "_extract_frames", extract)
    play = session(tmp_path, policy_config, RecordedGame)
    agent = MockAgent()
    with pytest.raises(Stopped):
        play.run(agent)
    assert RecordedGame.starts == 2
    play.run(agent)
    assert RecordedGame.starts == 4 and calls[0] == calls[1]


def test_extract_keyframes_from_real_local_video(tmp_path, policy_config):
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        pytest.skip("FFmpeg not installed")
    policy_config["ffmpeg"] = ffmpeg
    video = tmp_path / "fixture.mp4"
    subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                    "color=red:size=640x360:rate=2", "-t", "11", str(video)], check=True, timeout=20)
    frames = module._extract_frames(policy_config, video, tmp_path / "frames", tmp_path, lambda: None)
    assert [f["time_seconds"] for f in frames] == [0.0, 10.0]
    assert all(f["clock"] == "video" and (tmp_path / f["image"]["path"]).is_file() for f in frames)


@pytest.mark.parametrize("count", [1, 5])
def test_number_of_trajectories_is_configurable(tmp_path, policy_config, count):
    policy_config["player"]["trajectories_per_version"] = count
    play = session(tmp_path, policy_config)
    report = play.run(MockAgent())
    assert len(report["episodes"]) == count
    assert valid_play(report, play.job, play.version, allow_mock=True)


def test_policy_workspace_aliases_are_normalized(tmp_path, policy_config):
    from tests.conftest import require_symlinks
    require_symlinks(tmp_path)
    real = tmp_path / "real"
    real.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    play = session(alias, policy_config)
    report = play.run(MockAgent())
    assert play.job == real / "player"
    assert valid_play(report, alias / "player", play.version, allow_mock=True)


def test_loop_resumes_same_player_session(tmp_path, policy_config):
    policy_config["rounds"] = 1
    class StopSecondEpisode(MockGame):
        starts = 0
        stopped = False
        def start(self):
            type(self).starts += 1
            super().start()
        def act(self, action):
            if type(self).starts == 3 and not type(self).stopped:
                type(self).stopped = True
                raise Stopped()
            super().act(action)
    store = Store(tmp_path / "run")
    store.create(policy_config)
    agent = MockAgent()
    state = Loop(store, policy_config, agent=agent, game_factory=StopSecondEpisode).run()
    assert state["status"] == "stopped" and state["phase"] == "play"
    marker = store.root / "rounds/0000/player-session-candidate.json"
    before = marker.read_bytes()
    result = Loop(store, policy_config, agent=agent, game_factory=StopSecondEpisode).run(resume=True)
    assert result["status"] == "completed", result["error"]
    assert marker.read_bytes() == before and StopSecondEpisode.starts == 5
    assert agent.calls.count("player") == 2


def test_failure_stays_in_report_and_blocks_promotion(tmp_path, policy_config):
    policy_config["rounds"] = 1
    class BadInput(MockGame):
        def policy_act(self, action):
            raise RuntimeError("input failure")
    store = Store(tmp_path / "run")
    store.create(policy_config)
    state = Loop(store, policy_config, game_factory=BadInput).run()
    assert state["status"] == "completed", state["error"]
    assert state["best"] is None and not state["history"][0]["promoted"]
    path = store.root / state["history"][0]["evidence"]["candidate"]["report"]
    results = episodes(read_json(path), path.parent)
    assert len(results) == 3 and all(r["status"] == "failed" for r in results)


def test_worker_cleanup_keeps_ownership_marker_on_docker_failure(tmp_path, monkeypatch):
    marker = tmp_path / "worker.json"
    atomic_json(marker, {"container": "rsi-policy-" + "a" * 32})
    run = Mock(return_value=Mock(returncode=1, stderr="Docker unavailable"))
    monkeypatch.setattr(module.subprocess, "run", run)
    with pytest.raises(RuntimeError, match="clean up policy container"):
        PolicyWorker.cleanup(marker)
    assert marker.exists()
    run.return_value = Mock(returncode=1, stderr="No such container: already stopped")
    PolicyWorker.cleanup(marker)
    assert not marker.exists()


def test_real_godot_state_and_persistent_input_dispatch(tmp_path):
    from game_agent_lite.agent import child_env
    from game_agent_lite.game import install_bridge
    godot = shutil.which("godot") or "/Applications/Godot.app/Contents/MacOS/Godot"
    if not Path(godot).is_file():
        pytest.skip("Local Godot not installed")
    project = tmp_path / "bridge-fixture"
    project.mkdir()
    (project / "project.godot").write_text("config_version=5\n")
    install_bridge(project, "godot")
    (project / "test.gd").write_text('''extends SceneTree
const Bridge = preload("res://game_agent_bridge.gd")
class Target extends Node:
    func agent_reset(_seed): pass
    func agent_observe(): return {"x": 7}
func _initialize():
    # Stop on assertion failure instead of hanging the fixture indefinitely.
    create_timer(3).timeout.connect(func(): quit(1))
    run.call_deferred()
func run():
    var target = Target.new()
    root.add_child(target)
    target.add_to_group("game_agent_target")
    var bridge = Bridge.new()
    root.add_child(bridge)
    var state = await bridge._dispatch({"id": "state", "token": "", "method": "state"})
    assert(state.result == {"x": 7})
    var request = {"id": "act", "token": "", "method": "policy_act",
        "params": {"keys": ["ArrowRight"], "duration_ms": 16, "pointer": null}}
    await bridge._dispatch(request)
    assert(Input.is_key_pressed(KEY_RIGHT))
    await bridge._dispatch(request)
    assert(Input.is_key_pressed(KEY_RIGHT))
    await bridge._dispatch({"id": "release", "token": "", "method": "release"})
    assert(not Input.is_key_pressed(KEY_RIGHT))
    request.method = "act"
    await bridge._dispatch(request)
    assert(not Input.is_key_pressed(KEY_RIGHT))
    print("POLICY_BRIDGE_OK")
    quit()
''')
    result = subprocess.run([godot, "--headless", "--path", str(project), "--script", "res://test.gd"],
                            capture_output=True, text=True, timeout=30, env=child_env())
    assert result.returncode == 0 and "POLICY_BRIDGE_OK" in result.stdout, result.stdout + result.stderr
    assert "SCRIPT ERROR" not in result.stderr
