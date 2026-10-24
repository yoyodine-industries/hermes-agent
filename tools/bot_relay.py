"""Bot Mode cross-connection relay — connections ARE the peer set.

Gateway-side half of the relay letting agents on ANY Desktop-connected gateway
message agents on ANY other. Plain file plumbing under ``<root>/bot_relay/`` —
no network; the Desktop owns every socket: ``roster.json`` (union roster of
agents on OTHER connections, pushed via ``bot_relay.roster.sync``), ``outbox/``
(envelopes queued by ``message_agent``, drained via ``bot_relay.outbox.drain``),
``replies/`` (one JSON per envelope via ``bot_relay.reply``; a waiter spawned at
send time watches it so the reply wakes the sender like a local DM).
Public helpers never raise, except ``enqueue_envelope`` → ``EnvelopeRefusedError``
when the target is definitively offline (fail fast instead of queueing a DM nobody will drain).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import re
import shlex
import shutil
import socket
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Iterator, Mapping, Optional

from tools.bot_delivery_queue import STATUS_DETAIL_TARGET_BUSY
from tools.bot_mode_probe import _default_home, _hermes_root, alias_forms
from utils import atomic_json_write

logger = logging.getLogger(__name__)

RELAY_DIR_NAME = "bot_relay"
ROSTER_FILE = "roster.json"
OUTBOX_DIR = "outbox"
CLAIMED_DIR = "claimed"
REPLIES_DIR = "replies"
LOCKS_DIR = "locks"

# Config fallbacks (real knobs: ``bot_mode.turn_wait_seconds`` / ``bot_mode.envelope_ttl_seconds``).
TURN_WAIT_SECONDS_FALLBACK = 120
DEFAULT_ENVELOPE_TTL_SECONDS = 900  # older envelopes are refused at drain with 'queued_expired'
# Per-attempt turn timeout and attempt ceiling for bot_relay.deliver (tui_gateway/methods_bot_relay.py).
TURN_ATTEMPT_TIMEOUT_SECONDS = 600
TURN_MAX_ATTEMPTS = 2  # first attempt + the policy-gated re-run
# Mirrors RELAY_DELIVER_TIMEOUT_MS in apps/desktop/src/plugins/hermes-bots/relay-budget.ts; both test suites pin it.
DESKTOP_DELIVER_SETTLEMENT_MARGIN_SECONDS = 180
DESKTOP_DELIVER_TIMEOUT_SECONDS = (
    TURN_WAIT_SECONDS_FALLBACK + TURN_ATTEMPT_TIMEOUT_SECONDS * TURN_MAX_ATTEMPTS + DESKTOP_DELIVER_SETTLEMENT_MARGIN_SECONDS
)
# A claimed envelope still unanswered this long after its claim was taken by a Desktop that died
# before ``bot_relay.deliver``; the next drain re-offers it, once. The longest a LIVE delivery can
# be in flight without a reply on disk is the Desktop's own deliver deadline (it posts a
# ``delivery_timeout`` reply when that passes, and the gateway-side hold — lock wait + the turn
# attempts — ends before it by construction), so past that point plus posting headroom the silence
# is provably the Desktop's death, not a slow turn. tests/tools/test_bot_relay.py pins the order.
REOFFER_AFTER_SECONDS = DESKTOP_DELIVER_TIMEOUT_SECONDS + 60
# The waiter must outlive the Desktop's timeout reply for the first delivery AND for the one
# re-offered delivery: re-offer window + a whole deliver budget + posting headroom.
REPLY_WAIT_SECONDS = REOFFER_AFTER_SECONDS + DESKTOP_DELIVER_TIMEOUT_SECONDS + 60
# Envelopes/replies older than this are stale artifacts (Desktop closed) and are swept.
STALE_AFTER_SECONDS = 6 * 3600
# Only a recent roster is authoritative for the fail-fast offline check: the
# Desktop re-pushes roster.sync on connection-state changes.
ROSTER_FRESH_SECONDS = 600


class EnvelopeRefusedError(RuntimeError):
    """``enqueue_envelope`` refused to queue (nothing written); ``reason`` is a stable machine code.

    ``reason`` is a stable machine code; ``str(exc)`` is the human text. 'runtime_offline' matches the
    #93091 item-1 failure-reason enum (plain literal here so the branches merge cleanly).
    """

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


# Profile names, handles and connection ids share one shape (also the local
# ``message_agent`` target grammar in ``tools/bot_mode_dm.py``).
_HANDLE_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")

# One turn in a profile's canonical Bot Chat: ``hermes -p <profile> *BOT_CHAT_TURN_ARGS``.
# ``-c "Bot Chat"`` must match ``bot_mode_probe.BOT_CHAT_TITLE``.
BOT_CHAT_TURN_ARGS = ("chat", "--in", "~", "-c", "Bot Chat", "--create-if-missing", "-Q")

# Set by a dispatcher on the ONE policy-gated re-run of a failed delivery turn (``tools.bot_mode_dm``,
# ``tui_gateway.methods_bot_relay``). The failed attempt's turn-start persist already left the DM as the
# Bot Chat's unanswered tail row, and a fresh process cannot tell that from a new message on its own — so
# the re-run is told to adopt that row instead of appending a second copy
# (``hermes_cli.quiet_single_query.adopt_unanswered_turn``, which consumes the variable before the turn).
RESUME_UNANSWERED_TURN_ENV = "HERMES_RESUME_UNANSWERED_TURN"


def retry_turn_env(env: Optional[Mapping[str, str]]) -> dict[str, str]:
    """The re-run's child env: the first attempt's env plus the resume marker."""
    return {**(os.environ if env is None else env), RESUME_UNANSWERED_TURN_ENV: "1"}


def relay_root(root: Path | str) -> Path:
    return Path(root) / RELAY_DIR_NAME


def _ensure_dirs(root: Path | str) -> Path:
    base = relay_root(root)
    for sub in (OUTBOX_DIR, CLAIMED_DIR, REPLIES_DIR):
        from hermes_constants import mkdir_under_hermes_home
        mkdir_under_hermes_home(base / sub)
    return base


def _atomic_write_json(target: Path, payload: Any, *, sort_keys: bool = False) -> None:
    atomic_json_write(target, payload, indent=None, sort_keys=sort_keys, mode=0o600)


def _bot_mode_cfg(key: str, *, loader: str) -> Any:
    """``bot_mode.<key>`` from config, read lazily (tools/ must not import CLI
    config at import time); None when absent or the config is unreadable."""
    try:
        import hermes_cli.config as cfgmod

        cfg = getattr(cfgmod, loader)() or {}
        return (cfg.get("bot_mode") or {}).get(key)
    except Exception:
        logger.debug("bot_mode.%s config read failed", key, exc_info=True)
        return None


def _normalize_roster_row(row: Any) -> Optional[dict]:
    """Validated, minimal roster row or None. Rows come from the Desktop over
    RPC — treat as untrusted input."""
    if not isinstance(row, dict):
        return None
    profile = str(row.get("profile") or "").strip()
    handle = str(row.get("handle") or "").strip().lstrip("@") or ("hermes" if profile == "default" else profile)
    connection_id = str(row.get("connection_id") or "").strip()
    if not profile or not connection_id or not all(_HANDLE_RE.match(v) for v in (handle, profile, connection_id)):
        return None
    out = {
        "profile": profile, "handle": handle, "connection_id": connection_id,
        "connection_label": str(row.get("connection_label") or "").strip()[:80],
        "title": str(row.get("title") or "").strip()[:120],
        "description": " ".join(str(row.get("description") or "").split())[:160],
    }
    # Liveness kept only when a real bool: absent == unknown == fail-open on enqueue.
    if isinstance(row.get("online"), bool):
        out["online"] = row["online"]
    return out


def write_remote_roster(root: Path | str, rows: Any) -> int:
    """Atomically persist the Desktop-pushed remote roster. Returns count."""
    base = _ensure_dirs(root)
    by_key: dict[tuple[str, str], dict] = {}
    for norm in filter(None, map(_normalize_roster_row, rows if isinstance(rows, list) else [])):
        by_key.setdefault((norm["connection_id"], norm["profile"]), norm)
    cleaned = [by_key[k] for k in sorted(by_key)]
    _atomic_write_json(base / ROSTER_FILE, {"updated_at": int(time.time()), "agents": cleaned}, sort_keys=True)
    return len(cleaned)


def read_remote_roster(root: Path | str) -> list[dict]:
    """The current remote roster (possibly empty). Never raises."""
    try:
        raw = (relay_root(root) / ROSTER_FILE).read_text(encoding="utf-8-sig")
        data = json.loads(raw)
        agents = data.get("agents") if isinstance(data, dict) else None
        return [r for r in map(_normalize_roster_row, agents) if r] if isinstance(agents, list) else []
    except FileNotFoundError:
        return []
    except Exception:
        logger.debug("bot_relay roster read failed", exc_info=True)
        return []


def _target_ids(row: dict) -> set[str]:
    """Lower-cased routing ids of a roster row: its @handle and profile folder id."""
    return {row["handle"].lower(), row["profile"].lower()}


def _target_aliases(row: dict) -> set[str]:
    """Every lower-cased bare form that addresses ``row``: routing ids plus the Bot Mode title's
    mention slugs (``"CoS Bot"`` → ``cos-bot``/``cosbot``, what the Desktop picker inserts). A remote
    ``default`` is ``@hermes`` on every gateway, so its title is the only bare form that can single it out."""
    return _target_ids(row) | alias_forms(row.get("title") or "")


def resolve_remote_target(raw_target: str, roster: list[dict]) -> Any:
    """Matched row for a bare handle/profile/title slug (unique across connections) or
    ``<handle|profile|title-slug>@<connection-id>``; ``"ambiguous"`` for a bare form on several
    connections; None otherwise. An exact handle/profile match beats a title slug, so a title
    colliding with another row's handle never steals it."""
    want, at, conn = (p.strip() for p in str(raw_target or "").strip().lstrip("@").partition("@"))
    if not want or (at and not conn):
        return None
    want = want.lower()
    rows = [row for row in roster if not conn or row["connection_id"].lower() == conn.lower()]
    matches = [row for row in rows if want in _target_ids(row)] or [row for row in rows if want in _target_aliases(row)]
    if not matches:
        return None
    return matches[0] if len(matches) == 1 else "ambiguous"


