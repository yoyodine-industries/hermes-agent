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
