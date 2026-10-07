extends Node
## Autoload as GameAgentBridge. Only public observations leave the process.
const CAPTURE_WIDTH := 640
const CAPTURE_HEIGHT := 360
const CAPTURE_FPS := 2.0
var server := TCPServer.new()
var peers: Array[StreamPeerTCP] = []
var buffers: Dictionary = {}
var token := ""
var capture_dir := ""
var busy := false
var capturing := false
var capture_clock := 0.0
var capture_index := 0
var held_codes: Array[int] = []
var policy_runner: Node
var held_pointer: Dictionary = {}
var pointer_position := Vector2.ZERO
var pointer_position_known := false
var input_stream: FileAccess
var input_origin_usec := 0
var input_sequence := 0
var input_action_index := -1
var input_metadata: Dictionary = {}
var native_capture := false
var episode_capture_active := false
var episode_capture_origin := 0
var episode_capture_limit := 20.0
var episode_next_slot := 0

func begin_episode_capture(origin_usec: int, limit: float) -> void:
	if not native_capture:
		return
	episode_capture_origin = origin_usec
	episode_capture_limit = limit
	episode_next_slot = 0
	episode_capture_active = true
	await _capture_episode_frame()

func end_episode_capture() -> void:
	if not native_capture or episode_capture_origin == 0 or not episode_capture_active:
		return
	episode_capture_active = false
	var file := FileAccess.open(capture_dir.path_join("episode.json"), FileAccess.WRITE)
	if file:
		file.store_string(JSON.stringify({"origin_usec": episode_capture_origin,
			"duration_seconds": minf((Time.get_ticks_usec() - episode_capture_origin) / 1000000.0, episode_capture_limit),
			"width": 854, "height": 480, "fps": 2, "clock": "engine_input_wall_clock"}))
		file.close()

func _capture_episode_frame() -> void:
	capturing = true
	await RenderingServer.frame_post_draw
	var now := Time.get_ticks_usec()
	var elapsed := (now - episode_capture_origin) / 1000000.0
	# A stalled renderer skips slots; never invent frames to fill a missing interval.
	if episode_capture_active and elapsed >= 0 and elapsed < episode_capture_limit:
		var slot := int(floor(elapsed * CAPTURE_FPS))
		if slot >= episode_next_slot:
			episode_next_slot = slot + 1
			var image := prepare_capture(get_viewport().get_texture().get_image(), Vector2i(854, 480))
			var name := "video-%06d.png" % slot
			if not image.is_empty() and image.save_png(capture_dir.path_join(name)) == OK:
				var path := capture_dir.path_join("timing.jsonl")
				var file := FileAccess.open(path, FileAccess.READ_WRITE if FileAccess.file_exists(path) else FileAccess.WRITE)
				if file:
					file.seek_end()
					file.store_line(JSON.stringify({"file": name, "time": now / 1000000.0,
						"slot": slot, "planned_seconds": slot / CAPTURE_FPS, "time_seconds": elapsed}))
					file.close()
	capturing = false

func _ready() -> void:
	process_mode = Node.PROCESS_MODE_ALWAYS
	token = OS.get_environment("GAME_AGENT_TOKEN")
	capture_dir = OS.get_environment("GAME_AGENT_CAPTURE")
	native_capture = OS.get_environment("GAME_AGENT_RECORDING_MODE") == "player_recording"
	if token.is_empty():
		set_process(false)
		return
	var port := int(OS.get_environment("GAME_AGENT_PORT"))
	if server.listen(port, "127.0.0.1") != OK:
		push_error("Game agent bridge could not listen")
		set_process(false)