def _title_slug(row: dict) -> str:
    """The Bot Mode title's slug form (``"CoS Bot"`` → ``cos-bot``, what the picker inserts); "" when the
    title is empty, reserved (a bot titled "Hermes") or not a valid handle."""
    title = str(row.get("title") or "")
    slug = re.sub(r"[^a-z0-9_-]+", "-", title.strip().lower()).strip("-")
    return slug if slug in alias_forms(title) else ""


def remote_target_forms(roster: list[dict], local_taken: set[str] | frozenset[str] = frozenset()) -> list[str]:
    """One unambiguous target string per row, shortest first: the bare handle when no other remote
    row and no LOCAL profile (``local_taken``: this gateway's handles and friendly-name slugs) answers
    to it; else the title slug under the same test (a remote ``default`` titled "CoS Bot" is
    ``@cos-bot``, since bare ``@hermes`` is always this gateway's own default); else
    ``handle@connection``. Mirrors ``resolve_remote_target``."""
    taken = {form.lower() for form in local_taken}
    id_claims: dict[str, int] = {}
    alias_claims: dict[str, int] = {}
    for row in roster:
        for form in _target_ids(row):
            id_claims[form] = id_claims.get(form, 0) + 1
        for form in _target_aliases(row):
            alias_claims[form] = alias_claims.get(form, 0) + 1

    def _form(row: dict) -> str:
        # The handle needs only be unique among routing ids (resolution gives it precedence over a
        # colliding title); a title slug must be unique among every alias.
        for candidate, claims in ((row["handle"], id_claims), (_title_slug(row), alias_claims)):
            if candidate and candidate.lower() not in taken and claims.get(candidate.lower(), 0) == 1:
                return candidate
        return f"{row['handle']}@{row['connection_id']}"

    return [_form(row) for row in roster]


