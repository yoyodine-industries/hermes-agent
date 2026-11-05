"""The ONE exemption to the delegated-child fence: the operator-ask door's three acts.

WHY THE FENCE HAS AN EXEMPTION AT ALL
-------------------------------------
The delegated-child fence exists to stop a claimed worker mutating the estate: filing, closing,
re-prioritising, commenting, claiming. The operator-ask door (``admin_operator_card``) is the ONE
surface a host-administration seat uses to raise an ASK TO THE OPERATOR — and that door shells the
same ``hermes kanban`` verbs, so the fence refused it too ("NOTHING was carded") and every
consent-gated card a seat parked needed a top-level session for the mechanical write.

An operator ask is a RAISE, not a self-dealing board mutation: the card it creates is addressed to
the operator, it is born at the board's own floor (never in the band), it is designated through the
kernel's designation door, and its final lift is the kernel's own re-rank door — which still
enforces the designation ledger, the board floor and the storage guard. Nothing here lets a worker
move a card it did not just create, and nothing here writes a priority itself. That is the whole
reason this is the SOLE exemption: do not generalise it to another verb, another board, or another
card. A second call site is the abuse the fence exists to prevent.

THE EXEMPTION IS A NAMED GRANT, NOT A FLAG WE TRUST
---------------------------------------------------
Three acts and no others: the ``create`` that births the ask at the target board's floor, the
``defcon designate`` write that follows it, and the ``edit`` that raises THAT card's priority into
the operator band. Every other denied action keeps the fence exactly as it is.

The door is a plugin that shells the CLI, so the grant has to cross a process boundary: it rides
three explicit options (``--operator-ask-door``, ``--operator-ask-raise``,
``--operator-ask-card``) rather than an environment variable, so it cannot leak into a descendant
process and it is visible in the command line an auditor reads.

* ``--operator-ask-door`` names the door; only :data:`DOORS` may hold the exemption, so a second
  tool cannot inherit it by copying the flags.
* ``--operator-ask-raise <request>`` is the door's per-call id. The CREATE writes it onto the card
  as a provenance line; the DESIGNATE and EDIT must name ``--operator-ask-card <id>`` and are
  refused unless that exact card carries the same door+request. That is what stops the exemption
  becoming "a worker may re-prioritise any card on its own board": the id the edit may re-rank is
  the one this invocation's door just created, and no other.
* The CREATE is refused a priority at or above the reserved tranche, so the exemption can never
  birth a card inside the operator band.

PROVENANCE ON THE CARD, NOT IN A LOG
------------------------------------
The created card's body carries

    Operator-ask-door: door=<name> request=<id> filed-by=<lane> fenced-by=<task-id> board=<slug>

``fenced-by`` is MEASURED where it can be (the task id of the live claim whose ancestor pid would
otherwise fence the call) and falls back to the worker's own ``HERMES_KANBAN_TASK``. Raise acts are
therefore auditable by reading the card instead of reconstructing shell lineage.
"""

from __future__ import annotations

import os
import re
import sqlite3
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Optional

#: The explicit options the door passes. One vocabulary, so the three acts and their bind read as
#: one mechanism in ``hermes kanban --help`` and in the audit trail.
DOOR_OPTION = "--operator-ask-door"
REQUEST_OPTION = "--operator-ask-raise"
CARD_OPTION = "--operator-ask-card"

#: The doors that may hold the exemption. Deliberately a closed set of ONE: a copied flag with any
#: other door name is refused, which is the point of naming it.
DOORS: frozenset[str] = frozenset({"admin_operator_card"})

#: The three acts, and no others. ``defcon revoke`` and every other fence-denied verb stay fenced.
ACTS: frozenset[str] = frozenset({"create", "designate", "edit"})

#: The body line that binds the create to the designate/edit. Deliberately NOT the register's
#: ``Operator-ask:`` stamp: that stamp is an ADMISSION to the reserved top (999999) and is read by
#: keyword, and this line must never be mistaken for one (``Operator-ask-door:`` does not contain
#: ``Operator-ask:``).
PROVENANCE_PREFIX = "Operator-ask-door:"

