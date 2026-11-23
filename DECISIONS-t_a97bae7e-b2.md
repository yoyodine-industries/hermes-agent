# B2 host-cli — per-file resolution record (t_a97bae7e)

Rules applied, in priority order:

- R0 never restore a line upstream deleted, unless the row owns the behaviour it removed.
- R1 upstream's side empty -> keep the row's.
- R2 both sides independent -> keep both, row's ordering.
- R3 upstream's shape, the row's semantics carried inside it.

`vs up` / `vs row` are diff line counts of the resolved file against each side.

- `hermes_cli/cli_commands_session_tools.py` — merged. vs up: 1+/1- vs row: 112+/46-
- `hermes_cli/cli_single_query.py` — merged. vs up: 70+/2- vs row: 40+/47-
- `hermes_cli/commands.py` — merged. vs up: 4+/2- vs row: 17+/16-
- `hermes_cli/config_defaults.py` — merged. vs up: 66+/8- vs row: 148+/214-
- `hermes_cli/cron.py` — merged. vs up: 4+/2- vs row: 51+/5- R2. Both sides kept: the row's job-arg table (script_timeout_seconds) and upstream's create/edit plumbing (timezone, grace_minutes, catch_up). NOTE: this passes `timezone`/`grace_minutes`/`catch_up` to `tools/cronjob_tools.cronjob`, which the row's version of that tool does not accept -- see FINDINGS in the card handoff.
- `hermes_cli/kanban_db.py` — merged. vs up: 4704+/238- vs row: 49+/256-
- `hermes_cli/kanban_db_dispatch.py` — merged. vs up: 4895+/171- vs row: 124+/195- R2. Row's dispatch body (lockdown, tick accounting, spawn boundary) with upstream's sibling modules intact; verified import-level that the row's guard/accounting hooks still run (test_kanban_dispatch_health, test_kanban_db_repair green).
- `hermes_cli/kanban_diagnostics.py` — merged. vs up: 190+/0- vs row: 76+/0-
- `hermes_cli/main.py` — merged. vs up: 5+/2- vs row: 141+/224-
- `hermes_cli/subcommands/cron.py` — merged. vs up: 13+/0- vs row: 40+/2- R2. Union parser: row's --script-timeout-seconds plus upstream's --timezone, --grace-minutes, --catch-up. Same cross-file dependency as hermes_cli/cron.py.
- `hermes_cli/subcommands/pause.py` — row-identical (upstream's side was superseded by the row's own rewrite). vs up: 583+/27-
- `hermes_cli/subcommands/peer.py` — merged. vs up: 1235+/41- vs row: 74+/4- R3. Kept the row's run/reply surface and carried upstream's distinct /p/<profile>/ refusal: `_profile_404_message()` reads the 404 body ONCE (an HTTPError body is a stream; two readers left the second empty) and the generic 'Unknown or unconfigured profile' stays the ambiguous LookupError while any other message is reported as the peer's own refusal. Without this, `hermes peer status` answered a parked-profile refusal with exit 0 (upstream test test_hermes_handle_reaches_the_profile_the_peer_serves[served3] red).
- `hermes_cli/web_routers/chat_workspaces.py` — merged. vs up: 12+/6- vs row: 10+/2-
- `hermes_cli/worktree_cmd.py` — merged. vs up: 19+/4- vs row: 100+/8- R3. Attention reporting now reads the UNFILTERED audit: the sweep plan claims every kanban-owning tree (skip-kanban), so taking attention from the attended records reported nothing for a tree kept only because its card cannot be resolved.
- `hermes_cli/worktree_gc.py` — merged. vs up: 226+/22- vs row: 24+/6-
- `hermes_state_maintenance.py` — merged. vs up: 438+/46- vs row: 64+/7-
- `hermes_state_repair.py` — merged. vs up: 26+/14- vs row: 35+/24-
- `hermes_yaml.py` — row-identical (upstream's side was superseded by the row's own rewrite). vs up: 64+/4-
- `plugins/platforms/matrix/adapter.py` — merged. vs up: 5+/1- vs row: 24+/11-
- `scripts/verify-bootstrap-version-stamp.py` — merged. vs up: 1+/1- vs row: 1+/1-
- `scripts/write_install_stamp.py` — merged. vs up: 1+/1- vs row: 1+/1-
- `tests/hermes_state/test_bounded_reclaim.py` — merged. vs row: 12+/4- Row's file, one helper adapted: `_grow()` now uses a fresh session id per call, because `prune_sessions` RETIRES what it removes (upstream `retire_prunable`, adopted by hermes_state_maintenance.py) and a retired id may not be re-created. 8/8 green.
- `tests/hermes_state/test_hermes_state_readonly_preflight.py` — row-identical (upstream's side was superseded by the row's own rewrite). vs up: 97+/42- ROW WINS. The clean merge kept three upstream tests asserting that the preflight POLICES sidecars, which the row deliberately removed; the merged file could not pass against its own preflight. The row's version is 12/12 green on the merged source.

