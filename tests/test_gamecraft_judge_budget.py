"""Exercise the actual child bootstrap without a network, judge, or container."""
import importlib.util
from pathlib import Path
import runpy
import sys
from types import ModuleType, SimpleNamespace

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "docker/gamecraft/entrypoint.py"
spec = importlib.util.spec_from_file_location("gamecraft_entrypoint", SCRIPT)
entrypoint = importlib.util.module_from_spec(spec)
spec.loader.exec_module(entrypoint)


@pytest.mark.parametrize("budget,expected", [(None, 2048), (4096, 4096)])
def test_bootstrap_keeps_default_or_sets_explicit_budget(monkeypatch, budget, expected):
    names = ["gamecraft_bench", "gamecraft_bench.verifier", "gamecraft_bench.verifier.judges",
             "gamecraft_bench.verifier.judges.openai_gpt"]
    modules = {name: ModuleType(name) for name in names}
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
        if "." in name:
            parent, attr = name.rsplit(".", 1)
            setattr(modules[parent], attr, module)
    judge = modules[names[-1]]
    judge._MAX_TOKENS = 2048
    client = SimpleNamespace(OpenAI=lambda **kwargs: kwargs)
    monkeypatch.setitem(sys.modules, "openai", client)
    monkeypatch.setitem(sys.modules, "httpx", SimpleNamespace(Timeout=lambda *args, **kw: (args, kw)))
    calls = []
    monkeypatch.setattr(runpy, "run_module", lambda module, **kw: calls.append((module, judge._MAX_TOKENS, client.OpenAI())))
    exec(compile(entrypoint.openai_bootstrap("fixture_scorer", budget), "<bootstrap>", "exec"), {})
    assert calls == [("fixture_scorer", expected, {"timeout": ((600.0,), {"connect": 30.0})})]
