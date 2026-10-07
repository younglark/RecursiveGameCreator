extends Node
## Framework-owned recorder. Policies receive detached JSON state, not game objects.
var bridge: Node
var target: Node
var policy: RefCounted
var options: Dictionary
var receipt: Dictionary = {}
var stream: FileAccess
var started_usec := 0
var physics_frame := 0
var running := false
var pending: Dictionary = {}
var before: Dictionary = {}
var action_started := 0.0
var action_deadline := 0.0

func begin(owner_bridge: Node, settings: Dictionary) -> void:
	bridge = owner_bridge
	options = settings
	process_mode = Node.PROCESS_MODE_ALWAYS
	set_physics_process(false)
	target = get_tree().get_first_node_in_group("game_agent_target")
	stream = FileAccess.open(bridge.capture_dir.path_join("../policy-trajectory.jsonl"), FileAccess.WRITE)
	started_usec = Time.get_ticks_usec()
	receipt = {"status": "running", "reason": "starting", "steps": 0, "elapsed_seconds": 0.0}
	var input_opened: bool = bridge.begin_input_recording(started_usec,
		{"version": options.version, "policy_sha256": options.policy_sha256})
	if stream == null or target == null or not input_opened:
		finish("failed", "Cannot open recorder or locate game_agent_target")
		return
	var script = load("res://_rsi_policy.gd")
	if script == null or not script.can_instantiate():
		finish("failed", "Cannot load policy.gd")
		return
	var instance = script.new()
	if not instance is RefCounted or not instance.has_method("reset") or not instance.has_method("act"):
		finish("failed", "policy.gd must extend RefCounted and implement reset/act")
		return
	policy = instance
	policy.call("reset", int(options.seed), str(options.profile))
	before = state()
	if receipt.status == "failed":
		return
	write_row({"type": "initial", "state": before})
	if bridge.native_capture:
		await bridge.begin_episode_capture(started_usec, float(options.get("max_seconds", 20.0)))
		if receipt.status != "running":
			return
	running = true
	set_physics_process(true)

func elapsed() -> float:
	return (Time.get_ticks_usec() - started_usec) / 1000000.0

func state() -> Dictionary:
	# Scene transitions may free the previous target while the autoload keeps running.
	target = get_tree().get_first_node_in_group("game_agent_target")
	if not is_instance_valid(target) or not target.has_method("agent_observe"):
		finish("failed", "Current scene has no game_agent_target/agent_observe")
		return {}
	var observed = target.call("agent_observe")
	if not observed is Dictionary or not json_value(observed):
		finish("failed", "agent_observe must return finite JSON data")
		return {}
	# JSON round trip normalizes StringName keys/values to String and detaches game data.
	return JSON.parse_string(JSON.stringify(observed))

func json_value(value: Variant) -> bool:
	if value is Dictionary:
		for key in value:
			if not (key is String or key is StringName) or not json_value(value[key]):
				return false
		return true
	if value is Array:
		for item in value:
			if not json_value(item):
				return false
		return true
	return value == null or value is String or value is StringName or value is bool or value is int or (value is float and is_finite(value))

func write_row(row: Dictionary) -> void:
	if stream == null:
		return
	row.merge({"version": options.version, "policy_sha256": options.policy_sha256,
		"time_seconds": elapsed(), "physics_frame": physics_frame})
	stream.store_line(JSON.stringify(row))
	stream.flush()

func status() -> Dictionary:
	return receipt.duplicate(true)