func _process(delta: float) -> void:
	capture_clock += delta
	if native_capture:
		if episode_capture_active and not capturing and Time.get_ticks_usec() >= episode_capture_origin + episode_next_slot * 500000:
			_capture_episode_frame()
	elif capture_clock >= 1.0 / CAPTURE_FPS and not capturing:
		capture_clock = 0.0
		_capture_frame()
	if busy:
		return
	if server.is_connection_available():
		var peer := server.take_connection()
		peers.append(peer)
		buffers[peer.get_instance_id()] = ""
	for peer in peers.duplicate():
		peer.poll()
		if peer.get_status() != StreamPeerTCP.STATUS_CONNECTED:
			buffers.erase(peer.get_instance_id())
			peers.erase(peer)
			continue
		var key: int = peer.get_instance_id()
		var text: String = buffers[key] + peer.get_utf8_string(peer.get_available_bytes())
		if text.length() > 1048576:
			peer.disconnect_from_host()
			continue
		if not text.contains("\n"):
			buffers[key] = text
			continue
		buffers[key] = ""
		busy = true
		var result: Dictionary = await _dispatch(JSON.parse_string(text.get_slice("\n", 0)))
		peer.put_data((JSON.stringify(result) + "\n").to_utf8_buffer())
		busy = false

static func prepare_capture(image: Image, size: Vector2i = Vector2i(CAPTURE_WIDTH, CAPTURE_HEIGHT)) -> Image:
	# Resize BEFORE PNG encoding, even after window resizing or HiDPI changes.
	# Keep the whole viewport (no crop/padding) so normalized input coordinates
	# still match observation screenshots.
	if not image.is_empty() and image.get_size() != size:
		image.resize(size.x, size.y, Image.INTERPOLATE_BILINEAR)
	return image

func _capture_frame() -> void:
	capturing = true
	await RenderingServer.frame_post_draw
	var sampled_at := Time.get_ticks_usec() / 1000000.0
	var image := prepare_capture(get_viewport().get_texture().get_image())
	var name := "video-%06d.png" % capture_index
	if not image.is_empty() and image.save_png(capture_dir.path_join(name)) == OK:
		var path := capture_dir.path_join("timing.jsonl")
		var file := FileAccess.open(path, FileAccess.READ_WRITE if FileAccess.file_exists(path) else FileAccess.WRITE)
		if file:
			file.seek_end()
			file.store_line(JSON.stringify({"file": name, "time": sampled_at}))
			file.close()
		capture_index += 1
	capturing = false

func _dispatch(raw: Variant) -> Dictionary:
	if not raw is Dictionary:
		return {"id": null, "error": "invalid request"}
	var id: Variant = raw.get("id")
	if raw.get("token") != token:
		return {"id": id, "error": "unauthorized"}
	var target := get_tree().get_first_node_in_group("game_agent_target")
	if target == null or not target.has_method("agent_reset") or not target.has_method("agent_observe"):
		return {"id": id, "error": "missing game_agent_target contract"}
	var params: Dictionary = raw.get("params", {})
	var result: Variant = {}
	match str(raw.get("method")):
		"native_policy_start":
			if policy_runner != null:
				return {"id": id, "error": "Policy already started"}
			policy_runner = load("res://godot_policy_runner.gd").new()
			add_child(policy_runner)
			policy_runner.begin.call_deferred(self, params)
			result = {"accepted": true}
		"native_policy_status":
			result = policy_runner.status() if policy_runner != null else {"status": "failed", "reason": "No Policy"}
		"ready":
			result = {"ready": true, "user_data_dir": OS.get_user_data_dir()}
		"input_info":
			var size := get_viewport().get_visible_rect().size
			result = {"viewport": {"width": size.x, "height": size.y},
				"viewport_units": "Godot viewport coordinates",
				"window": {"width": get_window().size.x, "height": get_window().size.y}}
		"reset":
			_release_inputs()
			await target.call("agent_reset", int(params.get("seed", 0)))
		"state":
			# State-only reads do not wait for rendering or encode a screenshot.
			result = target.call("agent_observe")
		"release":
			_release_inputs()
		"observe":
			var name := str(params.get("file", ""))
			if name.get_file() != name or not name.ends_with(".png"):
				return {"id": id, "error": "invalid capture name"}
			await RenderingServer.frame_post_draw
			var image := prepare_capture(get_viewport().get_texture().get_image())
			if image.is_empty() or image.save_png(capture_dir.path_join(name)) != OK:
				return {"id": id, "error": "viewport capture unavailable"}
			result = target.call("agent_observe")
		"act", "policy_act":
			var duration := int(params.get("duration_ms", 0))
			if duration < 16 or duration > 2000:
				return {"id": id, "error": "invalid duration"}
			var keys: Array = params.get("keys", [])
			var codes: Array[int] = []
			for key in keys:
				var code := _key_code(str(key))
				if code == KEY_NONE:
					return {"id": id, "error": "unsupported key"}
				codes.append(code)
			var persistent := str(raw.get("method")) == "policy_act"
			var pointer: Variant = params.get("pointer")
			var error := pointer_error(pointer)
			if not error.is_empty():
				return {"id": id, "error": error}
			apply_keys(codes)
			apply_pointer(pointer)
			Input.flush_buffered_events()
			await get_tree().create_timer(duration / 1000.0, true).timeout
			if not persistent:
				apply_keys([])
			finish_pointer(pointer)
			Input.flush_buffered_events()
			result = {"accepted": true}
		"stop":
			end_episode_capture()
			if policy_runner != null and policy_runner.running:
				policy_runner.finish("interrupted", "host stopped")
			_release_inputs()
			get_tree().quit.call_deferred()
		_:
			return {"id": id, "error": "unknown method"}
	return {"id": id, "result": result}

