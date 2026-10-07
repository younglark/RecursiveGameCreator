# Performance and artifact retention

The optimization keeps Designer, Builder (draft/integrate), Player, Reviewer and the two-order selection rule. It does not lower model reasoning, remove checks, shorten formal trajectories, or reuse old gameplay as new observations.

## Implemented changes

| Area | Behavior |
| --- | --- |
| Saved config | Execution resolves paths temporarily; run configs persist relative references. Standard login remains user-local. |
| Snapshot copies | APFS clonefile / Linux FICLONE when available; independent copy2 fallback. No hardlinks between writable versions. |
| Hashing | Listing evidence and copying files no longer computes hashes unnecessarily. Version and evidence validation remain SHA-256 based. |
| Builder handoff | Host records changed files and input hashes, plus Builder-reported checks, evidence paths and unfinished work. Reuse is conditional on complete unaffected dependencies. |
| Validation output | Builder writes to its IO validation directory outside the source project. `.rgc-output` is also reserved and excluded from future snapshots. Existing arbitrary `tests/` trees are not silently deleted. |
| Tool preparation | The role image copies Godot from a specified, already built runtime image. Browser tools are preinstalled. |
| Reviewer | Independent jobs can run concurrently; deterministic receipt ordering, separate contexts and anonymous labels. One completed receipt survives failure of the other. |
| Host capacity | Per-user OS file-lock leases coordinate game instances across controllers. Crashed-process leases are reclaimed. The minimum of the requesting and active limits is respected. |
| Retention | After a valid policy report is saved and episodes are completed/truncated, disposable author and runtime project copies may be removed. Formal evidence, failed workspaces and candidate projects remain. |
| Delivery | `export-game` exports a checked version, source/assets and a relative manifest without the run config or role logs. |

Copy-on-write saves physical blocks where supported; it does not make logical byte counts or the number of view files smaller. A filesystem without reflink support falls back to normal copies. Shared extents must not be estimated by summing each file's allocated-block count. Inspect actual storage use when evaluating disk improvements.

Cleanup is limited to framework-owned disposable project directories. It is not a retroactive cleanup of old experiments. Set `storage.keep_workspaces=true` to retain them. Missing or invalid evidence still prevents acceptance.

## Measurement

Compare the same game requirements, starting source, model/settings, round count, policies/trajectory count, seeds and hardware. Record:

- Stage wall times and full run wall time, with failures and retries separated.
- Agent call counts, input/output tokens and cached tokens.
- Logical file bytes, file count, filesystem use and temporary-workspace removal.
- Completed trajectories, evidence validity, failures, game checks and retained versions.

Real model runs are stochastic, so matching configuration does not guarantee identical generated games. A mock timing result is a controller test, not a game-quality or model-speed benchmark. No numeric end-to-end speedup is asserted without a controlled live comparison.

## Deliberately deferred

Cross-round rollout/analysis caching needs exact policy, seed, observation and runtime identities, plus an explicit decision about independent samples. Engine-import caches need validation against Godot version, source/resource changes, renderer and import options. Neither is enabled implicitly. A global content-addressed service is unnecessary for the first portable release; independent copy-on-write views preserve existing evidence contracts with less migration risk.