_SENDER_STAMP_RE = re.compile(r"^(Message from 🤖 .+? \(@)([A-Za-z0-9_-]+)(\): )", re.DOTALL)


def qualify_sender_stamp(message: str, from_handle: Any, from_connection: Any, roster: list[dict],
                         local_taken: set[str] | frozenset[str] = frozenset()) -> str:
    """Rewrite a relayed DM's ``Message from 🤖 <name> (@<handle>):`` stamp so the handle is the
    form THIS gateway can reply to: the sender's row in the local relay roster as
    ``remote_target_forms`` renders it, else ``handle@connection``. A relayed ``@hermes`` is another
    machine's default — left bare, a reply lands on the recipient's own default (#103731)."""
    handle, conn = str(from_handle or "").strip().lstrip("@"), str(from_connection or "").strip()
    match = _SENDER_STAMP_RE.match(str(message or ""))
    if not match or not conn or not _HANDLE_RE.match(handle) or not _HANDLE_RE.match(conn):
        return message
    forms = dict(zip(((r["connection_id"].lower(), r["handle"].lower()) for r in roster), remote_target_forms(roster, local_taken)))
    form = forms.get((conn.lower(), handle.lower())) or f"{handle}@{conn}"
    return f"{match.group(1)}{form}{match.group(3)}{message[match.end():]}"


def _envelope_ttl_seconds() -> int:
    """Configured drain TTL (``bot_mode.envelope_ttl_seconds``), read per-drain.
    ``0`` (or negative) disables expiry."""
    val = _bot_mode_cfg("envelope_ttl_seconds", loader="load_config_readonly")
    if val is None:
        return DEFAULT_ENVELOPE_TTL_SECONDS
    try:
        return int(val)
    except (TypeError, ValueError, OverflowError):
        logger.debug("Invalid bot_mode.envelope_ttl_seconds %r; using fallback", val)
        return DEFAULT_ENVELOPE_TTL_SECONDS


def _target_liveness(root: Path | str, target: dict) -> Optional[bool]:
    """Tri-state liveness: True / False / None (unknown → callers fail open). Offline =
    explicit ``online: false`` or ABSENT from a *fresh* roster; a missing, unreadable,
    empty or stale roster proves nothing → None. Never raises."""
    try:
        try:
            age = time.time() - (relay_root(root) / ROSTER_FILE).stat().st_mtime
        except OSError:
            return None
        roster = read_remote_roster(root) if age <= ROSTER_FRESH_SECONDS else []
        if not roster:
            return None
        key = (str(target.get("connection_id") or ""), str(target.get("profile") or ""))
        row = next((r for r in roster if (r["connection_id"], r["profile"]) == key), None)
        if row is None:
            return False  # fresh roster no longer lists the target — offline
        return row["online"] if isinstance(row.get("online"), bool) else None
    except Exception:
        logger.debug("bot_relay liveness check failed", exc_info=True)
        return None


def enqueue_envelope(root: Path | str, *, target: dict, message: str, sender_profile: str, sender_handle: str) -> dict:
    """Queue a cross-connection DM for the Desktop relay; returns the envelope. Raises
    ``EnvelopeRefusedError`` ('runtime_offline') without writing when the target is
    definitively offline; unknown liveness enqueues (fail-open)."""
    if _target_liveness(root, target) is False:
        label = (f"@{target.get('handle') or target.get('profile') or '?'} on "
                 f"{target.get('connection_label') or target.get('connection_id') or '?'}")
        raise EnvelopeRefusedError("runtime_offline", f"{label} is offline right now — the message was NOT queued. "
                                   "Try again once that machine reconnects to the Desktop.")
    base = _ensure_dirs(root)
    envelope = {
        "id": uuid.uuid4().hex, "created_at": int(time.time()),
        "from_profile": sender_profile, "from_handle": sender_handle,
        "target_connection": target["connection_id"], "target_profile": target["profile"],
        "target_handle": target["handle"], "message": message,
    }
    _atomic_write_json(base / OUTBOX_DIR / f"{envelope['id']}.json", envelope)
    return envelope


def _expire_if_stale(root: Path | str, path: Path, ttl: float, now: float) -> bool:
    """True when the outbox envelope is older than ``ttl``; writes the 'queued_expired'
    reply so the sender's waiter resolves (best effort). Unreadable envelopes are left for the claim."""
    try:
        env = json.loads(path.read_text(encoding="utf-8-sig"))  # BOM-tolerant (pm-era read fix)
        if not isinstance(env, dict):
            raise ValueError(f"expected a JSON object, got {type(env).__name__}")
        created = float(env.get("created_at") or path.stat().st_mtime)
    except (OSError, ValueError):
        return False
    if now - created <= ttl:
        return False
    with contextlib.suppress(OSError, ValueError):
        write_reply(root, str(env.get("id") or ""), reason="queued_expired", error=(
            f"queued message to @{env.get('target_handle') or '?'} on {env.get('target_connection') or '?'} "
            f"expired after {ttl}s waiting for the Desktop to drain it — it was NOT delivered. "
            "Resend once the Desktop reconnects."))
    return True


def _queued_at(path: Path) -> tuple[float, str]:
    """Claim order for one outbox entry: oldest first. ``mtime`` is what ``_sweep_stale`` already
    treats as an envelope's age, and unlike the whole-second ``created_at`` field it separates two
    DMs sent in the same second. The name only breaks ties."""
    with contextlib.suppress(OSError):
        return (path.stat().st_mtime, path.name)
    return (0.0, path.name)


