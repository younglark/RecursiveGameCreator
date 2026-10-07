"""Relocation, write isolation, evidence retention and independent review scheduling."""
import copy
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import shutil
import threading
import time

import pytest

from game_agent_lite.agent import MockAgent
from game_agent_lite.cli import main, load_config
from game_agent_lite.loop import Loop
from game_agent_lite.paths import load_saved_config
from game_agent_lite.policy import PolicySession, valid_policy_play
from game_agent_lite.game import MockGame
from game_agent_lite.resources import GameLease
from game_agent_lite.store import Store, atomic_json, copy_file, read_json, snapshot, version


def test_relocated_config_and_run_resume_without_original_directory(tmp_path, config):
    old = tmp_path / 'old checkout'
    old.mkdir()
    seed = old / 'games/source'
    seed.mkdir(parents=True)
    (seed / 'index.html').write_text('fixture')
    config.update(source=str(seed), auth_file=str(old / 'local-auth.json'))
    config['assets']['env_file'] = str(old / '.env.hunyuan')
    store = Store(old / 'runs/demo')
    store.create(config)
    saved = read_json(store.root / 'config.json')
    assert all(not Path(saved[k]).is_absolute() for k in ('source', 'auth_file'))
    assert not Path(saved['assets']['env_file']).is_absolute()
    assert str(old) not in (store.root / 'config.json').read_text()
    new = tmp_path / 'relocated checkout with spaces'
    old.rename(new)
    run = new / 'runs/demo'
    loaded = load_saved_config(run)
    assert Path(loaded['source']) == new / 'games/source'
    assert Path(loaded['auth_file']) == new / 'local-auth.json'
    assert main(['resume', str(run)]) == 0
    state = read_json(run / 'state.json')
    assert state['status'] == 'completed'
    assert version(run / state['best']['project']) == state['best']['version']


def test_independent_copy_and_output_exclusion(tmp_path):
    source, target = tmp_path / 'source', tmp_path / 'target'
    source.mkdir()
    (source / 'asset.bin').write_bytes(b'a' * 1048576)
    (source / 'tests').mkdir()
    (source / 'tests/test_game.py').write_text('# retain executable tests')
    (source / '.rgc-output').mkdir()
    (source / '.rgc-output/capture.png').write_bytes(b'not game source')
    before = version(source)
    snapshot(source, target)
    assert version(target) == before
    assert not (target / '.rgc-output').exists()
    assert (target / 'tests/test_game.py').exists()
    assert (target / 'asset.bin').stat().st_ino != (source / 'asset.bin').stat().st_ino
    (target / 'asset.bin').write_bytes(b'changed')
    assert (source / 'asset.bin').read_bytes() == b'a' * 1048576
    (source / '.rgc-output/capture.png').write_bytes(b'changed diagnostic')
    assert version(source) == before


def test_builder_handoff_keeps_validation_outside_next_project(tmp_path, config):
    class Builder(MockAgent):
        def call(self, role, job, context, **kwargs):
            value = super().call(role, job, context, **kwargs)
            if role == 'builder':
                if context['stage'] == 'integrate':
                    handoff = context['draft_handoff']
                    assert handoff['checks'][0]['evidence_available']
                    assert handoff['checks'][0]['input_hashes']['mock-artifact.txt']
                    assert handoff['remaining'] == ['inspect integration']
                output = job / 'io/validation'
                output.mkdir(parents=True)
                (output / 'check.txt').write_text('fixture check')
                return {**value, 'checks': [{'name': 'fixture', 'command': 'fixture command',
                    'inputs': ['mock-artifact.txt'], 'status': 'passed', 'details': 'fixture',
                    'evidence': ['validation/check.txt']}], 'remaining': ['inspect integration']}
            return value
    config['rounds'] = 1
    store = Store(tmp_path / 'run'); store.create(config)
    state = Loop(store, config, agent=Builder()).run()
    assert state['status'] == 'completed'
    project = store.root / state['best']['project']
    assert not list(project.rglob('check.txt'))
    handoff = read_json(store.round_dir(0) / 'integrate-handoff.json')
    assert handoff['checks'][0]['evidence_files'][0]['path'].endswith('/io/validation/check.txt')


