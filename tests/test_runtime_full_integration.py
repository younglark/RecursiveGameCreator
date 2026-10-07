"""Opt-in HTML/Blender/media integration with fixed policies, never a rollout."""
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import shutil
import threading

import pytest

from game_agent_lite.agent import MockAgent
from game_agent_lite.assets import Assets
from game_agent_lite.game import install_bridge, make_game, valid_play
from game_agent_lite.policy import PolicySession, _extract_frames
from game_agent_lite.runtime import DockerGame, run_tool
from game_agent_lite.store import Stopped, read_json, snapshot, version

pytestmark = pytest.mark.skipif(not os.environ.get('GAME_AGENT_RUNTIME_IMAGE'),
                               reason='opt-in full Docker runtime fixture')


def configure(config):
    config.update(backend='codex', godot='/missing/host/godot',
                  blender='/missing/host/blender', ffmpeg='/missing/host/ffmpeg')
    config['runtime'].update(mode='docker', image=os.environ['GAME_AGENT_RUNTIME_IMAGE'])
    config['harbor_image'] = config['runtime']['image']
    config['player'].update(mode='policy', trajectories_per_version=3, parallel_episodes=3)


@pytest.fixture
def html_source(tmp_path, config):
    configure(config)
    source = tmp_path / 'html-source'
    source.mkdir()
    (source / 'index.html').write_text('''<!doctype html><meta charset="utf-8">
<title>固定输入测试</title><canvas width="960" height="540"></canvas>
<script src="game_agent_bridge.js"></script><script>
let keys = new Set(), moves = 0, clicks = 0, seed = 0, backs = 0;
let pointer={buttons:0,x:0,y:0};
for (const event of ['pointerdown','pointermove','pointerup'])
 document.addEventListener(event,e=>{pointer={buttons:e.buttons,x:e.clientX,y:e.clientY};});
const ctx=document.querySelector('canvas').getContext('2d');
document.addEventListener('keydown', e=>{keys.add(e.key);if(e.key==='Backspace') backs++;});
document.addEventListener('keyup', e=>keys.delete(e.key));
document.addEventListener('pointerdown',()=>{clicks++;localStorage.setItem('clicked','yes');});
setInterval(()=>{if(keys.has('ArrowRight')) moves++;ctx.fillStyle='#5599dd';ctx.fillRect(0,0,960,540);
ctx.fillStyle='white';ctx.font='25px sans-serif';ctx.fillText('中文测试 '+moves,30,100);},20);
GameAgentBridge.register({reset(s){keys.clear();moves=0;clicks=0;seed=s;backs=0;},
observe(){return {moves,clicks,seed,backs,pointer,held:[...keys],saved:localStorage.getItem('clicked')};}});
</script>''')
    install_bridge(source, 'html')
    assert DockerGame(config, source, tmp_path / 'html-build').build()['passed']
    return source


def action(keys=(), pointer=None, duration=300):
    return {'keys': list(keys), 'pointer': pointer, 'duration_ms': duration}


def test_html_three_parallel_inputs_storage_video_and_media(tmp_path, config, html_source):
    before = version(html_source)
    barrier = threading.Barrier(3)
    def episode(index):
        output = tmp_path / f'episode-{index}'
        game = DockerGame(config, html_source, output / 'engine')
        try:
            game.start()
            game.reset(100 + index)
            initial = game.observe_state()
            assert initial['saved'] is None and initial['seed'] == 100 + index
            barrier.wait(timeout=45)
            game.policy_act(action(['ArrowRight', 'Backspace'] if index == 0 else [],
                pointer={'x': .5, 'y': .5, 'button': 'left'} if index == 0 else None))
            game.release_inputs()
            state = game.observe_state()
            game.policy_act(action())
            released = game.observe_state()
            assert not released['held'] and released['moves'] == state['moves']
            assert (released['moves'] > 0) == (index == 0)
            assert released['backs'] == released['clicks'] == int(index == 0)
            assert released['saved'] == ('yes' if index == 0 else None)
            assert game.observe()['image'].is_file()
        finally:
            video = game.stop()
        assert video['capture']['width'] == 640 and video['capture']['height'] == 360
        assert video['capture']['fps'] == 2
        frames = _extract_frames(config, video['path'], output / 'frames', tmp_path, lambda: None)
        assert frames and (tmp_path / frames[0]['image']['path']).is_file()
        receipt = read_json(output / 'frames/container-runtime.json')
        assert receipt['purpose'] == 'media' and receipt['network'] == 'none'
        assert 'ffmpeg_version' in receipt['tools']
        assert not list(output.rglob('*-runtime-owner.json'))
    with ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(episode, range(3)))
    assert version(html_source) == before


