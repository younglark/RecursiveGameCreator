import base64
from pathlib import Path
import struct

import pytest

from game_agent_lite.assets import Assets, RejectedRequest, TokenHub
from game_agent_lite.store import UncertainSubmission, read_json

PNG = b"\x89PNG\r\n\x1a\nfixture"
GLB = b"glTF" + struct.pack("<II", 2, 12)


class Client:
    def __init__(self, replies):
        self.replies, self.calls = list(replies), []
        self.downloads = 0

    def key(self):
        return "test-only-not-a-real-key"

    def post(self, route, payload):
        self.calls.append((route, payload))
        response = self.replies.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response

    def download(self, url, destination):
        self.downloads += 1
        destination.write_bytes(GLB)


def request(kind="model", source=None):
    return {"id": "asset", "kind": kind, "prompt": "test asset", "source": source}


def test_3d_submit_query_resume_no_duplicate(tmp_path, config):
    client = Client([{"id": "remote-1"}, OSError("lost query response")])
    assets = Assets(config, client=client)
    root = tmp_path / "assets"
    with pytest.raises(OSError):
        assets.generate([request()], tmp_path, root)
    saved = read_json(root / "asset/job.json")
    assert saved["job_id"] == "remote-1"
    client.replies = [{"status": "completed", "data": [{"type": "glb", "url": "https://cdn.test/file"}]}]
    outputs = assets.generate([request()], tmp_path, root)
    assert outputs[0]["kind"] == "model"
    assert sum(route == "/api/3d/submit" for route, _ in client.calls) == 1
    assert assets.generate([request()], tmp_path, root) == outputs
    assert client.downloads == 1
    payload = client.calls[0][1]
    assert payload["model"] == "hy-3d-3.1" and payload["face_count"] == 30000
    assert "result_format" not in payload and "low_poly" not in payload


@pytest.mark.parametrize("error", [OSError("uncertain"), ValueError("bad response"), KeyboardInterrupt()])
def test_uncertain_submit_pauses_and_requires_reconciliation(tmp_path, config, error):
    client = Client([error])
    assets = Assets(config, client=client)
    root = tmp_path / "assets"
    with pytest.raises((UncertainSubmission, KeyboardInterrupt)):
        assets.generate([request()], tmp_path, root)
    with pytest.raises(UncertainSubmission):
        assets.generate([request()], tmp_path, root)
    assert len(client.calls) == 1
    Assets.reconcile(root, "asset", "recovered-123")
    client.replies = [{"status": "completed", "data": [{"type": "glb", "url": "https://cdn.test/file"}]}]
    assets.generate([request()], tmp_path, root)
    assert client.calls[-1][1]["id"] == "recovered-123"


def test_definitive_failure_is_not_resubmitted(tmp_path, config):
    client = Client([{"id": "one"}, {"status": "failed"}])
    root = tmp_path / "assets"
    assets = Assets(config, client=client)
    with pytest.raises(RejectedRequest):
        assets.generate([request()], tmp_path, root)
    with pytest.raises(RejectedRequest):
        assets.generate([request()], tmp_path, root)
    assert len(client.calls) == 2


def test_http_rejection_and_malformed_submission(tmp_path, config):
    for i, response in enumerate([RejectedRequest("HTTP 401"), {"request_id": "no-job-id"}]):
        assets = Assets(config, client=Client([response]))
        with pytest.raises((RejectedRequest, UncertainSubmission)):
            assets.generate([request()], tmp_path, tmp_path / f"assets-{i}")


def test_image_and_model_reference_dependency(tmp_path, config):
    image = {**request("image"), "id": "ref"}
    model = request(source="ref")
    client = Client([{"data": [{"b64_json": base64.b64encode(PNG).decode()}]}, {"id": "one"},
                    {"status": "completed", "data": [{"type": "glb", "url": "https://cdn.test/a"}]}])
    outputs = Assets(config, client=client).generate([model, image], tmp_path, tmp_path / "assets")
    assert len(outputs) == 2
    assert client.calls[0][0] == "/wand/hunyuan-image/v3-generation"
    assert client.calls[0][1]["model"] == "hy-image-v3"
    assert client.calls[0][1]["size"] == "1024x1024"
    assert client.calls[1][1]["image_base64"] == base64.b64encode(PNG).decode()
    assert "prompt" not in client.calls[1][1]


def test_limits_duplicate_ids_and_path_escape_before_submission(tmp_path, config):
    config["assets"]["max_models"] = 1
    client = Client([])
    assets = Assets(config, client=client)
    with pytest.raises(ValueError, match="limit"):
        assets.generate([request(), {**request(), "id": "second"}], tmp_path, tmp_path / "assets")
    with pytest.raises(ValueError, match="unique"):
        assets.generate([request(), request()], tmp_path, tmp_path / "assets")
    with pytest.raises(ValueError, match="inside"):
        assets.generate([request(source="../outside.png")], tmp_path, tmp_path / "assets")
    assert not client.calls


