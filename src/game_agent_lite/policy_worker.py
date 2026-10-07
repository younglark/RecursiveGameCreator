"""Stdlib-only policy child. Real policies run inside a networkless container.

This transport is not itself a Python sandbox. Do not run untrusted policies on
the host; the controller uses host execution only for explicit mock fixtures.
"""
import contextlib
import importlib.util
import json
import sys


def main():
    wire = sys.stdout
    with contextlib.redirect_stdout(sys.stderr):
        spec = importlib.util.spec_from_file_location("play_policy", sys.argv[1])
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        policy = module.Policy()
    for line in sys.stdin:
        try:
            request = json.loads(line)
            with contextlib.redirect_stdout(sys.stderr):
                if request["method"] == "reset":
                    policy.reset(request["seed"], request["profile"])
                    value = None
                elif request["method"] == "act":
                    value = policy.act(request["state"])
                else:
                    raise ValueError("Unknown policy request")
            response = {"result": value}
            payload = json.dumps(response, allow_nan=False)
        except Exception as exc:
            payload = json.dumps({"error": f"{type(exc).__name__}: {exc}"})
        wire.write(payload + "\n")
        wire.flush()


if __name__ == "__main__":
    main()