---

# Addendum — review round 2 (t_b0e62cf7): row content on NON-conflicted paths

## R5 (new rule) — a row path is not safe because git did not mark it conflicted

A batch branch is `origin/main` plus that batch's resolutions, so a row path
**_outside_** the assigned list sits at UPSTREAM content on the branch even
though the assembly (a merge **on the carrier**) keeps the row's side of it.
Binding measurement: `git merge-tree --write-tree origin/overrides/live origin/main`
reports exactly **57** conflicted paths; every other path auto-merges with the
carrier's blob intact. 53 of the 57 are batch-owned (B1 18, B2 21, B3 14);
**4 are owned by no batch** and are reported to platform-stl below.

Consequence, and the reason this addendum exists: a batch can be "conflict-clean"
and still not import, because its modules carry the row's imports and the target
module is upstream-only on the branch. The resolution therefore carries the row
content the batch itself reaches, and records it here.

## Files whose row-side content the branch dropped, and what was done

(One row, `tests/conftest.py`, is the inverse — a file the row never touched where
the branch dropped **upstream's** content. It is marked as such and resolved in the
round-3 addendum.)

Carried (all verified NOT conflicted and NOT a B1/B3 path). `MERGE` = three-way
`git merge-file` (BASE=cut, OURS=carrier) which is what git does on a clean path;
`TAKE` = upstream never touched the file, so the merge result is the carrier blob.

| path | row-only content the batch needs | upstream | resolution |
|---|---|---|---|
| `hermes_state_common.py` | `RECLAIM_CHUNK_PAGES`, `RECLAIM_MAX_PAGES_PER_PASS`, `RECLAIM_MAX_SECONDS_PER_PASS`, `RECLAIM_MIN_FREELIST_PAGES`, `RECLAIM_CHUNK_YIELD_MS`, `RECLAIM_MIGRATE_MIN_FREELIST_RATIO` — imported by `hermes_state_maintenance` | absent | MERGE, +30 |
| `hermes_state.py` | `SessionDB.MESSAGE_RETENTION_SOURCES` — read by `prune_archived_messages` | absent | MERGE, +12 |
| `hermes_cli/kanban_db_boards.py` | `board_dispatch_enabled` et al. — imported by `kanban_db` / `kanban_db_dispatch` | absent | MERGE, +141/-5 |
| `hermes_cli/projects_db.py` | `sanitize_branch_name` — imported by `kanban_db_workspace` | unchanged | TAKE |
| `agent/delegation_context.py` | `PROVENANCE_REFUSAL_MESSAGE`, `ancestor_owns_live_kanban_claim`, `child_kanban_write_allowance`, `board_name_resolves` | absent | MERGE, +238 |
| `agent/estop.py`, `agent/skill_commands.py`, `gateway/status.py` | `preload_skill_problems`, `get_process_uptime_fingerprint`, `host_boot_witness`, estop surface | present, row-only symbols absent | MERGE |
| `hermes_cli/kanban_db_workspace.py`, `hermes_cli/kanban_pr_acceptance.py`, `hermes_cli/kanban_pr_acceptance_store.py` | `WorkspaceVolumeUnavailable`, `WorktreeAnchorNotRepo`, `needs_repository_checks`, `_COMPLETABLE` | present, symbols absent | MERGE |
| `hermes_cli/code_skew_content.py`, `hermes_cli/tree_fingerprint.py`, `hermes_cli/kanban_gate_invariants.py`, `hermes_cli/kanban_proof_gate.py`, `hermes_cli/kanban_landing_carrier.py`, `hermes_cli/kanban_filing_policy.py`, `hermes_cli/kanban_priority_policy.py`, `hermes_cli/kanban_protected_trees.py`, `hermes_cli/kanban_contract_resolve.py`, `hermes_cli/kanban_crash_sweep_guard.py`, `hermes_cli/kanban_consent_gate.py`, `hermes_cli/kanban_top_level_gate.py`, `hermes_cli/kanban_register.py`, `hermes_cli/substrate_access.py` | the batch's `from hermes_cli import …` targets | mixed | MERGE/ADD |
| `hermes_cli/delivery_keys.py`, `hermes_cli/kanban_completion_refs.py`, `hermes_cli/kanban_db_dispatch_retire.py`, `hermes_cli/kanban_db_links.py`, `hermes_cli/kanban_exits.py`, `hermes_cli/kanban_inbox.py`, `hermes_cli/kanban_adjudicate.py`, `hermes_cli/kanban_parent_state.py`, `hermes_cli/substrate_view.py`, `hermes_cli/subcommands/groups.py`, `tools/bot_delivery_queue.py`, `tools/bot_delivery_lease.py`, `tools/bot_delivery_notices.py`, `tools/process_registry_output.py` | carrier-only modules the batch imports (a missing module is invisible to a name audit: it resolves to no file at all) | absent | ADD |
| `tests/conftest.py` | **NOT row content — the inverse case.** Row vs cut: `git diff dce1e9b37581..origin/overrides/live -- tests/conftest.py` is **empty** (the row never touched this file). Upstream vs cut: `+233/-0` — upstream ADDED 233 lines after the cut | **changed, +233/-0** | **TAKE upstream's blob** (`git checkout origin/main -- tests/conftest.py`, blob `07259c6c`). The round-2 branch carried the *carrier* blob `025f7a88` (== the cut's content) and thereby dropped upstream's 233 lines — an R0 regression. Upstream's conftest is a strict superset of the cut's, so reverting removes no carrier fixture. See the round-3 addendum. |

