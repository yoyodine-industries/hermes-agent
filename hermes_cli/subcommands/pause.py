"""``hermes pause`` / ``hermes resume`` — the global emergency stop.

``pause`` writes a hold into the ESTOP registry at ``$HERMES_HOME/ESTOP``; cron, kanban and
new gateway turns halt on their next check (in-flight work is never killed). ``resume``
releases the CALLER'S OWN hold — never another holder's — and reports any that remain.
Ported from gastownhall/gastown estop.go (MIT).

Two scopes, one sentinel:

* a BARE ``hermes pause`` is ``mode=estop`` — the total panic button: every non-platform lane
  stops for dispatch and for new turns. The standing platform floor (``agent.estop.
  STANDING_ADMITTED_LANES``: ``default`` and the ``platform-*`` lanes) keeps working — the
  cron tick and the kanban dispatcher RUN and refuse per item by lane, so the lanes that keep
  the box alive are not parked by the stop meant to protect it.
* ``hermes pause --lockdown --allow-profile P`` is ``mode=lockdown`` — the stop holds every
  lane except the ones named. The lanes are the key: **board placement is not a factor**, so
  an admitted lane spawns on any board and a held lane spawns on none. There is no ``--board``.

``--allow-user`` (the operator's authenticated id) keeps messaging working THROUGH either
scope. Every hold carries its owner and a handle; ``--ttl`` arms a deadman that lifts only
ITS OWN hold if the window job dies before release.

WHO this command arms AS is resolved from the environment (``agent.estop.actor_handle``), not
from a default: an interactive operator session arms as ``operator``; an unattended caller
arms as ``bot:<profile>``, a dispatched card as ``card:<id>``. The hold is recorded under that
actor's own name, and the ``--owner``-shaped claim is kept on the record as ``claimed_owner``
rather than worn. A third-party re-arm that lands after the operator's resume needs
``--order`` (or HERMES_ESTOP_ORDER) — see ``agent.estop.order_gate`` — and is alerted to the
ops board. ``--json`` prints one machine-readable object on stdout (human notices move to
stderr).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# The lanes the operator's ruling names as the always-on set ("ops BOTS should always be
# enabled, but not ops BOARD", defcon t_067be5b6, 2026-09-27). Printed as the recommendation
# when a lockdown names no lane; never applied silently — the allowlist is the operator's.
RECOMMENDED_LANES = ("default", "platform-stl", "platform-worker", "platform-coder")

# D9: the ops board and the lane the re-arm alert is filed to.
OPS_BOARD = "ops"
OPS_ALERT_ASSIGNEE = "yoyodine-majordomo"


def _say(message: str, json_mode: bool) -> None:
    """Print a human line — to STDERR under ``--json``, so stdout stays parseable."""
    print(message, file=sys.stderr if json_mode else sys.stdout)


def _file_ops_alert(*, resume_at: str, actor: str, reason, order, ttl) -> dict:
    """D9: file ONE card on the ops board for a re-arm that overrode the operator's resume.

    Best-effort by contract: it NEVER changes the exit status of the pause, and its failure is
    returned (and recorded as a ledger event by the caller) rather than swallowed. Deduped on
    the RESUME timestamp through ``idempotency_key``, so a flapping re-arm files one card for
    the resume it overrode, not one per attempt.
    """
    title = f"ESTOP re-armed without an operator order (after the operator resume at {resume_at})"
    body = "\n".join([
        "Filed by `hermes pause` (D9 alert): a third-party actor re-armed the emergency stop",
        "AFTER the operator had resumed, carrying an order the platform did not adjudicate.",
        "",
        f"- actor: {actor}",
        f"- operator resume overridden: {resume_at}",
        f"- order given: {order}",
        f"- reason: {reason}",
        f"- ttl: {ttl}",
        "",
        "The pause IS in force (the arm landed). This card exists so the override is visible to",
        "the lane that owns host administration, not discovered later in a ledger nobody read.",
    ])
    try:
        from hermes_cli.kanban_db import create_task
        from hermes_cli.kanban_db_connect import connect
    except ImportError as exc:  # pragma: no cover - defensive
        return {"filed": False, "id": "", "detail": f"kanban unavailable: {exc}"}
    try:
        conn = connect(board=OPS_BOARD)
        try:
            task_id = create_task(
                conn,
                title=title,
                body=body,
                assignee=OPS_ALERT_ASSIGNEE,
                created_by="hermes-pause",
                idempotency_key=f"estop-rearm-after-resume-{resume_at}",
            )
        finally:
            conn.close()
    except Exception as exc:
        return {"filed": False, "id": "", "detail": f"{type(exc).__name__}: {exc}"}
    return {"filed": True, "id": task_id, "detail": ""}


def _print_history(limit: int, json_out: bool) -> int:
    """Operator-facing read of the hold history (the D6 ledger), oldest first.

    Answers *what was held, by whom, over which lanes, and whether it was lifted* from the
    append-only record beside the sentinel — the record that survives a lift. READ-ONLY: it
    never arms or lifts, so it is safe from ANY venue (a fenced worker may still read the
    record of the hold that is starving it). This is the surface the record exists for: the
    history is legible to the lane that needs it, not a file only the armer can see.
    """
    from agent.estop import _ledger_files, read_events

    rows = read_events()
    if limit and limit > 0:
        rows = rows[-limit:]
    sources = [str(p) for p in _ledger_files() if p.exists()]
    where = ", ".join(sources) if sources else "no ledger file written yet"
    if json_out:
        print(json.dumps(
            {"ledgers": sources, "count": len(rows), "events": rows},
            indent=2, sort_keys=True, default=str,
        ))
        return 0
    print(f"ESTOP hold history — ledger(s): {where}  "
          f"({len(rows)} row{'s' if len(rows) != 1 else ''} shown)")
    if not rows:
        print("  (no recorded holds — no arm or lift has been logged beside this sentinel)")
        return 0
    for row in rows:
        lanes = row.get("allow_profiles")
        if lanes is None:
            allow = row.get("allow")
            lanes = (allow or {}).get("profiles") if isinstance(allow, dict) else None
        scope = ", ".join(lanes) if lanes else (
            "total halt" if row.get("mode") == "estop" else "none")
        parts = [
            str(row.get("at") or "?"),
            f"{str(row.get('event') or '?'):<16}",
            f"handle={row.get('handle') or '-'}",
            f"mode={row.get('mode') or '-'}",
            f"lanes=[{scope}]",
        ]
        if row.get("actor"):
            parts.append(f"actor={row['actor']}")
        if row.get("reason"):
            parts.append(f"reason={row['reason']}")
        print("  " + "  ".join(parts))
    return 0


def _arm_refusal(action: str = "arm") -> "str | None":
    """Non-None when THIS process must not arm or lift a hold.

    Thin delegate to :func:`agent.estop.arm_admission`: the fence lives on the HOLD
    (``agent/estop.py``), so this command, the gateway's ``/pause`` path and a direct
    ``estop.acquire()``/``release()`` all enforce ONE policy, not three copies of it. A
    dispatched kanban worker and a delegated child are both work the fleet itself started:
    the lane being held would be the one releasing its own hold, which is the one thing the
    allowlist must never be — lane-editable. Interactive operator/ops-head contexts (and the
    yoyoflow runner that owns its own handle) arm and release normally.

    Returns the refusal STRING this module's callers print (``None`` when admitted), and
    fails OPEN on any probe error: nothing may block the operator's panic button.
    """
    try:
        from agent.estop import arm_admission
    except ImportError:  # pragma: no cover - defensive
        return None
    try:
        _admitted, _venue, refusal = arm_admission(action)
    except Exception:  # pragma: no cover - defensive
        return None
    return refusal or None


def _live_profile_names() -> "list[str]":
    """Every live profile name, for an allowlist-typo refusal message."""
    try:
        from hermes_cli.profiles import list_profiles
    except ImportError:  # pragma: no cover - defensive
        return []
    try:
        return sorted({info.name for info in list_profiles()})
    except Exception:  # pragma: no cover - defensive
        return []


def _profile_is_real(name: str) -> bool:
    try:
        from hermes_cli.profiles import profile_exists
    except ImportError:  # pragma: no cover - defensive
        return True
    try:
        return bool(profile_exists(name))
    except Exception:  # pragma: no cover - defensive
        return True


def cmd_pause(args: argparse.Namespace) -> int:
    """Engage the global emergency stop (total, or lane-scoped with ``--lockdown``)."""
    from agent.estop import (
        MODE_ESTOP, MODE_LOCKDOWN, EstopOrderRequired, EstopRefusal, ORDER_ENV, actor_handle, arm,
        get_state, is_engaged, ledger_path, parse_duration, record_event,
    )

    if getattr(args, "history", False):
        # READ-ONLY, and handled BEFORE the arm fence: any venue (including a fenced worker)
        # may read the record of the hold that is in force.
        return _print_history(int(getattr(args, "history_limit", 20) or 0),
                              bool(getattr(args, "json", False)))

    refusal = _arm_refusal()
    if refusal:
        print(f"⛔ Refusing to arm the emergency stop: {refusal}.")
        print("   The stop is operator/ops-head owned: a lane cannot arm it for itself, and the")
        print("   worker whose card a hold is starving cannot be the thing that lifts it.")
        return 3

    reason = getattr(args, "reason", None)
    lanes = list(getattr(args, "allow_profile", None) or [])
    lockdown = bool(getattr(args, "lockdown", False))

    if lockdown and not lanes:
        print("⛔ --lockdown names no lane — refusing to arm a lockdown that admits nobody.")
        print(f"   Pass --allow-profile for at least one lane (recommended: {', '.join(RECOMMENDED_LANES)}).")
        print("   A bare `hermes pause` is the total halt; use that instead of an empty lockdown.")
        return 2

    unknown = [name for name in lanes if not _profile_is_real(name)]
    if unknown and lockdown:
        print(f"⛔ Unknown profile(s) in --allow-profile: {', '.join(unknown)} — refusing to arm.")
        print("   The lane gate fails CLOSED on an unknown id (unknown ids grant nothing), so")
        print("   this lockdown would hold the very lane it meant to admit.")
        live = _live_profile_names()
        if live:
            print(f"   Live profiles: {', '.join(live)}")
        return 2

    ttl = getattr(args, "ttl", None)
    if ttl and parse_duration(ttl) is None:
        print(f"⛔ Invalid --ttl {ttl!r} — use a duration such as 45m, 90m or 2h. NOT pausing.")
        return 2

    allow = {
        "user_ids": list(getattr(args, "allow_user", None) or []),
        "profiles": lanes,
    }
    mode = MODE_LOCKDOWN if lockdown else MODE_ESTOP
    json_out = bool(getattr(args, "json", False))
    actor = actor_handle()
    order = (getattr(args, "order", None) or os.environ.get(ORDER_ENV) or "").strip() or None
    already = is_engaged()
    try:
        outcome = arm(
            reason=reason, allow=allow, ttl=ttl, mode=mode, actor=actor, order=order,
            # A repeated `hermes pause` is idempotent for its OWN holder: it replaces the entry
            # recorded under this actor, never stacking a second hold for the same venue.
            replace_owner=True,
        )
    except EstopOrderRequired as exc:
        # D8: the venue is admitted, but this arm would resurrect a stop the operator lifted
        # and carries no order. Nothing was written — the sentinel is untouched.
        print(f"⛔ {exc}")
        return 5
    except EstopRefusal as exc:
        # The HOLD itself refused this venue (agent/estop.py::arm_admission), i.e. exactly the
        # refusal the pre-check above prints. Same leading shape, rc=3, nothing armed —
        # whatever surface got here (this CLI, the gateway, a direct acquire()).
        print(f"⛔ {exc}")
        return 3
    except OSError as exc:
        # A pause the operator believes is armed but that never reached the disk is the one
        # failure this command must never hide.
        print(f"⛔ The pause could NOT be recorded: {exc}")
        return 4

    path = outcome.path
    # Attribution facts that must survive BOTH output modes (under --json they go to stderr,
    # so stdout stays one parseable object).
    if outcome.renamed:
        _say(
            f"    ⚠ armed as {outcome.owner}: the body's claim '{outcome.claimed_owner}' is "
            "RECORDED, not worn (agent.estop.actor_handle decided who may wear what).",
            json_out,
        )
    if not outcome.verified:
        _say(
            "    ⚠ no provenance key could be written: this hold is UNSIGNED and will read "
            "back as unverified — a TOTAL halt.",
            json_out,
        )
    if not outcome.ledger:
        _say("    ⚠ the ledger could not be written — this arm is NOT recorded.", json_out)
    if outcome.after_resume:
        _say(
            f"    ⚠ RE-ARM over the operator's resume: {outcome.actor} re-armed the stop the "
            f"operator lifted at {outcome.after_resume}.", json_out,
        )
        _say(f"      order carried: {outcome.order}", json_out)
        _say(f"      ledger: {ledger_path()}", json_out)
        alert = _file_ops_alert(
            resume_at=outcome.after_resume, actor=outcome.actor, reason=reason,
            order=outcome.order, ttl=ttl,
        )
        recorded = record_event(
            "alert", sentinel=Path(path), path=str(path), action="arm_after_resume",
            actor=outcome.actor, filed=alert["filed"], task=alert["id"] or None,
            detail=alert["detail"] or None, after_resume=outcome.after_resume,
        )
        if alert["filed"]:
            _say(
                f"      alert card {alert['id']} filed on the '{OPS_BOARD}' board to "
                f"{OPS_ALERT_ASSIGNEE}", json_out,
            )
        else:
            _say(
                f"      ⚠ the ops alert card could NOT be filed ({alert['detail']}) — recorded "
                "as a ledger event instead", json_out,
            )
        if not recorded:
            _say("      ⚠ the ledger could not be written — this re-arm is NOT recorded", json_out)

    if json_out:
        # D10: the exact machine shape (sorted, stable keys).
        print(json.dumps(
            {
                "handle": outcome.handle,
                "owner": outcome.owner,
                "actor": outcome.actor,
                "mode": outcome.mode,
                "path": outcome.path,
                "order": outcome.order,
                "verified": outcome.verified,
                "ttl_s": outcome.ttl_s,
            },
            indent=2, sort_keys=True,
        ))
        return 0

    state = get_state() or {}
    verb = "Still paused" if already else "Hermes paused"
    detail = f" — reason: {state.get('reason')}" if state.get("reason") else ""
    print(f"⏸️  {verb}{detail} ({mode})")
    print(f"    sentinel: {path}")
    print(f"    handle:   {outcome.handle}   (release this one: "
          f"`hermes resume --handle {outcome.handle}`)")
    if mode == MODE_LOCKDOWN:
        print(
            f"    scope: lanes admitted — {', '.join(lanes)}; every other lane's cards and jobs "
            "are HELD and recorded, never silently skipped")
        print("    board is NOT a key: an admitted lane spawns on any board, a held lane on none")
    else:
        from agent.estop import STANDING_ADMITTED_LANES

        print(
            "    scope: TOTAL halt — standing platform floor keeps working: "
            f"{', '.join(sorted(STANDING_ADMITTED_LANES))}; every other lane is held on "
            "every board")
        print("    board is NOT a key: a floor lane spawns on any board, a held lane on none")
        if lanes:
            print(
                f"    lanes named for TURN service only ({', '.join(lanes)}) — a total halt "
                "still stops their dispatch, and grants nothing beyond the floor")
        if unknown:
            print(
                f"    note: no live profile named {', '.join(unknown)} — the turn gate simply "
                "never matches it; lane identity only decides dispatch under --lockdown")
    allowed = state.get("allow") or {}
    if allowed.get("user_ids"):
        print(f"    allowlist (turns): {', '.join(allowed['user_ids'])} — served through the pause")
    if state.get("expires_at"):
        print(f"    deadman: auto-resumes at {state['expires_at']} (--ttl {ttl})")
    print("    In-flight work keeps running. `hermes resume` releases YOUR hold only.")
    return 0


def cmd_resume(args: argparse.Namespace) -> int:
    """Release the caller's own hold (``--handle``/``--owner`` name one; ``--all`` is the
    operator's exit)."""
    from agent.estop import EstopRefusal, actor_handle, is_engaged, release, sentinel_path

    refusal = _arm_refusal("lift")
    if refusal:
        print(f"⛔ Refusing to lift an emergency-stop hold: {refusal}.")
        print("   Release must come from the hold's own holder, not from the work it holds.")
        return 3

    handle = getattr(args, "handle", None)
    owner = getattr(args, "owner", None)
    lift_all = bool(getattr(args, "all", False))
    json_out = bool(getattr(args, "json", False))
    asked = [name for name, given in (("--handle", handle), ("--owner", owner), ("--all", lift_all))
             if given]
    if len(asked) > 1:
        print(f"⛔ Pass ONE of --handle/--owner/--all, not {len(asked)} of them ({', '.join(asked)}).")
        return 2

    actor = actor_handle()
    if not is_engaged():
        if json_out:
            print(json.dumps(
                {"released": [], "actor": actor, "unverified_cleared": 0, "remaining": []},
                indent=2, sort_keys=True,
            ))
        else:
            print(f"Hermes is not paused (no live hold at {sentinel_path()}).")
        return 0

    try:
        result = release(handle=handle, owner=owner, actor=actor, all_holds=lift_all)
    except EstopRefusal as exc:
        # The HOLD refused this venue (agent/estop.py::arm_admission) — same shape as the
        # pre-check above. The live hold stays in force: nothing was lifted.
        print(f"⛔ {exc}")
        print("   Release must come from the hold's own holder, not from the work it holds.")
        return 3
    if result.forbidden:
        # D7: admitted, but this is not the caller's to lift. Exit 6, distinct from a stale
        # handle (rc=2) and from "nothing released" (rc=3), so a script can tell them apart.
        print(f"⛔ {result.message}")
        if result.remaining:
            print(f"   Still held: {', '.join(sorted(result.remaining_owners))}")
        return 6
    if result.stale:
        print(f"⛔ Release refused: {result.message}")
        print("   Nothing was released — a blind release would lift someone else's scope.")
        return 2
    if not result.released:
        print(f"Nothing released: {result.message}")
        if result.remaining:
            print(f"Still held by: {', '.join(result.remaining_owners)} — this is NOT a resume.")
        return 3

    if not result.ledger:
        print("⚠  the ledger could not be written — this release is NOT recorded (the re-arm")
        print("   gate reads those records, so a later third-party arm will not be gated).")

    if json_out:
        # D10: the exact machine shape.
        print(json.dumps(
            {
                "released": list(result.handles),
                "actor": result.actor,
                "unverified_cleared": result.unverified_cleared,
                "remaining": sorted(result.remaining_owners),
            },
            indent=2, sort_keys=True,
        ))
        return 0

    print(f"▶️  {result.message}")
    if result.remaining:
        print(f"    STILL HELD by {', '.join(result.remaining_owners)} — the fleet is NOT resumed.")
        print("    Each remaining hold keeps its own scope until its holder releases it.")
    else:
        print("    Hermes resumed — dispatch picks up on the next tick.")
    return 0


