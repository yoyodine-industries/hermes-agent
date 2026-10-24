"""Refusal plumbing for a WRITE to a consent-gated live surface (design t_cb9259fc).

WHY this module exists. The card doors of the consent gate (`t_e31d9241`) read card
PROSE, so they can refuse a card that *claims* consent it cannot show. They cannot
answer the question the standing order actually names -- "does this run intend to WRITE
a consent-gated live surface?" -- because prose is a terrible detector of intent (the
lexical rule measured ~0 precision over 396 live cards; the matches were prohibitions).
At the WRITE BOUNDARY the surface is not inferred, it is *named by the target*
(`config.yaml`, `.env`, live `SOUL.md`, plugins, launchd plists, `hermes config set`),
so detection is exact and the refusal is deterministic.

`hermes_cli/consent_surface.py` answers *which* surface a target is (pure, no I/O).
This module answers *may this run write it*, and when the answer is no, produces the
whole refusal:

* a YOLO-PROOF tool deny that names the surface, the target, the owning card, every
  reference the card cites, and the scope token a filer must file under (D4a);
* exactly ONE deduped durable receipt on the owning card -- ``task_events.kind =
  'consent_refused'``, cause :data:`CAUSE`, plus one comment -- and the card parked
  ``blocked``/``needs_input``, mirroring ``kanban_db_dispatch._park_for_consent`` (D4b);
* a per-session latch, so a worker cannot re-route the same surface through another
  door for the rest of the run (D4c).

The allow path is deliberately boring: no card, no gate; a covered surface, no gate.

Contract for the door cards (2-4) -- do not re-shape it::

    consent_gate_active(ctx=None)            -> bool
    surface_decision(hit, ctx=None)          -> ("allow" | "deny", message)
    refuse_surface_write(hit, ctx=None)      -> {"approved": False, "message": ...} (+receipt)
    emit_surface_refusal_receipt(hit, ctx=None) -> bool

``hit`` is a ``hermes_cli.consent_surface.SurfaceHit``
(``surface``, ``scope_token``, ``path``, ``reason``). It is read by attribute, so a test
double needs no import of the classifier. ``ctx`` is a :class:`GuardContext`, a mapping,
or ``None`` (resolve from the environment).

No exception ever escapes into the tool path: the guard may refuse a surface write, but
it may never wedge a worker (a store outage must not take a tool call down).
"""
from __future__ import annotations

import logging
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

logger = logging.getLogger(__name__)

__all__ = [
    "CAUSE",
    "COMMENT_AUTHOR",
    "DISARM_ENV",
    "GuardContext",
    "consent_gate_active",
    "emit_surface_refusal_receipt",
    "refusal_message",
    "refuse_surface_write",
    "reset_latch",
    "surface_decision",
]

#: Emergency disarm (D6). An ENV switch on purpose: a ``config.yaml`` key would need
#: the very consent this guard exists to require (chicken-and-egg).
DISARM_ENV = "KANBAN_CONSENT_SURFACE_GUARD"
_OFF_VALUES = frozenset({"off", "0", "false", "no", "disabled"})

#: The cause recorded on the card, distinct from the RUN door's ``no_ref``/``declared``.
CAUSE = "surface_write_without_consent"
RECEIPT_KIND = "consent_refused"
COMMENT_AUTHOR = "consent-surface-guard"

#: Per-session latch: (session key, surface) -> the cause that refused it. Bounded so a
#: long-lived gateway process cannot grow it without limit; clearing it only ever
#: re-allows re-evaluation.
_LATCH: dict[tuple[str, str], str] = {}
_LATCH_MAX = 512

_DISARM_WARNED = False
_APPROVALS_CORE: Any = None
_APPROVALS_CORE_TRIED = False


@dataclass(frozen=True)
class GuardContext:
    """Everything the guard needs to answer for one write, resolved once.

    ``board_db``/``approvals_db`` exist so tests (and a caller that already knows) can
    point the guard at a stand-in store; ``None`` means "resolve the normal way"
    (``kanban_db.kanban_db_path()`` and ``kanban_consent_gate._approvals_path()``).
    """

    task_id: Optional[str] = None
    session_id: Optional[str] = None
    board_db: Optional[Path] = None
    approvals_db: Optional[Path] = None

    @classmethod
    def from_env(cls) -> GuardContext:
        return cls(
            task_id=_env("HERMES_KANBAN_TASK"),
            session_id=_env("HERMES_SESSION_ID"),
        )


