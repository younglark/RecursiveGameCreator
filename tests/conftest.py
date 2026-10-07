import copy
import pytest
from game_agent_lite.types import DEFAULTS


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    # Any accidental live provider/network call makes offline tests fail immediately.
    import socket
    def forbidden(*args, **kwargs):
        raise AssertionError("Network is disabled in the local test suite")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)


@pytest.fixture
def config(tmp_path):
    value = copy.deepcopy(DEFAULTS)
    value.update(engine="html", brief="test", backend="mock", rounds=2)
    # Existing tests exercise the legacy protocol; policy tests opt into the new mode.
    value["player"]["mode"] = "gui"
    value["auth_file"] = str(tmp_path / "auth.json")
    value["assets"]["env_file"] = str(tmp_path / ".env.hunyuan")
    value["assets"]["poll_seconds"] = .1
    return value