class ConcurrentReviewer:
    def __init__(self):
        self.other = MockAgent()
        self.barrier = threading.Barrier(2)
        self.reviews = []
        self.lock = threading.Lock()

    def call(self, role, job, context, **kwargs):
        if role != 'reviewer':
            return self.other.call(role, job, context, **kwargs)
        self.barrier.wait(timeout=5)
        with self.lock:
            self.reviews.append((version(job / 'io/A'), version(job / 'io/B')))
        return {'winner': 'tie', 'reason': 'fixture', 'evidence': ['A/summary.json', 'B/summary.json']}


def test_reviews_overlap_but_remain_anonymous_and_ordered(tmp_path, config):
    config['execution'] = {'parallel_reviews': 2}
    store = Store(tmp_path / 'run'); store.create(config)
    agent = ConcurrentReviewer()
    state = Loop(store, config, agent=agent).run()
    assert state['status'] == 'completed'
    assert len(agent.reviews) == 2
    assert agent.reviews[0] == tuple(reversed(agent.reviews[1]))
    reviews = read_json(store.round_dir(1) / 'review.json')['reviews']
    assert reviews[0]['mapping']['A'] == reviews[1]['mapping']['B']
    assert not state['history'][-1]['promoted']


def test_parallel_review_failure_resumes_only_missing_comparison(tmp_path, config):
    class ReviewOnce:
        def __init__(self):
            self.other = MockAgent(); self.calls = 0; self.fail = True
            self.lock = threading.Lock()
        def call(self, role, job, context, **kwargs):
            if role != 'reviewer': return self.other.call(role, job, context, **kwargs)
            with self.lock:
                self.calls += 1; number = self.calls
            if self.fail and number == 2:
                deadline = time.monotonic() + 5
                while not list(job.parent.parent.glob('comparison-*.json')):
                    if time.monotonic() > deadline: raise AssertionError('sibling did not commit')
                    time.sleep(.01)
                raise RuntimeError('fixture review failure')
            return {'winner': 'tie', 'reason': 'fixture', 'evidence': ['A/summary.json', 'B/summary.json']}
    store = Store(tmp_path / 'run'); store.create(config)
    agent = ReviewOnce(); loop = Loop(store, config, agent=agent)
    assert loop.run()['status'] == 'failed'
    saved = list(store.round_dir(1).glob('comparison-*.json'))
    assert len(saved) == 1
    original = saved[0].read_bytes()
    agent.fail = False
    assert loop.run(resume=True)['status'] == 'completed'
    assert agent.calls == 3 and saved[0].read_bytes() == original


def test_host_game_limit_waits_and_releases(tmp_path):
    first = GameLease(1, root=tmp_path)
    second = GameLease(3, root=tmp_path)
    first.acquire()
    ready = threading.Event()
    def run():
        second.acquire(); ready.set(); second.release()
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(run)
        try:
            assert not ready.wait(.2)
        finally:
            first.release()
        future.result(timeout=3)
    assert ready.is_set()
    third = GameLease(1, root=tmp_path)
    third.acquire(); third.release()


def test_compact_player_preserves_evidence_and_can_recover(tmp_path, config):
    config['player']['mode'] = 'policy'
    config['storage'] = {'keep_workspaces': False}
    project = tmp_path / 'project'; project.mkdir()
    (project / 'index.html').write_text('fixture')
    job = tmp_path / 'player'
    session = PolicySession(config, project, job, version(project), MockGame)
    report = session.run(MockAgent())
    assert valid_policy_play(report, job, version(project), allow_mock=True)
    assert not list(job.glob('author-*/source'))
    assert (job / 'workspace-cleanup.json').exists()
    class NoCalls:
        def call(self, *a, **kw): raise AssertionError('completed session should be reused')
    assert session.run(NoCalls()) == report


def test_export_is_standalone_and_excludes_execution_files(tmp_path, config):
    from game_agent_lite.delivery import export_game
    store = Store(tmp_path / 'run'); store.create(config)
    state = Loop(store, config).run()
    project = store.root / state['best']['project']
    (project / '.rgc-output').mkdir()
    (project / '.rgc-output/private-log.txt').write_text('not in the export')
    output = export_game(store.root, tmp_path / 'published game')
    manifest = read_json(output / 'manifest.json')
    shutil.rmtree(store.root)
    assert version(output / 'game') == manifest['version']
    assert not (output / 'game/.rgc-output').exists()
    assert not (output / 'config.json').exists()
    assert all(not Path(p).is_absolute() for p in manifest['files'])
