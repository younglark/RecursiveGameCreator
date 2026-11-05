"""Small shared contracts; external data is validated at every boundary."""
from copy import deepcopy
from typing import Any, Literal, TypedDict

from jsonschema import Draft202012Validator


class GameSpec(TypedDict):
    engine: Literal["html", "godot"]
    brief: str


class AssetRequest(TypedDict):
    id: str
    kind: Literal["image", "model", "blender"]
    prompt: str
    source: str | None


class ChangePlan(TypedDict):
    hypothesis: str
    changes: list[str]
    acceptance: list[str]
    assets: list[AssetRequest]


class PlayReport(TypedDict):
    version: str
    seed: int
    steps: list[dict]
    video: dict
    summary: dict


class Review(TypedDict):
    winner: Literal["A", "B", "tie", "unavailable"]
    reason: str
    evidence: list[str]


class RunState(TypedDict):
    schema_version: int
    status: str
    round: int
    phase: str
    best: dict | None
    history: list[dict]
    error: str | None


def obj(properties: dict, required: list | None = None) -> dict:
    return {"type": "object", "properties": properties,
            "required": list(properties) if required is None else required,
            "additionalProperties": False}


STR = {"type": "string", "minLength": 1}
TEXT = {"type": "string"}
STRINGS = {"type": "array", "items": STR}
ID = {"type": "string", "pattern": "^[a-z][a-z0-9_-]{0,47}$"}
ASSET_SCHEMA = obj({"id": ID, "kind": {"enum": ["image", "model", "blender"]},
                    "prompt": TEXT, "source": {"type": ["string", "null"]},
                    "output_format": {"enum": ["glb", "png", None]}},
                   required=["id", "kind", "prompt", "source"])
PLAN_SCHEMA = obj({"hypothesis": STR, "changes": {**STRINGS, "minItems": 1},
                   "acceptance": {**STRINGS, "minItems": 1},
                   "assets": {"type": "array", "items": ASSET_SCHEMA}})
BUILDER_CHECK_SCHEMA = obj({"name": STR, "command": TEXT, "inputs": STRINGS,
    "status": {"enum": ["passed", "failed", "not_run"]}, "details": STR, "evidence": STRINGS})
BUILDER_SCHEMA = obj({"summary": STR, "checks": {"type": "array", "items": BUILDER_CHECK_SCHEMA},
    "remaining": STRINGS}, required=["summary"])
PLAYER_SCHEMA = obj({"experience": STR, "problems": STRINGS, "suggestions": STRINGS})
GAMECRAFT_COVERAGE_SCHEMA = {"type": "array", "minItems": 1, "items": obj({
    "id": {"type": "string", "pattern": "^R[1-9][0-9]*$"},
    "requirement": {**STR, "pattern": r"\S"},
    "episodes": {"type": "array", "uniqueItems": True,
                 "items": {"type": "integer", "minimum": 1}},
    "expected": {**STR, "pattern": r"\S"}, "omission_reason": TEXT})}
_COVERAGE_FINDING = obj({
    "status": {"enum": ["shown", "partial", "not_shown", "unverified"]},
    "reason": {**STR, "pattern": r"\S"},
    "evidence": {"type": "array", "items": STR, "uniqueItems": True}})
GAMECRAFT_COVERAGE_RESULTS_SCHEMA = {"type": "array", "minItems": 1, "items": obj({
    "id": STR, "recording": _COVERAGE_FINDING, "replay": _COVERAGE_FINDING})}
POLICY_PLAN_SCHEMA = obj({"summary": STR, "episodes": {"type": "array", "minItems": 1,
    "items": obj({"profile": ID, "purpose": STR, "goal": STR,
                  "scenario": {"type": ["string", "null"], "pattern": "^[A-Za-z0-9_-]+$"}},
                 required=["profile", "purpose"])}}, required=["summary", "episodes"])
