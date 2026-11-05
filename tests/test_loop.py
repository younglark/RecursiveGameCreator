import json

import pytest

from game_agent_lite.agent import MockAgent
from game_agent_lite.game import MockGame
from game_agent_lite.loop import Loop, promotion
from game_agent_lite.store import Store, Stopped, atomic_json, read_json, version


def run(tmp_path, config, **kwargs):
    store = Store(tmp_path / "run")
    store.create(config)
    loop = Loop(store, config, **kwargs)
    return store, loop, loop.run()


def test_full_loop_baseline_then_candidate_promotion(tmp_path, config):
    store, _, state = run(tmp_path, config, agent=MockAgent(["B", "A"]))
    assert state["status"] == "completed"
    assert state["best"]["round"] == 1
    assert len(state["history"]) == 2
    assert all(item["promoted"] for item in state["history"])
    assert state["history"][0]["candidate"]["project"] != state["best"]["project"]
    assert "initial validated baseline" in state["history"][0]["reason"]
    assert version(store.root / state["best"]["project"]) == state["best"]["version"]
    review = read_json(store.root / "rounds/0001/review.json")
    assert [r["review"]["winner"] for r in review["reviews"]] == ["B", "A"]
    bundles = list((store.root / "rounds/0001/jobs").glob("reviewer-*/io/A/summary.json"))
    assert len(bundles) == 2
    for path in bundles:
        body = path.read_text()
        assert '"version"' not in body and '"builder"' not in body


@pytest.mark.parametrize("winners", [["A", "B"], ["B", "B"], ["tie", "A"], ["unavailable", "A"]])
def test_rejected_or_inconsistent_preference_keeps_best(tmp_path, config, winners):
    _, _, state = run(tmp_path, config, agent=MockAgent(winners))
    assert state["status"] == "completed"
    assert state["best"]["round"] == 0
    assert state["history"][1]["promoted"] is False


def test_build_failure_rejects_without_player_or_reviewer(tmp_path, config):
    class BadGame(MockGame):
        def build(self):
            return {"passed": False}
    agent = MockAgent()
    _, _, state = run(tmp_path, config, agent=agent, game_factory=BadGame)
    assert state["status"] == "completed" and state["best"] is None
    assert "player" not in agent.calls and "reviewer" not in agent.calls


def test_resume_does_not_repeat_completed_stages(tmp_path, config):
    class InterruptOnce(MockAgent):
        def call(self, role, *args, **kwargs):
            if role == "builder" and "builder" not in self.calls:
                self.calls.append(role)
                raise Stopped()
            return super().call(role, *args, **kwargs)
    agent = InterruptOnce(["B", "A"])
    store, loop, first = run(tmp_path, config, agent=agent)
    assert first["status"] == "stopped" and first["phase"] == "draft"
    directory = store.round_dir(0)
    assert not (directory / "draft-builder.json").exists()
    assert len(list(directory.glob("draft-*/project"))) == 1
    state = loop.run(resume=True)
    assert state["status"] == "completed"
    assert len(list(directory.glob("draft-*/project"))) == 2
    assert agent.calls.count("designer") == 2
    assert len(state["history"]) == 2


@pytest.mark.parametrize("phase", ["draft", "integrate"])
def test_resume_reuses_successful_builder_after_host_snapshot_failure(tmp_path, config, phase):
    from tests.conftest import require_symlinks
    require_symlinks(tmp_path)
    config["rounds"] = 1

    class CreatesSymlink(MockAgent):
        broken_link = None
        def call(self, role, job, context, **kwargs):
            result = super().call(role, job, context, **kwargs)
            if role == "builder" and context["stage"] == phase:
                self.broken_link = kwargs["project"] / "asset.glb"
                self.broken_link.symlink_to(tmp_path / "missing-asset")
            return result

    agent = CreatesSymlink()
    store, _, state = run(tmp_path, config, agent=agent)
    assert state["status"] == "failed" and state["phase"] == phase
    assert "Snapshot does not accept symlinks" in state["error"]
    directory = store.round_dir(0)
    checkpoint = read_json(directory / f"{phase}-builder.json")
    assert not (directory / f"{phase}.json").exists()
    assert state["best"] is None and state["history"] == []
    agent.broken_link.unlink()
    agent.broken_link.write_bytes(b"repaired asset")
    # A new controller process can reuse the durable result. The agent would
    # recreate the bad link if it were invoked again for this phase.
    result = Loop(store, config, agent=agent).run(resume=True)
    assert result["status"] == "completed"
    assert agent.calls.count("builder") == 2  # draft + integrate, exactly once each
    sealed = read_json(directory / f"{phase}.json")
    assert sealed["project"] == checkpoint["project"]
    assert sealed["version"] == version(store.root / sealed["project"])
    assert result["best"] is not None


@pytest.mark.parametrize("corruption", ["missing_project", "other_round", "bad_result"])
def test_builder_checkpoint_cannot_silently_start_a_new_call(tmp_path, config, corruption):
    store = Store(tmp_path / "run")
    store.create(config)
    directory = store.round_dir(0)
    project = directory / "draft-saved/project"
    if corruption == "other_round":
        project = store.round_dir(1) / "draft-saved/project"
    if corruption != "missing_project":
        project.mkdir(parents=True)
    atomic_json(directory / "draft-builder.json", {
        "project": project.relative_to(store.root).as_posix(),
        "builder": {} if corruption == "bad_result" else {"summary": "done"}})
    state = store.load()
    state.update(status="failed", phase="draft")
    store.save(state)
    agent = MockAgent()
    result = Loop(store, config, agent=agent).run(resume=True)
    assert result["status"] == "failed"
    assert agent.calls == []
    assert result["best"] is None