#: One token-safe word: the line is machine-readable and is written into a card body, so a value
#: carrying whitespace or a newline is a refusal, never a body-injection.
_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/@-]{0,127}$")
_PROVENANCE_RE = re.compile(
    r"^[ \t>*\-]*Operator-ask-door:[ \t]*(?P<rest>\S.*?)[ \t]*$", re.MULTILINE
)


class RaiseGrantRefused(ValueError):
    """A malformed, incomplete or unbound operator-ask exemption. Nothing is written."""


@dataclass(frozen=True)
class RaiseGrant:
    """One validated exemption, scoped to exactly one kernel call.

    ``act`` is the verb it was validated for; ``card_id`` is the card this invocation may lift
    (``None`` on the create, whose id does not exist yet — the create's own binding is the
    ``request`` it writes onto the new card).
    """

    door: str
    request: str
    act: str
    board: str
    store: Path
    filed_by: str
    fenced_by: Optional[str] = None
    card_id: Optional[str] = None


_ACTIVE: ContextVar[Optional[RaiseGrant]] = ContextVar(
    "hermes_kanban_operator_ask_grant", default=None
)


@contextmanager
def scoped(grant: Optional[RaiseGrant]) -> Iterator[Optional[RaiseGrant]]:
    """Enter *grant* for exactly one handler call; ``None`` is a no-op scope."""
    if grant is None:
        yield None
        return
    token = _ACTIVE.set(grant)
    try:
        yield grant
    finally:
        _ACTIVE.reset(token)


def active_grant() -> Optional[RaiseGrant]:
    """The grant in force for the kernel call being executed, or ``None``."""
    return _ACTIVE.get()


def store_matches(store: str | Path, path: str | Path | None) -> bool:
    """True when *path* is *store*, or the board directory *store* lives in."""
    if path is None:
        return False
    try:
        target_r = Path(path).expanduser().resolve()
        store_r = Path(store).expanduser().resolve()
    except OSError:  # pragma: no cover - a path that cannot be resolved is not this grant's
        return False
    if target_r == store_r:
        return True
    return target_r.name != "kanban.db" and (target_r / "kanban.db") == store_r


def admits_target(grant: Optional[RaiseGrant], path: str | Path | None) -> bool:
    """True when *grant* was validated for the store *path* names (no ContextVar needed)."""
    return grant is not None and store_matches(grant.store, path)


def admits_store(path: str | Path | None) -> bool:
    """True when the grant in force admits a fence check against *path*.

    The single read the delegated-child fence makes. The grant was validated against the same
    store before it was entered (:func:`grant_for`), so this is an identity check, not a policy.
    A path that is a board DIRECTORY is compared through its ``kanban.db``.
    """
    return admits_target(_ACTIVE.get(), path)


# ------------------------------------------------------------------------------------ provenance


def provenance_line(*, door: str, request: str, filed_by: str, fenced_by: Optional[str],
                    board: str) -> str:
    """The machine-readable body line the create writes onto the ask card."""
    return (f"{PROVENANCE_PREFIX} door={door} request={request} filed-by={filed_by} "
            f"fenced-by={fenced_by or '-'} board={board}")


def parse_provenance(body: Optional[str]) -> dict[str, str]:
    """The first provenance line in *body* as a dict, or ``{}`` when there is none."""
    if not body:
        return {}
    match = _PROVENANCE_RE.search(body)
    if match is None:
        return {}
    fields: dict[str, str] = {}
    for part in match.group("rest").split():
        key, sep, value = part.partition("=")
        if sep and key not in fields:
            fields[key] = value
    return fields