func apply_keys(codes: Array[int]) -> void:
	# One transition implementation for RPC actions and native policies.
	# Release ordinary keys before modifiers; press modifiers before ordinary keys.
	for modifiers in [false, true]:
		for code in held_codes.duplicate():
			if not codes.has(code) and _is_modifier(code) == modifiers:
				_key(code, false)
	for modifiers in [true, false]:
		for code in codes:
			if not held_codes.has(code) and _is_modifier(code) == modifiers:
				_key(code, true)

func _is_modifier(code: int) -> bool:
	return code in [KEY_SHIFT, KEY_CTRL, KEY_ALT, KEY_META]

func _modifiers(event: InputEventWithModifiers) -> void:
	event.shift_pressed = held_codes.has(KEY_SHIFT)
	event.ctrl_pressed = held_codes.has(KEY_CTRL)
	event.alt_pressed = held_codes.has(KEY_ALT)
	event.meta_pressed = held_codes.has(KEY_META)

func _release_inputs() -> void:
	apply_keys([])
	if not held_pointer.is_empty():
		_pointer(held_pointer.duplicate(), false)
	Input.flush_buffered_events()

func begin_input_recording(origin_usec: int, metadata: Dictionary) -> bool:
	input_stream = FileAccess.open(capture_dir.path_join("../input_events.jsonl"), FileAccess.WRITE)
	input_origin_usec = origin_usec
	input_metadata = metadata.duplicate()
	input_sequence = 0
	input_action_index = -1
	if input_stream == null:
		return false
	_record_input({"type": "start", "format": "godot-input-events-v1", "clock": "engine_input_wall_clock"}, 0.0)
	return true

func end_input_recording(outcome: String, reason: String) -> void:
	if input_stream != null:
		_record_input({"type": "end", "status": outcome, "reason": reason})
		input_stream.close()
		input_stream = null

func _record_input(row: Dictionary, at: float = -1.0) -> void:
	if input_stream == null:
		return
	row.merge(input_metadata)
	row.merge({"sequence": input_sequence, "action_index": input_action_index,
		"time_seconds": (Time.get_ticks_usec() - input_origin_usec) / 1000000.0 if at < 0 else at})
	input_stream.store_line(JSON.stringify(row))
	input_stream.flush()
	input_sequence += 1

func _inject(event: InputEventWithModifiers, row: Dictionary) -> void:
	_modifiers(event)
	var at := (Time.get_ticks_usec() - input_origin_usec) / 1000000.0
	Input.parse_input_event(event)
	# This records framework injection, not an acknowledgement from game logic.
	row["modifiers"] = {"shift": event.shift_pressed, "ctrl": event.ctrl_pressed,
		"alt": event.alt_pressed, "meta": event.meta_pressed}
	_record_input(row, at)

func _key_code(name: String) -> int:
	var aliases := {"ArrowUp": KEY_UP, "ArrowDown": KEY_DOWN, "ArrowLeft": KEY_LEFT,
		"ArrowRight": KEY_RIGHT, "Space": KEY_SPACE, "Escape": KEY_ESCAPE, "Enter": KEY_ENTER,
		"Shift": KEY_SHIFT, "Control": KEY_CTRL, "Tab": KEY_TAB,
		"Alt": KEY_ALT, "Backspace": KEY_BACKSPACE, "Delete": KEY_DELETE}
	if aliases.has(name):
		return aliases[name]
	if name.length() == 1:
		return name.to_upper().unicode_at(0)
	return KEY_NONE