def test_corrupt_completed_asset_and_changed_request_rejected(tmp_path, config):
    client = Client([{"data": [{"b64_json": base64.b64encode(PNG).decode()}]}])
    assets = Assets(config, client=client)
    root = tmp_path / "assets"
    outputs = assets.generate([request("image")], tmp_path, root)
    with pytest.raises(ValueError, match="parameters changed"):
        assets.generate([{**request("image"), "prompt": "different"}], tmp_path, root)
    (root / outputs[0]["artifact"]["path"]).write_text("broken")
    with pytest.raises(ValueError, match="missing or changed"):
        assets.generate([request("image")], tmp_path, root)


def test_blender_argument_array_and_output(tmp_path, config):
    (tmp_path / "build.py").write_text("# fixture, never run")
    class Runner:
        def run(self, argv, cwd, log, **kwargs):
            assert argv[:3] == [config["blender"], "--background", "--factory-startup"]
            assert "--python-exit-code" in argv
            Path(argv[-1]).write_bytes(GLB)
    result = Assets(config, client=Client([]), runner=Runner()).generate(
        [request("blender", "build.py")], tmp_path, tmp_path / "assets")
    assert result[0]["model"] == "blender"


@pytest.mark.parametrize("kind,count", [("image", 3), ("model", 2), ("blender", 3)])
def test_unlimited_assets_exceed_old_caps_and_resume_without_resubmission(tmp_path, config, kind, count):
    (tmp_path / "build.py").write_text("# fixture, never run")
    if kind == "image":
        replies = [{"data": [{"b64_json": base64.b64encode(PNG).decode()}]}] * count
    elif kind == "model":
        replies = []
        for index in range(count):
            replies.extend([{"id": f"remote-{index}"}, {"status": "completed",
                "data": [{"type": "glb", "url": "https://cdn.test/fixture"}]}])
    else:
        replies = []
    client = Client(replies)
    class Runner:
        calls = 0
        def run(self, argv, cwd, log, **kwargs):
            self.calls += 1
            Path(argv[-1]).write_bytes(GLB)
    runner = Runner()
    requests = [{**request(kind, "build.py" if kind == "blender" else None), "id": f"asset-{i}"}
                for i in range(count)]
    assets = Assets(config, client=client, runner=runner)
    root = tmp_path / "assets"
    outputs = assets.generate(requests, tmp_path, root)
    assert len(outputs) == count
    expected_calls = count * (2 if kind == "model" else 1) if kind != "blender" else 0
    assert len(client.calls) == expected_calls
    assert runner.calls == (count if kind == "blender" else 0)
    assert assets.generate(requests, tmp_path, root) == outputs
    assert len(client.calls) == expected_calls
    assert runner.calls == (count if kind == "blender" else 0)


@pytest.mark.parametrize("kind,key", [("image", "max_images"), ("model", "max_models"),
                                      ("blender", "max_blender")])
@pytest.mark.parametrize("limit", [0, 1])
def test_explicit_asset_caps_still_apply_with_other_kinds_unlimited(tmp_path, config, kind, key, limit):
    config["assets"][key] = limit
    client = Client([])
    requests = [{**request(kind), "id": f"asset-{i}"} for i in range(limit + 1)]
    with pytest.raises(ValueError, match=key):
        Assets(config, client=client).generate(requests, tmp_path, tmp_path / "assets")
    assert client.calls == []


def test_env_loader_has_no_shell_expansion(tmp_path):
    path = tmp_path / ".env.hunyuan"
    path.write_text("OTHER=ignore\nexport HUNYUAN_API_KEY='literal-$(do-not-execute)'\n")
    hub = TokenHub(path, 10)
    assert hub.key() == "literal-$(do-not-execute)"


def test_image_download_resume_uses_receipt(tmp_path, config):
    class FailingClient(Client):
        def download(self, url, destination):
            self.downloads += 1
            if self.downloads == 1:
                raise OSError("download interrupted")
            destination.write_bytes(PNG)
    client = FailingClient([{"data": [{"url": "https://cdn.test/image"}]}])
    assets = Assets(config, client=client)
    root = tmp_path / "assets"
    with pytest.raises(OSError):
        assets.generate([request("image")], tmp_path, root)
    assets.generate([request("image")], tmp_path, root)
    assert len(client.calls) == 1


def test_http_client_payload_auth_and_no_raw_error_body(tmp_path, monkeypatch):
    import io
    import json
    import urllib.error
    import game_agent_lite.assets as module
    env = tmp_path / ".env.hunyuan"
    env.write_text("HUNYUAN_API_KEY=unit-provider-key\n")
    class Opener:
        def open(self, request, timeout):
            assert request.full_url.endswith("/api/3d/submit")
            assert request.get_header("Authorization") == "Bearer unit-provider-key"
            assert json.loads(request.data)["model"] == "hy-3d-3.1"
            return io.BytesIO(b'{"id":"remote-job"}')
    monkeypatch.setattr(module.urllib.request, "build_opener", lambda *_: Opener())
    assert TokenHub(env, 10).post("/api/3d/submit", {"model": "hy-3d-3.1"}) == {"id": "remote-job"}


