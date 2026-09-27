"""``hermes pause`` / ``hermes resume`` — the global emergency stop.

``pause`` writes a hold into the ESTOP registry at ``$HERMES_HOME/ESTOP``; cron, kanban and
new gateway turns halt on their next check (in-flight work is never killed). ``resume``
releases the CALLER'S OWN hold — never another holder's — and reports any that remain.
Ported from gastownhall/gastown estop.go (MIT).

Two scopes, one sentinel:

* a BARE ``hermes pause`` is ``mode=estop`` — the total panic button: no exemptions anywhere.
* ``hermes pause --lockdown --allow-profile P`` is ``mode=lockdown`` — the stop holds every
  lane except the ones named. The lanes are the key: **board placement is not a factor**, so
  an admitted lane spawns on any board and a held lane spawns on none. There is no ``--board``.

``--allow-user`` (the operator's authenticated id) keeps messaging working THROUGH either
scope. Every hold carries its owner and a handle; ``--ttl`` arms a deadman that lifts only
ITS OWN hold if the window job dies before release.
"""

from __future__ import annotations

import argparse

# The lanes the operator's ruling names as the always-on set ("ops BOTS should always be
# enabled, but not ops BOARD", defcon t_067be5b6, 2026-09-27). Printed as the recommendation
# when a lockdown names no lane; never applied silently — the allowlist is the operator's.
RECOMMENDED_LANES = ("default", "platform-stl", "platform-worker", "platform-coder")


def _arm_refusal() -> "str | None":
    """Non-None when THIS process must not arm or lift a hold.

    A dispatched kanban worker and a delegated child are both work the fleet itself started:
    the lane being held would be the one releasing its own hold, which is the one thing the
    allowlist must never be — lane-editable. Interactive operator/ops-head contexts (and the
    yoyoflow runner that owns its own handle) arm and release normally.
    """
    try:
        from agent.delegation_context import is_delegated_child_process_context, owned_kanban_task
    except ImportError:  # pragma: no cover - defensive
        return None
    if is_delegated_child_process_context():
        return "this is a delegated child context"
    task = owned_kanban_task()
    if task:
        return f"this process is the dispatched worker for kanban task {task}"
    return None


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
    from agent.estop import MODE_ESTOP, MODE_LOCKDOWN, engage, get_state, is_engaged, parse_duration

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
    already = is_engaged()
    try:
        path = engage(reason=reason, allow=allow, ttl=ttl, mode=mode)
    except OSError as exc:
        # A pause the operator believes is armed but that never reached the disk is the one
        # failure this command must never hide.
        print(f"⛔ The pause could NOT be recorded: {exc}")
        return 4

    state = get_state() or {}
    verb = "Still paused" if already else "Hermes paused"
    detail = f" — reason: {state.get('reason')}" if state.get("reason") else ""
    print(f"⏸️  {verb}{detail} ({mode})")
    print(f"    sentinel: {path}")
    if mode == MODE_LOCKDOWN:
        print(
            f"    scope: lanes admitted — {', '.join(lanes)}; every other lane's cards and jobs "
            "are HELD and recorded, never silently skipped")
        print("    board is NOT a key: an admitted lane spawns on any board, a held lane on none")
    else:
        print("    scope: TOTAL halt — no exemptions, every lane and every board")
        if lanes:
            print(
                f"    lanes named for TURN service only ({', '.join(lanes)}) — this total halt "
                "still stops their dispatch")
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
    """Release the caller's own hold (``--handle`` names one explicitly)."""
    from agent.estop import DEFAULT_OWNER, is_engaged, release, sentinel_path

    refusal = _arm_refusal()
    if refusal:
        print(f"⛔ Refusing to lift an emergency-stop hold: {refusal}.")
        print("   Release must come from the hold's own holder, not from the work it holds.")
        return 3

    handle = getattr(args, "handle", None)
    owner = getattr(args, "owner", None)
    if handle and owner:
        print("⛔ Pass either --handle or --owner, not both.")
        return 2
    if not is_engaged():
        print(f"Hermes is not paused (no live hold at {sentinel_path()}).")
        return 0

    result = release(handle=handle, owner=None if handle else (owner or DEFAULT_OWNER))
    if result.stale:
        print(f"⛔ Release refused: {result.message}")
        print("   Nothing was released — a blind release would lift someone else's scope.")
        return 2
    if not result.released:
        print(f"Nothing released: {result.message}")
        if result.remaining:
            print(f"Still held by: {', '.join(result.remaining_owners)} — this is NOT a resume.")
        return 3

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
    pause_parser.set_defaults(func=cmd_pause)

    resume_parser = subparsers.add_parser(
        "resume", help="Lift the emergency stop set by `hermes pause`",
        description="Release the calling holder's own hold (owner 'operator' by default, or "
            "the hold named by --handle). NEVER another holder's: if a co-holder's hold "
            "remains, it is reported and the fleet is not resumed.")
    resume_parser.add_argument(
        "--handle", default=None, metavar="H",
        help="Release exactly this hold (the value stored in the sentinel). A stale handle "
             "is refused, never silently ignored.")
    resume_parser.add_argument(
        "--owner", default=None, metavar="OWNER",
        help="Release the holds owned by OWNER (default: operator)")
    resume_parser.set_defaults(func=cmd_resume)
