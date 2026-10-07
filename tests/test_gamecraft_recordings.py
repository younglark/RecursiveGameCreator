"""Native recordings: real PNG files, synthetic input, stub judges; no rollout/network."""
from copy import deepcopy
import importlib.util
import json
import shutil
from pathlib import Path
import struct
import sys
from types import SimpleNamespace
import zlib

import pytest

from game_agent_lite.gamecraft_recordings import (CAPTURE, check_frames, export_recordings,
                                                finish_capture, validate_recordings)
from game_agent_lite.store import artifact, atomic_json, read_json, version

ROOT = Path(__file__).resolve().parents[1]


def png(path, width=854, height=480):
    def chunk(name, data):
        return struct.pack('>I', len(data)) + name + data + struct.pack('>I', zlib.crc32(name + data))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0)) +
                    chunk(b'IDAT', zlib.compress((b'\0' + b'\x80' * width * 3) * height)) + chunk(b'IEND', b''))


def capture_files(engine, *, slots=(0, 1, 2), duration=1.3):
    folder = engine / 'capture'
    folder.mkdir(parents=True, exist_ok=True)
    frames, timings = [], []
    for slot in slots:
        path = folder / f'video-{slot:06d}.png'
        png(path)
        actual = slot / 2 + .01
        timings.append({'file': path.name, 'time': 10 + actual, 'time_seconds': actual,
                        'planned_seconds': slot / 2, 'slot': slot})
        frames.append({'clock': CAPTURE['clock'], 'slot': slot, 'planned_seconds': slot / 2,
                       'time_seconds': actual, 'image': artifact(path, engine)})
    atomic_json(folder / 'episode.json', {**CAPTURE, 'origin_usec': 10000000, 'duration_seconds': duration})
    (folder / 'timing.jsonl').write_text(''.join(json.dumps(t) + '\n' for t in timings))
    return frames


class Encoder:
    def run(self, command, cwd, log, **kw):
        assert '-fps_mode' in command and command[command.index('-fps_mode') + 1] == 'vfr'
        assert '-vf' not in command  # Never interpolate or resample canonical PNG evidence.
        Path(command[-1]).write_bytes(b'fixture-preview-not-for-decoding')
        log.write_text('fixture')


def fixture_bundle(tmp_path, count=3):
    project, job = tmp_path / 'source', tmp_path / 'player'
    project.mkdir(parents=True)
    (project / 'project.godot').write_text('config_version=5\n')
    job.mkdir()
    (job / 'policy.gd').write_text('extends RefCounted\n')
    specs = [{'profile': f'p{i}', 'purpose': f'purpose{i}', 'goal': f'goal{i}', 'scenario': None} for i in range(count)]
    atomic_json(job / 'plan.json', {'summary': 'fixture', 'episodes': specs})
    report = {'version': version(project), 'policy': artifact(job / 'policy.gd', job),
              'plan': artifact(job / 'plan.json', job), 'settings': {}, 'episodes': []}
    for i, spec in enumerate(specs):
        engine = job / f'episode-{i}'
        capture_files(engine)
        game = SimpleNamespace(frames=engine / 'capture', output=engine, config={'ffmpeg': 'ffmpeg'}, runner=Encoder())
        raw = finish_capture(game)
        frames = [{**f, 'image': artifact(engine / f['image']['path'], job)} for f in raw.pop('raw_frames')]
        video = {**{k: v for k, v in raw.items() if k != 'path'}, 'artifact': artifact(raw['path'], job)}
        for k in ('capture_receipt', 'capture_timing'):
            video[k] = artifact(engine / raw[k]['path'], job)
        for name in ('trajectory.jsonl', 'input_events.jsonl'):
            (engine / name).write_text('{"type":"fixture"}\n')
        result = {'version': report['version'], 'policy_sha256': report['policy']['sha256'], 'index': i,
                  **spec, 'seed': i, 'status': 'completed', 'reason': 'fixture', 'elapsed_seconds': 1.3,
                  'capture_duration_seconds': 1.3, 'frames': frames, 'video': video,
                  'trajectory': artifact(engine / 'trajectory.jsonl', job),
                  'input_events': artifact(engine / 'input_events.jsonl', job)}
        atomic_json(engine / 'result.json', result)
        report['episodes'].append(artifact(engine / 'result.json', job))
    session = SimpleNamespace(job=job, project=project, version=report['version'], context={'round': 2},
                              config={'benchmark': {'task': 'puzzle-fixture'}})
    bundle = tmp_path / 'recordings'
    export_recordings(session, report, bundle)
    return bundle, session, report


@pytest.fixture
def bundle(tmp_path):
    return fixture_bundle(tmp_path)[0]


