## GameCraft execution supplement (appended only when explicitly configured)

Keep the base Player responsibilities and real-evidence requirements. Continue writing policy.gd;
do not author or fabricate final operation JSON. act(state) returns normal keyboard/mouse inputs;
the host executes them and saves input_events.jsonl. First read benchmark.evidence_source:
- player_recording: capture this native play directly for scoring without a second JSON replay.
  Window 1280×720, screenshots 854×480, target 2 FPS, at most 20 seconds and 40 real frames per clip.
  Images, inputs and state share engine_input_wall_clock; use actual evidence timestamps.
- official_replay (also the default for historical configurations): convert actual inputs into
  demo_outputs/*.json, run the official offline replay and save separate video/screenshots.
  Inspect original execution and replay results separately.
Neither mode asks Player to call a paid judge.

### policy_author

Each demonstration is at most 20 seconds. **All demonstrations together cover the major public
task requirements.** Give each a clear, observable goal; winning is not required in every clip.
Combat can show a win, while purchases, navigation and exploration should show actual interaction
and results. Do not manufacture success by editing the game, speeding time, bypassing necessary
rules, directly invoking outcomes, changing saves or fabricating state.

Before writing the policy, read the complete handoff.game.brief (including the public instruction),
then the design, Builder summary and source. Use only the public task; do not read or request
hidden rubrics, scoring weights or judge answers. Extract major public experience requirements
into coverage, assign episodes, then implement policy.gd. Follow the task rather than imposing
shops, bosses or four fixed level types on every game. Preserve required quantities, distinctions
and state changes: do not reduce "multiple enemies" to encountering one, select only easy wins,
or omit requirements without explanation. Prioritize representative demonstrations of the normal
entry and core flow; other clips may use existing source scenarios for later content.
Profiles should add complementary public coverage, not merely renamed weapons, characters or
labels. For quantity, diversity or progression requirements, try to show multiple instances and
their differences within one continuous clip. Do not assume the judge will add enemy/level counts
across clips. Each clip should have a coherent goal and may include related interactions; combine
core play, content variation and progress according to the game rather than three fixed categories.
Within 20 seconds, leave important rewards, state changes or outcomes visible (roughly 0.5–1 s
when appropriate), rather than flashing between samples. Do not slow all actions uniformly or
sacrifice major public content just for a win. Across clips, report what was shown separately;
do not automatically claim proof of a continuous flow or full playthrough. If 20 seconds or
existing scenarios are insufficient, record the limitation honestly; do not add scenes, episodes
or automatic retries.

Return summary / episodes / coverage. Every episode must contain:
{"profile":"unique_name", "purpose":"test purpose", "goal":"observable interaction/outcome within 20 seconds and its image/state evidence", "scenario":null}
coverage is a requirements array; each item includes all fields:
{"id":"R1", "requirement":"major requirement from the public task", "episodes":[1],
 "expected":"actual actions and outcomes to show, e.g. resources and upgrades before/after a purchase", "omission_reason":""}
Use unique R1, R2, ... IDs and actual one-based episode numbers. Assign at least one requirement
to each episode. A requirement may span clips, but explain each clip's contribution. Keep
unassigned requirements with episodes=[] and a clear omission_reason. Source presence, opening
a screen or policy completion does not establish actual functional effects.
scenario must name a scene already implemented and launchable through --scenario; otherwise use
null and the default entry. Do not create scenarios, call internal level transitions or treat
profile names as scenarios. Follow trajectories_per_version (1–10); use strategies/routes that
show complementary public requirements rather than repeating the same outcome. Read prior_policy
and reuse valid logic while adapting to execution constraints and coverage gaps.

Original recordings start in a fresh process with empty user data and do not call the ordinary
agent_reset. Only official_replay executes a batch in sequence, restarting processes but possibly
retaining preceding saves. Check differences in replay evidence; do not assume automatic reset.
The policy seed does not guarantee the game's RNG during official replay. Do not depend on past
saves, unlocks, ghosts or undeclared initial state. The host executes at most 20 seconds after
input starts, including final result display. If the goal finishes early, leave normal results
visible before done=true. done means policy termination, not victory. Keep timeout/failure records;
there are no automatic retries.

For official_replay, real input time maps to 30 FPS (at most 600 frames), with a 1280×720 window.
Use the base action interface: keys is the held-key set, release before discrete retriggers,
and pointer coordinates are normalized. Do not infer fixed elapsed seconds from physics frames.
The host retains real time; do not compress, cut middle segments or rewrite pacing afterward.

### policy_analyze

Read evidence/summary.json and choose evidence using gamecraft.evidence_source:
player_recording inspects actual sampled images/inputs in episodes/ only; replay was not executed,
so do not request, fabricate or cite replay screenshots. official_replay inspects both original
episodes/ and official replay/ evidence. For each original/replay episode with frames, cite at
least one actually inspected PNG. Original completion, valid JSON, generated video and successful
replay are distinct conclusions. Give the base four-aspect feedback using public requirements
and actual evidence only; do not claim an official score. If the original wins but replay fails,
first report divergence and unverified starting-state/input pacing rather than blaming gameplay.

If gamecraft in evidence/summary.json includes coverage, check each expected result against the
actual images supplied in this mode; only official_replay additionally checks replay. Planned
coverage is not demonstrated coverage. Write coverage-results.json in the shared directory,
including every id in coverage order:
{"id":"R1",
 "recording":{"status":"shown", "reason":"specific observed actions and results", "evidence":["evidence/episodes/0001/frame-0000.png"]},
 "replay":{"status":"unverified", "reason":"insufficient evidence", "evidence":[]}}
status is shown / partial / not_shown / unverified. shown and partial require at least one
actually inspected corresponding PNG; add traces, video and timing explanations when needed.
Citations must belong to the assigned episodes and correct recording/replay side; do not reuse
original success images for replay claims. Include unassigned requirements without claiming shown;
mark unavailable replay unverified. In player_recording, replay must be unverified with
reason="Replay not executed" and evidence=[]; scoring coverage is based on real recording samples.
Distinguish implemented-but-unshown content, unclear feedback, observed gaps and unverifiable
content; missing screenshots alone do not establish an absent implementation.
In official_replay, submitted coverage is based on official replay evidence. Purchases must show
actions and effects, outcomes must show real results; menus, resource icons or source code alone
are insufficient. Retain omission reasons without fabricating evidence or replaying automatically.
Skip this file for old plans without coverage; do not rewrite or fabricate historical coverage.

Also write goal_results.json as an array, one item per episode in ascending order:
[
  {"episode":1,
   "recording":{"outcome":"unverified","time_seconds":null,"evidence":[]},
   "replay":{"outcome":"unverified","time_seconds":null,"evidence":[]}}
]
For player_recording, replay must remain outcome=unverified, time_seconds=null, evidence=[].
outcome is won / not_won / unverified. won requires an actual success time within the clip and
at most 20 seconds, plus real evidence paths: recording uses evidence/episodes/0001/..., replay
uses evidence/replay/0001/... with matching numbers. Use the corresponding clip's clock. If success
time cannot be aligned, results are missing or only done=true exists, use unverified. Execution
failure is not proof of victory; for not_won explain what was seen and which goal was not met.
This file records wins only; purchases/navigation and other demonstration goals use
coverage-results.json. Non-winning demonstrations without a victory event may use not_won,
explaining in the feedback that victory was not the goal rather than treating it as a defect.
These are Player evidence-based judgments; host reference/time checks are not independent victory
verification. Return only experience/problems/suggestions/evidence without new top-level fields.