@dataclass(frozen=True)
class SurfaceVerdict:
    """The guard's deterministic answer for one (surface, card) pair."""

    cause: str = ""                 # "" == licensed
    detail: str = ""
    refs: tuple = ()
    approved_refs: tuple = ()       # licensed refs that DO cover the token

    @property
    def licensed(self) -> bool:
        return not self.cause


# --- small helpers -----------------------------------------------------------

def _env(name: str) -> Optional[str]:
    value = (os.environ.get(name) or "").strip()
    return value or None


def _ctx(ctx: Any) -> GuardContext:
    """Coerce a caller's context: ``None`` (env), a context, a mapping, or a duck."""
    if ctx is None:
        return GuardContext.from_env()
    if isinstance(ctx, GuardContext):
        return ctx
    if isinstance(ctx, Mapping):
        fields = {k: ctx.get(k) for k in ("task_id", "session_id", "board_db", "approvals_db")}
    else:
        fields = {k: getattr(ctx, k, None) for k in
                  ("task_id", "session_id", "board_db", "approvals_db")}
    for key in ("board_db", "approvals_db"):
        if fields.get(key):
            fields[key] = Path(str(fields[key]))
    for key in ("task_id", "session_id"):
        if fields.get(key) is not None:
            fields[key] = str(fields[key]).strip() or None
    return GuardContext(**fields)


def _hit(hit: Any) -> tuple[str, str, str]:
    """``(surface, scope_token, target)`` from a ``SurfaceHit`` or any duck of one.

    ``token``/``detail`` are read as fallbacks so the design doc's field names also
    work; ``scope_token``/``path`` are the pinned names in the classifier.
    """
    surface = str(getattr(hit, "surface", "") or "").strip()
    token = str(getattr(hit, "scope_token", "") or getattr(hit, "token", "") or "").strip()
    target = str(getattr(hit, "path", "") or getattr(hit, "detail", "") or "").strip()
    return surface, (token or surface), target


def _board_db_path(ctx: GuardContext) -> Path:
    if ctx.board_db is not None:
        return Path(ctx.board_db)
    from hermes_cli import kanban_db as kb

    return Path(kb.kanban_db_path())


def _approvals_path(ctx: GuardContext) -> Path:
    from hermes_cli import kanban_consent_gate as cg

    if ctx.approvals_db is not None:
        return Path(ctx.approvals_db)
    return Path(cg._approvals_path())


def _session_key(ctx: GuardContext) -> str:
    return ctx.session_id or ctx.task_id or f"pid:{os.getpid()}"


def _disarmed() -> bool:
    global _DISARM_WARNED
    value = (os.environ.get(DISARM_ENV) or "").strip().lower()
    if value not in _OFF_VALUES:
        return False
    if not _DISARM_WARNED:
        _DISARM_WARNED = True
        logger.warning(
            "KANBAN CONSENT SURFACE GUARD DISARMED via %s=%s: writes to config.yaml, "
            ".env, live SOUL.md, plugins, launchd plists and `hermes config set` are no "
            "longer checked for a covering approval. This is the documented rollback; "
            "unset the variable to re-arm.", DISARM_ENV, value,
        )
    return True


def reset_latch() -> None:
    """Drop the per-session latch (tests; a caller starting a fresh run)."""
    _LATCH.clear()


# --- the owning card ---------------------------------------------------------

def _read_owning_card(ctx: GuardContext) -> tuple[Optional[dict], Optional[str]]:
    """``(card row, store error)`` for the card that owns this session.

    Read-only on purpose: a guard must never migrate or create a board. A missing file
    is "no card" (inert); anything unreadable is an error the caller fails closed on --
    "cannot tell" is not "nobody owns this write".
    """
    path = _board_db_path(ctx)
    if not Path(path).exists():
        return None, None
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2.0)
    except (sqlite3.Error, OSError) as exc:  # unreadable store -> named cause
        return None, f"{path}: {type(exc).__name__}: {exc}"
    try:
        con.row_factory = sqlite3.Row
        con.text_factory = lambda b: b.decode("utf-8", "replace")
        row = con.execute(
            "SELECT id, title, body, status FROM tasks WHERE id = ?", (ctx.task_id,),
        ).fetchone()
        return (dict(row) if row is not None else None), None
    except sqlite3.Error as exc:
        return None, f"{path}: {type(exc).__name__}: {exc}"
    finally:
        con.close()


def _card_text(card: Mapping[str, Any]) -> str:
    return f"{card.get('title') or ''}\n{card.get('body') or ''}"


# --- coverage (D2) -----------------------------------------------------------

