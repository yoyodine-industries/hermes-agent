# B3 (tools-tests-docs) merge resolution — decisions

Fixtures: CUT `dce1e9b37581` · CARRIER `origin/overrides/live` (c68adbe5e4b) · UP `origin/main`
(b7736eca98cc). Method: `git merge-file -p <up> <cut> <ours>` per file, then the 32 markers resolved
by R0–R6. No marker survives; `compileall` is clean on all 16 `.py` files.

Row content kept = `git diff --no-index <up> <resolved>` added lines; row content dropped = lines the
row has that the resolution does not (this count includes row-side lines upstream has since
superseded with newer code — the row is a month behind upstream on several of these files).

## Per file

| file | decision |
|---|---|
| `tools/bot_mode_dm.py` | **rewritten.** Upstream's admission-then-wait structure kept whole. Row carried: the §1.2/P9(a) receipt on the ack (`result: "receipt"`, 64-char content-addressed `delivery_id`, do-NOT-wait/do-NOT-resend guidance) — added to the live-owner ack branch, which upstream had left with `status`/`detail` only, so the tool's own description and the row's contract agree again. Row dropped: the delivery-turn CLI path (`DELIVERY_TURN_ENV` child env, `_live_owner_holder`, `STATUS_DETAIL_LIVE_OWNER`) — see D1. |
| `tools/cronjob_tools.py` | merged; row's `departure`/`--no-probe` flags carried on upstream's signatures (+14). |
| `tools/kanban_tools_schemas.py` | merged; row's schema text carried (+224, upstream only touched wording). |
| `tools/mcp_tool.py` | **upstream wins outright.** The row's 2 hunks are the `_packaged_npx_entry` npx path that upstream has since removed in favour of `_npx_cached_bin` + `_get_mcp_stderr_log`; restoring either would revert upstream's fix (R0). |
| `tools/process_registry.py` | merged; row's `departure` plumbing carried (+27), upstream's newer process-registry restructure kept (-31 of the row side was stale). |
| `tools/terminal_tool.py` | merged; row's foreground-promotion/departure plumbing carried (+41). |
| `tools/terminal_tool_background.py` | merged; row's persist-on-release + departure conditions carried (+17). |
| `tests/agent/test_shell_hooks.py` | merged; row's hook tests carried whole (+73). |
| `tests/gateway/test_chat_completions_approval.py` | merged; row's streamed-approval tests carried (+126). |
| `tests/gateway/test_lifecycle_state_db_check.py` | **upstream wins outright.** The row's version predates the deferred scan (`t_8a73f016`): it lacks `journal_mode` and the rollback-journal case, and upstream's is newer (R0/R1). |
| `tests/hermes_cli/test_cron_parser_builder.py` | merged; row's `script_timeout` test carried (+11). |
| `tests/hermes_cli/test_kanban_db.py` | merged; row's storage-locked/respawn-guard/archive tests carried (+233). |
| `tests/tools/test_bot_mode_dm.py` | merged and re-expressed on upstream's seams: the live-wait tests now set up through the file's own `_admitted_live_dm` helper (fake authority + door) instead of a bare `deliver_to_live_owner` call that now dials; the ack test asserts upstream's live-owner `detail` wording and the waiter-only transport argv (`hermes -p <profile>`, no model turn). Drops: see D2. |
| `tests/tools/test_bot_mode_refusal_contract.py` | **upstream's file only** (row's one added test dropped, D3). |
| `tests/tools/test_bot_turn_lock.py` | merged; row's turn-lock suite carried (+231). The row's `import subprocess` line was lost by `merge-file` while the helper needing it survived — restored. `test_local_delivery_command_never_reenters_the_lock` dropped: `local_delivery_command` is deleted upstream (D4). |
| `tests/tools/test_skill_usage.py` | merged; row's skill-usage tests carried whole (+145). |
| `website/docs/user-guide/features/hooks.md` | merged on upstream's page structure; only the row's added sentences re-applied (+30). |

## Drops, with evidence

**D1 — `tools/bot_mode_dm.py`: the delivery-turn CLI path dropped.**
`_delivery_turn_env`/`_live_owner_holder` read the single-owner hold through
`tools.bot_live_delivery.find_canonical_owner`, which upstream **deleted** (it exists in
`dce1e9b37581` and in the row, and not in `origin/main`, where `find_canonical_live_owner` replaces
it), and `holder_from_live_owner` never existed on any of the three sides. Upstream's
`_run_delivery(..., stdin_file=False)` never executes the child CLI at all: it admits through the
profile authority and waits. Upstream carries the intent — a busy/held target is **retained**, never
lost — through `_admit_live_dm` → `LiveTargetHeldError` → `_settled_no_send`, with green tests:
`tests/tools/test_bot_live_owner_delivery.py` (19 passed) and the live-wait tests in
`tests/tools/test_bot_mode_dm.py` (68 passed / 3 skipped). R4.

