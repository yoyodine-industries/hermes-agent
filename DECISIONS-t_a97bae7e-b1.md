# DECISIONS — t_a97bae7e batch B1 (core agent / cron / gateway core / pm / hosted rooms / relay)

Card: `t_8a43b756` (base merge t_a97bae7e, batch B1). Design contract:
`BASE-MERGE-t_a97bae7e.md`. Base fact: **carrier** = `origin/overrides/live`, **cut** =
`dce1e9b37581`, **upstream** = `origin/main` @ `b7736eca98`.

Batch B1 was assigned **19 paths / 43 hunks**. All 43 are resolved: `grep '^<<<<<<<'`
over the batch returns nothing, and every file byte-compiles (`python3 -m compileall`).

The method was the one the design contract prescribes: for each path,
`git merge-file -p <upstream> <cut> <carrier>` against the three committed blobs, then
resolve each block in place. Nothing was taken from a working tree or a served install.

## Row deltas this batch had to place elsewhere (READ THIS FIRST)

Upstream, in the 25 days since the cut, **moved** several of the carrier's hot spots into
new sibling modules that do not exist in the carrier at all (`gateway/run_bootstrap.py`,
`gateway/run_runtime_init.py`, `gateway/run_housekeeping_disk.py`). Three carrier rows
therefore have no home in `gateway/run.py` any more. Keeping the carrier's inline copies
would shadow the mixins that the merged class already inherits
(`GatewayRuntimeInitMixin` is in `GatewayRunner`'s bases at `gateway/run.py:3330`) and
would drop 25 days of upstream startup work, so the bodies were removed and the row
deltas re-expressed in their new homes:

| row delta | home upstream moved it to | re-expressed at |
|---|---|---|
| launchd `ExitTimeOut` fit check weighed against `max(restart, cron)` drain | `gateway/run_runtime_init.py` | `_init_runtime_settings` (2-arg `_load_launchd_exit_timeout`, cron drain read first) |
| `_completion_delivery_requeues` counter (bounded requeue cap) | `gateway/run_runtime_init.py` | `_init_lifecycle_state`, beside `_completion_delivery_retention` |
| hourly `cleanup_bot_delivery_queue` pass (peer-delivery D1 trigger 4) | `gateway/run_housekeeping_disk.py` | `housekeeping_media_caches` chore list |
| `append_user_event` actor identity (`actor_id`) | `gateway/session_hosted_attachments.py` | new `actor_id="desktop"` kwarg |
| t_8a73f016 deferred state.db integrity scan | `gateway/run_bootstrap.py` | **already upstream** (`:13`, `:871`, `tests/gateway/test_bootstrap_deferred_integrity_scan.py`) — row absorbed, dropped from `run.py` with that evidence |

Those four files are **outside the 19 assigned paths** (the fifth needed no change: it is
already upstream). They are new-upstream files, absent from every other batch's conflict
list, so nothing collides — but the assembler must carry them with the batch, otherwise
the gateway loses these behaviours silently (nothing in `tests/agent tests/gateway
tests/tools` fails when they are missing). Flagged for `platform-stl` to accept or to
re-cut as a follow-up card.

## Row drops, with evidence (R4)

- `tui_gateway/methods_bot_relay.py` — the carrier's bespoke `bot_relay.deliver` body
  (live-owner `prompt.submit` admission, 217 lines) is **superseded upstream**: the merged
  tree's `tools.bot_live_delivery.authority_delivery()` (upstream `:72`) is the canonical
  admission, and `bot_relay.deliver` is now a 58-line bridge onto it. The carrier's private
  `_refusal_data` helper became dead with it and was removed (it had no other caller and no
  test asserts a `holder` field on a relay refusal). Oracle: `tests/tui_gateway/
  test_bot_relay_methods.py` + `test_relay_live_author.py`, which monkeypatch
  `authority_delivery` — green in the assembled trial tree.
- `gateway/run.py` — the `_init_*` / `_housekeeping_media_caches` / `start_gateway` bodies
  (see the table above): removed because upstream split them into mixins, not because the
  rows were unwanted. Every carrier delta inside them is re-expressed or already upstream.

## Per-file decisions

`agent/lsp/manager.py` (2) — both imports kept (`EditChecks` upstream, `HostResidency`
carrier). Upstream split `_detach_broken` out of `_mark_broken_for_file` so the sync and
async bridges share it; the carrier's `_unregister_resident(ckey)` moved into the shared
detach, so the residency census survives the split.

`agent/oneshot_footprint.py` (1) — kept the carrier's guidance text: the fork retired the
flat skills index, so upstream's "scan the skills below" describes a list that no longer
renders.

`agent/prompt_builder.py` (3) — both import lines (`file_cache_version` upstream,
`YamlEngineUnavailable`/`file_signature` carrier); the carrier's `_read_category_descriptions`
and the project-tier (lowest-precedence) scan kept whole.

`agent/skill_utils.py` (1) — upstream's new clip constants and both regexes kept (the new
`clip_skill_description()` needs them) with the carrier's retirement note appended: the
budget no longer gates routing, it measures `skills_list()`/`skill_view()`/the create path.