def _approvals_core() -> Any:
    """``approvals_core`` from the ops repo, or ``None`` when it is not reachable.

    The store and its core module live in the same directory, so a deployment that
    cannot import the core almost certainly cannot read the store either -- in which
    case the caller fails closed on the store error regardless. Resolved once per
    process; never raises.
    """
    global _APPROVALS_CORE, _APPROVALS_CORE_TRIED
    if _APPROVALS_CORE_TRIED:
        return _APPROVALS_CORE
    _APPROVALS_CORE_TRIED = True
    try:
        import sys

        from hermes_cli import kanban_consent_gate as cg

        core_dir = str(Path(os.environ.get("KANBAN_APPROVALS_CORE_DIR")
                            or cg.DEFAULT_APPROVALS_DB.parent))
        added = core_dir not in sys.path
        if added:
            sys.path.insert(0, core_dir)
        try:
            import approvals_core  # noqa: PLC0415 -- optional cross-repo module

            _APPROVALS_CORE = approvals_core
        finally:
            if added:
                try:
                    sys.path.remove(core_dir)
                except ValueError:
                    pass
    except Exception:
        logger.debug("consent-surface guard: approvals_core unavailable, using the "
                     "documented _key_covers rule", exc_info=True)
        _APPROVALS_CORE = None
    return _APPROVALS_CORE


def _key_covers(covering: str, token: str) -> bool:
    """Does the approval scope ``covering`` cover ``token``?

    ONE definition of coverage: ``approvals_core._key_covers`` -- a scope covers
    itself and every sub-scope appended after a ``:`` (``hermes-config`` covers
    ``hermes-config:platform-stl``), and never the other way round. The local form is
    the same two-line rule, used only when the ops repo is not on this host, so the
    guard degrades to identical semantics instead of wedging.
    """
    core = _approvals_core()
    fn = getattr(core, "_key_covers", None) if core is not None else None
    if fn is not None:
        return bool(fn(covering, token))
    return token == covering or token.startswith(covering + ":")


def _scope_of_key(key: str) -> str:
    """The scope half of an approvals ``idempotency_key`` (``project\\x1fscope``).

    Surface tokens carry no project axis, and ``_key_covers`` documents that a prefix
    match must never cross the project boundary -- comparing the scope halves keeps
    that property true by construction: whether the row's project is the surface's or
    another one, the comparison is on the scope the row actually declared.
    """
    return " ".join(str(key or "").split("\x1f")[-1].split()).lower()


def _covers(row: Mapping[str, Any], token: str, surface: str) -> bool:
    """Coverage for one approved row: declared scope, else the legacy literal test."""
    key = str(row.get("idempotency_key") or "").strip()
    if key:
        scope = _scope_of_key(key)
        return bool(scope) and _key_covers(scope, token.strip().lower())
    haystack = " \n ".join(
        str(row.get(field) or "") for field in ("decision", "reason", "task", "context")
    ).lower()
    return any(needle and needle in haystack
               for needle in {token.strip().lower(), surface.strip().lower()})


def _read_coverage_rows(path: Path, ids: list[int]) -> dict[int, dict]:
    """Read-only lookup of the coverage fields for the cited rows."""
    if not ids:  # an empty IN () is a syntax error, and nothing to cover either way
        return {}
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2.0)
    try:
        con.row_factory = sqlite3.Row
        con.text_factory = lambda b: b.decode("utf-8", "replace")
        placeholders = ",".join("?" * len(ids))
        rows = con.execute(
            "SELECT id, status, decision, reason, task, context, idempotency_key "
            f"FROM approvals WHERE id IN ({placeholders})", tuple(ids),
        ).fetchall()
        return {int(row["id"]): dict(row) for row in rows}
    finally:
        con.close()


def _ref_id(ref: str) -> Optional[int]:
    from hermes_cli import kanban_consent_gate as cg

    match = cg._REF.search(str(ref or ""))
    return int(match.group(1)) if match else None


# --- the assessment ----------------------------------------------------------

def _unreadable(cause: str, detail: str) -> SurfaceVerdict:
    return SurfaceVerdict(cause=cause, detail=detail)