def claim_pending_envelopes(root: Path | str) -> list[dict]:
    """Drain the outbox (rename → claimed/ so a second drain can't double-deliver).
    TTL-expired envelopes get a 'queued_expired' reply and are removed instead.

    Envelopes older than ``bot_mode.envelope_ttl_seconds`` are NOT delivered: each gets an error reply
    (reason ``'queued_expired'``) so the sender's waiter resolves, and its outbox file is removed (#93091
    item 2).
    """
    base = _ensure_dirs(root)
    _sweep_stale(base)
    ttl = _envelope_ttl_seconds()
    now = time.time()
    # Re-offers first: they are the oldest mail this drain hands out.
    out: list[dict] = _reoffer_unanswered(root, base, ttl, now)
    # Oldest first: the Desktop delivers each target's claimed envelopes in the order this list
    # gives them, so a sender's two DMs to one agent arrive in the order they were sent. Sorting
    # by filename ordered them by ``uuid4().hex`` — at random.
    for path in sorted((base / OUTBOX_DIR).glob("*.json"), key=_queued_at):
        if ttl > 0 and _expire_if_stale(root, path, ttl, now):
            with contextlib.suppress(OSError):
                path.unlink()
            continue
        claimed = base / CLAIMED_DIR / path.name
        with contextlib.suppress(OSError, ValueError):
            os.replace(path, claimed)  # atomic claim
            os.utime(claimed, (now, now))  # the re-offer window counts from the claim, not the enqueue
            envelope = json.loads(claimed.read_text(encoding="utf-8-sig"))
            if not isinstance(envelope, dict):
                raise ValueError(f"expected a JSON object, got {type(envelope).__name__}")
            out.append(envelope)
    return out


def _reoffer_unanswered(root: Path | str, base: Path, ttl: float, now: float) -> list[dict]:
    """``claimed/`` envelopes unanswered ``REOFFER_AFTER_SECONDS`` after their claim, at most once each.

    The claim is the Desktop's: one that disconnects between ``outbox.drain`` and ``bot_relay.deliver``
    leaves the envelope here with no reply, silent until the waiter's deadline and then swept, while
    the reconnected Desktop's drains see an empty outbox (#111021, #111207). Bounds, in check order:

    * ``created_at + REPLY_WAIT_SECONDS`` passed with no reply — the waiter is gone (or about to be);
      a ``delivery_timeout`` reply is written so it learns, and the envelope is never handed out again.
    * already re-offered (``reoffered_at`` stamped on the envelope) — one extra delivery per message,
      never a turn loop against a target nobody is listening for.
    * ``bot_mode.envelope_ttl_seconds`` applies to the re-offer leg exactly as to the outbox: the
      message is back in the queue from ``claim + REOFFER_AFTER_SECONDS``; a drain that comes ``ttl``
      later than that refuses it with ``queued_expired``.
    """
    out: list[dict] = []
    for path in sorted((base / CLAIMED_DIR).glob("*.json"), key=_queued_at):
        if (base / REPLIES_DIR / path.name).exists():
            continue
        with contextlib.suppress(OSError, ValueError):
            claimed_at = path.stat().st_mtime
            envelope = json.loads(path.read_text(encoding="utf-8-sig"))
            if not isinstance(envelope, dict):
                raise ValueError(f"expected a JSON object, got {type(envelope).__name__}")
            env_id = str(envelope.get("id") or "")
            label = f"@{envelope.get('target_handle') or '?'} on {envelope.get('target_connection') or '?'}"
            created = float(envelope.get("created_at") or claimed_at)
            if now - created > REPLY_WAIT_SECONDS:
                write_reply(root, env_id, reason="delivery_timeout", error=(
                    f"no reply from {label} within {REPLY_WAIT_SECONDS}s of sending — the Desktop picked "
                    "the message up but never reported a delivery. It will not be retried; resend if it matters."))
                continue
            if envelope.get("reoffered_at"):
                continue
            queued_for = now - claimed_at - REOFFER_AFTER_SECONDS
            if queued_for < 0:
                continue
            if ttl > 0 and queued_for > ttl:
                write_reply(root, env_id, reason="queued_expired", error=(
                    f"re-queued message to {label} expired after {ttl}s waiting for the Desktop to drain it "
                    "again — it was NOT delivered. Resend once the Desktop reconnects."))
                continue
            envelope["reoffered_at"] = int(now)
            _atomic_write_json(path, envelope)
            out.append(envelope)
    return out


def write_reply(root: Path | str, envelope_id: str, *, reply: str = "", error: str = "", reason: str = "") -> Path:
    """Persist the relayed reply (or delivery error) for the waiter. ``reason`` (typed
    code, ``tools.bot_failure_reasons``) is classified from ``error`` when omitted.

    Idempotent by envelope id — the first settled reply stands, error or not. That is safe because
    two deliveries of one envelope never overlap: ``_reoffer_unanswered`` waits past the Desktop's own
    deliver deadline (``REOFFER_AFTER_SECONDS``), so by the time a second delivery can start, the first
    has either replied (and the waiter may already have read it) or provably died without one. A
    later write is therefore a duplicate or a bookkeeping timeout, never a truer answer."""
    base = _ensure_dirs(root)
    safe = str(envelope_id or "").strip()
    if not re.match(r"^[0-9a-f]{32}$", safe):
        raise ValueError(f"invalid envelope id: {envelope_id!r}")
    path = base / REPLIES_DIR / f"{safe}.json"
    if path.exists():
        # Idempotent by envelope id: the first settled reply is the one the waiter already read (or
        # will). A re-offered delivery's second outcome — or a late duplicate — never displaces it.
        return path
    err, code = str(error or ""), str(reason or "")
    if not code and err:
        from tools.bot_failure_reasons import classify_agent_error

        code = classify_agent_error(err)
    _atomic_write_json(path, {"id": safe, "at": int(time.time()), "reply": str(reply or ""), "error": err, "reason": code})
    return path


def unlink_files_older_than(directory: Path, pattern: str, cutoff: float) -> int:
    """Unlink regular files matching ``pattern`` with mtime before ``cutoff``; returns count. Never raises."""
    removed = 0
    with contextlib.suppress(OSError):
        for path in directory.glob(pattern):
            with contextlib.suppress(OSError):
                if path.is_file() and path.stat().st_mtime < cutoff:
                    path.unlink()
                    removed += 1
    return removed


def _sweep_stale(base: Path, *, now: float | None = None) -> int:
    cutoff = (time.time() if now is None else now) - STALE_AFTER_SECONDS
    return sum(unlink_files_older_than(base / sub, "*.json", cutoff) for sub in (CLAIMED_DIR, REPLIES_DIR, OUTBOX_DIR))


