"""Offline version-aware handoffs and policy inheritance; no model/provider calls."""
from copy import deepcopy
import shutil

import pytest

from game_agent_lite.agent import MockAgent
from game_agent_lite.game import MockGame, valid_play
from game_agent_lite.loop import Loop
from game_agent_lite.policy import PolicySession
from game_agent_lite.store import Store, Stopped, atomic_json, read_json, version


@pytest.fixture
def policy_config(config):
    config["player"].update(mode="policy", parallel_episodes=3)
    return config


def baseline(tmp_path, config):
    project = tmp_path / "game"
    project.mkdir()
    (project / "index.html").write_text("fixture")
    session = PolicySession(config, project, tmp_path / "baseline", version(project), MockGame,
                            context={"plan": {"acceptance": ["normal flow"]}, "builder_summary": "baseline checks"})
    session.run(MockAgent())
    return session


def child(base, config, name="child", *, project=None, context=None):
    project = project or base.project
    return PolicySession(config, project, base.job.parent / name, version(project), MockGame,
        context=base.context if context is None else context,
        parent={"path": base.job / "play.json", "version": base.version, "reference": "baseline/play.json"})


def test_same_version_reuses_code_but_records_new_games(tmp_path, policy_config):
    base = baseline(tmp_path, policy_config)
    old = (base.job / "play.json").read_bytes()
    agent = MockAgent()
    player = child(base, policy_config)
    report = player.run(agent)
    assert agent.calls == ["player"]  # Analyze only; all three games still run.
    assert report["inheritance"]["mode"] == "reused"
    assert not report["inheritance"]["author_called"]
    assert (player.job / "policy.py").read_bytes() == (base.job / "policy.py").read_bytes()
    previous = read_json(base.job / "play.json")
    assert len(report["episodes"]) == 3 and report["episodes"] != previous["episodes"]
    assert all((player.job / e["path"]).is_file() for e in report["episodes"])
    assert valid_play(report, player.job, player.version, allow_mock=True)
    assert (base.job / "play.json").read_bytes() == old


def test_changed_version_adapts_reference_without_touching_parent(tmp_path, policy_config):
    base = baseline(tmp_path, policy_config)
    original = (base.job / "policy.py").read_bytes()
    project = tmp_path / "new-game"
    shutil.copytree(base.project, project)
    (project / "index.html").write_text("changed fixture")
    handoff = {"plan": {"acceptance": ["new mechanic"]}, "builder_summary": "changed mechanic checked"}

    class Adapt(MockAgent):
        def call(self, role, job, context, **kwargs):
            answer = super().call(role, job, context, **kwargs)
            assert context["handoff"]["plan"] == handoff["plan"]
            if context["phase"] == "policy_author":
                prior = context["prior_policy"]
                assert all(not p.startswith("/") for p in prior.values())
                code = (job / "io" / prior["policy"]).read_text()
                feedback = read_json(job / "io" / prior["feedback"])
                assert len(feedback["episodes"]) == 3
                (job / "io/policy.py").write_text(code.replace("self.steps > 2", "self.steps > 3"))
                answer["summary"] = "Kept controls; extended coverage for new mechanic"
            else:
                assert context["policy_changes"]["mode"] == "adapted"
            return answer

    player = child(base, policy_config, project=project, context=handoff)
    report = player.run(Adapt())
    assert report["inheritance"]["mode"] == "adapted"
    assert report["inheritance"]["source_version"] == base.version
    assert report["version"] != base.version
    assert all(read_json(player.job / e["path"])["steps"] == 3 for e in report["episodes"])
    assert (base.job / "policy.py").read_bytes() == original
    assert valid_play(report, player.job, player.version, allow_mock=True)


@pytest.mark.parametrize("change", ["goal", "seed", "count", "entry"])
def test_changed_test_contract_requires_author_check(tmp_path, policy_config, change):
    base = baseline(tmp_path, policy_config)
    config, context = deepcopy(policy_config), deepcopy(base.context)
    if change == "goal":
        context["plan"]["acceptance"] = ["new goal"]
    elif change == "seed":
        config["seed"] += 1
    elif change == "count":
        config["player"]["trajectories_per_version"] = 4
    else:
        config["entry"] = "other.html"
        context["game"]["entry"] = config["entry"]
    player = child(base, config, context=context)
    agent = MockAgent()
    result = player.author(agent)
    assert agent.calls == ["player"]
    assert result["inheritance"]["author_called"]


def test_resume_keeps_frozen_context_policy_and_completed_episode(tmp_path, policy_config):
    base = baseline(tmp_path, policy_config)
    player = child(base, policy_config)
    agent = MockAgent()
    policy = player.author(agent)
    plan = read_json(player.job / "plan.json")
    first = player.episode(0, plan["episodes"][0], policy["policy"])
    saved = (player.job / "preparation.json").read_bytes()
    resumed = PolicySession(policy_config, player.project, player.job, player.version, MockGame,
                            context={"plan": "unrelated new input"})
    report = resumed.run(agent)
    assert agent.calls == ["player"]  # Analysis only, no regeneration.
    assert report["episodes"][0] == first
    assert (player.job / "preparation.json").read_bytes() == saved
    assert len(list(player.job.glob("episodes/*/attempt-*"))) == 3