# Optional only for reading historical plans; new GameCraft role outputs require it.
POLICY_PLAN_SCHEMA["properties"]["coverage"] = GAMECRAFT_COVERAGE_SCHEMA
POLICY_REVIEW_SCHEMA = obj({**PLAYER_SCHEMA["properties"],
    "evidence": {"type": "array", "minItems": 1, "items": {"type": "string",
        "pattern": r"^evidence/[A-Za-z0-9_-][A-Za-z0-9_.-]*(/[A-Za-z0-9_-][A-Za-z0-9_.-]*)*$"}}})
REVIEW_SCHEMA = obj({"winner": {"enum": ["A", "B", "tie", "unavailable"]},
    "reason": STR, "evidence": {"type": "array", "items": {"type": "string",
        "pattern": r"^[AB]/[A-Za-z0-9_-][A-Za-z0-9_.-]*(/[A-Za-z0-9_-][A-Za-z0-9_.-]*)*$"}}})
ROLE_SCHEMAS = {"designer": PLAN_SCHEMA, "builder": BUILDER_SCHEMA,
                "player": PLAYER_SCHEMA, "reviewer": REVIEW_SCHEMA}


def role_schema(role: str, context: dict) -> dict:
    if role == "builder":
        return {**BUILDER_SCHEMA, "required": list(BUILDER_SCHEMA["properties"])}
    if role == "designer":
        # New structured outputs require every property; historical plans may omit it.
        schema = deepcopy(PLAN_SCHEMA)
        schema["properties"]["assets"]["items"]["required"].append("output_format")
        return schema
    if role == "player" and context.get("phase") == "policy_author":
        count = context["trajectories_per_version"]
        item = POLICY_PLAN_SCHEMA["properties"]["episodes"]["items"]
        if context.get("benchmark"):
            item = {**item, "required": ["profile", "purpose", "goal", "scenario"]}
        else:
            # Keep ordinary role output unchanged and all schema fields required
            # for structured-output backends. The saved-plan reader accepts both.
            item = obj({k: item["properties"][k] for k in ("profile", "purpose")})
        properties = {k: v for k, v in POLICY_PLAN_SCHEMA["properties"].items()
                      if k != "coverage" or context.get("benchmark")}
        if "coverage" in properties:
            # Codex structured output rejects uniqueItems. Keep it in the local
            # saved-plan schema, which freeze_policy validates before execution.
            properties["coverage"] = deepcopy(properties["coverage"])
            properties["coverage"]["items"]["properties"]["episodes"].pop("uniqueItems")
        return {**POLICY_PLAN_SCHEMA,
            "required": ["summary", "episodes"] + (["coverage"] if context.get("benchmark") else []),
            "properties": {**properties,
            "episodes": {**POLICY_PLAN_SCHEMA["properties"]["episodes"],
                         "items": item, "minItems": count, "maxItems": count}}}
    if role == "player" and context.get("phase") == "policy_analyze":
        schema = POLICY_REVIEW_SCHEMA
    else:
        schema = ROLE_SCHEMAS[role]
    if "evidence_paths" in context and "evidence" in schema["properties"]:
        return {**schema, "properties": {**schema["properties"], "evidence": {
            **schema["properties"]["evidence"],
            "items": {"type": "string", "enum": context["evidence_paths"]}}}}
    return schema

ACTION_SCHEMA = obj({
    "keys": {"type": "array", "items": {"type": "string", "maxLength": 40},
             "uniqueItems": True, "maxItems": 8},
    "duration_ms": {"type": "integer", "minimum": 16, "maximum": 2000},
    "pointer": {"anyOf": [{"type": "null"}, obj({
        "x": {"type": "number", "minimum": 0, "maximum": 1},
        "y": {"type": "number", "minimum": 0, "maximum": 1},
        "button": {"enum": ["none", "left", "right"]},
        "phase": {"enum": ["click", "down", "move", "up"]}},
        required=["x", "y", "button"])]},
})
REQUEST_SCHEMA = obj({"id": ID, "method": {"enum": ["observe", "act"]},
                      "action": {"anyOf": [{"type": "null"}, ACTION_SCHEMA]}})