def cleanup_bot_relay_artifacts(max_age_hours: float | None = None) -> int:
    """Hourly sweep of stale relay artifacts (DM plaintext; ``_sweep_stale`` otherwise runs
    only on Desktop drains). ``max_age_hours`` is for ``cleanup_*_cache`` signature parity only."""
    del max_age_hours
    try:
        base = relay_root(_hermes_root(Path(_default_home())))
        return _sweep_stale(base) if base.is_dir() else 0
    except Exception:
        logger.debug("bot_relay artifact sweep failed", exc_info=True)
        return 0


def waiter_command(root: Path | str, envelope: dict) -> str:
    """Shell command that blocks until the reply file appears, then prints it; spawned
    via ``terminal_tool(background=True, notify_on_complete=True)`` so its stdout arrives
    as the same completion notification local DMs use.

    A ``tools/bot_mode_dm.py --wait-reply`` entrypoint, like the local delivery runner — not
    ``python -c``. The approval gate flags inline interpreter code ("script execution via -e/-c
    flag"), and ``approvals.single_query_mode`` defaults to ``deny`` for the one-shot ``-Q`` turn a
    bot replies from, so the reply waiter was refused exactly when a bot answered a teammate: the
    message was delivered, the reply never woke the sender. Roster fields ride as argv (``shlex``
    quoted), never as source text, so a hostile handle or connection id stays data.
    """
    reply_path = str(relay_root(root) / REPLIES_DIR / f"{envelope['id']}.json")
    label = f"@{envelope.get('target_handle', '')} on {envelope.get('target_connection', '')}"
    runner = str(Path(__file__).resolve().with_name("bot_mode_dm.py"))
    argv = [sys.executable or "python3", runner, "--wait-reply", reply_path, label, str(REPLY_WAIT_SECONDS)]
    if sys.platform == "win32":
        # Same rewrite as the delivery runner: the tracked local backend uses Git Bash on native
        # Windows, where forward-slash drive paths run and backslash paths parse as command names.
        argv = [part.replace("\\", "/") for part in argv]
    return shlex.join(argv)


def _hermes_cli() -> str:
    """Prefer this install's published launcher, then interpreter/PATH fallbacks.

    A long-lived caller can still run in an older dependency generation. Its
    sibling console script pins that generation, whereas the published launcher
    selects current dependencies at child start. Keep the historical fallbacks
    for external/developer installs that have no published launcher (#93590).
    """
    # Do not select batch shims: cmd.exe reinterprets otherwise literal argv
    # (for example an ampersand in a query-file path), even with shell=False.
    name = "hermes.exe" if sys.platform == "win32" else "hermes"
    published = Path(__file__).resolve().parents[1] / ".hermes" / "bin" / name
    if published.is_file():
        return str(published)
    sibling = Path(sys.executable or "").parent / name
    return str(sibling) if sibling.is_file() else shutil.which("hermes") or "hermes"


def local_delivery_command(profile: str, query_file: str) -> list[str]:
    """argv that delivers a DM into ``profile``'s Bot Chat on THIS gateway."""
    return [_hermes_cli(), "-p", profile, *BOT_CHAT_TURN_ARGS, "--query-file", query_file]


class DeliveryAuthor:
    """A relayed turn's author as an in-process object. ``bot_relay.deliver`` builds it from the sender fields
    an admitted gateway client relays for another connection; nothing verifies the sender itself. A JSON
    client cannot build one, so ``prompt.submit`` accepts the object and refuses a dict."""

    __slots__ = ("author",)

    def __init__(self, author: dict) -> None:
        self.author = dict(author)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, DeliveryAuthor) and other.author == self.author

    def __repr__(self) -> str:
        return f"DeliveryAuthor({self.author!r})"


def delivery_turn_author(from_profile: Any, from_handle: Any, from_connection: Any = None) -> Optional[dict]:
    """The author of a relayed DM's recipient turn, built from the sender fields as the relaying client reports
    them. A relayed DM always comes from another gateway, so the id carries the Desktop's id for the sender's
    connection (``local`` included) and only the recipient's own profiles are bare ``bot:<profile>``. None when
    the envelope names no sender."""
    from agent.turn_author import bot_author_id

    profile = str(from_profile or "").strip()
    if not profile:
        return None
    return {"id": bot_author_id(profile, str(from_connection or "")), "name": str(from_handle or "").strip() or profile,
            "is_bot": True}


def _delivery_child_session_env_names() -> tuple[str, ...]:
    """Session-bound env names to strip from a delivery child, from ``gateway.session_context``.

    Synced with the session binding surface as vars are added; deliberately NOT a
    ``HERMES_SESSION_*`` prefix match, which would also strip non-identity knobs
    (e.g. ``HERMES_SESSION_STALL_TIMEOUT``)."""
    from gateway.session_context import _VAR_MAP

    return tuple(_VAR_MAP)


def relaying_principal_author(principal: str) -> dict:
    """The author of a relayed DM whose sender fields cannot be trusted: a logged-in client named them.

    Server-derived and unspoofable — the id is built from the caller's minted identity digest, never from
    anything the client sent — and still a BOT author, because the recipient's memory routes on that:
    Honcho writes a bot-authored turn into the bot's own a2a session and refuses conclusion / profile /
    mirror writes for it, while an unattributed turn is treated as the human's (#107598 review). The
    human-facing signature stays in the message text the sender composed."""
    from agent.turn_author import bot_author_id

    return {"id": bot_author_id("relay", str(principal or "").strip()), "name": "relayed teammate", "is_bot": True}