**Not carried, deliberately:** the remaining row paths in the batch's import
closure (measured at 106 row-changed paths before these carries). Carrying them
would be performing the assembly merge inside a batch branch, which the design
forbids; they are guaranteed by the carrier-based merge measured above. This
addendum names the class and the boundary, so the assembler knows exactly what
is carried here and what it must supply.

## Acceptance (batch protocol)

- **Every module the row owns must still IMPORT in the merged tree — checked by
  import-smoke of the batch's modules *and* of the row paths they import, or by
  the row's test files COLLECTING. Never by `python -m compileall`:** a vanished
  cross-module NAME compiles green and only raises at import.

## Verification this round (all commands run on the branch)

- **import-smoke: 43/43 modules** imported (19 assigned + 24 carried row paths),
  `PYTHONPATH=.:<testlibs>` against the harness venv.
- `tests/hermes_state/test_bounded_reclaim.py` — **8 passed**.
- `tests/hermes_state/test_hermes_state_readonly_preflight.py` — **12 passed**.
- Broader row-oracle run on exactly those four files (boards, peer, yaml,
  diagnostics): **63 passed, 0 failed** (round 2 wrote 83 here; 83 counted the two
  `hermes_state` suites above as well — measured 63 for the four alone, round 3,
  and the reviewer measured 63 independently).
- `hermes_state_repair.py` against `test_state_db_repair_{non_destructive,live_writer_guard}.py`:
  this resolution fails **15** tests where the row's own file fails **17** — and the
  baseline is red on BOTH sides independently (the row's file and upstream's each
  fail the same 14 in `test_state_db_repair_non_destructive.py`), so the failures
  are environmental, not resolution defects. This resolution fixes two the row's
  version does not (`test_repair_checks_foreign_holders_before_opening_sqlite`,
  `test_repair_refuses_while_another_connection_holds_the_db`).
- Worktree teardown, with the ROW's own test file (the assembled oracle) and
  `HERMES_KANBAN_EVIDENCE_GATE=measure`: **26 passed** — the row's evidence gate
  defaults to `refuse` by design, so a bare `kb.complete_task()` in a temp home is
  refused; that is the gate working, not a resolution defect.

## Cross-batch dependencies (about the MERGED tree, not this branch)