**D2 — `tests/tools/test_bot_mode_dm.py`: six live-wait-loop tests dropped.**
`test_live_dm_wait_releases_queued_runner_when_pinned_owner_is_gone`,
`…_rechecks_receipt_when_owner_disappears_at_claim`, `…_keeps_claimed_turn_until_it_settles`,
`…_releases_claimed_runner_after_owner_is_gone`, `…_releases_runner_when_owner_lookup_keeps_failing`,
`…_stops_at_its_deadline_while_the_owner_holds_the_receipt`. They drive the row's local-mailbox wait
loop; upstream's `tools/bot_live_delivery.py` (not in this batch, merges clean, 306 lines of upstream
change) replaced that loop with the authority-RPC door, so the setups no longer reach a mailbox and
the loop never settles. Upstream carries the same intent — a bounded wait that releases the runner
and never cancels an admission — with green tests: `tests/tools/test_bot_live_owner_delivery.py`
(19 passed), plus the two live-wait tests kept in this file
(`…_reports_reply_after_initial_wait_budget`, `…_is_bounded_and_never_cancels_an_admission`). R4.
**If B1/B2 resolve `tools/bot_live_delivery.py` toward the row's mailbox loop, these six should come
back** — they are recorded here so they are not silently lost.

**D3 — `tests/tools/test_bot_mode_refusal_contract.py`: the row's added test dropped.**
`test_delivery_uses_refusal_code_before_human_wording` asserts that a delivery turn whose child CLI
exits with `hermes-refusal-reason: SESSION_NOT_OWNED` is reported as a receipt (`result: "receipt"`,
`status_detail: "live_owner_present"`, exit 0). That path does not exist upstream (D1): there is no
child CLI on the local route, and `status_detail` is not part of upstream's record. Measured against
the merged tree the test fails on its first branch (`1 == 0`). Upstream's retention semantics
(`LiveTargetHeldError` → `_settled_no_send`, `PreDialError` → "not sent, safe to resend") plus its
green tests carry the intent. The receipt **word** the row wanted is carried where it still applies:
the ack's `result: "receipt"` (D1). R4.

**D4 — `tests/tools/test_bot_turn_lock.py`: `test_local_delivery_command_never_reenters_the_lock`
dropped.** `tools.bot_relay.local_delivery_command` is deleted upstream (present in `dce1e9b37581`
and the row, absent from `origin/main`); the test guards a function that no longer exists. Upstream
keeps the turn lock itself (`acquire_turn_lock`/`read_turn_lock_holder`, whose tests in this file all
pass), and the remaining row suite covers it. R0 forbids restoring the deleted command. R4.

## Verification (this branch, 17 paths)

- markers: none in any of the 17 (`grep -n '^<<<<<<<\|^=======$\|^>>>>>>>'`).
- `python3 -m compileall -q` over the 16 `.py` files: OK.
- Tests, run in a throwaway shadow tree (upstream tree + every row change that merges cleanly + these
  17 resolved files) because several row modules this batch's tests import
  (`tools/bot_delivery_queue.py`, …) are outside this batch and absent from a main-only worktree:
  **240 passed, 16 skipped, 17 failed**. Every failure is in a test whose production counterpart is
  another batch's file, or upstream's own pre-existing failure:
  - `tests/gateway/test_chat_completions_approval.py` (6) — `gateway/platforms/api_server.py` has no
    `_register_live_run_handle` yet → **B1 runtime-core** (`api_server.py`, `api_server_runs.py`).
  - `tests/hermes_cli/test_cron_parser_builder.py::test_cron_script_timeout_flag_on_create_and_edit`
    (1) — `hermes_cli/subcommands/cron.py` lacks `--script-timeout-seconds` → **B2 host-cli**.
  - `tests/hermes_cli/test_kanban_db.py` (10) — `hermes_cli/kanban_db.py` (`archive_task(reason=…)`,
    link/delete rows) and `hermes_cli/kanban_db_dispatch.py` (storage-locked/respawn guard) →
    **B2 host-cli**. Of these, `test_infrastructure_spawn_refusal_never_charges_the_card` already
    fails on pure `origin/main` (53 passed, 1 failed, 1 skipped at baseline) — upstream's, not the
    merge's.