def test_html_policy_batch_resume_without_host_browser_or_ffmpeg(tmp_path, config, html_source):
    class FixedAgent(MockAgent):
        def call(self, role, job, context, **kwargs):
            result = super().call(role, job, context, **kwargs)
            if context['phase'] == 'policy_author':
                (job / 'io/policy.py').write_text('''class Policy:
 def reset(self, seed, profile):
  self.steps=0
 def act(self, state):
  self.steps+=1
  if self.steps>6:
   return {"action":None,"done":True,"reason":"fixed fixture finished"}
  return {"action":{"keys":["ArrowRight"] if self.steps<4 else [],"duration_ms":150,"pointer":None},"done":False,"reason":"fixed fixture movement"}
''')
            return result
    play = PolicySession(config, html_source, tmp_path / 'player', version(html_source), make_game)
    report = play.run(FixedAgent())
    assert valid_play(report, play.job, play.version)
    results = [read_json(play.job / item['path']) for item in report['episodes']]
    assert all(r['status'] == 'completed' and r['steps'] == 6 and r['frames'] for r in results)
    before = list(play.job.glob('episodes/*/attempt-*'))
    resumed = PolicySession(config, html_source, play.job, play.version, make_game).run(FixedAgent())
    assert resumed == report and list(play.job.glob('episodes/*/attempt-*')) == before
    assert not list(play.job.rglob('*-runtime-owner.json'))


def test_html_persistent_drag_and_viewport_in_real_runtime(tmp_path, config, html_source):
    game = DockerGame(config, html_source, tmp_path / 'drag')
    try:
        game.start()
        game.reset(42)
        size = game.input_info()['viewport']
        assert size == {'width': 960, 'height': 540}
        game.policy_act(action(pointer={'x': .2, 'y': .3, 'button': 'left', 'phase': 'down'}, duration=16))
        game.policy_act(action(duration=16))
        assert game.observe_state()['pointer']['buttons'] == 1
        point = {'x': .6, 'y': .7, 'button': 'none', 'phase': 'move'}
        game.policy_act(action(pointer=point, duration=16))
        state = game.observe_state()['pointer']
        assert state['buttons'] == 1
        assert state['x'] == pytest.approx(.6 * (size['width'] - 1), abs=1)
        assert state['y'] == pytest.approx(.7 * (size['height'] - 1), abs=1)
        game.policy_act(action(pointer={**point, 'button': 'left', 'phase': 'up'}, duration=16))
        assert game.observe_state()['pointer']['buttons'] == 0
        game.policy_act(action(pointer={**point, 'button': 'left', 'phase': 'down'}, duration=16))
        game.release_inputs()
        assert game.observe_state()['pointer']['buttons'] == 0
    finally:
        game.stop()