CONFIG_SCHEMA = obj({
    "engine": {"enum": ["html", "godot"]}, "brief": STR,
    "backend": {"enum": ["harbor", "codex", "grok", "mock"]}, "model": STR,
    "reasoning": {"enum": ["low", "medium", "high", "xhigh"]},
    "rounds": {"type": ["integer", "null"], "minimum": 1},
    "source": {"type": ["string", "null"]},
    "entry": STR, "build_command": STRINGS, "check_command": STRINGS,
    "codex": STR, "harbor": STR, "grok": STR, "godot": STR, "blender": STR, "ffmpeg": STR,
    "harbor_image": STR, "codex_version": STR, "auth_file": STR, "grok_auth_file": STR,
    "seed": {"type": "integer", "minimum": 0},
    "max_actions": {"type": ["integer", "null"], "minimum": 1},
    "headless": {"type": "boolean"},
    "benchmark": {"anyOf": [{"type": "null"}, obj({
        "name": {"const": "gamecraft"}, "task": {"type": "string", "pattern": "^[a-z0-9][a-z0-9-]*$"},
        "path": STR, "image": STR,
        "evidence_source": {"enum": ["official_replay", "player_recording"]}},
        required=["name", "task", "path"])]},
    "runtime": obj({"mode": {"enum": ["local", "docker"]}, "image": STR,
        "health_timeout_seconds": {"type": "number", "minimum": 5},
        "render_threads": {"type": "integer", "minimum": 1},
        "max_game_instances": {"type": "integer", "minimum": 1},
        "build_network": {"enum": ["none", "bridge"]}}, required=[]),
    "storage": obj({"keep_workspaces": {"type": "boolean"}}, required=[]),
    "execution": obj({"parallel_reviews": {"type": "integer", "minimum": 1, "maximum": 2}}, required=[]),
    "player": obj({"mode": {"enum": ["gui", "policy"]},
        "trajectories_per_version": {"type": "integer", "minimum": 1},
        "parallel_episodes": {"type": "integer", "minimum": 1},
        "policy_call_timeout_seconds": {"type": "number", "exclusiveMinimum": 0}}, required=[]),
    "assets": obj({"env_file": STR, "image_model": STR, "model_3d": STR,
        "max_images": {"type": ["integer", "null"], "minimum": 0},
        "max_models": {"type": ["integer", "null"], "minimum": 0},
        "max_blender": {"type": ["integer", "null"], "minimum": 0},
        "face_count": {"type": "integer", "minimum": 3000, "maximum": 1500000},
        "poll_seconds": {"type": "number", "minimum": 0.1},
        "http_timeout": {"type": "number", "minimum": 1}}, required=[]),
}, required=["engine", "brief"])

DEFAULTS: dict[str, Any] = {
    "backend": "harbor", "model": "gpt-6-astra", "reasoning": "high",
    "rounds": None, "source": None, "entry": "index.html",
    "build_command": [], "check_command": [],
    "codex": "codex", "harbor": "harbor", "grok": "grok", "godot": "godot",
    "blender": "blender", "ffmpeg": "ffmpeg",
    "harbor_image": "game-agent-lite/codex:0.153.4-godot-v1", "codex_version": "0.153.4",
    "auth_file": "~/.codex/auth.json", "grok_auth_file": "~/.grok/auth.json",
    "seed": 42, "max_actions": None,
    "headless": True,
    "benchmark": None,
    "execution": {"parallel_reviews": 2},
    "storage": {"keep_workspaces": False},
    "runtime": {"mode": "local", "image": "rsi-gamecreator/runtime:full-v1",
                "health_timeout_seconds": 30, "build_network": "none", "render_threads": 4, "max_game_instances": 1},
    "player": {"mode": "policy", "trajectories_per_version": 3,
               "parallel_episodes": 1,
               "policy_call_timeout_seconds": 30},
    "assets": {"env_file": "../.env.hunyuan", "image_model": "hy-image-v3",
               "model_3d": "hy-3d-3.1", "max_images": None, "max_models": None,
               "max_blender": None, "face_count": 30000,
               "poll_seconds": 10, "http_timeout": 120},
}
PHASES = ("design", "draft", "assets", "integrate", "check", "play", "review", "select")


def validate(value: Any, schema: dict) -> Any:
    Draft202012Validator(schema).validate(value)
    return value