def test_stage_receipt_replayed_after_state_write_crash(tmp_path, config, monkeypatch):
    store = Store(tmp_path / "run")
    store.create(config)
    agent = MockAgent()
    loop = Loop(store, config, agent=agent)
    directory = store.round_dir(0)
    atomic_json(directory / "design.json", loop.stage("design", store.load(), directory))
    assert store.load()["phase"] == "design"
    state = loop.run(resume=True)
    assert state["status"] == "completed"
    assert agent.calls.count("designer") == 2


def test_stop_marker_and_best_tampering(tmp_path, config):
    store = Store(tmp_path / "run")
    store.create(config)
    store.request_stop()
    loop = Loop(store, config)
    assert loop.run()["status"] == "stopped"
    state = loop.run(resume=True)
    state.update(status="stopped", round=1, phase="design")
    store.save(state)
    (store.root / state["best"]["project"] / "mock-artifact.txt").write_text("tampered")
    assert loop.run(resume=True)["status"] == "failed"


@pytest.mark.parametrize("corruption", ["missing_image", "wrong_version", "missing_video", "forged_summary"])
def test_corrupt_play_evidence_cannot_promote(tmp_path, config, corruption):
    class CorruptLoop(Loop):
        def stage(self, phase, state, directory):
            result = super().stage(phase, state, directory)
            if phase == "play" and state["round"] == 1:
                path = self.store.root / result["candidate"]["report"]
                data = read_json(path)
                if corruption == "missing_image":
                    (path.parent / data["steps"][0]["after"]["image"]["path"]).unlink()
                elif corruption == "missing_video":
                    (path.parent / data["video"]["artifact"]["path"]).unlink()
                else:
                    data["version" if corruption == "wrong_version" else "summary"] = "forged"
                    atomic_json(path, data)
            return result
    store = Store(tmp_path / "run")
    store.create(config)
    state = CorruptLoop(store, config, agent=MockAgent(["B", "A"])).run()
    assert state["status"] == "completed"
    assert state["best"]["round"] == 0
    assert not state["history"][1]["promoted"]


def test_missing_citations_rejects_positive_reviews(tmp_path, config):
    class NoEvidence(MockAgent):
        def call(self, role, *args, **kwargs):
            result = super().call(role, *args, **kwargs)
            if role == "reviewer":
                result["evidence"] = []
            return result
    _, _, state = run(tmp_path, config, agent=NoEvidence(["B", "A"]))
    assert state["best"]["round"] == 0


def test_promotion_gate_requires_both_orders_and_evidence():
    reviews = [{"preferred_version": "new", "evidence_valid": True}] * 2
    assert promotion({"passed": True}, "new", "old", reviews, evidence_valid=True)["promoted"]
    assert not promotion({"passed": True}, "new", "old", reviews, evidence_valid=False)["promoted"]
    assert not promotion({"passed": False}, "new", "old", reviews, evidence_valid=True)["promoted"]
    assert not promotion({"passed": True}, "same", "same", reviews, evidence_valid=True)["promoted"]


def test_uncertain_assets_stop_loop_and_preserve_stage(tmp_path, config):
    from game_agent_lite.store import UncertainSubmission
    class UncertainAssets:
        def generate(self, *args):
            raise UncertainSubmission("existing submission needs reconciliation")
    store, _, state = run(tmp_path, config, assets=UncertainAssets())
    assert state["status"] == "needs_reconciliation"
    assert state["phase"] == "assets"
    assert (store.root / "rounds/0000/draft.json").exists()
    assert not state["history"]


def test_saved_selection_is_rechecked_on_resume(tmp_path, config):
    config["rounds"] = 1
    store, loop, completed = run(tmp_path, config)
    state = store.load()
    state.update(status="stopped", round=0, phase="select", history=[], best=None)
    store.save(state)
    selected = read_json(store.root / "rounds/0000/select.json")
    (store.root / selected["candidate"]["project"] / "mock-artifact.txt").write_text("changed after receipt")
    result = loop.run(resume=True)
    assert result["status"] == "failed" and result["best"] is None


def test_interrupted_comparison_reuses_completed_order(tmp_path, config):
    class StopSecondReview(MockAgent):
        stopped = False
        def call(self, role, *args, **kwargs):
            if role == "reviewer" and self.calls.count("reviewer") == 1 and not self.stopped:
                self.stopped = True
                raise Stopped()
            return super().call(role, *args, **kwargs)
    agent = StopSecondReview(["B", "A"])
    store, loop, state = run(tmp_path, config, agent=agent)
    assert state["phase"] == "review" and state["status"] == "stopped"
    assert (store.root / "rounds/0001/comparison-0.json").exists()
    result = loop.run(resume=True)
    assert result["best"]["round"] == 1
    assert agent.calls.count("reviewer") == 2