def test_bundle_round_trip_preserves_all_episodes_and_source(tmp_path):
    bundle, session, report = fixture_bundle(tmp_path)
    project, data, _ = validate_recordings(bundle)
    assert version(project) == version(session.project) == report['version']
    assert data['evidence_source'] == 'player_recording' and data['replay_performed'] is False
    assert [d['episode'] for d in data['demos']] == [1, 2, 3]
    assert len(data['demos'][0]['frames']) == 3  # No padding to 40.
    assert not (project / 'demo_outputs').exists()  # No fake replay JSON required.


def test_capture_20_seconds_and_missing_slots_are_honest(tmp_path):
    frames = capture_files(tmp_path, slots=tuple(range(40)), duration=20)
    assert check_frames(frames, 20, tmp_path)['missing_slots'] == []
    frames.pop(12)
    assert check_frames(frames, 20, tmp_path)['missing_slots'] == [12]
    frames.append(deepcopy(frames[-1]))
    with pytest.raises(ValueError, match='order|slot'):
        check_frames(frames, 20, tmp_path)


@pytest.mark.parametrize('change', ['game', 'frame', 'video', 'input', 'missing', 'timestamp', 'duration',
                                    'count', 'duplicate', 'escape', 'resolution', 'plan', 'quality'])
def test_tampered_or_mismatched_recordings_rejected(bundle, change):
    manifest = read_json(bundle / 'manifest.json')
    d = manifest['demos'][0]
    if change == 'game':
        (bundle / 'game/project.godot').write_text('changed')
    elif change in ('video', 'input'):
        (bundle / d['video' if change == 'video' else 'input_events']['path']).write_text('changed')
    elif change == 'frame':
        (bundle / d['frames'][0]['image']['path']).write_bytes(b'changed')
    elif change == 'missing':
        (bundle / d['frames'][0]['image']['path']).unlink()
    elif change == 'timestamp':
        d['frames'][0]['time_seconds'] = .02
    elif change == 'duration':
        d['duration_seconds'] = 21
    elif change == 'count':
        manifest['demos'].pop()
    elif change == 'duplicate':
        d['frames'][1] = deepcopy(d['frames'][0])
    elif change == 'escape':
        d['frames'][0]['image']['path'] = '../outside.png'
    elif change == 'resolution':
        path = bundle / d['frames'][0]['image']['path']
        png(path, 640, 360)
        d['frames'][0]['image'] = artifact(path, bundle)
    elif change == 'plan':
        d['goal'] = 'different'
    elif change == 'quality':
        d['capture_quality']['missing_slots'] = [1]
    atomic_json(bundle / 'manifest.json', manifest)
    with pytest.raises(ValueError):
        validate_recordings(bundle)


def test_scoring_uses_real_frames_upstream_judge_and_aggregation(bundle, tmp_path, monkeypatch):
    if not (ROOT / 'benchmarks/gamecraft-bench/gamecraft_bench/verifier/score.py').is_file():
        pytest.skip('Clone GameCraft-Bench to check the upstream scoring integration')
    monkeypatch.syspath_prepend(str(ROOT / 'benchmarks/gamecraft-bench'))
    from gamecraft_bench.verifier import score as upstream
    from game_agent_lite.gamecraft_recording_score import score_recordings
    calls = []
    def forbidden(*a, **kw):
        raise AssertionError('No replay, decoding, Docker or paid judge')
    monkeypatch.setattr(upstream, 'replay_trace', forbidden)
    monkeypatch.setattr(upstream, '_sample_frames', forbidden)
    monkeypatch.setattr(upstream, '_run_build_check', lambda *a: (True, 'fixture build'))
    values = iter([(.2, .9), (.8, .6), (.5, .3)])
    class Judge:
        model = 'offline-fixture'
        def score(self, request):
            calls.append(request)
            assert len(request.frame_paths) == 3
            assert request.frame_paths[0].read_bytes() == (bundle / read_json(bundle / 'manifest.json')['demos'][0]['frames'][0]['image']['path']).read_bytes()
            m, v = next(values)
            return SimpleNamespace(scores={'M': m, 'V': v}, rationales={}, raw='fixture')
    rubric = tmp_path / 'rubric.json'
    atomic_json(rubric, {'build_check': {'id': 'BUILD', 'cmd': 'never'}, 'score_formula': 'BUILD * (M + V) / 2',
        'requirements': [{'id': 'M', 'description': 'fixture mechanic', 'agg': 'max'},
                         {'id': 'V', 'description': 'fixture visual', 'agg': 'mean'}]})
    output = tmp_path / 'score'
    result = score_recordings(bundle, rubric, output, Judge())
    assert len(calls) == 3 and result.reward == pytest.approx(.7)
    data = read_json(output / 'breakdown.json')
    assert data['replay_performed'] is False and 'trace' not in data['demos'][0]
    assert data['requirements'][0]['aggregated'] == .8
    assert data['requirements'][1]['aggregated'] == pytest.approx(.6)


