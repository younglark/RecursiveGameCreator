"""Docker and WSLC command lines. Docker keeps the stricter isolation flags."""
import json
import os
import re
import subprocess
import sys

from .host import child_env

IMAGE_ID = re.compile(r"sha256:[a-f0-9]{64}")
MISSING = ("No such", "WSLC_E_CONTAINER_NOT_FOUND", "找不到")


def container_cli(config) -> str:
    value = config.get("runtime", {}).get("cli", "docker")
    if value not in ("docker", "wslc"):
        raise ValueError("runtime.cli must be docker or wslc")
    return value


def capture(binary, args, timeout=30):
    return subprocess.run([binary, *args], capture_output=True, text=True,
                          timeout=timeout, env=child_env())


def is_missing(result) -> bool:
    text = result.stderr or ""
    return bool(result.returncode) and any(token in text for token in MISSING)


def inspect_image(binary, name):
    args = [binary, "image", "inspect", name]
    if binary == "wslc":
        args += ["-f", "json"]
    data = json.loads(subprocess.check_output(args, text=True, env=child_env()))
    if isinstance(data, dict):
        data = [data]
    return data[0]


def validated_image(binary, name):
    try:
        data = inspect_image(binary, name)
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(exc.stderr or str(exc.output) or "image inspect failed") from exc
    if data.get("Os") != "linux" or not IMAGE_ID.fullmatch(data.get("Id", "")):
        raise ValueError("Runtime image must be a Linux image with a content-addressed ID")
    return data


def inspect_container(binary, name):
    if binary == "wslc":
        args = ["inspect", name, "-f", "json"]
    else:
        args = ["container", "inspect", name]
    return capture(binary, args)


def list_labeled(binary, label):
    return capture(binary, ["ps", "-aq", "--filter", "label=" + label], timeout=15)


def stop_container(binary, name, seconds):
    return capture(binary, ["stop", "--time", str(seconds), name], timeout=max(30, seconds + 15))


def remove_container(binary, *names):
    return capture(binary, ["rm", "-f", *names], timeout=15)


def mount_spec(source, target, readonly):
    text = str(source)
    if "," in text:
        raise ValueError("Runtime mount paths cannot contain commas")
    spec = f"type=bind,source={text},target={target}"
    if readonly:
        spec += ",readonly"
    return spec


def game_user(cli):
    if cli == "wslc" or sys.platform == "win32":
        return "1000:1000"
    return f"{os.getuid()}:{os.getgid()}"


def _isolation(cli, *, read_only_root):
    if cli != "docker":
        return []
    flags = ["--init"]
    if read_only_root:
        flags += ["--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges"]
    return flags


def run_argv(cli, *, name, label, network, user, shm, tmpfs, workdir, interactive, read_only_root,
             mounts, envs, entrypoint, image, command):
    """One container. WSLC omits the flags its 3.0.3 CLI rejects."""
    argv = [cli, "run", "--rm", "--pull=never"]
    isolated = _isolation(cli, read_only_root=read_only_root)
    if isolated and isolated[0] == "--init":
        argv.append("--init")
        isolated = isolated[1:]
    if interactive:
        argv.append("-i")
    argv += ["--name", name]
    if label:
        argv += ["--label", label]
    if network is not None:
        argv += ["--network", network]
    argv += isolated
    if shm:
        argv += ["--shm-size", shm]
    if user:
        argv += ["--user", user]
    if tmpfs:
        argv += ["--tmpfs", tmpfs]
    argv += ["--workdir", workdir]
    for source, target, readonly in mounts:
        argv += ["--mount", mount_spec(source, target, readonly)]
    for value in envs:
        argv += ["--env", value]
    argv += ["--entrypoint", entrypoint, image, *command]
    return argv


def codex_exec(binary, config, *, cd, schema, result, add_dir, prompt, sandbox):
    argv = [binary, "exec", "--json", "--ephemeral", "--skip-git-repo-check",
            "--model", config["model"],
            "-c", f'model_reasoning_effort="{config["reasoning"]}"',
            "--sandbox", sandbox, "--cd", cd,
            "--output-schema", schema, "--output-last-message", result]
    if add_dir:
        argv += ["--add-dir", add_dir]
    argv.append(prompt)
    return argv