def test_blender_single_png_in_real_runtime(tmp_path, config):
    import struct
    configure(config)
    source = tmp_path / 'png-source'
    source.mkdir()
    (source / 'render.py').write_text('''import bpy, sys
bpy.ops.wm.read_factory_settings(use_empty=False)
scene = bpy.context.scene
scene.render.engine = 'CYCLES'
scene.cycles.samples = 1
scene.cycles.use_denoising = False
scene.render.resolution_x = scene.render.resolution_y = 64
scene.render.resolution_percentage = 100
scene.render.image_settings.file_format = 'PNG'
scene.render.filepath = sys.argv[sys.argv.index('--output') + 1]
bpy.ops.render.render(write_still=True)
''')
    requested = {'id': 'tile', 'kind': 'blender', 'source': 'render.py', 'prompt': '', 'output_format': 'png'}
    assets = Assets(config)
    root = tmp_path / 'png-assets'
    result = assets.generate([requested], source, root)
    data = (root / result[0]['artifact']['path']).read_bytes()
    assert data[:8] == b'\x89PNG\r\n\x1a\n'
    assert struct.unpack('>II', data[16:24]) == (64, 64)
    assert assets.generate([requested], source, root) == result
    assert len(list((root / 'tile').glob('blender-run-*'))) == 1


def test_blender_asset_reuse_and_godot_import(tmp_path, config):
    configure(config)
    config.update(engine='godot', entry='project.godot')
    source = tmp_path / 'blender-source'
    snapshot(Path(__file__).parent / 'fixtures/blender', source)
    before = version(source)
    requested = {'id': 'cube', 'kind': 'blender', 'source': 'export_cube.py', 'prompt': ''}
    assets = Assets(config)
    root = tmp_path / 'assets'
    result = assets.generate([requested], source, root)
    assert version(source) == before and not (source / 'scratch.txt').exists()
    assert (root / result[0]['artifact']['path']).read_bytes().startswith(b'glTF')
    saved = read_json(root / 'cube/job.json')
    assert saved['runtime']['mode'] == 'docker'
    attempts = list((root / 'cube').glob('blender-run-*'))
    assert len(attempts) == 1 and (attempts[0] / 'project/scratch.txt').is_file()
    assert assets.generate([requested], source, root) == result
    assert list((root / 'cube').glob('blender-run-*')) == attempts
    project = tmp_path / 'import-source'
    snapshot(Path(__file__).parent / 'fixtures/godot', project)
    install_bridge(project, 'godot')
    shutil.copy2(root / result[0]['artifact']['path'], project / 'cube.glb')
    (project / 'verify.gd').write_text('''extends SceneTree
func _initialize():
    var imported = load("res://cube.glb") as PackedScene
    if imported == null:
        quit(1)
    else:
        var model = imported.instantiate()
        assert(model.get_child_count() > 0)
        model.free()
        print("GLB_IMPORT_OK")
        quit(0)
''')
    config['check_command'] = ['godot', '--headless', '--path', '.', '--script', 'verify.gd']
    output = tmp_path / 'import-build'
    assert DockerGame(config, project, output).build()['passed']
    assert 'GLB_IMPORT_OK' in (output / 'build-1.log').read_text()
    assert not list(tmp_path.rglob('*-runtime-owner.json'))


def test_blender_failure_and_stop_are_cleaned(tmp_path, config):
    configure(config)
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'bad.py').write_text('raise RuntimeError("fixture failure")')
    with pytest.raises(RuntimeError, match='blender exited'):
        run_tool(config, source, tmp_path / 'failed', 'blender', script='bad.py')
    assert not list(tmp_path.glob('*-runtime-owner.json'))
    (source / 'slow.py').write_text('from pathlib import Path\nimport time\nPath("/output/started").write_text("ready")\ntime.sleep(60)')
    output = tmp_path / 'stopped'
    def stop():
        if (output / 'started').exists():
            raise Stopped('fixture cancellation')
    with pytest.raises(Stopped):
        run_tool(config, source, output, 'blender', stop, script='slow.py')
    assert not list(tmp_path.glob('*-runtime-owner.json'))


def test_html_build_uses_container_node(tmp_path, config):
    configure(config)
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'build.cjs').write_text('require("fs").writeFileSync("index.html", "<!doctype html>container build")')
    config['build_command'] = ['node', 'build.cjs']
    assert DockerGame(config, source, tmp_path / 'build').build()['passed']
    assert (source / 'index.html').read_text().endswith('container build')
