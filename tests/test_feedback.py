import copy

import pytest

from game_agent_lite.agent import MockAgent
from game_agent_lite.game import MockGame
from game_agent_lite.loop import Loop
from game_agent_lite.store import (Store, Stopped, artifact, atomic_json, file_hash,
                                   read_json, safe_path, version)


class RecordingAgent(MockAgent):
    def __init__(self, winners=None):
        super().__init__(winners)
        self.designs = []

    def call(self, role, job, context, **kwargs):
        if role == "designer":
            self.designs.append((job, copy.deepcopy(context)))
        result = super().call(role, job, context, **kwargs)
        if role == "player":
            result.update(experience=f"play from {job.parent.parent.name}/{job.name.split('-')[1]}",
                          problems=["背景遮挡目标"], suggestions=["重新生成低对比背景并验证辨识度"])
        if role == "reviewer":
            result["reason"] = f"order {self.calls.count('reviewer')}: B 的背景遮挡目标，建议重新生成。"
            video = read_json(job / "io/B/summary.json")["video"]
            result["evidence"] += ["A/0-after.png", f"B/{video}"]
        return result


def complete(tmp_path, config, agent=None, **kwargs):
    store = Store(tmp_path / "run")
    store.create(config)
    agent = agent or RecordingAgent(["A", "B"])
    loop = Loop(store, config, agent=agent, **kwargs)
    state = loop.run()
    assert state["status"] == "completed", state["error"]
    return store, loop, agent, state


@pytest.mark.parametrize("winners,promoted", [(["A", "B"], False), (["B", "A"], True),
                                             (["B", "B"], False)])
def test_next_design_receives_both_reports_reviews_and_version_mapping(tmp_path, config, winners, promoted):
    config["rounds"] = 3
    store, _, agent, state = complete(tmp_path, config, RecordingAgent(winners))
    first = agent.designs[0][1]["evolution_feedback"]
    assert first["current_best"] is None and first["rounds"] == []
    baseline = agent.designs[1][1]["evolution_feedback"]["rounds"][0]
    assert baseline["reviews"] == [] and baseline["incumbent_version"] is None
    assert baseline["plays"]["incumbent"]["unavailable_reason"] == "not played"

    job, context = agent.designs[2]
    feedback = context["evolution_feedback"]
    assert feedback == read_json(job / "io/evolution_feedback.json")
    entry = feedback["rounds"][-1]
    best_round = 1 if promoted else 0
    assert feedback["current_best"]["version"] == state["history"][best_round]["candidate"]["version"]
    assert entry["promoted"] == promoted
    assert entry["plan"] == read_json(store.root / "rounds/0001/design.json")
    assert entry["check"]["passed"]
    assert entry["plays"]["candidate"]["summary"]["experience"] == "play from 0001/candidate"
    assert entry["plays"]["incumbent"]["summary"]["experience"] == "play from 0001/incumbent"
    assert entry["plays"]["candidate"]["summary"]["suggestions"] == ["重新生成低对比背景并验证辨识度"]
    saved = read_json(store.root / "rounds/0001/review.json")["reviews"]
    assert [r["review"] for r in entry["reviews"]] == [r["review"] for r in saved]
    assert entry["reviews"][0]["mapping"]["B"] == entry["candidate_version"]
    assert entry["reviews"][1]["mapping"]["B"] == entry["incumbent_version"]
    assert entry["reviews"][0]["evidence_files"]["B/play.txt"] == "feedback/0001/candidate/play.txt"
    assert entry["reviews"][1]["evidence_files"]["B/play.txt"] == "feedback/0001/incumbent/play.txt"
    for review in entry["reviews"]:
        assert review["evidence_available"] and review["evidence_valid_at_review"]
        for path in review["evidence_files"].values():
            assert safe_path(job / "io", path).is_file()
    # Summary dependencies are all in the shared mount, not inaccessible host paths.
    bundle = job / "io" / entry["plays"]["candidate"]["evidence_root"]
    summary = read_json(bundle / "summary.json")
    assert (bundle / summary["video"]).is_file()
    for step in summary["steps"]:
        for side in ("before", "after"):
            assert (bundle / step[side]["image"]).is_file()
    # Copies isolate Designer writes from the source evidence and candidate.
    source = store.root / state["history"][1]["evidence"]["candidate"]["report"]
    report = read_json(source)
    original_frame = source.parent / report["steps"][0]["after"]["image"]["path"]
    before = file_hash(original_frame)
    (bundle / "0-after.png").write_bytes(b"designer must not affect original evidence")
    assert file_hash(original_frame) == before
    assert version(store.root / state["best"]["project"]) == state["best"]["version"]
    # No summarizer role or additional model calls are introduced.
    assert agent.calls.count("designer") == 3 and agent.calls.count("reviewer") == 4
    assert set(agent.calls) == {"designer", "builder", "player", "reviewer"}