def build_pause_parser(subparsers) -> None:
    """Attach the ``pause`` and ``resume`` subcommands to ``subparsers``."""
    pause_parser = subparsers.add_parser(
        "pause", help="Emergency stop: pause cron/kanban dispatch and new gateway turns",
        description="Engage the global emergency stop. Halts NEW work only — cron "
            "dispatch, kanban dispatch, and new gateway turns — until "
            "`hermes resume`. In-flight work is never killed. A bare pause is a "
            "TOTAL halt; --lockdown scopes it to every lane NOT named by "
            "--allow-profile. There is no --board: the stop is lane-scoped, never "
            "board-scoped.")
    pause_parser.add_argument(
        "--reason", default=None, help="Optional reason stored in the sentinel and shown to users")
    pause_parser.add_argument(
        "--lockdown", action="store_true",
        help="Lane-scoped stop: hold every lane EXCEPT the ones in --allow-profile "
             "(requires at least one lane)")
    pause_parser.add_argument(
        "--allow-profile", action="append", default=None, metavar="PROFILE",
        help="Lane admitted through --lockdown (repeatable; validated against the live "
             "profile roster). Also the turn gate for that profile in a total pause")
    pause_parser.add_argument(
        "--allow-user", action="append", default=None, metavar="ID",
        help="Authenticated user id served through the pause (repeatable) — normally the operator's")
    pause_parser.add_argument(
        "--ttl", default=None, metavar="DUR",
        help="Deadman: auto-lift THIS hold after this long (e.g. 45m, 90m, 2h, or seconds). "
            "Bounds a window job that dies between arm and release.")
    pause_parser.add_argument(
        "--order", default=None, metavar="TEXT",
        help="The authority for a re-arm that lands AFTER the operator's resume (also "
             "HERMES_ESTOP_ORDER). Without one such an arm is refused (rc=5): it would "
             "resurrect a stop the operator lifted, silently.")
    pause_parser.add_argument(
        "--history", action="store_true",
        help="Print the ESTOP hold history (the append-only ledger beside the sentinel) and "
             "exit. READ-ONLY — it neither arms nor lifts; it shows what was held, by whom, "
             "over which lanes, and whether it was released.")
    pause_parser.add_argument(
        "--history-limit", type=int, default=20, metavar="N",
        help="With --history, show only the newest N rows (default 20; 0 shows every row)")
    pause_parser.add_argument(
        "--json", action="store_true",
        help="Print one machine-readable object on stdout "
             "{handle, owner, actor, mode, path, order, verified, ttl_s}; notices go to stderr")
    pause_parser.set_defaults(func=cmd_pause)

    resume_parser = subparsers.add_parser(
        "resume", help="Lift the emergency stop set by `hermes pause`",
        description="Release the calling holder's OWN hold (the handle its actor was recorded "
            "under, or the one named by --handle). NEVER another holder's: if a co-holder's "
            "hold remains, it is reported and the fleet is not resumed. --all is the "
            "operator's emergency exit and is refused to every other actor.")
    resume_parser.add_argument(
        "--handle", default=None, metavar="H",
        help="Release exactly this hold (the value stored in the sentinel). A stale handle "
             "is refused, never silently ignored.")
    resume_parser.add_argument(
        "--owner", default=None, metavar="OWNER",
        help="Release the holds owned by OWNER. Releasing 'operator' from another actor is "
             "refused (rc=6) — a bot may not resume the fleet the operator stopped.")
    resume_parser.add_argument(
        "--all", action="store_true",
        help="Release EVERY live hold (operator only, rc=6 otherwise): the exit for a fleet "
             "stranded by holds nobody can name")
    resume_parser.add_argument(
        "--json", action="store_true",
        help="Print one machine-readable object on stdout "
             "{released, actor, unverified_cleared, remaining}")
    resume_parser.set_defaults(func=cmd_resume)
