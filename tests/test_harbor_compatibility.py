"""Opt-in checks against installed Harbor; parsing only, no agents or containers.

Set GAME_AGENT_HARBOR_PYTHON to the Python interpreter that has Harbor installed.
"""
import os
from pathlib import Path
import subprocess
import textwrap

import pytest

from game_agent_lite.agent import Agent, child_env


def test_generated_task_is_discovered_by_real_harbor(tmp_path, config):
    python = os.environ.get("GAME_AGENT_HARBOR_PYTHON")
    if not python:
        pytest.skip("Set GAME_AGENT_HARBOR_PYTHON to enable offline Harbor compatibility checks")
    Path(config["auth_file"]).write_text("{}")
    job = tmp_path / "job"
    (job / "io").mkdir(parents=True)
    argv, _ = Agent(config).harbor_command(job, "Offline schema test; do not invoke a model", None, "designer")
    script = textwrap.dedent('''
        import asyncio
        import math
        import sys
        from pathlib import Path
        from harbor.models.task.task import Task
        from harbor.models.task.config import TaskConfig
        from harbor.models.job.config import JobConfig
        from harbor.job import Job
        from harbor.environments.docker.docker import DockerEnvironment
        from unittest.mock import AsyncMock, Mock

        root = Path(sys.argv[1])
        # The original name reproduces the silent failure in CLI task discovery.
        text = (root / "task.toml").read_text()
        broken = text.replace("local/game-agent-lite-role", "game-agent-lite-role")
        try:
            TaskConfig.model_validate_toml(broken)
        except ValueError as exc:
            assert "org/name" in str(exc)
        else:
            raise AssertionError("Regression fixture must reproduce the invalid name")
        assert Task.is_valid_dir(root), "Harbor would mistake this task for an empty dataset"
        task = Task(root)
        assert task.config.agent.timeout_sec is None
        assert math.isinf(task.config.verifier.timeout_sec)
        assert math.isinf(task.config.environment.build_timeout_sec)
        tasks = asyncio.run(Job._resolve_task_configs(JobConfig(tasks=[{"path": str(root)}])))
        assert len(tasks) == 1
        # Exercise Harbor's real stop logic with a mocked Docker transport. Both roles
        # must tear down their containers without ever removing the shared image.
        delete = sys.argv[2] == "true"
        for _role in ("designer", "builder"):
            environment = object.__new__(DockerEnvironment)
            environment._keep_containers = False
            environment.prepare_logs_for_host = AsyncMock()
            environment._run_docker_compose_command = AsyncMock()
            environment._cleanup_mounts_compose_file = Mock()
            environment._cleanup_resources_compose_file = Mock()
            asyncio.run(environment.stop(delete=delete))
            commands = [c.args[0] for c in environment._run_docker_compose_command.call_args_list]
            assert commands == [["down"]], commands
        print("Real Harbor task parsing, discovery and image-preserving cleanup passed; no agent started")
    ''')
    env = child_env()
    env["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
    result = subprocess.run([python, "-c", script, str(job / "task"),
                             "true" if "--delete" in argv else "false"],
                            capture_output=True, text=True, timeout=30, env=env)
    assert result.returncode == 0, result.stdout + result.stderr
