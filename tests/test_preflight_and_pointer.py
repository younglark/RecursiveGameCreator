"""Small shared-runtime regressions: no models, scoring or generated-game experiments."""
from unittest.mock import Mock, call

import pytest

from game_agent_lite.agent import MockAgent
from game_agent_lite.game import HTMLGame, MockGame
from game_agent_lite.policy import PolicySession
from game_agent_lite.store import Stopped, read_json, version


def make_session(tmp_path, config, factory):
    project = tmp_path / 'game'
    project.mkdir()
    (project / 'index.html').write_text('immutable source fixture')
    return PolicySession(config, project, tmp_path / 'player', version(project), factory)


@pytest.mark.parametrize('fault', ['start', 'reset', 'state'])
def test_preflight_failure_prevents_authoring_and_retains_diagnostic(tmp_path, config, fault):
    class Broken(MockGame):
        closed = 0
        def start(self):
            super().start()
            # Attempted source writes affect only the disposable probe copy.
            (self.project / 'index.html').write_text('probe touched its copy')
            if fault == 'start':
                raise RuntimeError('bridge not ready')
        def reset(self, seed):
            if fault == 'reset':
                raise RuntimeError('reset failed')
            super().reset(seed)
        def observe_state(self):
            return {'value': float('nan')} if fault == 'state' else super().observe_state()
        def capture_diagnostic(self, path):
            path.write_bytes(b'fixture screenshot')
            return True
        def stop(self):
            type(self).closed += 1
            return super().stop()
    play = make_session(tmp_path, config, Broken)
    agent = MockAgent()
    with pytest.raises(RuntimeError, match='before Policy authoring'):
        play.run(agent)
    record = read_json(play.job / 'preflight/result.json')
    assert record['status'] == 'failed' and not record['formal_episodes_started']
    assert (play.job / record['image']['path']).is_file()
    assert agent.calls == [] and Broken.closed == 1
    assert not (play.job / 'episodes').exists()
    assert not (play.job / 'preflight/source').exists()
    assert version(play.project) == play.version


def test_preflight_is_once_isolated_and_available_to_author(tmp_path, config):
    class Probe(MockGame):
        seeds = []
        def reset(self, seed):
            type(self).seeds.append(seed)
            super().reset(seed)
    play = make_session(tmp_path, config, Probe)
    class Author(MockAgent):
        def call(self, role, job, context, **kwargs):
            if context['phase'] == 'policy_author':
                assert Probe.seeds == [42]
                result = read_json(job / 'io' / context['preflight']['result'])
                assert result['initial_state'] == {'x': 0}
                assert (job / 'io' / context['preflight']['image']).is_file()
                assert context['handoff']['game']['brief'] == config['brief']
            return super().call(role, job, context, **kwargs)
    agent = Author()
    report = play.run(agent)
    assert Probe.seeds == [42, 42, 43, 44]
    assert len(report['episodes']) == 3 and len(agent.calls) == 2
    assert play.run(agent) == report
    assert Probe.seeds == [42, 42, 43, 44] and len(agent.calls) == 2


def test_interrupted_preflight_can_resume_without_authoring_twice(tmp_path, config):
    class Interrupted(MockGame):
        starts = 0
        def start(self):
            type(self).starts += 1
            super().start()
            if self.starts == 1:
                raise Stopped('fixture cancellation')
    play = make_session(tmp_path, config, Interrupted)
    agent = MockAgent()
    with pytest.raises(Stopped):
        play.run(agent)
    assert not agent.calls
    assert read_json(play.job / 'preflight/result.json')['status'] == 'interrupted'
    report = play.run(agent)
    assert len(report['episodes']) == 3 and len(agent.calls) == 2
    assert read_json(play.job / 'preflight/result.json')['status'] == 'passed'
    assert len(list(play.job.glob('preflight-interrupted-*/result.json'))) == 1


def action(phase=None, *, pointer=True):
    point = {'x': .2, 'y': .4, 'button': 'left'} if pointer else None
    if phase is not None:
        point['phase'] = phase
    return {'keys': [], 'duration_ms': 16, 'pointer': point}


def test_html_pointer_hold_move_release_legacy_and_reset(tmp_path, config):
    game = HTMLGame(config, tmp_path, tmp_path / 'engine')
    game.page = Mock()
    game.page.viewport_size = {'width': 800, 'height': 600}
    game.policy_act(action('down'))
    game.policy_act(action(pointer=False))
    move = action('move')
    move['pointer'].update(x=.8, y=.7, button='none')
    game.policy_act(move)
    assert game.page.mouse.up.call_count == 0
    assert game.page.mouse.down.call_args_list == [call(button='left')]
    game.policy_act(action('up'))
    assert game.page.mouse.up.call_count == 1 and game.held_button is None
    game.policy_act(action())  # Unchanged legacy press/wait/release.
    assert game.page.mouse.down.call_count == game.page.mouse.up.call_count == 2
    game.policy_act(action('down'))
    game.reset(42)
    assert game.page.mouse.up.call_count == 3 and game.held_button is None
    assert game.input_info()['viewport'] == {'width': 800, 'height': 600}
    assert call(.8 * 799, .7 * 599) in game.page.mouse.move.call_args_list


def test_html_drag_error_releases_mouse_and_keys(tmp_path, config):
    game = HTMLGame(config, tmp_path, tmp_path / 'engine')
    game.page = Mock()
    game.page.wait_for_timeout.side_effect = RuntimeError('fixture browser failure')
    with pytest.raises(RuntimeError):
        game.policy_act({**action('down'), 'keys': ['ArrowRight']})
    game.page.keyboard.up.assert_called_once_with('ArrowRight')
    game.page.mouse.up.assert_called_once_with(button='left')
    assert not game.held_keys and game.held_button is None
