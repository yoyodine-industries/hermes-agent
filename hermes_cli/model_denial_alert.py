"""The ACT half of the model denylist: a refusal that is LOUD, NAMED and leaves a card.

A refusal that only raises is a *silent* failure by the estate's own rule: the run dies, the
operator never hears, and the same misconfiguration fires again tomorrow. So the refusal does
two things, in this order, and neither is optional:

1. files **exactly one** acting kanban card per ``(lane, model, day)``, idempotency key
   ``model-denied:<lane>:<canonical-model>:<yyyymmdd>`` (card t_a1ae7cc6, ruling B3);
2. raises :class:`hermes_cli.model_denylist.ModelDeniedError` — a hard, typed, non-zero-exit
   refusal, with **no** fallback model (a silent fallback still spends, which is the defect).

Routing is deterministic, never inferred (ruling B3):

* a **config/pin** cause (a ``model.default``, a stored pin, a cron ``model:``) is the lane's
  own misconfiguration -> the lane's ``<domain>-stl`` (the Design Authority that owns the
  lane's config);
* a **code/curation** cause (a catalog still offering the id) is the platform's defect ->
  ``platform-coder``.

This module is the *reuse* the ruling demands: the card is filed through the estate's own
kanban instrument (``hermes_cli.kanban_db.create_task`` + ``kanban_db_connect.connect``) — the
same call the deployed one-card-per-lane filer in ``scripts/model_egress_watch_nodes.py`` and
the sentinel's ``file_alerts`` use — with the same idempotency-key contract. There is no second
filer and no second board mapping.

Filing is BEST-EFFORT and must never mask the refusal: if the board cannot be reached the
refusal still raises (the spend is still stopped); the failure to file is reported on stderr.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from typing import Any, Optional

from hermes_cli.model_denylist import Denial, ModelDeniedError, canonical

#: The ``created_by`` label every denial card carries.
SOURCE = "model_denylist"

#: The seat that owns every code/curation-caused denial.
CODE_OWNER = "platform-coder"

#: lane-family prefix -> the board that family's cards live on. Mirrors the deployed filer's
#: mapping (``scripts/model_egress_watch_nodes.py``); a lane whose family is not named falls
#: back to DEFAULT_BOARD rather than guessing a board that does not exist.
BOARD_BY_PREFIX = {
    "platform": "platform",
    "financially": "financially",
    "research": "research",
    "default": "default",
}
DEFAULT_BOARD = "platform"

#: A cause in this set is a config/pin misconfiguration and routes to the lane's ``<domain>-stl``.
#: Anything else is a code/curation defect and routes to the coder seat.
_CONFIG_PIN_CAUSES = frozenset({"config", "pin", "stored_pin", "cron", "cron_pin", "model_default"})


def current_lane(home: Optional[str] = None) -> str:
    """The lane a refusal is attributed to: the profile name from ``HERMES_HOME``.

    ``HERMES_HOME`` for a profile is ``<fleet>/profiles/<name>``, so the lane is that ``<name>``;
    anything else (the fleet root, a bare home) is the host seat ``default``. The value is only
    used for routing and the idempotency key — a wrong lane mis-routes one card, it never weakens
    the refusal.
    """
    resolved = os.path.abspath(home or os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes"))
    parent = os.path.dirname(resolved)
    if os.path.basename(parent) == "profiles" and os.path.basename(resolved):
        return os.path.basename(resolved)
    return "default"


def board_for(lane: str) -> str:
    """The board a lane's card belongs on: the lane's OWN board."""
    name = (lane or "").strip().lower()
    if name in BOARD_BY_PREFIX:
        return BOARD_BY_PREFIX[name]
    return BOARD_BY_PREFIX.get(name.split("-", 1)[0], DEFAULT_BOARD)


def domain_of(lane: str) -> str:
    """``platform-coder`` -> ``platform``. The family a lane's Design Authority belongs to."""
    return (lane or "default").strip().lower().split("-", 1)[0] or "default"


def route(lane: str, cause: str) -> str:
    """The assignee for a denial card. Deterministic given ``(lane, cause)`` (ruling B3)."""
    if (cause or "").strip().lower() in _CONFIG_PIN_CAUSES:
        return "%s-stl" % domain_of(lane)
    return CODE_OWNER


def filing_key(lane: str, model: str, *, now: Optional[float] = None) -> str:
    """``model-denied:<lane>:<canonical-model>:<yyyymmdd>`` — one card per lane+model+day."""
    stamp = datetime.fromtimestamp(now if now is not None else _now(), tz=timezone.utc).strftime("%Y%m%d")
    return "model-denied:%s:%s:%s" % (lane, canonical(model), stamp)


