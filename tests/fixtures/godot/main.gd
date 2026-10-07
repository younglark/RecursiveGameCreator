extends Node2D
var x := 20.0

func _ready() -> void:
	add_to_group("game_agent_target")

func _process(delta: float) -> void:
	if Input.is_physical_key_pressed(KEY_RIGHT):
		x += 80.0 * delta
	queue_redraw()

func _draw() -> void:
	draw_rect(Rect2(x, 90, 20, 20), Color.CORNFLOWER_BLUE)

func agent_reset(_seed: int) -> void:
	x = 20.0

func agent_observe() -> Dictionary:
	return {"x": x}