func _physics_process(_delta: float) -> void:
	if not running:
		return
	physics_frame += 1
	var now := elapsed()
	var limited: bool = options.get("max_seconds") != null and now >= float(options.max_seconds)
	if not pending.is_empty():
		if now < action_deadline and not limited:
			return
		complete_action()
		if not running:
			return
	if limited:
		finish("truncated", "benchmark demo time limit reached; target completion not established")
		return
	var decision = policy.call("act", before.duplicate(true))
	if not decision is Dictionary or not decision.has_all(["action", "done", "reason"]):
		finish("failed", "Policy must return action, done and reason")
		return
	if not decision.done is bool or not decision.reason is String or decision.reason.is_empty():
		finish("failed", "Invalid Policy done/reason")
		return
	if decision.done:
		if decision.action != null:
			finish("failed", "Finished Policy must return action=null")
		else:
			finish("completed", decision.reason)
		return
	if options.get("max_actions") != null and receipt.steps >= int(options.max_actions):
		finish("truncated", "configured action limit reached")
		return
	var action = normalize_action(decision.action)
	if action.is_empty():
		return
	# Recheck after decision computation; never apply an action beyond the demo budget.
	if options.get("max_seconds") != null and elapsed() >= float(options.max_seconds):
		finish("truncated", "benchmark demo time limit reached")
		return
	bridge.input_action_index = receipt.steps
	action_started = elapsed()
	pending = action
	write_row({"type": "input", "index": receipt.steps, "action": action,
		"before": before, "reason": decision.reason, "requested_duration_ms": decision.action.duration_ms})
	var codes: Array[int] = []
	for name in action.keys:
		codes.append(bridge._key_code(name))
	bridge.apply_keys(codes)
	bridge.apply_pointer(action.pointer)
	Input.flush_buffered_events()
	action_deadline = action_started + action.duration_ms / 1000.0

func complete_action() -> void:
	var release_at: Variant = null
	if bridge.finish_pointer(pending.pointer):
		release_at = elapsed()
	Input.flush_buffered_events()
	var after := state()
	if not running:
		return
	write_row({"type": "feedback", "index": receipt.steps, "action": pending,
		"before": before, "after": after, "action_start_seconds": action_started,
		"pointer_release_seconds": release_at})
	receipt.steps += 1
	before = after
	pending = {}

func normalize_action(raw: Variant) -> Dictionary:
	if not raw is Dictionary or not raw.has_all(["keys", "duration_ms", "pointer"]):
		finish("failed", "Invalid action object")
		return {}
	if not raw.keys is Array or not (raw.duration_ms is int or raw.duration_ms is float):
		finish("failed", "Invalid keys/duration_ms")
		return {}
	if raw.keys.size() > 8 or not is_finite(float(raw.duration_ms)):
		finish("failed", "Invalid action size/duration")
		return {}
	if raw.duration_ms != int(raw.duration_ms) or raw.duration_ms < 16 or raw.duration_ms > 2000:
		finish("failed", "duration_ms must be an integer in 16..2000")
		return {}
	var names: Array = []
	for key in raw.keys:
		if not key is String:
			finish("failed", "Key must be a string")
			return {}
		var name: String = options.aliases.get(key, key)
		if name.length() == 1:
			name = name.to_lower()
		if bridge._key_code(name) == KEY_NONE:
			finish("failed", "Unsupported key: " + key)
			return {}
		if not names.has(name):
			names.append(name)
	var pointer = raw.pointer
	var pointer_problem: String = bridge.pointer_error(pointer)
	if not pointer_problem.is_empty():
		finish("failed", pointer_problem)
		return {}
	var duration := int(raw.duration_ms)
	# Optional GameCraft-only recording pace: actually hold the released state.
	# This changes NEW execution/evidence, never the timing of saved JSON exports.
	var release_ms := int(options.get("min_key_release_ms", 0))
	if release_ms > 0:
		var desired: Array = names.map(func(name): return bridge._key_code(name))
		if bridge.held_codes.any(func(code): return not desired.has(code)):
			duration = maxi(duration, release_ms)
	return {"keys": names, "duration_ms": duration, "pointer": pointer}

func finish(outcome: String, reason: String) -> void:
	running = false
	set_physics_process(false)
	if bridge != null:
		bridge.input_action_index = -1
		bridge._release_inputs()
		bridge.end_input_recording(outcome, reason)
		bridge.end_episode_capture()
	receipt.merge({"status": outcome, "reason": reason, "elapsed_seconds": elapsed()}, true)
	write_row({"type": "end", "status": outcome, "reason": reason})
	if stream != null:
		stream.close()
		stream = null