func _key(code: int, pressed: bool) -> void:
	if held_codes.has(code) == pressed:
		return
	if pressed:
		held_codes.append(code)
	else:
		held_codes.erase(code)
	var event := InputEventKey.new()
	event.keycode = code
	event.physical_keycode = code
	event.pressed = pressed
	event.echo = false
	_inject(event, {"type": "key_down" if pressed else "key_up", "key": OS.get_keycode_string(code)})

func pointer_error(pointer: Variant) -> String:
	if pointer == null:
		return ""
	if not pointer is Dictionary or not pointer.has_all(["x", "y", "button"]):
		return "Invalid pointer"
	for key in ["x", "y"]:
		if not (pointer[key] is int or pointer[key] is float) or not is_finite(float(pointer[key])) or pointer[key] < 0 or pointer[key] > 1:
			return "Pointer coordinates must be in 0..1"
	var phase: String = str(pointer.get("phase", "click"))
	if not phase in ["click", "down", "move", "up"] or not pointer.button in ["none", "left", "right"]:
		return "Invalid pointer phase/button"
	if phase in ["down", "up"] and pointer.button == "none":
		return "Pointer down/up requires a button"
	if not held_pointer.is_empty() and pointer.button != "none":
		if phase == "click" or (phase == "down" and held_pointer.button != pointer.button):
			return "Release the held mouse button before another press/click"
	return ""

func apply_pointer(pointer: Variant) -> void:
	if pointer == null:
		return
	var phase: String = str(pointer.get("phase", "click"))
	if phase == "up":
		if not held_pointer.is_empty() and held_pointer.button == pointer.button:
			_pointer(pointer, false)
		else:
			_pointer({"x": pointer.x, "y": pointer.y, "button": "none"}, true)
	elif phase == "move" or (phase == "down" and not held_pointer.is_empty()):
		_pointer({"x": pointer.x, "y": pointer.y, "button": "none"}, true)
	else:
		_pointer(pointer, true)

func finish_pointer(pointer: Variant) -> bool:
	if pointer is Dictionary and pointer.get("phase", "click") == "click" and pointer.button != "none":
		_pointer(pointer, false)
		return true
	return false

func _pointer(pointer: Dictionary, pressed: bool) -> void:
	if not pressed and held_pointer.is_empty():
		return
	var viewport := get_viewport()
	var rect := viewport.get_visible_rect()
	var viewport_position := rect.position + Vector2(float(pointer.get("x", 0)), float(pointer.get("y", 0))) * rect.size
	# parse_input_event uses window coordinates, including stretch/letterboxing.
	var position := viewport.get_final_transform() * viewport_position
	var window_size := Vector2(get_window().size)
	var point := {"x": position.x / window_size.x, "y": position.y / window_size.y}
	var motion := InputEventMouseMotion.new()
	motion.position = position
	motion.global_position = position
	motion.relative = position - pointer_position if pointer_position_known else Vector2.ZERO
	pointer_position = position
	pointer_position_known = true
	if not held_pointer.is_empty():
		held_pointer.x = pointer.x
		held_pointer.y = pointer.y
	motion.button_mask = _mouse_mask()
	_inject(motion, {"type": "mouse_move", "x": point.x, "y": point.y})
	if pointer.get("button", "none") != "none":
		if pressed:
			held_pointer = pointer.duplicate()
		else:
			held_pointer = {}
		var event := InputEventMouseButton.new()
		event.position = position
		event.global_position = position
		event.button_index = MOUSE_BUTTON_LEFT if pointer.get("button") == "left" else MOUSE_BUTTON_RIGHT
		event.button_mask = _mouse_mask()
		event.pressed = pressed
		_inject(event, {"type": "mouse_down" if pressed else "mouse_up", "button": pointer.button,
			"x": point.x, "y": point.y})

func _mouse_mask() -> int:
	if held_pointer.is_empty():
		return 0
	return MOUSE_BUTTON_MASK_LEFT if held_pointer.button == "left" else MOUSE_BUTTON_MASK_RIGHT
