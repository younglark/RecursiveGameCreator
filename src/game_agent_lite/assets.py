"""TokenHub and Blender adapters with durable submission receipts."""
import base64
import hashlib
import json
from pathlib import Path
import shutil
import time
import uuid
from copy import deepcopy
import urllib.error
import urllib.parse
import urllib.request

from .agent import CommandRunner
from .store import (SECRETS, UncertainSubmission, artifact, atomic_json, read_json,
                    safe_path, verify_artifact)
from .types import ASSET_SCHEMA, validate

BASE_URL = "https://tokenhub.tencentmaas.com/v1"


def image_api(model: str) -> tuple[str, str]:
    # Keep the old payload format for validating historical receipt fingerprints.
    if model == "hy-image-v3":
        return "/wand/hunyuan-image/v3-generation", "1024x1024"
    return "/images/generations", "1024:1024"


class RejectedRequest(Exception):
    """Definitive HTTP rejection, never automatically retried."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RejectedRequest("Provider API redirect refused")


class TokenHub:
    def __init__(self, env_file: Path, timeout: float):
        self.env_file, self.timeout = env_file, timeout
        self._key = None

    def key(self):
        if self._key is None:
            # No shell sourcing, interpolation, printing, or child environment injection.
            for line in self.env_file.read_text().splitlines():
                name, sep, value = line.removeprefix("export ").partition("=")
                if sep and name.strip() == "HUNYUAN_API_KEY":
                    self._key = value.strip().strip("\"'")
                    break
            if not self._key:
                raise ValueError("HUNYUAN_API_KEY is missing in the configured env file")
            SECRETS.add(self._key)
        return self._key

    def post(self, route: str, payload: dict) -> dict:
        request = urllib.request.Request(BASE_URL + route, json.dumps(payload).encode(),
            {"Authorization": "Bearer " + self.key(), "Content-Type": "application/json"})
        try:
            with urllib.request.build_opener(NoRedirect).open(request, timeout=self.timeout) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            if 400 <= exc.code < 500 and exc.code not in (408, 429):
                raise RejectedRequest(f"Provider rejected request (HTTP {exc.code})") from None
            raise OSError(f"Provider transport unavailable (HTTP {exc.code})") from None

    def download(self, url: str, destination: Path):
        if urllib.parse.urlsplit(url).scheme != "https":
            raise ValueError("Asset download must use HTTPS")
        # No API Authorization on CDN requests. URL/query never written to public logs.
        temporary = destination.with_suffix(destination.suffix + ".part")
        try:
            with urllib.request.urlopen(url, timeout=self.timeout) as response, temporary.open("wb") as out:
                shutil.copyfileobj(response, out)
            if not temporary.stat().st_size:
                raise ValueError("Provider returned an empty asset")
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)


class Assets:
    def __init__(self, config: dict, stop=lambda: None, client=None, runner=None, sleep=time.sleep):
        self.config, self.options, self.stop = config, config["assets"], stop
        self.client = client or TokenHub(Path(self.options["env_file"]), self.options["http_timeout"])
        self.runner, self.sleep = runner or CommandRunner(), sleep

    def generate(self, requests: list[dict], draft: Path, root: Path) -> list[dict]:
        ids = set()
        for request in requests:
            validate(request, ASSET_SCHEMA)
            if request["kind"] != "blender" and request.get("output_format") is not None:
                raise ValueError("output_format is only supported for Blender requests")
            if request["id"] in ids:
                raise ValueError("Asset ids must be unique within a round")
            ids.add(request["id"])
            if request["kind"] != "blender" and not request["prompt"].strip() and not request["source"]:
                raise ValueError("Generation needs a prompt or image source")
        for kind, key in (("image", "max_images"), ("model", "max_models"), ("blender", "max_blender")):
            limit = self.options[key]
            if limit is not None and sum(r["kind"] == kind for r in requests) > limit:
                raise ValueError(f"Round exceeds asset limit: {key}")
        root.mkdir(parents=True, exist_ok=True)
        outputs = []
        generated = {}
        for request in sorted(requests, key=lambda r: {"image": 0, "model": 1, "blender": 2}[r["kind"]]):
            self.stop()
            result = self.one(request, draft, root, generated)
            outputs.append(result)
            generated[request["id"]] = root / result["artifact"]["path"]
        return outputs

    def one(self, request, draft, root, generated):
        folder = root / request["id"]
        folder.mkdir(exist_ok=True)
        state_path = folder / "job.json"
        kind = request["kind"]
        model = self.options["image_model" if kind == "image" else "model_3d"] if kind != "blender" else "blender"
        state = read_json(state_path) if state_path.exists() else None
        if state is not None:
            if (state["id"], state["kind"]) != (request["id"], kind):
                raise ValueError("Asset receipt identity does not match request")
            if kind == "image":
                # A new default must not relabel or regenerate an existing image.
                # Failed/uncertain receipts still require explicit recovery below.
                model = state["model"]
        payload = self.payload(request, draft, generated, model)
        fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        state = state if state is not None else {
            "id": request["id"], "kind": kind, "model": model, "fingerprint": fingerprint, "status": "new"}
        if state["fingerprint"] != fingerprint:
            raise ValueError("Asset parameters changed during resume; start a new candidate")
        if state["status"] == "completed":
            if not verify_artifact(state["artifact"], root):
                raise ValueError("Completed asset file is missing or changed")
            return self.public(state)
        if state["status"] in ("submitting", "uncertain"):
            raise UncertainSubmission(f"Asset {request['id']} submission is uncertain; reconcile its existing result")
        if state["status"] == "failed":
            raise RejectedRequest(f"Asset {request['id']} previously failed; inspect its job.json")
        output_format = (request.get("output_format") or "glb") if kind == "blender" else (
            "png" if kind == "image" else "glb")
        destination = folder / ("asset." + output_format)
        if kind == "blender":
            script = safe_path(draft, request["source"] or "")
            if not script.is_file() or script.suffix != ".py":
                raise ValueError("Blender requires an existing relative .py source")
            if self.config.get("runtime", {}).get("mode") == "docker" and self.config["backend"] != "mock":
                from .runtime import run_blender, runtime_identity, same_runtime
                identity = runtime_identity(self.config)
                if "runtime" in state and not same_runtime(state["runtime"], identity):
                    raise ValueError("Blender runtime changed during resume; use a new asset job")
                state.update(status="running", runtime=identity)
                atomic_json(state_path, state)
                pinned = deepcopy(self.config)
                pinned["runtime"]["image"] = identity["image_id"]
                pinned["_runtime_identity"] = identity
                output = folder / ("blender-run-" + uuid.uuid4().hex)
                produced, _ = run_blender(pinned, draft, output,
                    script.relative_to(draft.resolve()).as_posix(), self.stop, output_format=output_format)
                shutil.copy2(produced, destination)
            else:
                if state.get("runtime") is not None:
                    raise ValueError("Blender runtime changed during resume; use a new asset job")
                self.runner.run([self.config["blender"], "--background", "--factory-startup",
                    "--python-exit-code", "1", "--python", str(script), "--", "--output", str(destination)],
                    draft, folder / "blender.log", tick=self.stop)
        else:
            if state["status"] == "new":
                # Credentials are checked before crossing the possibly billable boundary.
                self.client.key()
                state["status"] = "submitting"
                atomic_json(state_path, state)
                try:
                    response = self.client.post(image_api(model)[0] if kind == "image" else "/api/3d/submit", payload)
                    if kind == "model":
                        if not isinstance(response.get("id"), str) or not response["id"]:
                            raise ValueError("Submit response has no job id")
                        state.update(status="queued", job_id=response["id"])
                    else:
                        data = response.get("data")
                        if not isinstance(data, list) or not data or not (data[0].get("url") or data[0].get("b64_json")):
                            raise ValueError("Image response is missing result data")
                        state.update(status="download", data=[{
                            k: data[0][k] for k in ("url", "b64_json") if k in data[0]}])
                    atomic_json(state_path, state, private=True)
                except RejectedRequest:
                    state["status"] = "failed"
                    atomic_json(state_path, state)
                    raise
                except Exception:
                    state["status"] = "uncertain"
                    atomic_json(state_path, state)
                    raise UncertainSubmission(f"Asset {request['id']} may have been submitted; do not resubmit") from None
            while kind == "model" and state["status"] != "download":
                self.stop()
                response = self.client.post("/api/3d/query", {"model": model, "id": state["job_id"]})
                status = response.get("status")
                if status == "completed":
                    files = [f for f in response.get("data", []) if f.get("type", "").lower() == "glb"]
                    if not files or not files[0].get("url"):
                        raise ValueError("Completed 3D job has no GLB; job id retained")
                    state.update(status="download", data=[{"url": files[0]["url"]}])
                elif status in ("queued", "in_progress", "failed"):
                    state["status"] = status
                else:
                    raise ValueError("Unknown 3D status; existing job id retained")
                atomic_json(state_path, state, private=True)
                if status == "failed":
                    raise RejectedRequest(f"3D job {request['id']} failed")
                if state["status"] != "download":
                    until = time.monotonic() + self.options["poll_seconds"]
                    while time.monotonic() < until:
                        self.stop()
                        self.sleep(min(.2, max(0, until - time.monotonic())))
            data = state["data"][0]
            self.stop()
            if data.get("b64_json"):
                content = base64.b64decode(data["b64_json"], validate=True)
                destination.write_bytes(content)
            else:
                self.client.download(data["url"], destination)
        # Minimal magic checks catch JSON error pages and unusable fixture outputs.
        header = destination.read_bytes()[:12]
        if output_format == "glb" and not header.startswith(b"glTF"):
            raise ValueError("Asset is not a binary glTF file")
        if kind == "blender" and output_format == "png" and not header.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError("Blender output is not PNG")
        if kind == "image" and not (header.startswith(b"\x89PNG\r\n\x1a\n") or header.startswith(b"\xff\xd8")):
            raise ValueError("Asset is not PNG/JPEG")
        if header.startswith(b"\xff\xd8"):
            destination = destination.rename(destination.with_suffix(".jpg"))
        state.update(status="completed", artifact=artifact(destination, root))
        state.pop("data", None)
        atomic_json(state_path, state)
        return self.public(state)

    def payload(self, request, draft, generated, model):
        if request["kind"] == "blender":
            script = safe_path(draft, request["source"] or "")
            return {"request": request, "script_sha256": hashlib.sha256(script.read_bytes()).hexdigest()}
        payload = {"model": model}
        if request["kind"] == "image":
            if request["source"] is not None:
                raise ValueError("v1 image requests are text-to-image; source must be null")
            if model == "hy-image-v3" and len(request["prompt"]) > 8192:
                raise ValueError("hy-image-v3 prompt exceeds 8192 characters")
            payload.update(prompt=request["prompt"], size=image_api(model)[1])
        else:
            payload.update(enable_pbr=True, face_count=self.options["face_count"])
            if request["source"]:
                source = generated.get(request["source"]) or safe_path(draft, request["source"])
                content = source.read_bytes()
                if len(content) > 6 * 1024 * 1024:
                    raise ValueError("3D reference image exceeds 6 MB")
                payload["image_base64"] = base64.b64encode(content).decode()
            else:
                payload["prompt"] = request["prompt"]
            # No low_poly for 3.1; omit result_format to obtain standard GLB.
        return payload

    @staticmethod
    def public(state):
        return {k: state[k] for k in ("id", "kind", "model", "artifact")}

    @staticmethod
    def reconcile(root: Path, asset_id: str, job_id: str):
        path = safe_path(root, asset_id) / "job.json"
        state = read_json(path)
        if state["kind"] != "model" or state["status"] not in ("submitting", "uncertain"):
            raise ValueError("Only uncertain 3D submissions can be reconciled with a job id")
        if not job_id.strip():
            raise ValueError("Job id cannot be empty")
        state.update(status="queued", job_id=job_id)
        atomic_json(path, state)

    @staticmethod
    def reconcile_file(root: Path, asset_id: str, existing: Path):
        """Attach an externally recovered result; never triggers another submission."""
        path = safe_path(root, asset_id) / "job.json"
        state = read_json(path)
        if state["status"] not in ("submitting", "uncertain") or state["kind"] not in ("image", "model"):
            raise ValueError("Only uncertain image/3D submissions accept a recovered file")
        content = existing.read_bytes()
        suffix = ".glb" if state["kind"] == "model" else ".png" if content.startswith(b"\x89PNG\r\n\x1a\n") else ".jpg"
        valid = content.startswith(b"glTF") if suffix == ".glb" else content.startswith(b"\x89PNG\r\n\x1a\n") or content.startswith(b"\xff\xd8")
        if not valid:
            raise ValueError("Recovered file type does not match requested asset")
        destination = path.parent / ("asset" + suffix)
        destination.write_bytes(content)
        state.update(status="completed", artifact=artifact(destination, root), recovered=True)
        atomic_json(path, state)