def test_feedback_rebuilt_on_resume_without_repeating_completed_reviews(tmp_path, config):
    config["rounds"] = 3

    class StopThirdDesign(RecordingAgent):
        def call(self, role, *args, **kwargs):
            if role == "designer" and len(self.designs) == 2:
                raise Stopped()
            return super().call(role, *args, **kwargs)

    store = Store(tmp_path / "run")
    store.create(config)
    interrupted = StopThirdDesign(["A", "B"])
    state = Loop(store, config, agent=interrupted).run()
    assert state["status"] == "stopped" and state["round"] == 2 and state["phase"] == "design"
    old = next((store.root / "rounds/0002/jobs").glob("designer-*/io/evolution_feedback.json"))
    original_receipts = {p: file_hash(p) for p in (store.root / "rounds/0001").glob("*.json")}
    resumed = RecordingAgent()
    result = Loop(store, config, agent=resumed).run(resume=True)
    assert result["status"] == "completed"
    assert resumed.designs[0][1]["evolution_feedback"] == read_json(old)
    assert len(resumed.designs) == 1
    assert interrupted.calls.count("reviewer") == resumed.calls.count("reviewer") == 2
    assert all(file_hash(p) == digest for p, digest in original_receipts.items())


@pytest.mark.parametrize("corruption", ["missing_frame", "wrong_version", "invalid_report", "missing_review"])
def test_unavailable_history_evidence_is_explicit_not_fabricated(tmp_path, config, corruption):
    store, loop, _, state = complete(tmp_path, config)
    source = store.root / state["history"][1]["evidence"]["candidate"]["report"]
    report = read_json(source)
    if corruption == "missing_frame":
        (source.parent / report["steps"][0]["after"]["image"]["path"]).unlink()
    elif corruption == "wrong_version":
        report["version"] = "other-game"
        atomic_json(source, report)
        state["history"][1]["evidence"]["candidate"]["sha256"] = file_hash(source)
    elif corruption == "invalid_report":
        source.write_text("not json")
    else:
        (store.root / "rounds/0001/review.json").unlink()
    feedback = loop.collect_feedback(state, tmp_path / "io")
    entry = feedback["rounds"][-1]
    if corruption == "missing_review":
        assert entry["reviews"] == []
        assert any("review.json" in note for note in entry["unavailable"])
    else:
        play = entry["plays"]["candidate"]
        assert not play["evidence_valid"] and play["summary"] is None
        assert play["evidence_root"] is None and play["unavailable_reason"]
        assert not (tmp_path / "io/feedback/0001/candidate").exists()
        assert len(entry["reviews"]) == 2
        assert all(not review["evidence_available"] for review in entry["reviews"])


def test_build_failure_is_forwarded_without_inventing_play_or_review(tmp_path, config):
    class BadGame(MockGame):
        def build(self):
            return {"passed": False, "error": "build failed"}

    _, _, agent, _ = complete(tmp_path, config, game_factory=BadGame)
    feedback = agent.designs[1][1]["evolution_feedback"]
    entry = feedback["rounds"][0]
    assert feedback["current_best"] is None
    assert entry["check"]["passed"] is False and entry["check"]["error"] == "build failed"
    assert entry["reviews"] == []
    assert entry["plays"]["candidate"]["summary"] is None
    assert "player" not in agent.calls and "reviewer" not in agent.calls


def test_last_five_rounds_keep_original_incumbent_identity(tmp_path, config):
    config["rounds"] = 7
    _, _, agent, state = complete(tmp_path, config, RecordingAgent())
    feedback = agent.designs[-1][1]["evolution_feedback"]
    assert [entry["round"] for entry in feedback["rounds"]] == [1, 2, 3, 4, 5]
    baseline = state["history"][0]["candidate"]["version"]
    assert all(entry["incumbent_version"] == baseline for entry in feedback["rounds"])


def test_asset_requests_and_generated_manifest_reach_next_designer(tmp_path, config):
    class AssetAgent(RecordingAgent):
        def call(self, role, *args, **kwargs):
            result = super().call(role, *args, **kwargs)
            if role == "designer":
                result["assets"] = [{"id": "background", "kind": "image",
                                     "prompt": "低对比卡牌战斗背景", "source": None}]
            return result

    class FakeAssets:
        def generate(self, requests, project, root):
            root.mkdir(parents=True)
            path = root / "background.png"
            path.write_bytes(b"offline generated asset fixture")
            return [{"id": requests[0]["id"], "kind": "image", "model": "mock-image",
                     "artifact": artifact(path, root)}]

    _, _, agent, _ = complete(tmp_path, config, AssetAgent(), assets=FakeAssets())
    entry = agent.designs[1][1]["evolution_feedback"]["rounds"][0]
    assert entry["plan"]["assets"][0]["prompt"] == "低对比卡牌战斗背景"
    assert entry["assets"][0]["id"] == "background"
    assert entry["assets"][0]["model"] == "mock-image"
    assert len(entry["assets"][0]["sha256"]) == 64


def test_invalid_review_citation_cannot_escape_evidence_copy(tmp_path, config):
    store, loop, _, state = complete(tmp_path, config)
    path = store.root / "rounds/0001/review.json"
    value = read_json(path)
    value["reviews"][0]["review"]["evidence"].append("A/../candidate/summary.json")
    value["reviews"][1]["mapping"] = {"A": "unrelated", "B": "unrelated"}
    atomic_json(path, value)
    reviews = loop.collect_feedback(state, tmp_path / "io")["rounds"][-1]["reviews"]
    assert reviews[0]["evidence_files"]["A/../candidate/summary.json"] is None
    assert not reviews[0]["evidence_available"]
    assert all(path is None for path in reviews[1]["evidence_files"].values())
    assert not reviews[1]["evidence_available"]