def _now() -> float:
    import time

    return time.time()


def card_text(denial: Denial, *, lane: str, cause: str, where: str = "") -> tuple[str, str]:
    """(title, body) for the denial card. The body names the machine reason and the repair."""
    title = ("model DENIED: %s may not run %s (%s)"
             % (lane, denial.model, denial.machine_reason))
    lines = [
        "**%s** — a DENIED model was refused (auto-filed, %s)"
        % (SOURCE, datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")),
        "",
        "The fleet bans `%s` outright (operator, 2026-10-10: it broke the bank). A run on lane "
        "`%s` asked for it and was REFUSED — typed `ModelDeniedError`, no provider call, and "
        "deliberately NO fallback model, because a silent fallback still spends." % (denial.model, lane),
        "",
        "- refused id: `%s` (canonical `%s`)" % (denial.model, denial.canonical_id),
        "- machine reason: `%s`" % denial.machine_reason,
        "- banned by: `%s` layer" % denial.source,
        "- cause: `%s`" % cause,
        "- refused at: `%s`" % (where or "unknown"),
        "- routed to: `%s`" % route(lane, cause),
        "",
        "Repair: remove `%s` from the lane's `model.default`, stored pins, cron `model:` or "
        "`providers.*.models` entry (a config/pin cause), or drop it from the catalog/curation "
        "that still offers it (a code cause). Do NOT re-point it at a substitute model by hand "
        "and call it fixed — the id is what is banned." % denial.model,
        "",
        "Idempotency: `%s` — one card per lane+model+day; later refusals the same day file nothing."
        % filing_key(lane, denial.canonical_id, now=None),
    ]
    return title, "\n".join(lines)


def file_denial_card(
    denial: Denial, *, lane: str, cause: str = "pin", where: str = "",
    board: Optional[str] = None, now: Optional[float] = None,
) -> Optional[str]:
    """File ONE idempotency-keyed denial card. Returns its id, or ``None`` if not filed.

    Never raises: filing is best-effort so a broken board can never soften the refusal itself.
    The idempotency key makes a second refusal the same day a no-op (``create_task`` returns the
    existing row, which this function reports as already-filed rather than filing again).
    """
    try:
        from hermes_cli.kanban_db import create_task
        from hermes_cli.kanban_db_connect import connect as kb_connect
    except Exception as exc:  # pragma: no cover - environment dependent
        print("model-denial alert: kanban_db unavailable (%s); no card filed" % exc, file=sys.stderr)
        return None

    target_board = board or board_for(lane)
    key = filing_key(lane, denial.canonical_id, now=now)
    title, body = card_text(denial, lane=lane, cause=cause, where=where)
    try:
        conn = kb_connect(board=target_board) if target_board else kb_connect()
    except Exception as exc:  # pragma: no cover - environment dependent
        print("model-denial alert: cannot open board %r (%s)" % (target_board, exc), file=sys.stderr)
        return None
    try:
        tid = create_task(
            conn,
            title=title,
            assignee=route(lane, cause),
            body=body,
            idempotency_key=key,
            created_by=SOURCE,
            board=target_board,
        )
        return str(tid) if tid else None
    except Exception as exc:  # pragma: no cover - environment dependent
        print("model-denial alert: create_task failed for %s (%s)" % (lane, exc), file=sys.stderr)
        return None
    finally:
        try:
            conn.close()
        except Exception:
            pass


def refuse_denied_model(
    denial: Any, *, where: str = "", lane: Optional[str] = None, cause: str = "pin",
    board: Optional[str] = None, home: Optional[str] = None, now: Optional[float] = None,
) -> None:
    """The one refusal entry point: FILE one card, then RAISE. Never returns.

    ``denial`` may be a :class:`Denial`; anything else is coerced by :func:`denial` so a caller
    that only has the id still gets the full refusal.
    """
    if not isinstance(denial, Denial):
        from hermes_cli.model_denylist import denial as _denial

        found = _denial(denial)
        if found is None:  # pragma: no cover - a caller asked to refuse a permitted model
            raise ModelDeniedError(
                Denial(model=str(denial), canonical_id=canonical(denial), source="build_floor"),
                where=where,
            )
        denial = found
    resolved_lane = lane or current_lane(home)
    file_denial_card(denial, lane=resolved_lane, cause=cause, where=where, board=board, now=now)
    raise ModelDeniedError(denial, where=where)