@pytest.mark.parametrize('budget', [None, 4096])
def test_scoring_cli_native_dry_run_is_read_only(bundle, tmp_path, monkeypatch, capsys, budget):
    spec = importlib.util.spec_from_file_location('score_native', ROOT / 'scripts/score_gamecraft.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    bench = tmp_path / 'bench'
    for name in ('tasks/puzzle-fixture/tests/rubric.json', 'gamecraft_bench/verifier/cli.py'):
        path = bench / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{}')
    monkeypatch.setattr(module.subprocess, 'run', lambda *a, **kw: pytest.fail('no subprocess'))
    monkeypatch.setattr(module.subprocess, 'check_output', lambda *a, **kw: pytest.fail('no subprocess'))
    output = tmp_path / 'score'
    extra = [] if budget is None else ['--judge-max-tokens', str(budget)]
    assert module.main(['--task', 'puzzle-fixture', '--recordings', str(bundle), '--benchmark', str(bench),
                        '--output', str(output), '--dry-run', *extra]) == 0
    assert not output.exists()
    text = capsys.readouterr().out
    assert 'no JSON replay' in text and 'entrypoint-current.py' in text
    command = json.loads(text[text.index('{\n'):])['command']
    assert ('--judge-max-tokens' in command) == (budget is not None)
    if budget is not None:
        assert command[command.index('--judge-max-tokens') + 1] == '4096'


@pytest.mark.parametrize('failure', ['build', 'judge', 'formula'])
def test_scoring_failures_are_not_successful_measurements(bundle, tmp_path, monkeypatch, failure):
    if not (ROOT / 'benchmarks/gamecraft-bench/gamecraft_bench/verifier/score.py').is_file():
        pytest.skip('Clone GameCraft-Bench to check the upstream scoring integration')
    monkeypatch.syspath_prepend(str(ROOT / 'benchmarks/gamecraft-bench'))
    from gamecraft_bench.verifier import score as upstream
    from gamecraft_bench.verifier.judges.base import JudgeError
    from game_agent_lite import gamecraft_recording_score as scorer
    monkeypatch.setattr(upstream, '_run_build_check', lambda *a: (failure != 'build', 'fixture'))
    monkeypatch.setattr(scorer.time, 'sleep', lambda *a: None)
    calls = []
    class Judge:
        model = 'offline-fixture'
        def score(self, request):
            calls.append(request.demo_id)
            if failure == 'judge':
                raise JudgeError('offline fixture failure')
            return SimpleNamespace(scores={'M': .5}, rationales={}, raw='fixture')
    rubric = tmp_path / 'rubric.json'
    atomic_json(rubric, {'build_check': {'id': 'BUILD', 'cmd': 'never'},
        'score_formula': 'invalid(' if failure == 'formula' else 'BUILD * M',
        'requirements': [{'id': 'M', 'description': 'fixture', 'agg': 'max'}]})
    result = scorer.score_recordings(bundle, rubric, tmp_path / 'score', Judge())
    if failure == 'build':
        assert not result.build_ok and not calls and not result.demos
    else:
        assert result.errors
        assert len(calls) == 3 * (upstream._JUDGE_MAX_ATTEMPTS if failure == 'judge' else 1)


def test_native_player_session_and_resume_without_replay(tmp_path, config, monkeypatch):
    from game_agent_lite.agent import MockAgent
    from game_agent_lite.game import MockGame, valid_play
    from game_agent_lite.policy import PolicySession
    from game_agent_lite import gamecraft_player
    config.update(engine='godot', entry='project.godot')
    config['player']['mode'] = 'policy'
    config['benchmark'] = {'name': 'gamecraft', 'task': 'puzzle-fixture', 'path': str(tmp_path),
                           'evidence_source': 'player_recording'}
    project = tmp_path / 'project'
    project.mkdir()
    (project / 'project.godot').write_text('config_version=5\n')
    class Engine(MockGame):
        def native_input_events(self):
            return self.output / 'input_events.jsonl'
        def native_policy_start(self, **settings):
            value = super().native_policy_start(**settings)
            self.native_input_events().write_text('{"type":"fixture"}\n')
            return value
        def observe(self):
            if 'preflight' in self.output.parts:
                return super().observe()
            pytest.fail('Native mode must not add host-clock screenshots')
        def native_policy_status(self):
            value = super().native_policy_status()
            return {**value, 'elapsed_seconds': 1.3}
        def stop(self):
            capture_files(self.output)
            return finish_capture(SimpleNamespace(frames=self.output / 'capture', output=self.output,
                                  config={'ffmpeg': 'ffmpeg'}, runner=Encoder()))
    def forbidden(*a, **kw):
        pytest.fail('Native Player must not replay or re-extract preview frames')
    monkeypatch.setattr(gamecraft_player, 'replay_submission', forbidden)
    monkeypatch.setattr('game_agent_lite.policy._extract_frames', forbidden)
    session = PolicySession(config, project, tmp_path / 'job', version(project), Engine)
    agent = MockAgent()
    report = session.run(agent)
    assert valid_play(report, session.job, session.version, allow_mock=True)
    assert session.run(agent) == report and len(agent.calls) == 2
    data = read_json(session.job / 'gamecraft.json')
    assert data['status'] == 'recorded' and data['replay_performed'] is False
    assert not (session.job / 'gamecraft/replay').exists()
    assert validate_recordings(session.job / 'gamecraft/recordings')[1]['capture'] == CAPTURE
    frame = report['episodes'][0]
    episode = read_json(session.job / frame['path'])
    (session.job / episode['video']['capture_timing']['path']).write_text('changed')
    assert not valid_play(report, session.job, session.version, allow_mock=True)


def test_real_docker_native_recording_fixture(tmp_path, config, monkeypatch):
    import os
    import subprocess
    if os.environ.get('RSI_TEST_RECORDINGS_DOCKER') != '1':
        pytest.skip('Set RSI_TEST_RECORDINGS_DOCKER=1 for the offline Docker fixture')
    from game_agent_lite.agent import MockAgent
    from game_agent_lite.game import make_game, install_bridge, valid_play
    from game_agent_lite.policy import PolicySession
    from game_agent_lite.store import snapshot
    from game_agent_lite.gamecraft_player import EVAL_IMAGE
    config.update(engine='godot', entry='project.godot', backend='codex')
    config['runtime'].update(mode='docker', image=EVAL_IMAGE)
    config['player'].update(mode='policy', trajectories_per_version=3, parallel_episodes=3)
    config['benchmark'] = {'name': 'gamecraft', 'task': 'example',
                          'path': str(ROOT / 'benchmarks/gamecraft-bench'), 'evidence_source': 'player_recording'}
    project = tmp_path / 'source'
    snapshot(ROOT / 'tests/fixtures/godot', project)
    install_bridge(project, 'godot')
    before = version(project)
    class Roles(MockAgent):
        def call(self, role, job, context, **kw):
            result = super().call(role, job, context, **kw)
            if context['phase'] == 'policy_author':
                policy = job / 'io/policy.gd'
                policy.write_text(policy.read_text().replace('duration_ms": 300', 'duration_ms": 700'))
            return result
    monkeypatch.setattr('game_agent_lite.gamecraft_player.replay_submission', lambda *a: pytest.fail('no replay'))
    session = PolicySession(config, project, tmp_path / 'player', before, make_game)
    agent = Roles()
    report = session.run(agent)
    assert valid_play(report, session.job, before)
    bundle = session.job / 'gamecraft/recordings'
    _, manifest, _ = validate_recordings(bundle)
    assert len(manifest['demos']) == 3
    assert all(len(d['frames']) >= 2 for d in manifest['demos'])
    assert all(d['duration_seconds'] <= 20 for d in manifest['demos'])
    assert session.run(agent) == report and len(agent.calls) == 2
    assert version(project) == before
    output = tmp_path / 'score'
    bench = tmp_path / 'fixture-benchmark'
    shutil.copytree(ROOT / 'benchmarks/gamecraft-bench/gamecraft_bench', bench / 'gamecraft_bench')
    atomic_json(bench / 'tasks/example/tests/rubric.json', {
        'build_check': {'id': 'BUILD', 'cmd': 'godot --headless --editor --path /workspace/game --quit', 'timeout_seconds': 30},
        'score_formula': 'BUILD * M', 'requirements': [{'id': 'M', 'description': 'Offline fixture only', 'agg': 'max'}]})
    # Stub only, Docker network disabled; exercise the actual CLI/container boundary.
    result = subprocess.run([sys.executable, str(ROOT / 'scripts/score_gamecraft.py'), '--task', 'example',
        '--recordings', str(bundle), '--benchmark', str(bench),
        '--output', str(output), '--smoke-test'], capture_output=True, text=True, timeout=90)
    assert result.returncode == 0, result.stdout + result.stderr + ((output / 'verifier.log').read_text() if (output / 'verifier.log').exists() else '')
    scored = read_json(output / 'evaluation.json')
    assert scored['smoke_passed'] and scored['replayed_demos'] == 0 and scored['scored_demos'] == 3
    assert scored['official_score'] is None and scored['recording_score'] is None
