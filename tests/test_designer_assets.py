"""Check advertised capabilities and executable prompt examples without live services."""
import base64
import json
from pathlib import Path
import re
import struct

import pytest

from game_agent_lite.agent import MockAgent
from game_agent_lite.assets import Assets
from game_agent_lite.loop import Loop
from game_agent_lite.store import Store
from game_agent_lite.types import ASSET_SCHEMA, validate


@pytest.mark.parametrize("limit", [None, 0])
def test_designer_receives_configured_models_and_enabled_status(tmp_path, config, limit):
    config["assets"].update(image_model="configured-image-model", model_3d="configured-3d-model",
                            face_count=60000, max_images=limit, max_models=limit, max_blender=limit)
    class Designer(MockAgent):
        context = None
        def call(self, role, job, context, **kwargs):
            assert role == "designer"
            self.context = context
            return super().call(role, job, context, **kwargs)
    store = Store(tmp_path / "run")
    store.create(config)
    agent = Designer()
    Loop(store, config, agent=agent).stage("design", store.load(), store.round_dir(0))
    capabilities = agent.context["asset_capabilities"]
    assert capabilities["hunyuan_image"]["model"] == "configured-image-model"
    assert capabilities["hunyuan_3d"]["model"] == "configured-3d-model"
    assert capabilities["hunyuan_3d"]["face_count"] == 60000
    assert all(item["enabled"] is (limit is None) for item in capabilities.values())
    assert "env_file" not in json.dumps(agent.context)
    assert "auth_file" not in json.dumps(agent.context)
    assert not Path(config["assets"]["env_file"]).exists()


def test_prompt_examples_dispatch_text_image_and_blender_requests_offline(tmp_path, config):
    prompt = (Path(__file__).resolve().parents[1] /
              "src/game_agent_lite/prompts/designer.md").read_text()
    examples = json.loads(re.search(r"```json\n(.*?)\n```", prompt, re.DOTALL).group(1))
    for request in examples:
        validate(request, ASSET_SCHEMA)
    png = b"\x89PNG\r\n\x1a\nfixture"
    glb = b"glTF" + struct.pack("<II", 2, 12)
    class Client:
        def __init__(self):
            self.submissions = []
        def key(self):
            return "offline-fixture"
        def post(self, route, payload):
            if route == "/wand/hunyuan-image/v3-generation":
                return {"data": [{"b64_json": base64.b64encode(png).decode()}]}
            if route == "/api/3d/submit":
                self.submissions.append(payload)
                return {"id": f"mock-job-{len(self.submissions)}"}
            assert route == "/api/3d/query"
            return {"status": "completed", "data": [{"type": "glb", "url": "https://cdn.test/mock.glb"}]}
        def download(self, url, destination):
            destination.write_bytes(glb)
    class Runner:
        def run(self, argv, cwd, log, **kwargs):
            # Simulate export without executing the example Blender script.
            Path(argv[-1]).write_bytes(glb)
    for request in examples:
        if request["kind"] == "blender":
            source = tmp_path / request["source"]
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_text("# fixture, never executed")
    client = Client()
    outputs = Assets(config, client=client, runner=Runner()).generate(examples, tmp_path, tmp_path / "assets")
    assert len(outputs) == len(examples) == 4
    assert len(client.submissions) == 2
    text, image = client.submissions
    assert text["prompt"] and "image_base64" not in text
    assert "prompt" not in image
    assert base64.b64decode(image["image_base64"]) == png
    assert all((tmp_path / "assets" / item["artifact"]["path"]).is_file() for item in outputs)