def stamp_body(body: Optional[str], grant: RaiseGrant) -> str:
    """Append the provenance line to *body* — the card, not a log, carries the raise's lineage."""
    line = provenance_line(
        door=grant.door, request=grant.request, filed_by=grant.filed_by,
        fenced_by=grant.fenced_by, board=grant.board,
    )
    text = (body or "").rstrip("\n")
    return f"{text}\n\n{line}\n" if text else f"{line}\n"


def _card_row(store: Path, card_id: str) -> Optional[tuple[str, str, int]]:
    """``(body, board, priority)`` for *card_id* on *store*, or ``None`` when it is not there.

    A read, on a read-only connection: the exemption must be decidable BEFORE the handler opens
    the write transaction, and a store that cannot be read is no bind at all.
    """
    try:
        conn = sqlite3.connect(f"file:{store}?mode=ro", uri=True, timeout=5.0)
    except sqlite3.Error:
        return None
    try:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT body, priority FROM tasks WHERE id = ?", (card_id,)
        ).fetchone()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    if row is None:
        return None
    return (row["body"] or "", "", int(row["priority"] or 0))


def card_is_bound(store: Path, card_id: str, *, door: str, request: str) -> bool:
    """True when *card_id* on *store* carries THIS door's provenance for THIS request."""
    row = _card_row(store, card_id)
    if row is None:
        return False
    body = row[0]
    fields = parse_provenance(body)
    return fields.get("door") == door and fields.get("request") == request


# ---------------------------------------------------------------------------------------- grant


def _act_of(args: Any) -> Optional[str]:
    """The exemption's act name for this invocation, or ``None`` when it is not one of the three."""
    action = getattr(args, "kanban_action", None)
    if action == "create":
        return "create"
    if action == "edit":
        return "edit"
    if action == "defcon" and getattr(args, "defcon_action", None) == "designate":
        return "designate"
    return None


def _token(value: Any, option: str) -> str:
    text = str(value or "").strip()
    if not _TOKEN_RE.match(text):
        raise RaiseGrantRefused(
            f"{option} must be one token-safe word (letters, digits, _ . : / @ -), got {text!r}"
        )
    return text


def _board_slug(store: Path) -> str:
    """The slug of the board whose DB is *store* (``boards/<slug>/kanban.db``; else ``default``)."""
    from hermes_cli import kanban_db as kb

    try:
        rel = Path(store).resolve().parent.relative_to(Path(kb.boards_root()).resolve())
    except ValueError:
        return kb.DEFAULT_BOARD
    return rel.parts[0] if rel.parts else kb.DEFAULT_BOARD


def _filed_by(args: Any) -> str:
    explicit = getattr(args, "created_by", None)
    if explicit:
        return _token(explicit, "--created-by")
    from hermes_cli.profiles import resolve_acting_profile_name

    return _token(resolve_acting_profile_name("user"), "the acting profile")


def _fenced_by(store: Path) -> Optional[str]:
    """The dispatched task id whose claim would otherwise fence this call, when it can be seen.

    Measured from the LIVE CLAIM (the provenance arm's own read) so the card records the truth
    rather than the caller's claim; falls back to the worker's own ``HERMES_KANBAN_TASK``.
    """
    try:
        from agent.delegation_context import fencing_claim_task_id

        measured = fencing_claim_task_id(store)
    except Exception:  # health: allow BLE001 -- unmeasurable provenance must not un-file an ask
        measured = None
    if measured:
        return measured
    task_id = (os.environ.get("HERMES_KANBAN_TASK") or "").strip()
    return task_id or None