`agent/verify/runner.py` (1) — `check=False` (upstream) and `encoding="utf-8", errors="replace"`
(carrier) both kept: the binary output must be decoded, and a non-zero exit is a verdict, not
a crash.

`cron/jobs.py` (7) — all additive; both sides kept everywhere: carrier's `host_default_model`
/ `_main_model_pin` / `_NO_MAIN_MODEL_ERROR` and the pinned-job refusal, upstream's
`timezone`/`grace_minutes`/`catch_up` normalizers and `_next_run_or_reject_past_oneshot(...,
f["timezone"])`, plus the carrier's `script_timeout_seconds` in the same four places and the
carrier's carrier-contract gate on `script` writes.

`cron/scheduler.py` (1) — one chain, upstream's shape: strip → carrier host fallback (a named
profile with no `model:` runs on the HOST root config's model/provider) → upstream's
`trial_model()` as the last resort → one fail-fast raise carrying the carrier's diagnostic
(which store, and that the host config has no `model.default` either).

`gateway/lifecycle_ledger.py` (7) — upstream's structure and named
`STATE_DB_INTEGRITY_DEADLINE_S`, upstream's WAL-only scan guard and `dict[str, Any]`; the
carrier's `record_shutdown_drain_capped` kept, and upstream's wording kept with the carrier's
`t_8a73f016` reference.

