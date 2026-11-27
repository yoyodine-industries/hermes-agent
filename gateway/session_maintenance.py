"""``session.maintenance``: the owner runs one state.db maintenance pass for its own store.

The always-on gateway owns ``state.db``; a CLI rewriting it underneath is the second writer the
held-store refusal exists to stop. ``session.prune`` set the shape (the CLI reads a preview, the
OWNER performs the write) and this is its counterpart for the maintenance pass: prune, archived-row
retention, stale-open close, orphan reap, the FTS merge and the bounded reclaim
(``SessionDB.maybe_auto_prune_and_vacuum``), on the owner's OWN live handle, in a worker thread.
The owner is exempt from its own holder scan, which is why this is the one path that can migrate a
store (``auto_vacuum`` NONE -> INCREMENTAL, one rewrite per store) while every lane stays up.

A skipped reclaim is DATA, not a silent success: the receipt carries ``vacuum_skipped_holders`` and
the holder list, so a caller can print who to move and tell "reclaimed 0 bytes" from "never
attempted".
"""
import asyncio
from pathlib import Path

from hermes_state_runtime import RuntimeStoreError


def _optional_bool(params, name):
    value = params.get(name)
    if value is not None and not isinstance(value, bool):
        raise RuntimeStoreError('invalid_params')
    return value


def _optional_retention(params):
    value = params.get('retention_days')
    if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
        # A negative window builds a future cutoff and matches every ended session: refuse it here,
        # exactly as the store does (auto_prune=false is the disable switch, not a negative bound).
        raise RuntimeStoreError('invalid_params')
    return value


def _holder_pids(db):
    """``"PID 381 (hermes gateway run) (state.db)"`` for every holder of the store RIGHT NOW.

    Read AFTER the pass: the store's own scan only counts them, and a refusal the caller cannot
    act on is a log line. Never raises — the count in the receipt is already the verdict.
    """
    try:
        from hermes_state_holders import (
            describe_holder_pid, foreign_state_db_holders, in_process_state_db_holders)
        holders = (foreign_state_db_holders(db.db_path)
                   + in_process_state_db_holders(db.db_path, exclude=db))
        out = []
        for pid, target in holders:
            name = Path(target.removesuffix(' (deleted)')).name
            out.append(f"{describe_holder_pid(pid)} ({name})")
        return out
    except Exception:
        return []


async def maintain(connection, ref, params):
    authority, actor = connection.authority, connection.actor
    # A store-wide rewrite on the host's own store: the local operator only (a ticket from this
    # host's private control socket), never a remote or browser grant.
    if not connection.native_owner or 'session:operator' not in actor.capabilities:
        raise RuntimeStoreError('permission_denied')
    if actor.profile_id != authority.profile_id:
        raise RuntimeStoreError('profile_mismatch')
    retention_days = _optional_retention(params)
    vacuum = _optional_bool(params, 'vacuum')
    dry_run = bool(_optional_bool(params, 'dry_run'))
    authority._require_admission_open()
    # Resolve the profile's own `sessions` config HERE, while dispatch still holds the owner scope:
    # the tracked task below starts later, after the scope may have been left.
    from hermes_cli.config import load_config
    from hermes_constants import get_hermes_home
    sessions_cfg = dict(load_config().get('sessions') or {})
    sessions_dir = Path(get_hermes_home()) / 'sessions'
    from gateway.session_runtime_workers import track_mutation
    task = track_mutation(authority, _apply(authority, sessions_cfg, sessions_dir,
                                            retention_days, vacuum, dry_run))
    return await asyncio.shield(task)


async def _apply(authority, sessions_cfg, sessions_dir, retention_days, vacuum, dry_run):
    from hermes_state_maintenance import run_state_db_maintenance
    db = authority.db
    receipt = await asyncio.to_thread(
        run_state_db_maintenance, db, sessions_cfg=sessions_cfg, retention_days=retention_days,
        vacuum=vacuum, sessions_dir=sessions_dir, dry_run=dry_run)
    if receipt.get('vacuum_skipped_holders'):
        receipt['vacuum_skipped_holder_pids'] = await asyncio.to_thread(_holder_pids, db)
    return receipt
