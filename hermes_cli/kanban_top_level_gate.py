"""The top-level-only gate: a card that needs a genuine top-level context must
never be born, or spawned, dispatchable.

RULING (platform-stl, card ``t_efa908e8``; implemented by ``t_0d8f71ae``). The
defect class: a card whose whole Definition of Done requires a genuine
top-level session -- one NOT fenced as a delegated child -- declares that in
prose (``GATE-TOP-LEVEL-ONLY`` line-leading in its title or body). Nothing in
the fork read the marker (measured 2026-10-08: ``grep -n GATE-TOP-LEVEL-ONLY``
over the live tree gave 0 code hits), so such cards were born dispatchable and
the dispatcher spawned a worker that is structurally fenced from the work (the
delegate-child fence plus board confinement). On the platform board alone, 63
cards carried the marker, 29 of them ``ready``, while that board ran 538
spawns/48h; on defcon, ``t_1f28a622`` (born ``ready``) and ``t_bcc3f8ba`` (born
``todo``) each spawned a worker that could only block.

THE MARKER, and exactly how precisely it fires. A card declares the gate when a
LINE of its TITLE or BODY, after leading whitespace, BEGINS with
``GATE-TOP-LEVEL-ONLY`` (case-insensitive). The pattern is deliberately
line-anchored, so a card that merely QUOTES the token mid-line -- the ruling
card ``t_efa908e8`` and ``t_1f28a622`` both do -- does not fire. That is the
whole grammar; v1 adds no alias.

WHY PARK AND NEVER REFUSE. Proposing work is legal: deleting the proposal loses
the record of work the fleet intends to do. So both doors PARK the card in
``scheduled`` (with ``due_at`` NULL -- waiting on a human, not on time), a
status ``recompute_ready`` never promotes (it promotes only ``todo``/``blocked``),
so the card is stably non-dispatchable and a later tick cannot silently pick it
up. A silent skip would itself be a defect: the RUN door therefore writes a
DEDUPED ``top_level_only_held`` event, ONE comment naming the routes, and counts
the hold on the tick's own suppression line (``skipped_top_level_only``).

Why the OLD prescription was unfollowable, and why this gate replaces it. The
skill told authors to "birth it ``initial_status='blocked'``", but
``create_task`` REFUSES a born-blocked card (``CREATED_BLOCKED_TOKEN``, operator
ruling 2026-09-27, card ``t_5c89c04e``), so that instruction could not be
followed and authors shipped plain ``ready`` cards instead -- which is why the
marker went unenforced. This gate is the deterministic cure: the CREATE door
parks a marker card the moment it is filed, and the RUN door re-checks the
card's live text (an amendment can add the marker AFTER filing), so the marker
is enforced at both doors and never merely declared.

Determinism: no inference, no model, no network, no store -- one regex pass over
the card's own text. :func:`declares` never raises.
"""

from __future__ import annotations

import re
from typing import Optional

__all__ = [
    "SENTINEL",
    "SENTINEL_RE",
    "ROUTES",
    "declares",
    "park_comment",
]

#: The literal marker authors put on the first line of a top-level-only card.
SENTINEL = "GATE-TOP-LEVEL-ONLY"

#: A line of the TITLE or BODY that BEGINS (after leading spaces/tabs) with the
#: marker. ``^`` + ``re.M`` anchors each line; the trailing ``\b`` keeps a
#: longer word from matching; ``re.I`` per the ruling. A mid-line QUOTE of the
#: token does not match -- that is the precision the ruling demands.
SENTINEL_RE = re.compile(r"^[ \t]*GATE-TOP-LEVEL-ONLY\b", re.I | re.M)

#: The sanctioned top-level executor routes. Carried in the held card's comment
#: so the hold is actionable, not merely visible.
ROUTES = (
    "a genuine TOP-LEVEL session -- `hermes peer dm yoyodine/default \"<msg>\"` "
    "(the gateway at 127.0.0.1:8644 spins the turn inside the gateway process, "
    "not a descendant of any worker); or",
    "a `no_agent` cron one-shot -- `repeat=1`, `schedule='in 1m'`, "
    "`deliver='local'`, script under ~/.hermes/scripts/;",
    "pickup: `hermes kanban --board <b> unblock <id>` "
    "(blocked/scheduled -> ready), then execute and complete.",
)


def _text(value) -> str:
    """Read a title/body column that a real board store may hold as TEXT or BLOB.

    A guard that crashes on a ``bytes`` body is worse than none: measured on the
    ops store, comment bodies land as BLOB, and a regex straight over them
    raises ``TypeError``. Coerce, never raise.
    """
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("utf-8", "replace")
    return value or ""


def declares(title: object, body: object = None) -> bool:
    """True when the card's title or body declares the top-level-only gate.

    Pure, deterministic, one regex pass over the card's own text; never raises
    (``None`` and ``bytes`` fields are coerced). ``body`` is optional so a caller
    that only has the title still gets an answer.
    """
    text = f"{_text(title)}\n{_text(body)}"
    return SENTINEL_RE.search(text) is not None


def park_comment(task_id: Optional[str] = None) -> str:
    """The one comment the RUN door writes when it holds a marker card.

    Names the marker, why a worker cannot do the work, the park itself, and the
    sanctioned top-level routes -- so the hold is actionable rather than merely
    visible.
    """
    who = f"card {task_id}: " if task_id else ""
    return (
        f"TOP-LEVEL-ONLY HELD (not spawned) -- {who}the card declares "
        f"`{SENTINEL}`: its whole Definition of Done needs a genuine top-level "
        "context, and a dispatched worker is structurally fenced from it "
        "(delegate-child fence + board confinement).\n\n"
        "The card is parked `scheduled` (due_at NULL -- waiting on a human, not "
        "on time) and is NOT dispatchable: `recompute_ready` promotes only "
        "`todo`/`blocked`. Execute it from a sanctioned top-level route:\n"
        + "\n".join(f"  - {route}" for route in ROUTES)
    )
