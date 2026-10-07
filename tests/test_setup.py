"""Installer orchestration with fake external tools; no downloads or global installs."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


@pytest.fixture
def setup_tree(tmp_path):
    root = tmp_path / 'checkout with spaces'
    (root / 'scripts').mkdir(parents=True)
    shutil.copy2(Path(__file__).parents[1] / 'scripts/setup.sh', root / 'scripts/setup.sh')
    (root / 'pyproject.toml').touch()
    (root / 'uv.lock').touch()
    fake = tmp_path / 'fake-bin'
    fake.mkdir()
    (fake / 'python3').symlink_to(sys.executable)
    stub = '#!/usr/bin/env python3\n' + '''
import json, os
from pathlib import Path
import sys
name = Path(sys.argv[0]).name
args = sys.argv[1:]
with open(os.environ['RGC_TEST_LOG'], 'a') as log:
    log.write(json.dumps([name, *args]) + '\\n')
def install(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(Path(__file__).read_text())
    path.chmod(0o755)
if name == 'uv':
    if args == ['--version']: print('uv 0.11.11 (fixture)')
    elif args[:1] == ['sync']: install(Path.cwd() / '.venv/bin/game-agent')
    elif args[:2] == ['tool', 'install']: install(Path(os.environ['UV_TOOL_BIN_DIR']) / 'harbor')
    elif args[:2] != ['python', 'install']: sys.exit(91)
elif name == 'node': print(os.environ.get('RGC_TEST_NODE', '22'))
elif name == 'npm':
    install(Path(args[args.index('--prefix')+1]) / 'node_modules/.bin/codex')
elif name == 'docker':
    if args == ['info'] and os.environ.get('RGC_TEST_DOCKER_DOWN'): sys.exit(1)
elif name not in ('git', 'harbor', 'codex', 'game-agent'):
    sys.exit(92)
'''
    for name in ('uv', 'git', 'node', 'npm', 'docker', 'curl'):
        path = fake / name
        path.write_text(stub)
        path.chmod(0o755)
    log = tmp_path / 'calls.jsonl'
    env = {**os.environ, 'PATH': str(fake) + ':/usr/bin:/bin', 'RGC_TEST_LOG': str(log)}
    def run(*options, **updates):
        return subprocess.run(['sh', str(root / 'scripts/setup.sh'), *options], cwd=tmp_path,
                              env={**env, **updates}, text=True, capture_output=True, timeout=30)
    def calls():
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    return root, run, calls


@pytest.mark.parametrize('options', [(), ('--minimal',), ('--skip-images',)])
def test_setup_modes_use_only_project_tools_and_ordered_builds(setup_tree, options):
    root, run, calls = setup_tree
    result = run(*options)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (root / '.venv/bin/uv').resolve() == root / '.tools/uv/uv'
    commands = calls()
    assert ['uv', 'sync', '--python', '3.12', '--extra', 'test', '--locked'] in commands
    assert any(c[:2] == ['game-agent', 'start'] for c in commands)
    if options == ('--minimal',):
        assert not any(c[0] in ('npm', 'docker', 'harbor', 'codex') for c in commands)
    else:
        assert (root / '.venv/bin/harbor').resolve().is_relative_to(root / '.tools')
        assert (root / '.venv/bin/codex').resolve().is_relative_to(root / '.tools')
    builds = [c for c in commands if c[:2] == ['docker', 'build']]
    if not options:
        assert len(builds) == 2
        assert 'docker/runtime.Dockerfile' in builds[0]
        assert 'game-agent-lite/codex:0.153.4-godot-v1' in builds[1]
    else:
        assert not builds
    # A second invocation must reuse local uv, tolerate existing links, and still pass.
    again = run(*options)
    assert again.returncode == 0, again.stdout + again.stderr


@pytest.mark.parametrize('changes, message', [
    ({'RGC_TEST_NODE': '20'}, 'Node.js 22 is required'),
    ({'RGC_TEST_DOCKER_DOWN': '1'}, 'Docker is not ready'),
])
def test_setup_preflight_fails_before_installing(setup_tree, changes, message):
    root, run, calls = setup_tree
    result = run(**changes)
    assert result.returncode != 0 and message in result.stderr
    assert not (root / '.tools').exists()
    assert not any(c[0] in ('uv', 'npm') for c in calls())


def test_setup_help_and_invalid_option_have_no_side_effects(setup_tree):
    root, run, calls = setup_tree
    assert run('--help').returncode == 0
    assert run('--unknown').returncode != 0
    assert not calls() and not (root / '.tools').exists()