def grant_for(args: Any) -> Optional[RaiseGrant]:
    """Validate the operator-ask options on *args*; ``None`` when the invocation carries none.

    THE ONE CONSTRUCTION SITE of an exemption: this function is called from exactly one place in
    the tree (``kanban.kanban_command``), and ``tests/hermes_cli/test_kanban_operator_ask_door.py``
    asserts that so a second tool cannot inherit the exemption by copying the call.

    Raises :class:`RaiseGrantRefused` for a partial, unknown, mismatched or unbound grant. A
    refusal is loud and nothing is written — an invocation that names the door but fails the bind
    must never degrade into a fenced-but-silent write.
    """
    door = getattr(args, "operator_ask_door", None)
    request = getattr(args, "operator_ask_raise", None)
    card_id = getattr(args, "operator_ask_card", None)
    if not any(value for value in (door, request, card_id)):
        return None

    act = _act_of(args)
    if act is None:
        raise RaiseGrantRefused(
            "the operator-ask exemption covers exactly three acts — `create`, `defcon designate` "
            "and `edit` — so it cannot be claimed for this verb; every other denied action keeps "
            "the delegated-child fence"
        )
    if not door or not request:
        raise RaiseGrantRefused(
            f"the operator-ask exemption needs BOTH {DOOR_OPTION} and {REQUEST_OPTION} "
            "(the door's name and its per-call request id)"
        )
    door = _token(door, DOOR_OPTION)
    request = _token(request, REQUEST_OPTION)
    if door not in DOORS:
        raise RaiseGrantRefused(
            f"{DOOR_OPTION} {door!r} is not an operator-ask door; the exemption is held by "
            f"{', '.join(sorted(DOORS))} only"
        )

    from hermes_cli import kanban as kcli
    from hermes_cli import kanban_priority_policy as policy

    store = kcli._fence_target_store(args)
    if store is None:
        raise RaiseGrantRefused(
            "the operator-ask exemption has no target board: name an existing board with --board"
        )
    store = Path(store)
    board = _board_slug(store)
    filed_by = _filed_by(args)
    fenced_by = _fenced_by(store)

    if act == "create":
        if card_id:
            raise RaiseGrantRefused(
                f"{CARD_OPTION} is for the designate/edit that follows the create; the create's "
                "card does not exist yet"
            )
        priority = getattr(args, "priority", None)
        if priority is None:
            raise RaiseGrantRefused(
                "the operator-ask create must name the board's floor priority: the ask is BORN at "
                "the floor, never in the operator band"
            )
        if int(priority) >= int(policy.TRANCHE_FLOOR):
            raise RaiseGrantRefused(
                f"the operator-ask create may not be BORN at priority {int(priority)}: "
                f"{int(policy.TRANCHE_FLOOR)}..{int(policy.TRANCHE_TOP)} is the reserved tranche "
                "and the raise into it is the kernel's designation + re-rank, not a birth"
            )
        return RaiseGrant(door=door, request=request, act=act, board=board, store=store,
                          filed_by=filed_by, fenced_by=fenced_by)

    # designate / edit: bound to the card the SAME door invocation just created.
    if not card_id:
        raise RaiseGrantRefused(
            f"the operator-ask {act} needs {CARD_OPTION} <the card this door invocation just "
            "created>: the exemption never reaches a pre-existing card"
        )
    card_id = _token(card_id, CARD_OPTION)
    row = _card_row(store, card_id)
    if row is None:
        raise RaiseGrantRefused(
            f"the operator-ask {act} names {card_id}, which is not a card on board {board!r}"
        )
    if not card_is_bound(store, card_id, door=door, request=request):
        raise RaiseGrantRefused(
            f"the operator-ask {act} names {card_id}, which carries no provenance for door "
            f"{door!r} request {request!r}: the exemption is bound to the card this invocation's "
            "door just created, never to another card on the board"
        )
    if act == "edit":
        priority = getattr(args, "priority", None)
        if priority is None:
            raise RaiseGrantRefused(
                "the operator-ask edit is the RAISE act and must name --priority; a title/body "
                "edit needs no exemption and is refused one"
            )
        if int(priority) < row[2]:
            raise RaiseGrantRefused(
                f"the operator-ask edit may only RAISE {card_id}: it reads priority {row[2]} and "
                f"the request would lower it to {int(priority)}; demotion is the revoke door's act"
            )
    return RaiseGrant(door=door, request=request, act=act, board=board, store=store,
                      filed_by=filed_by, fenced_by=fenced_by, card_id=card_id)