def test_interrupted_author_keeps_parent_and_handoff_snapshot(tmp_path, policy_config):
    base = baseline(tmp_path, policy_config)
    context = {"plan": {"acceptance": ["new coverage"]}}
    player = child(base, policy_config, context=context)

    class StopAuthor(MockAgent):
        def call(self, role, job, context, **kwargs):
            raise Stopped()

    with pytest.raises(Stopped):
        player.author(StopAuthor())
    preparation = (player.job / "preparation.json").read_bytes()

    class CheckSnapshot(MockAgent):
        def call(self, role, job, received, **kwargs):
            assert received["handoff"]["plan"] == context["plan"]
            if received["phase"] == "policy_author":
                assert received["prior_policy"] is not None
            return super().call(role, job, received, **kwargs)

    resumed = PolicySession(policy_config, player.project, player.job, player.version, MockGame,
                            context={"plan": "later changes"})
    report = resumed.run(CheckSnapshot())
    assert report["inheritance"]["source_version"] == base.version
    assert (player.job / "preparation.json").read_bytes() == preparation
    assert len(list(player.job.glob("episodes/*/attempt-*"))) == 3


def test_author_cannot_modify_its_prior_policy_reference(tmp_path, policy_config):
    base = baseline(tmp_path, policy_config)
    original = (base.job / "policy.py").read_bytes()
    player = child(base, policy_config, context={"plan": "check changed goal"})

    class Tamper(MockAgent):
        def call(self, role, job, context, **kwargs):
            answer = super().call(role, job, context, **kwargs)
            path = job / "io" / context["prior_policy"]["policy"]
            path.chmod(0o644)
            path.write_text("modified reference")
            return answer

    with pytest.raises(ValueError, match="inherited reference copy"):
        player.author(Tamper())
    assert (base.job / "policy.py").read_bytes() == original


def test_tampered_parent_or_preparation_is_rejected(tmp_path, policy_config):
    base = baseline(tmp_path, policy_config)
    player = child(base, policy_config)
    report = player.run(MockAgent())
    (player.job / "inherited/feedback.json").write_text("{}")
    assert not valid_play(report, player.job, player.version, allow_mock=True)
    with pytest.raises(ValueError, match="Inherited policy reference changed"):
        player.author(MockAgent())
    (base.job / "analysis.json").write_text("{}")
    with pytest.raises(ValueError, match="Baseline policy evidence is invalid"):
        child(base, policy_config, "other").author(MockAgent())


def test_legacy_report_without_handoff_is_reference_not_auto_reuse(tmp_path, policy_config):
    base = baseline(tmp_path, policy_config)
    for name in ("play.json", "policy.json"):
        data = read_json(base.job / name)
        data.pop("preparation")
        data.pop("inheritance")
        atomic_json(base.job / name, data)
    agent = MockAgent()
    player = child(base, policy_config)
    assert player.author(agent)["inheritance"]["author_called"]
    assert agent.calls == ["player"]
    assert (player.job / "inherited/policy.py").is_file()


def test_loop_handoff_uses_best_not_rejected_candidate(tmp_path, policy_config):
    policy_config["rounds"] = 3

    class Tracking(MockAgent):
        def __init__(self):
            super().__init__(["A", "B", "A", "B"])
            self.authors, self.analyses, self.feedback = [], [], []
            self.design_round = -1

        def call(self, role, job, context, **kwargs):
            answer = super().call(role, job, context, **kwargs)
            if role == "designer":
                self.design_round += 1
                self.feedback.append(deepcopy(context["evolution_feedback"]))
                answer["hypothesis"] = f"design-{self.design_round}"
            elif role == "builder":
                if context["stage"] == "integrate":
                    assert context["draft_summary"] == f"draft-{context['round']}"
                answer["summary"] = f"{context['stage']}-{context['round']}"
            elif role == "player":
                handoff = context["handoff"]
                if context["phase"] == "policy_author":
                    self.authors.append(deepcopy(context))
                    assert handoff["builder_summary"] == handoff["plan"]["hypothesis"].replace("design", "integrate")
                else:
                    self.analyses.append(handoff["plan"]["hypothesis"])
            return answer

    store = Store(tmp_path / "run")
    store.create(policy_config)
    agent = Tracking()
    state = Loop(store, policy_config, agent=agent).run()
    assert state["status"] == "completed", state["error"]
    assert state["best"]["round"] == 0
    assert len(agent.authors) == 3  # Incumbents reuse their frozen policy.
    assert agent.authors[0]["prior_policy"] is None
    assert all(c["prior_policy"] for c in agent.authors[1:])
    assert agent.analyses == ["design-0", "design-1", "design-0", "design-2", "design-0"]
    parents = list(store.root.glob("rounds/0002/jobs/player-candidate-*/policy.json"))
    inherited = read_json(parents[0])["inheritance"]
    assert inherited["source_version"] == state["best"]["version"]
    assert "rounds/0000/" in inherited["source_report"]
    assert agent.feedback[2]["rounds"][1]["builder_summaries"] == {
        "draft": "draft-1", "integrate": "integrate-1"}
    assert not agent.feedback[2]["rounds"][1]["promoted"]


def test_missing_old_handoff_and_gui_baseline_are_optional(tmp_path, policy_config):
    config = deepcopy(policy_config)
    config["rounds"] = 1
    config["player"]["mode"] = "gui"
    store = Store(tmp_path / "run")
    store.create(config)
    loop = Loop(store, config)
    state = loop.run()
    best = state["best"]
    directory = (store.root / best["project"]).parent.parent
    (directory / "design.json").unlink()
    context = loop.player_handoff(best)
    assert context["plan"] is None
    player = PolicySession(policy_config, store.root / best["project"], tmp_path / "new-player",
        best["version"], MockGame, context=context,
        parent={"path": store.root / best["play"], "version": best["version"], "reference": best["play"]})
    agent = MockAgent()
    assert player.author(agent)["inheritance"]["mode"] == "new"
    assert agent.calls == ["player"]