def _assess(card: Mapping[str, Any], ctx: GuardContext, token: str,
            surface: str) -> SurfaceVerdict:
    """Evaluate the D2 coverage rule for one surface against one card."""
    from hermes_cli import kanban_consent_gate as cg

    path = _approvals_path(ctx)
    refs, store_error = cg._resolve_refs(_card_text(card), path)
    if store_error:
        return _unreadable("approvals_store_unreadable", store_error)
    if not refs:
        return SurfaceVerdict(cause="no_ref")
    licensed = tuple(r for r in refs if r.resolved and r.status in cg.LICENSING_STATUSES)
    if not licensed:
        return SurfaceVerdict(cause="no_approved_ref", refs=refs)

    ids = [rid for rid in (_ref_id(r.ref) for r in licensed) if rid is not None]
    try:
        rows = _read_coverage_rows(path, ids)
    except (sqlite3.Error, OSError) as exc:
        return _unreadable("approvals_store_unreadable", f"{path}: {type(exc).__name__}: {exc}")

    covering = tuple(
        r for r in licensed if _covers(rows.get(_ref_id(r.ref) or -1) or {}, token, surface)
    )
    if covering:
        return SurfaceVerdict(refs=refs, approved_refs=covering)
    return SurfaceVerdict(cause="scope_not_covered", refs=refs, approved_refs=licensed)


# --- the public contract -----------------------------------------------------

def consent_gate_active(ctx: Any = None) -> bool:
    """Whether the surface guard applies to this session at all (D5).

    INERT when the disarm env says off, when no card owns the session
    (``HERMES_KANBAN_TASK`` unset -- a human at a console, or the operator, IS the
    consent authority), or when the named card is not on this board. ACTIVE when the
    card resolves, and also when the board store cannot be read: "cannot tell who owns
    this write" is not "nobody does", and the fail-closed direction is the surface.
    """
    ctx = _ctx(ctx)
    if _disarmed():
        return False
    if not ctx.task_id:
        return False
    card, error = _read_owning_card(ctx)
    if error is not None:
        logger.debug("consent-surface guard: board store unreadable (%s); refusing "
                     "surface writes", error)
        return True
    return card is not None


def surface_decision(hit: Any, ctx: Any = None) -> tuple[str, str]:
    """``("allow", "")`` or ``("deny", message)`` for one surface write. Never raises."""
    ctx = _ctx(ctx)
    try:
        return _decide(hit, ctx)
    except Exception:  # a guard must never take a tool call down
        logger.exception("consent-surface guard: decision failed; refusing the write")
        surface, token, target = _hit(hit)
        return "deny", (
            f"BLOCKED (consent surface): the consent-surface guard could not decide the "
            f"write to {surface!r} ({target or token}) and fails closed. See the logs for "
            f"the cause; the write needs an approved scope covering {token!r}.")


def _decide(hit: Any, ctx: GuardContext) -> tuple[str, str]:
    if not consent_gate_active(ctx):
        return "allow", ""
    surface, token, _ = _hit(hit)
    if not surface:
        # Not a classified surface, so not our clause: ordinary work is never refused
        # here (D5). The doors only build a hit from the classifier, which always
        # names a surface.
        logger.debug("consent-surface guard: called without a surface; allowing")
        return "allow", ""
    latched = _LATCH.get((_session_key(ctx), surface))
    if latched is not None:
        # One refusal per surface per run: the worker cannot re-route the same write
        # through another door and get a fresh evaluation (D4c).
        return "deny", refusal_message(hit, SurfaceVerdict(cause="latched", detail=latched), ctx)

    card, error = _read_owning_card(ctx)
    if error is not None:
        verdict = _unreadable("board_store_unreadable", error)
    elif card is None:  # gate was active on an unreadable store; keep failing closed
        verdict = _unreadable("board_store_unreadable", str(_board_db_path(ctx)))
    else:
        verdict = _assess(card, ctx, token, surface)

    if verdict.licensed:
        return "allow", ""
    _latch(_session_key(ctx), surface, verdict.cause)
    return "deny", refusal_message(hit, verdict, ctx)


def _latch(session_key: str, surface: str, cause: str) -> None:
    if len(_LATCH) >= _LATCH_MAX:
        _LATCH.clear()
    if (session_key, surface) not in _LATCH:
        # Logged once per surface per run, never per call.
        logger.warning("consent-surface guard: refusing %r for the rest of this session "
                       "(%s)", surface, cause)
    _LATCH[(session_key, surface)] = cause