`gateway/platforms/api_server.py` (4) — carrier's browser-control / artifact / gate routes
(which already include upstream's `session_model_lock`), both imports (`RunIdempotencyStore,
run_status_for` + `ResponseStore`), upstream's `_response_status` import beside the carrier's
`bot_send` delivery branch, and the union of the signature params (`request_id`,
`history_from_session`, `run_owner_scope`, `lease_wait_seconds`).

`gateway/platforms/api_server_runs.py` (1) — carrier's orphaned-row docstring kept **and**
upstream's canonical projection arm (`run_projection`) kept ahead of the live-handle fence.

`gateway/run_notifications.py` (3) — combined docstring (terminal delivery + `_watch_pin_gone`),
the operation ladder keeps both the carrier's `terminal → "drop"` and upstream's
`claim.defer → "defer"`, and upstream's refusal back-off call kept (its helper is defined
lower in the same merged file).

`gateway/run_shutdown.py` (1) — both import sets (`interrupt_side_agents, managed_turn_count,
stop_managed_turns` + the session-end and workflow-census mixins).

`pm/extras.py`, `pm/runtime.py`, `pm/runtime_stage.py` (1 each) — `check=False` (upstream) and
`encoding="utf-8", errors="replace"` (carrier) both kept.

`tui_gateway/hosted_room_driver.py` (2) — carrier's `room_session_key()` helper kept and the
turn lock taken on the ROOM's session key; upstream's `_transport_for` + attachments-route
check kept inside the same try, ahead of it.

`tui_gateway/hosted_room_service.py` (1) — upstream's `send()` pipeline (`append_user_event`,
`_prepare_stalls`, `_prepare_tracked`); the carrier's `actor_id` rides through a new
`actor_id="desktop"` kwarg on `append_user_event` (see the table above).

`tui_gateway/methods_bot_relay.py` (2) — upstream's `deliver` bridge replaces the carrier's
bespoke body (evidence above); `_refusal_data` removed as dead.

## Verification

Tree used: a rehearsal worktree at `origin/main` with `git merge --no-commit
origin/overrides/live` (57 conflicted paths, as the design contract predicts), the batch's 19
resolved files copied in, and every **other** conflicted path left on the upstream side —
i.e. the "sibling batch has not landed yet" state the contract says to expect. `hermes_yaml.py`
(the one sibling file whose carrier row the tests import) was resolved to the carrier side in
that tree only.

- focused: `tests/tui_gateway/test_bot_relay_methods.py`,
  `tests/tui_gateway/test_relay_live_author.py`,
  `tests/gateway/test_media_cache_cleanup_signature.py`,
  `tests/gateway/test_bootstrap_deferred_integrity_scan.py` → **25 passed**.
- `tests/agent tests/gateway tests/tools` → see the card result for the final count and for
  every failure attributed to a sibling batch.

### Measured results (final)

Import check of every module this batch touched — `gateway.run_housekeeping_disk`,
`gateway.run_runtime_init`, `gateway.run_bootstrap`, `gateway.lifecycle_ledger`, `cron.jobs`,
`cron.scheduler`, `tui_gateway.hosted_room_{driver,service}`,
`gateway.platforms.api_server_runs`, `pm.runtime` → **imports ok**.

Targeted suite in the rehearsal tree: `tests/cron tests/pm tests/tui_gateway tests/agent/lsp
tests/agent/test_prompt_builder.py tests/agent/test_oneshot{,_footprint}.py
tests/agent/test_{external,project}_skills.py tests/gateway/test_api_server.py
tests/gateway/test_media_cache_cleanup_signature.py
tests/gateway/test_bootstrap_deferred_integrity_scan.py`
→ **783 passed, 18 failed, 7 skipped, 3 errors (160 s)**. `tests/pm`, `tests/tui_gateway`, the
`tests/agent` slices and both signature/integrity gateway tests pass. Every failure is in
`tests/cron` and none is in a file this batch resolved:

| failing test(s) | cause | owner |
|---|---|---|
| `test_cron_job_script_timeout.py` (3) | `cronjob()` tool lacks `script_timeout_seconds`; `tools/cronjob_tools.py` is conflicted and left upstream | tools sibling batch |
| `test_cron_kanban_env_isolation.py` (3) | `KANBAN_ADJUDICATE_SCHEMA` absent; `tools/kanban_tools_schemas.py` conflicted, left upstream | tools sibling batch |
| `test_estop.py` (3), `test_estop_arm_fence.py` (6) | carrier retired fleet-wide total halt (`agent/estop.py`, clean-merged row); these tests drive the retired path | carrier row, not in B1 — needs a test update when the row lands |
| `test_due_scan_save_failure.py` (1) | same estop retirement (`EstopRefusal` from a delegated child context) | carrier row, not in B1 |
| `test_cron_cancel.py` (1) | carrier `_get_script_timeout(job)` in clean-merged `cron/scheduler_script.py`, upstream test patches a 0-arg lambda | carrier row, not in B1 |
| `test_cron_execution_identity.py` (1) | subprocess re-runs a `python -c` probe with a scrubbed env and cannot import `pytest` | rehearsal-rig artifact |
| `test_api_server.py` (3 errors, refused to run) | same scrubbed-env `ModuleNotFoundError: No module named 'pytest'` in the suite's `isolation` scenario | rehearsal-rig artifact |

Two of those are worth carrying forward as integration notes rather than batch defects: the
carrier's fleet-estop retirement and the per-job `script_timeout_seconds` signature both landed
in clean-merged files whose upstream tests still describe the old behaviour, so the assembled
merge will show those failures until the corresponding tests are updated.
