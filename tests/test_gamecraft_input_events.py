"""Injection-journal provenance, validation and legacy compatibility (no engine/network)."""
import json

import pytest

from game_agent_lite.gamecraft import export_gamecraft, _convert_input_events, validate_gamecraft_submission
from game_agent_lite.policy import _episode_intact
from game_agent_lite.store import artifact, read_json
from test_gamecraft_export import saved_run, saved, edit_episode, all_bytes


@pytest.fixture
def recorded_run(saved_run):
    _, job, report = saved(saved_run)
    episode = read_json(job / report["episodes"][0]["path"])
    path = (job / episode["trajectory"]["path"]).with_name("input_events.jsonl")
    rows = [
        {"type": "start", "format": "godot-input-events-v1", "clock": "engine_input_wall_clock", "time_seconds": 0, "action_index": -1},
        {"type": "key_down", "key": "Right", "time_seconds": .13, "action_index": 0},
        {"type": "mouse_move", "x": .25, "y": .5, "time_seconds": .14, "action_index": 0},
        {"type": "mouse_down", "x": .25, "y": .5, "button": "left", "time_seconds": .15, "action_index": 0},
        {"type": "mouse_up", "x": .25, "y": .5, "button": "left", "time_seconds": .20, "action_index": 0},
        {"type": "key_down", "key": "Space", "time_seconds": .34, "action_index": 1},
        {"type": "key_up", "key": "Right", "time_seconds": .61, "action_index": 2},
        {"type": "key_up", "key": "Space", "time_seconds": .91, "action_index": 3},
        {"type": "end", "time_seconds": 1.19, "action_index": -1, "status": "completed", "reason": episode["reason"]},
    ]
    for i, row in enumerate(rows):
        row.update(sequence=i, version=episode["version"], policy_sha256=episode["policy_sha256"])
        if 0 < i < len(rows)-1:
            row["modifiers"] = dict(shift=False, ctrl=False, alt=False, meta=False)
    path.write_text("".join(json.dumps(row)+"\n" for row in rows))
    edit_episode(saved_run, edit_result=lambda result: result.update(input_events=artifact(path, job), input_events_required=True))
    return saved_run, path


def test_actual_events_preferred_and_legacy_explicit(recorded_run, tmp_path):
    root, path = recorded_run
    before = all_bytes(root)
    output = export_gamecraft(root, tmp_path / "export")
    _, manifest, _ = validate_gamecraft_submission(output)
    demo, legacy = manifest["demos"][:2]
    assert demo["input_source"] == "injected_events" and legacy["input_source"] == "derived_actions"
    assert demo["source_input_events"] == artifact(path, root)
    trace = read_json(output / demo["trace"]["path"])
    assert trace["events"][0] == {"frame": 4, "type": "key_down", "keycode": "RIGHT"}  # Not intent at frame 3.
    assert trace["events"][2] == {"frame": 5, "type": "mouse_down", "button": "left", "x": 320, "y": 360}
    assert demo["input_event_map"][0] == {"event_index": 0, "source_sequence": 1, "source_time_seconds": .13, "origin": "injected"}
    assert demo["input_event_map"][-1]["origin"] == "end_marker"
    assert all_bytes(root) == before and manifest["replay_verified"] is False


@pytest.mark.parametrize("fault", ["hash", "missing", "required", "sequence", "version", "clock", "end", "flags", "coordinates", "key", "unbalanced", "action", "nan"])
def test_invalid_journal_never_falls_back(recorded_run, tmp_path, fault):
    root, path = recorded_run
    _, job, report = saved(root)
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    if fault == "missing": path.unlink()
    elif fault == "required": edit_episode(root, edit_result=lambda result: result.pop("input_events"))
    else:
        if fault in ("sequence", "version"): rows[1][fault] = -1
        if fault == "clock": rows[1]["time_seconds"] = 9
        if fault == "end": rows.pop()
        if fault == "flags": rows[1]["modifiers"]["shift"] = True
        if fault == "coordinates": rows[2]["x"] = 2
        if fault == "key": rows[1]["key"] = "Unknown"
        if fault == "unbalanced": rows[1]["type"] = "key_up"
        if fault == "action": rows[1]["action_index"] = 3
        if fault == "nan": rows[1]["time_seconds"] = float("nan")
        path.write_text("".join(json.dumps(row)+"\n" for row in rows) + (" " if fault == "hash" else ""))
        if fault != "hash": edit_episode(root, edit_result=lambda result: result.update(input_events=artifact(path, job)))
    with pytest.raises(ValueError):
        export_gamecraft(root, tmp_path / "export", episodes=[1])
    assert not (tmp_path / "export").exists()


def test_episode_integrity_includes_journal(recorded_run):
    root, path = recorded_run
    _, job, report = saved(root)
    episode = read_json(job / report["episodes"][0]["path"])
    episode["frames"] = []
    assert _episode_intact(episode, job, episode["version"], episode["policy_sha256"])
    path.write_text("modified\n")
    assert not _episode_intact(episode, job, episode["version"], episode["policy_sha256"])
    episode.pop("input_events")
    assert not _episode_intact(episode, job, episode["version"], episode["policy_sha256"])


def test_quantization_order_and_explicit_prefix_cleanup():
    rows = [{"type": kind, "keycode": "RIGHT", "time_seconds": at, "source_sequence": i+1}
            for i, (kind, at) in enumerate([( "key_down", .10), ("key_up", .301), ("key_down", .310), ("key_up", 20.01)])]
    actions = [{"start": .1}, {"start": .301}, {"start": .310}]
    with pytest.raises(ValueError, match="20-second"):
        _convert_input_events(rows, actions, 20.02, clip_long=False)
    trace, steps, mapping = _convert_input_events(rows, actions, 20.02, clip_long=True)
    assert steps == 3 and trace["duration_frames"] == 600
    assert [e["type"] for e in trace["events"][1:3]] == ["key_up", "key_down"]
    assert [e["frame"] for e in trace["events"][1:3]] == [9, 9]
    assert trace["events"][-2] == {"type": "key_up", "keycode": "RIGHT", "frame": 600}
    assert mapping[-2] == {"event_index": 3, "source_sequence": None, "origin": "prefix_release"}


def test_drag_events_export_as_executed_and_cannot_use_legacy_inference():
    from game_agent_lite.gamecraft import _convert
    actions = [{'start': .1, 'action': {'keys': [], 'duration_ms': 100,
                 'pointer': {'x': .2, 'y': .3, 'button': 'left', 'phase': 'down'}}, 'keys': []}]
    with pytest.raises(ValueError, match='require recorded input_events'):
        _convert(actions, 1, clip_long=False)
    rows = [
        {'type': 'mouse_down', 'button': 'left', 'x': 100, 'y': 200, 'time_seconds': .1, 'source_sequence': 1},
        {'type': 'mouse_move', 'x': 300, 'y': 400, 'time_seconds': .4, 'source_sequence': 2},
        {'type': 'mouse_up', 'button': 'left', 'x': 300, 'y': 400, 'time_seconds': .8, 'source_sequence': 3}]
    trace, _, _ = _convert_input_events(rows, actions, 1, clip_long=False)
    assert [e['type'] for e in trace['events'][:3]] == ['mouse_down', 'mouse_move', 'mouse_up']
    assert [e['frame'] for e in trace['events'][:3]] == [3, 12, 24]