def refusal_message(hit: Any, verdict: SurfaceVerdict, ctx: Any = None) -> str:
    """The deny text (D4a): surface, target, card, refs, and the token to file under."""
    ctx = _ctx(ctx)
    surface, token, target = _hit(hit)
    lines = [
        f"BLOCKED (consent surface): {surface!r} is a consent-gated live surface and this "
        f"run has no approval that covers it.",
        f"  target: {target or '(unspecified)'}",
        f"  owning card: {ctx.task_id or '(none)'}",
        f"  required scope token: {token}",
    ]
    store_error = verdict.cause in ("approvals_store_unreadable", "board_store_unreadable")
    if verdict.refs:
        cited = "; ".join(
            f"{r.ref} -> {r.status or 'unresolved'}"
            + ("" if r.resolved else " (does not resolve)")
            for r in verdict.refs
        )
        lines.append(f"  references on the card: {cited}")
    elif store_error:
        lines.append("  references on the card: not read (store error)")
    else:
        lines.append("  references on the card: none")
    if verdict.cause == "latched":
        lines.insert(3, f"  refused earlier in this session for: {verdict.detail or 'unknown'} "
                        "(one refusal per surface per run)")
    if verdict.cause == "scope_not_covered" and verdict.approved_refs:
        lines.append(
            "  the approved reference(s) do not cover this scope: "
            + ", ".join(r.ref for r in verdict.approved_refs)
            + " declare a narrower or different scope than the required token.")
    if verdict.cause in ("approvals_store_unreadable", "board_store_unreadable"):
        lines.append(f"  store error ({verdict.cause}): {verdict.detail}")
    lines += [
        "TO PROCEED: file an approval whose scope token is "
        f"{token!r} (or a covering scope such as {token.split(':')[0]!r}), get it "
        "APPROVED, cite its APR-NNNN in this card, then retry. The card has been parked "
        "blocked/needs_input for a human to release.",
        "This refusal is a FLOOR: --yolo, approvals.mode=off, an allowlist, and "
        "unattended mode cannot bypass it.",
    ]
    return "\n".join(lines)


def refuse_surface_write(hit: Any, ctx: Any = None) -> dict:
    """The whole refusal for a door that must emit one: deny dict + receipt + park.

    Returns the terminal-door contract shape (``{"approved": False, "message": ...}``)
    so the same call serves the FILE clause (deny when ``approved`` is False) and the
    TERMINAL/EXEC floors (return the dict). An ALLOW returns ``{"approved": True,
    "message": None}`` and writes nothing.
    """
    ctx = _ctx(ctx)
    decision, message = surface_decision(hit, ctx)
    if decision != "deny":
        return {"approved": True, "message": None}
    surface, token, target = _hit(hit)
    receipt = emit_surface_refusal_receipt(hit, ctx)
    return {
        "approved": False,
        "consent_surface": True,
        "surface": surface,
        "scope_token": token,
        "target": target,
        "receipt": receipt,
        "message": message,
    }


def emit_surface_refusal_receipt(hit: Any, ctx: Any = None) -> bool:
    """Record ONE deduped receipt on the owning card and park it ``needs_input``.

    Mirrors ``kanban_db_dispatch._park_for_consent``: the event dedupe keys on the
    payload so a per-call guard cannot bury the board, and a failed write logs rather
    than raising -- the deny is what stops the write, the receipt is the record.
    """
    ctx = _ctx(ctx)
    if not ctx.task_id:
        return False
    surface, token, target = _hit(hit)
    payload = {"cause": CAUSE, "surface": surface, "scope_token": token, "target": target}
    body = (
        "CONSENT REFUSED at the write boundary (write not performed).\n\n"
        + refusal_message(hit, SurfaceVerdict(cause="receipt"), ctx)
    )
    try:
        from hermes_cli import kanban_db as kb
        from hermes_cli import kanban_db_connect as kbc

        with kbc.connect_closing(db_path=_board_db_path(ctx)) as conn:
            try:
                with kbc.write_txn(conn):
                    last = conn.execute(
                        "SELECT payload FROM task_events WHERE task_id = ? AND "
                        "kind = ? ORDER BY created_at DESC, id DESC LIMIT 1",
                        (ctx.task_id, RECEIPT_KIND),
                    ).fetchone()
                    if last is None or last["payload"] != kb._json_or_null(payload):
                        kb._append_event(conn, ctx.task_id, RECEIPT_KIND, payload)
                        kb.add_comment(conn, ctx.task_id, COMMENT_AUTHOR, body)
            except Exception:
                logger.debug("consent-surface guard: receipt could not be recorded for "
                             "%s", ctx.task_id, exc_info=True)
                return False
    except Exception:
        logger.debug("consent-surface guard: receipt could not be recorded for %s",
                     ctx.task_id, exc_info=True)
        return False
    try:
        with kbc.connect_closing(db_path=_board_db_path(ctx)) as conn:
            kb.block_task(conn, ctx.task_id, reason=f"consent: {CAUSE} ({surface})",
                          kind="needs_input")
    except Exception:
        # Already parked (or the CAS moved the card on): the receipt is the record.
        logger.debug("consent-surface guard: could not park %s", ctx.task_id, exc_info=True)
    return True