def test_uncertain_image_can_attach_recovered_file_without_new_request(tmp_path, config):
    client = Client([OSError("lost submit response")])
    assets = Assets(config, client=client)
    root = tmp_path / "assets"
    with pytest.raises(UncertainSubmission):
        assets.generate([request("image")], tmp_path, root)
    recovered = tmp_path / "recovered.png"
    recovered.write_bytes(PNG)
    Assets.reconcile_file(root, "asset", recovered)
    result = assets.generate([request("image")], tmp_path, root)
    assert result[0]["kind"] == "image"
    assert len(client.calls) == 1


def test_new_image_api_http_request_and_url_response(tmp_path, config, monkeypatch):
    import io
    import json
    import game_agent_lite.assets as module
    Path(config["assets"]["env_file"]).write_text("HUNYUAN_API_KEY=offline-key\n")
    class Opener:
        def open(self, req, timeout):
            assert req.full_url == "https://tokenhub.tencentmaas.com/v1/wand/hunyuan-image/v3-generation"
            assert req.get_header("Authorization") == "Bearer offline-key"
            assert json.loads(req.data) == {
                "model": "hy-image-v3", "prompt": "test asset", "size": "1024x1024"}
            return io.BytesIO(json.dumps({"id": "image-request", "request_id": "trace-id",
                "data": [{"url": "https://cdn.test/image.png", "revised_prompt": "revised"}],
                "tokenhub_usage": {"total_tokens": 1024}}).encode())
    monkeypatch.setattr(module.urllib.request, "build_opener", lambda *_: Opener())
    def download(url, timeout):
        assert url == "https://cdn.test/image.png"  # No Authorization passed to CDN.
        return io.BytesIO(PNG)
    monkeypatch.setattr(module.urllib.request, "urlopen", download)
    result = Assets(config).generate([request("image")], tmp_path, tmp_path / "assets")
    assert result[0]["model"] == "hy-image-v3"
    assert (tmp_path / "assets/asset/asset.png").read_bytes() == PNG


def test_new_image_prompt_limit_checked_before_submission(tmp_path, config):
    client = Client([])
    with pytest.raises(ValueError, match="8192"):
        Assets(config, client=client).generate(
            [{**request("image"), "prompt": "图" * 8193}], tmp_path, tmp_path / "assets")
    assert client.calls == []


@pytest.mark.parametrize("phase", ["download", "uncertain", "failed"])
def test_new_default_does_not_resubmit_old_pending_image(tmp_path, config, phase):
    config["assets"]["image_model"] = "hy-image-v3.0"
    class PendingClient(Client):
        def download(self, url, destination):
            raise OSError("download interrupted")
    response = {"download": {"data": [{"url": "https://cdn.test/image"}]},
                "uncertain": OSError("lost response"), "failed": RejectedRequest("HTTP 402")}[phase]
    client = PendingClient([response])
    root = tmp_path / "assets"
    with pytest.raises((OSError, UncertainSubmission, RejectedRequest)):
        Assets(config, client=client).generate([request("image")], tmp_path, root)
    assert read_json(root / "asset/job.json")["status"] == phase
    config["assets"]["image_model"] = "hy-image-v3"
    if phase == "download":
        client.download = lambda url, dest: dest.write_bytes(PNG)
        result = Assets(config, client=client).generate([request("image")], tmp_path, root)
        assert result[0]["model"] == "hy-image-v3.0"
    else:
        with pytest.raises(UncertainSubmission if phase == "uncertain" else RejectedRequest):
            Assets(config, client=client).generate([request("image")], tmp_path, root)
    assert len(client.calls) == 1


def test_blender_png_and_legacy_glb_receipts(tmp_path, config):
    from pathlib import Path
    from unittest.mock import Mock
    import hashlib
    import json
    from game_agent_lite.store import read_json
    script = tmp_path / 'build.py'
    script.write_text('# fixed fixture')
    old = {'id': 'old', 'kind': 'blender', 'source': 'build.py', 'prompt': ''}
    png = {**old, 'id': 'sprite', 'output_format': 'png'}
    def render(argv, cwd, log, **kwargs):
        output = Path(argv[-1])
        output.write_bytes(b'\x89PNG\r\n\x1a\nfixture' if output.suffix == '.png' else b'glTFfixture')
    runner = Mock()
    runner.run.side_effect = render
    assets = Assets(config, runner=runner)
    root = tmp_path / 'output'
    results = assets.generate([old, png], tmp_path, root)
    assert [row['artifact']['path'] for row in results] == ['old/asset.glb', 'sprite/asset.png']
    # Exact pre-extension Blender fingerprint algorithm remains valid for missing-format requests.
    payload = {'request': old, 'script_sha256': hashlib.sha256(script.read_bytes()).hexdigest()}
    assert read_json(root / 'old/job.json')['fingerprint'] == hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    assert assets.generate([old, png], tmp_path, root) == results
    assert runner.run.call_count == 2
    runner.run.side_effect = lambda argv, *args, **kwargs: Path(argv[-1]).write_bytes(b'glTFwrong')
    with pytest.raises(ValueError, match='not PNG'):
        assets.generate([{**png, 'id': 'wrong'}], tmp_path, root)