def delivery_env(author: Optional[dict], profile_home: str | Path | None = None) -> dict[str, str]:
    """Environment for one delivery turn's ``hermes -p <profile>`` child. The dispatcher's own
    HERMES_TURN_AUTHOR is dropped first so a delivery without an author never inherits the author of the turn
    that sent it. Dispatcher session identity (the canonical ``gateway.session_context`` session env names) is
    dropped too: a nested recipient that ``message_agent``s onward must not stamp that grandchild
    notify with the grandparent's key, or the live recipient never resumes. The child runs the target
    profile's Bot Chat turn, so it starts from THAT profile's env (``served_profile_child_env``: launch
    profile ``.env`` / TERMINAL_* residue dropped, target secrets overlaid), never the multiplexer's raw
    ``os.environ``; ``-p`` alone only pinned HERMES_HOME. ``profile_home`` is the target's home when the
    caller knows it (relay RPC, roster); otherwise the active override, and under multiplex the launch
    home."""
    from agent.secret_scope import current_secret_scope, is_multiplex_active
    from agent.turn_author import TURN_AUTHOR_ENV, turn_author_env
    from hermes_constants import get_hermes_home_override, get_routing_process_hermes_home
    from tools.environments.local import served_profile_child_env

    # ``_profile_home`` answers None for the launch profile by design and a relay RPC binds no scope,
    # so under multiplex an empty target means the launch profile, not "unknown" (the fail-closed
    # raise; cf. the slash-worker spawn, #115427). An explicit target beats an override or bound scope
    # inside ``served_profile_child_env``, so fill it only when both are absent; a single-profile host
    # keeps its pass-through env.
    target_home = profile_home
    if (not target_home and is_multiplex_active() and not get_hermes_home_override()
            and current_secret_scope() is None):
        target_home = get_routing_process_hermes_home()
    env = served_profile_child_env(base=os.environ, target_home=target_home, inherit_credentials=True)
    env.pop(TURN_AUTHOR_ENV, None)
    for name in _delivery_child_session_env_names():
        env.pop(name, None)
    if author:
        env.update(turn_author_env(author))
    return env


# Two deliveries into the SAME profile must never run Bot Chat turns concurrently.
# Deliveries are separate ``hermes`` subprocesses, so the lock is a per-profile
# lockfile under ``<root>/bot_relay/locks/`` held with ``fcntl.flock`` for exactly
# the turn window; the kernel releases it on fd close (incl. process death), so a
# crashed turn can never wedge the profile.


# ── per-profile turn lock (#93091) ─────────────────────────────────────────── Two deliveries into the SAME
# target profile must never run their Bot Chat turns concurrently: deliveries spawn separate ``hermes``
# subprocesses, so an in-memory mutex is useless — the lock is a per-profile lockfile under
# ``<root>/bot_relay/locks/`` held with ``fcntl.flock`` for exactly the turn execution window. flock is
# released by the kernel when the holder's fd closes (including process death), so a crashed turn can never
# wedge the profile. On the DELIVERY path the lock is a mutual-exclusion primitive, never a waiting room
# (§2.5 step 1): a held lock raises at once and the delivery is handed to the receiver queue, which re-offers
# it when the turn slot frees. ``bot_mode.turn_wait_seconds`` stays the budget for callers that take the
# lock without their own timeout.
# The holder publishes WHO it is into its own lockfile while it holds the turn (t_4a32d803 SEV1 residual,
# t_f93b2489). flock alone leaves the file empty, so a wedged profile could not be attributed to a process:
# every waiter got a bare "another delivery turn is already running", the operator had no pid to reclaim,
# and the routable seat could stay dead for hours with nothing naming the culprit. The record is advisory —
# a waiter that cannot read it (empty, torn, written by an older build) reports the holder as UNKNOWN, never
# as an error, and the flock stays the single authority for who holds the turn.
TURN_LOCK_HOLDER_MAX_BYTES = 4096