- `hermes_cli/cron.py` / `subcommands/cron.py` pass `script_timeout_seconds=` to
  `tools/cronjob_tools.cronjob`; the kwarg is row-only. `cron/jobs.py` (B1) and
  `tools/cronjob_tools.py` (B3) must keep it — measured: B1's branch carries 9
  hits of `script_timeout_seconds` in `cron/jobs.py`. On this patchset branch the
  call is unexercisable (`TypeError`), which is expected.
- **Four conflicted paths belong to no batch** and still need an owner:
  `tools/mcp_tool.py`, `tui_gateway/methods_bot_relay.py`,
  `tests/gateway/test_lifecycle_state_db_check.py`,
  `tests/tools/test_bot_mode_refusal_contract.py`. Reported to platform-stl.

## Round-3 addendum — the inverse-R0 defect on `tests/conftest.py`

Round 2's record row for `tests/conftest.py` was **wrong**, and the resolution it
justified was wrong with it. Corrected measurements (all reproducible):

```
git diff dce1e9b37581..origin/overrides/live -- tests/conftest.py   -> EMPTY
git diff --stat dce1e9b37581..origin/main -- tests/conftest.py      -> +233 / -0
git rev-parse  origin/overrides/live:tests/conftest.py              -> 025f7a88  (== cut blob)
git rev-parse  origin/main:tests/conftest.py                        -> 07259c6c
```

So the row never touched this file; **upstream** added 233 lines to it after the
cut. Round 2 carried the carrier blob `025f7a88` on the path and thereby dropped all
233 upstream lines — an **R0 regression on a file the row does not own**, which is
the opposite of the "row content the branch dropped" framing the table uses, and
why the row read `unchanged | TAKE` (both wrong: upstream *had* changed it, and the
branch did not take upstream's version).

Fix: `git checkout origin/main -- tests/conftest.py` → blob `07259c6c`, identical to
`origin/main`. Upstream's conftest is a strict superset of the cut's (`+233/-0`), so
this removes nothing the carrier needed — no row fixture is lost. In particular
`scratch_prune_spawns` (conftest.py:716) is present and resolves.

**The class was audited, not just the instance.** Across all 63 paths this branch
changes vs `origin/main`, the inverse-R0 pattern — row-untouched file (`carrier blob
== cut blob`) AND upstream changed it AND the branch kept the carrier blob — has
**0 hits**. `tests/conftest.py` was the only member.

Round-3 measurements (harness venv + `testlibs` on `PYTHONPATH`), on the committed
tree `e236bb0c5e`:

- `tests/hermes_state/test_bounded_reclaim.py` + `test_hermes_state_readonly_preflight.py`
  — **20 passed** (reclaim stays 8/8).
- `tests/test_scratch_dir.py` — before: `29 passed, 3 skipped, 1 ERROR`
  (`fixture 'scratch_prune_spawns' not found`); after: **30 passed, 3 skipped,
  0 failed, 0 errors**. The two tests the reviewer saw failing at `origin/main`
  also pass here, so the fix is strictly better than the baseline.
- import-smoke over every changed `.py` — **62/62 imported** (63 in round 2; the
  63rd was `tests/conftest.py`, which no longer differs from `origin/main`).
- row-oracle selection (boards, peer, yaml, diagnostics) — **63 passed**.
- `tests/hermes_cli` **in full** was not run to completion — it is a multi-hour suite
  (~2h extrapolated) and rounds 1–2 already established that its baseline is red on
  both sides independently for reasons outside this batch. Two facts worth recording:
  1. The suite's collection aborts on a missing `keyring` module; `keyring` is an
     *optional extra* in `pyproject.toml` (`keychain = ["keyring==25.7.0"]`) and is
     absent from the host harness venv — environmental. Run with
     `--ignore=tests/hermes_cli/test_vault_key_cli.py` to get a signal.
  2. A bounded slice of the first six files (the region where the killed full run
     first went red): **119 passed, 1 failed**. The single failure is
     `test_agent_import_left_core.py::test_a_scheduled_sync_on_a_home_nothing_migrated_installs_the_plugin_and_runs`,
     which dies on `hermes: 'import-agent' is not a hermes command` because the
     optional `agent-importers` plugin is not installed in this venv — environmental,
     and not a path-guard or fixture effect. The batch's own four oracle files
     (63 passed) and `tests/test_scratch_dir.py` (0 errors) are green under the
     upstream guard fixtures, which is the specific risk this change introduced.