def _int_or_none(value: Any) -> Optional[int]:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _float_or_none(value: Any) -> Optional[float]:
    try:
        parsed = float(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed == parsed and abs(parsed) != float("inf") else None


def _format_holder_age(seconds: Any) -> str:
    """Compact age for a refusal line: ``42s`` / ``4m 12s`` / ``2h 07m``."""
    try:
        total = max(0, int(seconds))
    except (TypeError, ValueError):
        return "?"
    if total < 60:
        return f"{total}s"
    if total < 3600:
        return f"{total // 60}m {total % 60:02d}s"
    return f"{total // 3600}h {(total % 3600) // 60:02d}m"


def _pid_alive(pid: Any) -> Optional[bool]:
    """Liveness of ``pid``: True/False, or None when the OS refuses the probe (unknown ≠ dead).

    ``OverflowError`` matters as much as an OSError here: a torn lockfile can carry a pid outside
    the host's ``pid_t`` range, and the advisory probe must report that as *unknown* rather than
    letting it escape a read that is contracted never to raise.
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    if pid <= 0:
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # exists, owned by someone else
        return True
    except (OSError, OverflowError):
        return None
    return True


def publish_turn_lock_holder(fd: int, profile: str, session: str | None = None) -> None:
    """Publish this process as ``profile``'s turn holder for ``session``, in place on the flocked fd.

    Written through the SAME fd that holds the lock — a rename would swap the inode out from under the
    flock and let the next waiter acquire a second, unrelated lock. Best-effort: an unwritable lockfile
    costs the refusal its pid, never the turn its lock. ``session`` is the granularity the lock was
    taken at (``t_47082c1d``): a waiter names WHICH transcript is wedged, not just the seat.
    """
    record = {
        "pid": os.getpid(), "ppid": os.getppid(), "profile": str(profile or ""),
        "session": str(session or ""),
        "started_at": time.time(), "host": socket.gethostname(),
    }
    payload = (json.dumps(record, separators=(",", ":")) + "\n").encode("utf-8")[:TURN_LOCK_HOLDER_MAX_BYTES]
    try:
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, payload)
        os.fsync(fd)
    except OSError:  # pragma: no cover - a vanished/full lockfile
        logger.debug("turn lock: could not publish the holder record", exc_info=True)


def clear_turn_lock_holder(fd: int) -> None:
    """Drop the holder record at the end of the turn window; the flock is what actually gates."""
    with contextlib.suppress(OSError):
        os.ftruncate(fd, 0)
        os.fsync(fd)


def read_turn_lock_holder(root: Path | str, profile: str, session: str | None = None) -> Optional[dict]:
    """The holder a live turn published for ``profile``/``session``, or None when absent/unreadable.

    Never raises and never trusts the record: it is a claim written by another process, so a waiter uses
    it to NAME the holder, never to acquire the turn. ``alive`` is probed here rather than trusted from it.
    An absent, empty, torn or pid-less record is UNKNOWN (None) — never an error — and the projection keeps
    only the documented keys, non-empty, so a stray field in a stale lockfile cannot reach a wire payload.
    """
    try:
        raw = turn_lock_path(root, profile, session).read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    if not raw:
        return None
    try:
        record = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(record, dict):
        return None
    pid = _int_or_none(record.get("pid"))
    if pid is None or pid <= 0:
        return None
    started = _float_or_none(record.get("started_at"))
    fields = (
        ("kind", "turn_lock"),
        ("pid", pid),
        ("ppid", _int_or_none(record.get("ppid"))),
        ("profile", str(record.get("profile") or "").strip() or None),
        ("session", str(record.get("session") or "").strip() or None),
        ("host", str(record.get("host") or "").strip() or None),
        ("started_at", started),
        ("age_seconds", (round(max(0.0, time.time() - started), 3) if started is not None else None)),
        ("alive", _pid_alive(pid)),
    )
    return {key: value for key, value in fields if value is not None}


def holder_from_live_owner(entry: Mapping[str, Any], *, now: Optional[float] = None) -> Optional[dict]:
    """The ``live_owner`` holder a live-owner registry entry describes; None when it names nobody.

    The fields are read straight off the entry the caller already holds (``surface``, ``session_id``,
    ``started_at``, ``pid``) — the same entry ``hermes_cli.active_sessions`` prints in its owner
    refusals, so this wording and that one cannot drift. No new plumbing into that registry.
    """
    if not entry:
        return None
    surface = str(entry.get("surface") or "").strip()
    session_id = str(entry.get("session_id") or "").strip()
    if not surface and not session_id:
        return None
    started = _float_or_none(entry.get("started_at"))
    moments = time.time() if now is None else float(now)
    fields = (
        ("kind", "live_owner"),
        ("surface", surface or None),
        ("session_id", session_id or None),
        ("pid", _int_or_none(entry.get("pid"))),
        ("started_at", started),
        ("age_seconds", (round(max(0.0, moments - started), 3) if started is not None else None)),
        ("alive", True),
    )
    return {key: value for key, value in fields if value is not None}


def holder_fragment(holder: Optional[Mapping[str, Any]]) -> str:
    """The human fragment naming a slot's holder; ``""`` when the holder is unknown (§2).

    ONE wording source for every surface that names a holder: the delivery receipt's ``detail``, the
    :class:`TurnBusyError` message and the gateway refusal all append this. An unknown holder (None, an
    empty dict, a record with no pid) yields ``""`` — the caller then appends nothing, so an unreadable
    lockfile can never invent a holder.
    """
    if not holder:
        return ""
    if str(holder.get("kind") or "turn_lock") == "live_owner":
        surface = str(holder.get("surface") or "").strip() or "another surface"
        session_id = str(holder.get("session_id") or "").strip()
        who = f"seat held live by {surface}" + (f" (session {session_id})" if session_id else "")
        age = holder.get("age_seconds")
        return who if age is None else f"{who} for {_format_holder_age(age)}"
    pid = holder.get("pid")
    if not pid:
        return ""
    if holder.get("alive") is False:
        return f"held by pid {pid}, no longer running"
    age = holder.get("age_seconds")
    return f"held by pid {pid}" if age is None else f"held by pid {pid} for {_format_holder_age(age)}"


class TurnBusyError(RuntimeError):
    """A delivery turn is already running for the target profile (``waited_seconds`` ≈ time queued).

    ``reason`` is 'target_busy' — extends the #93091 item-1 structured refusal enum. ``waited_seconds`` is
    roughly how long the caller queued behind the current turn before the delivery was handed to the
    receiver queue. This is no longer a failure: the delivery is durably queued and the drainer re-offers
    it, so the sender gets a receipt (``attempts`` 0, ``busy`` true) and must NOT resend (§3.1).
    ``holder`` — when the lockfile carries one — names the process that did not finish (pid, start, age,
    liveness), so the sender (and an operator) can route around it without touching the seat. Unknown
    holder ⇒ the message is byte-identical to the pre-holder refusal.
    """

    reason = "target_busy"
    #: The receipt's ``status_detail`` (§3.1) — the same string ``tools.bot_delivery_queue``
    #: stamps, so a sender that greps for ``target_busy`` still finds it there, never as a reason.
    status_detail = STATUS_DETAIL_TARGET_BUSY

    def __init__(self, profile: str, waited_seconds: float, holder: Optional[dict] = None,
                 *, session: str | None = None):
        self.profile, self.waited_seconds = profile, waited_seconds
        self.holder = holder or None
        #: The transcript whose turn we were waiting on (``t_47082c1d``). ``None`` is the pre-granularity
        #: profile-wide key. Attribution only — it never widens or narrows the lock at refusal time.
        self.session = session
        fragment = holder_fragment(self.holder)
        super().__init__(f"target_busy: another delivery turn is already running for profile '{profile}' — "
                         f"queued behind it for ~{int(round(waited_seconds))}s. The delivery is durably "
                         "queued; the receiver re-offers it when the turn slot frees "
                         f"(status_detail: '{STATUS_DETAIL_TARGET_BUSY}'). Do not resend."
                         + (f" — {fragment}" if fragment else ""))

    def holder_payload(self) -> dict:
        """The holder as machine-readable data for a receipt/refusal; ``{}`` when the holder is unknown.

        Non-empty keys only, and a record that names no pid (a torn or foreign lockfile) is unknown too:
        it must reach the sender as ``holder: null``, never as an empty object. Never raises — an
        unreadable lockfile is *unknown*, not an error.
        """
        if not self.holder:
            return {}
        if str(self.holder.get("kind") or "turn_lock") == "turn_lock" and not self.holder.get("pid"):
            return {}
        return {key: value for key, value in self.holder.items() if value is not None}



def acquire_delivery_turn_lock(root: Path | str, profile: str, session: str | None = None) -> Iterator[Path]:
    """The DELIVERY path's turn lock: a NON-blocking probe (§2.5 step 1, P10).

    The lock is a mutual-exclusion primitive on this path, never a waiting room: a held
    lock raises :class:`TurnBusyError` immediately (``timeout_seconds=0``) instead of
    parking the sender, and the caller hands the delivery to the receiver queue, which
    re-offers it when the turn slot frees. Callers that take the lock without a timeout
    of their own still get ``turn_wait_seconds`` as the budget.

    ``session`` is the transcript the delivery will append to (``t_47082c1d``) — NOT the sender and NOT
    the profile. A DM that names the Bot Chat session it is bound for no longer waits behind a Desktop
    turn on the same seat; two DMs bound for that same Bot Chat still queue behind each other.
    """
    return acquire_turn_lock(root, profile, timeout_seconds=0, session=session)


def turn_wait_seconds() -> float:
    """Wait budget for a queued delivery turn (config, lazily read)."""
    val = _bot_mode_cfg("turn_wait_seconds", loader="load_config")
    return float(TURN_WAIT_SECONDS_FALLBACK) if val is None else max(0.0, float(val))


#: The transcript handle a DM is delivered into: a profile's ``Bot Chat`` is a SINGLETON transcript
#: (``SessionDB.get_session_by_title("Bot Chat")``), and every delivery surface — ``hermes -p <p> chat``,
#: ``bot_relay.deliver``, the queue drain, the API drain — appends to that one transcript. The turn lock
#: is therefore keyed on this handle, NOT on the session row id: the handle is stable BEFORE the row
#: exists, so two deliveries that would create it still serialize (``t_47082c1d``).
BOT_CHAT_SESSION_KEY = "botchat"


def room_session_key(room_id: str) -> str:
    """Transcript handle for one Group Chat room's session.

    A room's transcript is titled from ``room_id`` (``room_session_title``), so ``room_id`` — not the
    session row id — is the stable handle: the lock can be taken before the session is resolved or
    created, and two attempts for one room still exclude each other.
    """
    return f"room:{room_id}"


def turn_lock_path(root: Path | str, profile: str, session: str | None = None) -> Path:
    """Lockfile path for ``(profile, session)`` — short, and attributable from the filename alone.

    GRANULARITY (``t_47082c1d``): the lock guards ONE transcript, not one seat. Keying it on the profile
    alone made every surface that runs a turn as profile P — a Desktop session, a hosted room, a DM
    delivery — serialize against each other although they touch different sessions; measured 2026-10-23,
    a DM waited 1m59s / 34 re-offers behind a Desktop turn on the same profile while four other worker
    sessions on that profile ran freely. Two deliveries into the SAME Bot Chat must still serialize, so
    the session id is part of the key, never a fallback to "any turn of this profile".

    ``session=None`` keeps the old profile-wide key. It survives only for callers that genuinely mean
    "this whole seat" (and for tests of the pre-``t_47082c1d`` behaviour); every delivery and turn surface
    passes a session key. The sanitised session carries a digest of the RAW value, so two distinct
    sessions that sanitise to the same characters (``room:ops/1`` vs ``room:ops_1``) can never share a
    lockfile — a collision would silently re-serialize unrelated turns, i.e. reintroduce the defect.
    """
    safe = re.sub(r"[^a-zA-Z0-9_-]", "_", str(profile or ""))[:48] or "_"
    if session is None:
        return relay_root(root) / LOCKS_DIR / f"{safe}.lock"
    raw_session = str(session)
    safe_session = re.sub(r"[^a-zA-Z0-9_-]", "_", raw_session)[:80] or "_"
    digest = hashlib.sha256(raw_session.encode("utf-8", "replace")).hexdigest()[:10]
    return relay_root(root) / LOCKS_DIR / f"{safe}__{safe_session}__{digest}.lock"


@contextlib.contextmanager
def acquire_turn_lock(root: Path | str, profile: str, timeout_seconds: float | None = None, *,
                      session: str | None = None) -> Iterator[Path]:
    """Hold the ``(profile, session)`` cross-process turn lock for the ``with`` body: non-blocking
    flock probe + short-sleep retry up to the budget (``bot_mode.turn_wait_seconds``
    unless ``timeout_seconds``); raises :class:`TurnBusyError` when exhausted — naming
    the holder and its age when the lockfile carries a holder record. No
    ordering among waiters, but every waiter is bounded. Without ``fcntl`` (Windows)
    the lock is a no-op — those installs never had this race path.

    ``session`` narrows the key from the seat to the TRANSCRIPT (``t_47082c1d``): the flock, the bounded
    retry, the holder record and the refusal are all unchanged, but a turn on session A no longer blocks
    a turn on session B of the same profile.
    """
    try:
        import fcntl
    except ImportError:  # pragma: no cover — Windows
        logger.debug("bot turn lock disabled: fcntl unavailable on this platform")
        yield turn_lock_path(root, profile, session)
        return

    budget = turn_wait_seconds() if timeout_seconds is None else max(0.0, float(timeout_seconds))
    path = turn_lock_path(root, profile, session)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        start = time.monotonic()
        deadline = start + budget
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                now = time.monotonic()
                if now >= deadline:
                    holder = read_turn_lock_holder(root, profile, session)
                    # One line per refusal: a seat held past the budget is the signal (t_9acf5894 item 3)
                    # and it must name the holder AND the session, or nothing downstream can route around it.
                    logger.warning("bot turn lock busy: profile=%s session=%s waited=%.0fs holder=%s",
                                   profile, session if session is not None else "-", now - start,
                                   (f"pid {holder['pid']} age={_format_holder_age(holder.get('age_seconds'))}"
                                    f" alive={holder.get('alive')}") if holder else "unknown")
                    raise TurnBusyError(profile, now - start, holder, session=session)
                time.sleep(min(0.1, max(0.005, deadline - now)))
        publish_turn_lock_holder(fd, profile, session)
        try:
            yield path
        finally:
            clear_turn_lock_holder(fd)
            with contextlib.suppress(OSError):  # kernel releases on close anyway
                fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
