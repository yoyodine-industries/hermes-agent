"""``hermes peer`` — bot-to-bot DMs across machines/gateways.

A *peer* is another Hermes gateway running the ``api_server`` platform; its stock
API is the transport (no new server surface). ``dm`` resolves the remote canonical
"Bot Chat" session (creating it when missing) and runs ONE synchronous turn — the
cross-machine twin of ``hermes -p <bot> chat --in ~ -c "Bot Chat"``. ``run``/``status``
/``stop`` do the same turn through the async Runs API. Peer labels/URLs live in
config.yaml (``bot_peers``); the key lives in ``~/.hermes/.env`` as
``HERMES_PEER_<NAME>_KEY``. ``<peer>/<profile>`` targets the ``/p/<profile>/`` mirror.

``dm`` waits only as long as ITS CALLER lets it (``--timeout``, default 60 s; or
``--no-wait``): the verdict that says "delivered, still running, do NOT resend" is printed
by this client, so a hard-coded wait longer than the caller's own bound made silence the
default answer. When the reply does not come back on the call, the message row is pinned in
the peer's transcript and ``hermes peer reply`` (read-only) collects what the peer answered.
Exit 3 says exactly that: the message was TAKEN, so collect it — never resend it.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime
import json
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

BOT_CHAT_TITLE = "Bot Chat"
_PEER_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_PROFILE_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")

# One synchronous agent turn can legitimately take minutes. This stays the SERVER-budget
# default: it is the wait budget the client ASKS THE PEER FOR (``X-Hermes-Wait-Seconds``),
# and the peer runs the turn to completion whatever this client does. It is NOT this
# client's own bound -- that is ``_dm_ceiling`` below.
DM_TIMEOUT_S = 600

# ── how long `dm` waits, and how a caller still gets the reply ───────────────
# `dm` used to wait a hard-coded 600 s for the peer's reply, with no caller override. The
# only correct verdict ("delivered, still running, do NOT resend") is printed BY THIS CLIENT,
# so any caller bound shorter than 600 s — an agent terminal cap, a cron shim, `timeout N` —
# killed the client before it printed anything: delivered and never-arrived became
# indistinguishable, the natural next move was a resend, and the peer ran the same turn
# twice. The wait is therefore caller-bounded and short by default, and the outcome is
# mechanically readable off the exit code: 0 the reply came back, 3 accepted-but-reply-not-
# collected (collect it with `hermes peer reply`), 1 nothing on the wire, 2 usage.
DM_DEFAULT_TIMEOUT_S = 60
#: Floor for the delivery POST's socket timeout. `hermes <subcommand>` costs ~10 s of
#: launcher/bootstrap on this host before the client does any work, so a bound far below this
#: usually expires before the request even leaves; the POST must get a real chance to be SENT
#: or the caller reads "not delivered" for a message that was about to go out. The verdict
#: prints the ACTUAL elapsed wait, so nothing is overclaimed.
MIN_DELIVERY_S = 5
#: `dm --no-wait`: deliver, then give an already-finished reply this short grace. Never waits
#: for the reply past it.
NO_WAIT_GRACE_S = 3
#: `reply --wait`: how often to re-read the transcript while the peer's turn runs.
REPLY_POLL_S = 2
#: Transcript page the post-timeout confirm-read and `reply` both request.
REPLY_PAGE_LIMIT = 50
#: How much of the message a reply is answering `reply` attributes back (chars).
REPLY_TO_MAX_CHARS = 200
#: Bounds of the confirm-read that pins OUR message row to a transcript id. It runs only after
#: the reply is already lost, so it is short, bounded, and NEVER fatal — and it runs only while
#: budget remains inside the call's ceiling.
CONFIRM_READ_ATTEMPTS = 3
CONFIRM_READ_INTERVAL_S = 1.0
CONFIRM_READ_TIMEOUT_S = 5.0
#: A confirm-read attempt is made only while at least this much of the ceiling remains. A slice
#: too small to carry a read fires a request the client cannot wait for — a wasted round trip,
#: which is not evidence of anything — so it is no budget at all, deterministically.
CONFIRM_READ_MIN_BUDGET_S = 0.05
LIST_TIMEOUT_S = 30

# ── where a REFUSED dm is reported ───────────────────────────────────────────
# A refusal used to reach only the caller's stderr, and the measured cost (2026-10-13) was a
# FLEET-WIDE DM outage nobody heard about until the operator asked why DMs were broken.
# Every refusal that means "the DM path is broken" - not "you typed it wrong" -
# therefore files ONE card on the ops board, keyed per (peer, class) so a repeat never spams
# the board. `bot_peer_refusals: {board:, assignee:, priority:}` in the registry home's
# config.yaml overrides the shipped defaults (non-secret plumbing, so it is config, not .env).
REFUSAL_BOARD = "default"
REFUSAL_ASSIGNEE = "platform-stl"
REFUSAL_PRIORITY = 3
#: Slack added to the caller's wait budget so the gateway's own receipt window
#: (``bot_mode.receipt_after_seconds``) always fits inside the HTTP timeout.
_DM_TIMEOUT_SLACK_S = 30

#: §D3: after a read timeout on a *delivery* POST — the request that carries an
#: idempotency key — the identical request is replayed once with this wait
#: budget. Admission is idempotent on that key, so the replay can never admit a
#: second delivery: it replays the reservation and returns the true envelope.
_DM_REPLAY_WAIT_SECONDS = 1

#: HTTP read ceiling for that replay. A replayed reservation answers
#: immediately (no turn runs), so this stays far below ``DM_TIMEOUT_S``.
_DM_REPLAY_TIMEOUT_S = 30

#: The run/status read's ``object`` tag — the read-path twin of SEND_RESULT_OBJECT.
RUN_OBJECT = "hermes.peer.run"

#: The delivery envelope's ``object`` tag (§1.2). Its presence marks a peer that
#: understands queued receipts; anything else is the legacy reply shape.
SEND_RESULT_OBJECT = "hermes.peer.send_result"

#: ENUM A -> process exit code (§1.5). ``receipt`` is SUCCESS, never a failure.
RESULT_EXIT_CODES = {"delivered": 0, "receipt": 0, "failed": 1, "unknown": 1, "refused": 2}

#: Server error codes meaning "the request itself is defective" -> usage exit 2.
_REFUSAL_ERROR_CODES = frozenset({
    "invalid_idempotency_key", "idempotency_key_conflict", "idempotency_conflict",
    "invalid_request", "invalid_payload"})


def _peer_key_env(name: str) -> str:
    return f"HERMES_PEER_{name.upper().replace('-', '_')}_KEY"


def _hermes_root(home: Path) -> Path:
    """The machine-level ``~/.hermes`` for a profile home, and for the default home itself."""
    return home.parent.parent if home.parent.name == "profiles" else home


def _declares_registry(home: Path) -> bool:
    """True when ``home``'s OWN config.yaml declares a ``bot_peers`` mapping.

    Read from the raw file on purpose: which home OWNS the registry is settled before any
    loader runs, and a merged view would report the root's entry as the profile's own.
    """
    path = home / "config.yaml"
    if not path.is_file():
        return False
    try:
        import hermes_yaml as yaml

        raw = yaml.safe_load(path.read_text(encoding="utf-8-sig", errors="replace"))
    except Exception:
        return False
    return isinstance(raw, dict) and isinstance(raw.get("bot_peers"), dict)


def _registry_home() -> Path:
    """The home whose ``bot_peers`` registry THIS session resolves.

    THE REGISTRY IS A HOST-LEVEL FACT AND ITS HOME IS THE ROOT ONE (measured 2026-10-13).
    ``load_config()`` resolves against the session's own home, so a
    profile-scoped session whose config declares no ``bot_peers`` saw an EMPTY registry while
    the machine's root ``~/.hermes/config.yaml`` carried the peer: every lane's ``hermes peer
    dm`` died with ``No peer named '<peer>'``, and the one profile that had been re-registered
    by hand worked alone. A home that declares its own registry owns it (an install may
    legitimately point one profile at a different gateway); a home that declares none resolves
    the root's - one fact, one home, never a copy of the block per profile.
    """
    from hermes_constants import get_hermes_home

    home = get_hermes_home()
    if _declares_registry(home):
        return home
    return _hermes_root(home)


@contextlib.contextmanager
def _config_home(home: Path):
    """Pin config resolution (read AND write) to ``home`` for the block.

    The same context-local override the gateway and the Bot-Mode prompt builder use, so the
    process-wide ``HERMES_HOME`` is never touched: one process may serve several profiles.
    """
    from hermes_constants import get_hermes_home, reset_hermes_home_override, set_hermes_home_override

    if home == get_hermes_home():
        yield
        return
    token = set_hermes_home_override(str(home))
    try:
        yield
    finally:
        reset_hermes_home_override(token)


def _load_peers() -> dict:
    from hermes_cli.config import load_config

    with _config_home(_registry_home()):
        cfg = load_config() or {}
    peers = cfg.get("bot_peers")
    return peers if isinstance(peers, dict) else {}


def _save_peers(peers: dict) -> None:
    from hermes_cli.config import load_config, save_config

    with _config_home(_registry_home()):
        cfg = load_config() or {}
        cfg["bot_peers"] = peers
        save_config(cfg)


def _peer_secret(name: str) -> str:
    """The peer's API key: profile-scoped secret store first, raw env fallback."""
    env_name = _peer_key_env(name)
    try:
        from agent.secret_scope import get_secret

        return (get_secret(env_name, "") or "").strip()
    except Exception:
        import os

        return (os.environ.get(env_name) or "").strip()


def _request(
    url: str, key: str, *, method: str = "GET", body: dict | None = None,
    timeout: float = LIST_TIMEOUT_S, headers: dict[str, str] | None = None) -> dict:
    from hermes_cli.urllib_security import open_credentialed_url
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request_headers = {
        "Authorization": f"Bearer {key}", "Content-Type": "application/json",
        "User-Agent": "hermes-peer-dm"}
    if headers:
        request_headers.update(headers)
    req = urllib.request.Request(url, data=data, method=method, headers=request_headers)
    # The peer URL is user-registered (``hermes peer add``); a redirect to a
    # different origin must not carry the Authorization: Bearer key with it —
    # a compromised/MITM'd peer could otherwise harvest it. open_credentialed_url
    # strips non-safelisted headers across a cross-origin redirect.
    with open_credentialed_url(req, timeout=timeout) as resp:
        payload = resp.read().decode("utf-8", "replace")
    try:
        parsed = json.loads(payload)
    except ValueError as exc:
        raise RuntimeError(f"Peer returned non-JSON response: {payload[:200]}") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError("Peer returned a non-object JSON response")
    return parsed


def _base_url(peer: dict, profile: str | None) -> str:
    url = str(peer.get("url") or "").rstrip("/")
    if profile:
        # Multiplex mirror: same handlers, scoped to the named profile.
        return f"{url}/p/{urllib.parse.quote(profile, safe='')}"
    return url


def _find_bot_chat(base: str, key: str) -> str | None:
    """The remote canonical Bot Chat's session id, or None.

    Bot Mode always HIDES canonical chats, so the plain listing (which
    excludes hidden sessions) misses an existing Bot Chat and the caller
    would try to create a duplicate that the peer's UNIQUE(title) guard
    rejects (issue #91583). Newer peers support an exact-title lookup with
    ``include_hidden=1``; older peers ignore the unknown query params and
    return the ordinary visible listing, so this single request degrades
    to exactly the previous behavior against them.
    """
    query = urllib.parse.urlencode({"limit": 200, "title": BOT_CHAT_TITLE, "include_hidden": 1})
    listing = _request(f"{base}/api/sessions?{query}", key)
    for session in listing.get("data") or []:
        if isinstance(session, dict) and (session.get("title") or "").strip() == BOT_CHAT_TITLE:
            return str(session.get("id") or "") or None
    return None


def _ensure_bot_chat(base: str, key: str) -> str:
    existing = _find_bot_chat(base, key)
    if existing:
        return existing
    try:
        created = _request(
            f"{base}/api/sessions", key, method="POST",
            body={"title": BOT_CHAT_TITLE, "source": "bot_peer_dm"})
    except urllib.error.HTTPError as exc:
        detail = _http_error_detail(exc)
        if exc.code == 400 and "title" in detail.lower():
            # Older peer (no title/include_hidden lookup support): its
            # canonical Bot Chat exists but is hidden, so we couldn't see it
            # and the create collided with the UNIQUE(title) guard.
            raise RuntimeError(
                f"Peer already has a '{BOT_CHAT_TITLE}' session but it is hidden and the "
                f"peer's gateway is too old to expose hidden sessions to this lookup "
                f"(HTTP 400: {detail}). Update the peer's hermes-agent, or unhide the "
                f"session there: PATCH /api/sessions/<id> {{\"hidden\": false}}.") from exc
        raise
    # Real api_server wraps the row: {"object": "hermes.session", "session": {...}}.
    session = created.get("session") if isinstance(created.get("session"), dict) else created
    session_id = str(session.get("id") or session.get("session_id") or "")
    if not session_id:
        raise RuntimeError("Peer did not return a session id for the new Bot Chat")
    return session_id


def _parse_target(target: str) -> tuple[str, str | None]:
    """``<peer>`` or ``<peer>/<profile>`` → (peer, profile|None)."""
    raw = (target or "").strip()
    peer, _, profile = raw.partition("/")
    peer = peer.strip()
    profile = profile.strip() or None
    if not peer:
        raise ValueError("Peer name required (hermes peer dm <peer>[/<agent>] ...)")
    if profile and not _PROFILE_RE.match(profile):
        raise ValueError(f"Invalid agent/profile name: {profile!r}")
    return peer, profile


def _http_error_detail(exc: urllib.error.HTTPError) -> str:
    try:
        body = exc.read().decode("utf-8", "replace")
        parsed = json.loads(body)
        message = parsed.get("error", {}).get("message") if isinstance(parsed, dict) else None
        return message or body[:200]
    except Exception:
        return str(exc)


def _resolve_peer_target(target: str) -> tuple[str, str | None, dict, str]:
    """Resolve a registered target to ``(name, profile, config, key)``."""
    peer_name, profile = _parse_target(target)
    peer = _load_peers().get(peer_name)
    if not isinstance(peer, dict) or not peer.get("url"):
        raise LookupError(f"No peer named '{peer_name}'. Run: hermes peer list")
    key = _peer_secret(peer_name)
    if not key:
        raise PermissionError(
            f"No API key for peer '{peer_name}'. Set it: hermes peer add {peer_name} "
            f"--url <url> --key <key> (or add {_peer_key_env(peer_name)}=<key> to ~/.hermes/.env)")
    return peer_name, profile, peer, key


def _message_from_args(args) -> str:
    message = (getattr(args, "message", None) or "").strip()
    if not message and not sys.stdin.isatty():
        message = sys.stdin.read().strip()
    return message


def _peer_run_durability(base: str, key: str) -> bool | None:
    """Return durable support, or None when an older peer cannot advertise it."""
    try:
        capabilities = _request(f"{base}/v1/capabilities", key)
    except Exception:
        return None
    features = capabilities.get("features")
    if not isinstance(features, dict):
        return None
    contract = features.get("runs_idempotency")
    if not isinstance(contract, dict) or not contract.get("supported"):
        return None
    return bool(contract.get("durable"))


def _target_peer(target: str) -> str:
    """The peer name a ``<peer>[/<agent>]`` target names, for a report filed before
    resolution. Never raises, never empty."""
    return (str(target or "").split("/", 1)[0].strip() or "?")


def _refusal_class(exc: BaseException) -> str:
    """A stable slug for a refusal that must be REPORTED, or ``''`` when it is not one.

    Reported: no such peer, no key for the peer, and any HTTP status the peer itself returned
    (401/403/404 and 429 - the capacity cap that starves the control channel - measured
    2026-10-13).
    NOT reported: transport failures (timeout, refused connection, DNS). A peer that is briefly
    unreachable is the reachability probe's business, and a card per blip is noise.
    """
    if isinstance(exc, LookupError):
        return "no-peer"
    if isinstance(exc, PermissionError):
        return "no-key"
    if isinstance(exc, urllib.error.HTTPError):
        return f"http-{exc.code}"
    return ""


def _refusal_target() -> dict:
    """`bot_peer_refusals` from the registry home, over the shipped defaults."""
    from hermes_cli.config import load_config

    with _config_home(_registry_home()):
        cfg = load_config() or {}
    block = cfg.get("bot_peer_refusals")
    block = block if isinstance(block, dict) else {}
    try:
        priority = int(block.get("priority", REFUSAL_PRIORITY))
    except (TypeError, ValueError):
        priority = REFUSAL_PRIORITY
    return {"board": str(block.get("board") or REFUSAL_BOARD),
            "assignee": str(block.get("assignee") or REFUSAL_ASSIGNEE),
            "priority": priority}


def _create_card(*, title, body, assignee, priority, board, idempotency_key, db_path=None) -> str:
    """The ONE seam the tests stub: the fork's own board API, so the report is a first-class
    board row written by the same code path as ``hermes kanban create``.

    ``db_path`` is the store :func:`_refusal_store` already resolved (the same one the
    idempotency lookup read): passing it keeps the lookup and the write on ONE store when a
    pin or the current-board pointer is in play.
    """
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    opener = (kbc.connect_closing(db_path=Path(db_path)) if db_path
              else kbc.connect_closing(board=board))
    with opener as conn:
        return kb.create_task(conn, title=title, body=body, assignee=assignee,
                              created_by="peer-dm", priority=priority,
                              idempotency_key=idempotency_key)


def _refusal_key(peer_name: str, klass: str) -> str:
    return f"peer-refusal-{peer_name}-{klass}"


def _refusal_body(peer_name: str, klass: str, detail: str) -> str:
    from hermes_constants import get_hermes_home

    return "\n".join([
        f"A `hermes peer dm`/`hermes peer run` to peer `{peer_name}` was REFUSED and the "
        "caller could not deliver it. Cross-lane DM is the control channel between lanes, so a "
        "refusal that reaches only the caller's stderr is a comms outage nobody hears about "
        "(measured 2026-10-13: the whole fleet's DM was dead and nothing "
        "reported it).",
        "",
        f"- class: `{klass}`",
        f"- detail: {detail}",
        f"- sender home: `{get_hermes_home()}`",
        f"- registry home: `{_registry_home()}`",
        f"- when: {datetime.datetime.now().isoformat(timespec='seconds')}",
        "",
        "## What the classes mean",
        "- `no-peer`: this session resolves no registry entry for that peer name. The "
        "2026-10-13 fleet outage was exactly this: `bot_peers` existed in the root "
        "`~/.hermes/config.yaml` while a profile-scoped session resolves its OWN home.",
        "- `no-key`: no `HERMES_PEER_<PEER>_KEY` for that peer (auth).",
        "- `http-429`: the peer's own capacity cap refused the turn (`Too many concurrent "
        "runs`) - bulk work starving the control channel.",
        "- other `http-4xx`: the peer refused the request (401/403 auth, 404 unknown endpoint "
        "or unserved profile).",
        "",
        f"Filed by `hermes peer` - one card per (peer, class): idempotency key "
        f"`{_refusal_key(peer_name, klass)}`.",
    ])


def _refusal_store(board: str) -> tuple[str, Path]:
    """``(a label for the store the card goes to, its resolved DB path)`` — resolved ONCE.

    A dispatched worker's ``HERMES_KANBAN_DB`` pin (and the current-board pointer) are read
    HERE, once, and then used for the lookup AND the write: the key can never be checked
    against one store and spent on another, and the stderr line names where the card IS,
    never where it was asked to be (defect D11, card t_703af8d1). Measured 2026-10-14: with
    the board resolution read twice the two calls could disagree once a pin was in play.

    The label is the board SLUG when the resolved store can be identified as one, and the
    store PATH when it cannot (a custom ``HERMES_HOME`` beside a pin): a line that names a
    board it did not write to is the defect this report exists to end.
    """
    from hermes_cli import kanban_db as kb

    db_path = kb.kanban_db_path(board=board)
    try:
        slug = kb.board_for_store_path(db_path)
    except Exception:
        slug = None
    return (f"board '{slug}'" if slug else f"store {db_path}"), db_path


def _existing_refusal_card(db_path, idempotency_key: str) -> str:
    """The id of the card this key already filed, or ``''`` -- a READ, never a write.

    Read-only on purpose. The caller may be in the fenced context a dispatcher-spawned
    worker runs in, where a read is permitted and a write is not; and a lookup that
    CONNECTED a missing store would itself be a write. Nothing is created here.
    """
    try:
        from hermes_cli import kanban_db_connect as kbc

        if not Path(db_path).exists():
            return ""
        with kbc.connect_closing(db_path=Path(db_path)) as conn:
            row = conn.execute(
                "SELECT id FROM tasks WHERE idempotency_key = ? AND status != 'archived' "
                "ORDER BY created_at DESC LIMIT 1",
                (idempotency_key,),
            ).fetchone()
    except Exception:
        return ""
    return str(row["id"]) if row is not None else ""


#: The ONE named purpose the Kanban write fence admits from a child context
#: (``agent.delegation_context.CHILD_KANBAN_WRITE_PURPOSES``): a refusal report is a NEW,
#: idempotency-keyed row about a broken control channel, filed from the context the
#: channel broke in -- it moves no existing card.
REFUSAL_WRITE_PURPOSE = "peer-refusal-report"


def _file_refusal_card(peer_name: str, klass: str, detail: str, *, create=None,
                       target: dict | None = None, db_path=None) -> tuple[str, str]:
    """File ONE idempotency-keyed card for a refusal.

    Returns ``(card_id, reason)``: a non-empty id means the row is on the board; an empty
    one carries the REAL reason it is not, so the caller can print it instead of guessing
    (the old code swallowed every exception and called it "the board write failed", which
    mis-attributed a fenced-context refusal as an I/O failure).

    NEVER raises: a report that broke the refusal path would be worse than no report at all.
    """
    from agent.delegation_context import child_kanban_write_allowance

    target = target or _refusal_target()
    filer = create or _create_card
    title = f"[peer-dm] '{peer_name}' refused a cross-lane DM ({klass})"
    try:
        with child_kanban_write_allowance(REFUSAL_WRITE_PURPOSE):
            card = filer(title=title, body=_refusal_body(peer_name, klass, detail),
                         assignee=target["assignee"], priority=target["priority"],
                         board=target["board"], db_path=db_path,
                         idempotency_key=_refusal_key(peer_name, klass))
        return str(card or ""), ""
    except Exception as exc:  # reporting must never raise
        return "", f"{exc.__class__.__name__}: {exc}"


def _report_refusal(peer_name: str, klass: str, detail: str) -> str:
    """File the refusal card and SAY SO on stderr, in one of THREE outcomes.

    ``filed`` / ``already filed`` / ``not filed: <the real reason>``. The three are
    different facts: the idempotency hit used to be indistinguishable from a failure, and
    a fenced-context refusal used to be reported as an I/O error. The store is resolved ONCE
    (:func:`_refusal_store`) so the lookup and the write cannot land on different boards, the
    card is looked up by key FIRST (a read, permitted in a fenced context) so the hit is
    knowable, and the write runs under the reviewed child-context allowance
    (``REFUSAL_WRITE_PURPOSE``) so it can act from a dispatcher-spawned worker.
    """
    target = _refusal_target()
    where, db_path = _refusal_store(target["board"])
    key = _refusal_key(peer_name, klass)
    card = _existing_refusal_card(db_path, key)
    if card:
        outcome = f"already filed: card {card} on the {where} (idempotency key {key})"
    else:
        card, reason = _file_refusal_card(peer_name, klass, detail, target=target, db_path=db_path)
        outcome = (f"filed: card {card} on the {where}" if card
                   else f"NOT FILED: {reason or 'the board write failed'}")
    print(f"Reported: peer '{peer_name}' refused the request ({klass}) -> {outcome}",
          file=sys.stderr)
    return card


def _coerce_int(raw) -> int | None:
    """A flag value as an ``int``, or None when it is not an integer at all.

    ``--timeout``/``--wait``/``--after`` are integers: a bool is not a number here, ``1.5`` is
    not an integer, and only a float that IS integral (``3.0``) or a numeric string (``"3"``)
    converts. ``None`` (flag absent) converts to None.
    """
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float):
        return int(raw) if raw.is_integer() else None
    if isinstance(raw, str):
        try:
            return int(raw.strip())
        except ValueError:
            return None
    return None


def _validate_dm_flags(args) -> str | None:
    """Usage check for ``dm --timeout``/``--no-wait``/``--wait``: the message, or None when valid."""
    no_wait = bool(getattr(args, "no_wait", False))
    raw = getattr(args, "timeout", None)
    raw_wait = getattr(args, "wait_seconds", None)
    if no_wait and raw is not None:
        return ("--no-wait and --timeout are mutually exclusive: --no-wait never waits for the "
                "reply, --timeout bounds that wait.")
    if no_wait and raw_wait is not None:
        return ("--no-wait and --wait are mutually exclusive: --wait is the budget this client "
                "ASKS THE PEER FOR, --no-wait never waits for its reply.")
    if raw is None:
        return None
    seconds = _coerce_int(raw)
    if seconds is None:
        return f"--timeout must be an integer number of seconds (got {raw!r})."
    if seconds < 1:
        return ("--timeout must be at least 1 second; there is no --timeout 0 alias for "
                "--no-wait.")
    return None


def _validate_reply_flags(args) -> str | None:
    """Usage check for ``reply --wait``/``--after``: the message, or None when they are valid."""
    raw_wait = getattr(args, "wait", None)
    if raw_wait is not None:
        seconds = _coerce_int(raw_wait)
        if seconds is None:
            return f"--wait must be an integer number of seconds (got {raw_wait!r})."
        if seconds < 0:
            return "--wait must be 0 (a single read) or a whole number of seconds."
    raw_after = getattr(args, "after", None)
    if raw_after is not None and _coerce_int(raw_after) is None:
        return f"--after must be a transcript row id (an integer) (got {raw_after!r})."
    return None


def _validate_action_flags(action: str, args) -> str | None:
    """The per-action usage check ``cmd_peer`` runs BEFORE it resolves the target.

    A bad flag is a typo the caller can fix; resolving the peer first would answer a missing
    peer or a missing key instead, which is the wrong error for a malformed command.
    """
    if action == "dm":
        return _validate_dm_flags(args)
    if action == "reply":
        return _validate_reply_flags(args)
    return None


def _dm_ceiling(args) -> float:
    """The client's HARD ceiling on its OWN network phase for one ``dm`` call, in seconds.

    The whole phase — the delivery POST, any replay, and the confirm-read together — never
    runs past this, so a caller bound shorter than the client (an agent terminal cap, a cron
    shim, ``timeout N``) is never left with silence in place of the verdict.

    ``--wait W`` keeps its landed meaning: it is the budget this client ASKS THE PEER FOR,
    sent as ``X-Hermes-Wait-Seconds``. It is not, by itself, this client's bound — an explicit
    budget gets today's arithmetic (``W`` plus the receipt-window slack), so existing callers
    see no change; the no-flag default is ``DM_DEFAULT_TIMEOUT_S``; ``--timeout N`` sets the
    ceiling exactly, floored at ``MIN_DELIVERY_S`` so the request actually gets onto the wire.
    """
    if bool(getattr(args, "no_wait", False)):
        return float(NO_WAIT_GRACE_S)
    seconds = _coerce_int(getattr(args, "timeout", None))
    if seconds is not None:
        return float(max(MIN_DELIVERY_S, seconds))
    raw_wait = getattr(args, "wait_seconds", None)
    if raw_wait is not None:
        try:
            wait = float(raw_wait)
        except (TypeError, ValueError):
            wait = float(DM_DEFAULT_TIMEOUT_S)
        return max(1.0, wait) + _DM_TIMEOUT_SLACK_S
    return float(DM_DEFAULT_TIMEOUT_S)


def _row_id(row: dict) -> int | None:
    """A transcript row's integer ``id``, or None when the peer did not give one."""
    raw = row.get("id")
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    try:
        return int(str(raw).strip())
    except ValueError:
        return None


def _by_row_id(rows: list[dict]) -> list[dict]:
    """Rows in transcript order (insertion order IS id order); a row with no id sorts first."""
    return sorted(rows, key=lambda row: (_row_id(row) is None, _row_id(row) or 0))


def _transcript(base: str, key: str, session_id: str,
                *, timeout: float = CONFIRM_READ_TIMEOUT_S) -> list[dict]:
    """One newest-page read of a session's transcript, as the peer returns it.

    ``order=latest`` pages back from the newest but the peer returns chronological order, so the
    LAST element is the newest row. Every caller here goes through ``_row_id`` maxima instead,
    which is order-independent.
    """
    query = urllib.parse.urlencode({"order": "latest", "limit": REPLY_PAGE_LIMIT})
    payload = _request(
        f"{base}/api/sessions/{urllib.parse.quote(session_id, safe='')}/messages?{query}",
        key, timeout=timeout)
    rows = payload.get("data")
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def _pin_message_row(base: str, key: str, session_id: str, message: str,
                     *, deadline: float | None) -> int | None:
    """Run the bounded confirm-read and pin OUR message row; return its id, or None.

    Pinned means the newest ``role == "user"`` row whose content equals the message we sent —
    our row, named by the transcript itself, so ``reply --after`` cannot be pointed at an older
    turn by accident. Anything else (unreadable transcript, a different message, no transcript
    row at all) pins nothing and the caller says ``accepted``: the request was sent, nothing
    more is claimed. Bounded by ``CONFIRM_READ_ATTEMPTS`` and by the call's own ceiling —
    it runs only while budget remains inside it — and NEVER raises, never fatal.
    """
    for attempt in range(CONFIRM_READ_ATTEMPTS):
        remaining: float | None = None
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining < CONFIRM_READ_MIN_BUDGET_S:
                return None
        if attempt:
            time.sleep(CONFIRM_READ_INTERVAL_S)
        try:
            rows = _transcript(
                base, key, session_id,
                timeout=CONFIRM_READ_TIMEOUT_S if remaining is None
                else min(CONFIRM_READ_TIMEOUT_S, remaining))
        except Exception:
            continue
        matched: int | None = None
        for row in rows:
            if str(row.get("role") or "") != "user" or row.get("content") != message:
                continue
            row_id = _row_id(row)
            if row_id is not None and (matched is None or row_id > matched):
                matched = row_id
        if matched is not None:
            return matched
    return None


def _emit_pending_delivery(args, *, peer_name: str, profile: str | None, target: str,
                           session_id: str, row_id: int | None, elapsed_s: float,
                           idempotency_key: str = "", unknown_detail: str = "") -> int:
    """Report "accepted, reply not collected" and hand the caller a way to collect it; exit 3.

    Exit 3 is deliberately distinct from 1 ("nothing on the wire") so a caller can tell delivered
    from not-delivered MECHANICALLY, and the collect command is the post-hoc handle to the reply
    the peer is going to produce anyway. A pinned ``row_id`` names our own message row, so
    ``reply --after`` cannot be pointed at an older turn by accident. ``unknown_detail`` carries
    the delivery-outcome-unknown wording (the idempotency key a resend must reuse) for the path
    where both the delivery POST and its idempotent replay went unanswered.
    """
    where = f"session {session_id}, row {row_id}" if row_id is not None else f"session {session_id}"
    after = f" --after {row_id}" if row_id is not None else ""
    payload = {
        "peer": peer_name, "profile": profile, "session_id": session_id,
        "after": row_id, "status": "pending",
        "delivery": "confirmed" if row_id is not None else "accepted",
        "elapsed_s": round(elapsed_s, 1),
    }
    if idempotency_key:
        # The retry key stays reported: a resend of a once-ambiguous delivery MUST reuse it.
        payload["idempotency_key"] = idempotency_key
    if getattr(args, "json", False):
        print(json.dumps(payload))
        return 3
    if unknown_detail:
        print(unknown_detail, file=sys.stderr)
    print(f"Peer '{peer_name}' accepted the message but its turn is still running after "
          f"{int(elapsed_s)}s: the message is already in its Bot Chat ({where}) and will be "
          "answered there. The reply cannot come back on this call. Do NOT resend.",
          file=sys.stderr)
    print("Collect it with:", file=sys.stderr)
    print(f"  hermes peer reply {target} --session {session_id}{after}", file=sys.stderr)
    if row_id is None:
        print("Check that the newest user row `hermes peer reply` prints is yours.", file=sys.stderr)
    return 3


def _reply_window(rows: list[dict], after: int | None) -> list[dict]:
    """Rows to consider: with a cursor, only rows whose integer id is greater than it."""
    if after is None:
        return list(rows)
    kept = []
    for row in rows:
        row_id = _row_id(row)
        if row_id is not None and row_id > after:
            kept.append(row)
    return kept


def _assistant_reply(ordered: list[dict]) -> dict | None:
    """The LAST (highest-id) ``role == "assistant"`` row carrying non-empty content."""
    best = None
    for row in ordered:
        if str(row.get("role") or "") == "assistant" and str(row.get("content") or ""):
            best = row
    return best


def _reply_state(window: list[dict], *, has_cursor: bool) -> tuple[str, dict | None]:
    """``(status, reply_row)`` for the rows in the window.

    With a cursor all three outcomes are decided by the peer's own transcript: ``completed`` an
    assistant row landed after the cursor, ``pending`` a turn is in flight (a user row did and
    nothing answered it), ``not_delivered`` nothing landed at all. Without a cursor there is no
    baseline that could call anything missing, so the TAIL is reported — the newest conversational
    row is an assistant row (``completed``) or a user row (``pending``) — and ``not_delivered`` is
    never claimed.
    """
    ordered = _by_row_id(window)
    if not ordered:
        # Nothing after the cursor. Only a cursor makes "not delivered" a claim we can support:
        # without one an empty transcript is not evidence of a lost message.
        return ("not_delivered" if has_cursor else "pending"), None
    if not has_cursor:
        newest = next((row for row in reversed(ordered)
                       if str(row.get("role") or "") in ("user", "assistant")), None)
        if newest is not None and str(newest.get("role") or "") == "assistant":
            return "completed", _assistant_reply(ordered)
        return "pending", None
    if any(str(row.get("role") or "") == "assistant" for row in ordered):
        # An assistant row after the cursor IS the answer — including an empty one, which is the
        # peer's silence marker rather than a missing reply.
        return "completed", _assistant_reply(ordered)
    if any(str(row.get("role") or "") == "user" for row in ordered):
        return "pending", None
    # Rows landed but none is a turn boundary (tool rows, say): the turn is still in flight, so
    # this is not evidence that our message went missing.
    return "pending", None


def _reply_to(ordered: list[dict], anchor: dict | None) -> str | None:
    """What that row answers: the nearest preceding user row's content, truncated.

    This is the answer to "is this reply to MY message?" — the reply alone cannot say.
    """
    anchor_id = _row_id(anchor) if anchor is not None else None
    if anchor_id is None:
        return None
    best = None
    best_id = None
    for row in ordered:
        row_id = _row_id(row)
        if row_id is None or row_id >= anchor_id or str(row.get("role") or "") != "user":
            continue
        if best_id is None or row_id > best_id:
            best, best_id = row, row_id
    if best is None:
        return None
    return str(best.get("content") or "")[:REPLY_TO_MAX_CHARS]


def _peer_reply(args, peer_name: str, profile: str | None, base: str, key: str) -> int:
    """``hermes peer reply`` — collect the reply one ``dm`` could not wait for. READ-ONLY.

    It resolves an EXISTING session only (``_find_bot_chat``, never ``_ensure_bot_chat``): a read
    verb must not create a session on the peer, so `reply` can never be the reason a Bot Chat
    appears. Without a cursor it is the "any bound" guarantee — even when a caller killed ``dm``
    before it printed a handle, the sender can always ask the peer what it received and what it
    answered. stdout is the reply text ALONE (parity with ``dm``, so callers reuse their parsing);
    a non-completed status has no reply to print and reports itself on stderr.
    """
    usage = _validate_reply_flags(args)
    if usage is not None:
        print(usage, file=sys.stderr)
        return 2
    after = _coerce_int(getattr(args, "after", None))
    wait_seconds = _coerce_int(getattr(args, "wait", None)) or 0
    session_arg = (getattr(args, "session", None) or "").strip()
    started = time.monotonic()

    if session_arg:
        session_id = session_arg
    else:
        try:
            found = _find_bot_chat(base, key)
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError,
                RuntimeError) as exc:
            return _peer_failure(peer_name, exc)
        if not found:
            print(f"Peer '{peer_name}' has no '{BOT_CHAT_TITLE}' session to read, so there is "
                  "nothing to collect there.", file=sys.stderr)
            return 1
        session_id = found

    deadline = started + wait_seconds
    window: list[dict] = []
    reply_row: dict | None = None
    status = "pending"
    while True:
        try:
            rows = _transcript(base, key, session_id)
        except urllib.error.HTTPError as exc:
            if session_arg and exc.code == 404:
                print(f"Peer '{peer_name}': no session {session_id} to read "
                      f"(HTTP 404: {_http_error_detail(exc)}).", file=sys.stderr)
                return 1
            return _peer_failure(peer_name, exc)
        except (urllib.error.URLError, TimeoutError, OSError, RuntimeError) as exc:
            return _peer_failure(peer_name, exc)
        window = _reply_window(rows, after)
        status, reply_row = _reply_state(window, has_cursor=after is not None)
        now = time.monotonic()
        if status == "completed" or now >= deadline:
            break
        time.sleep(min(REPLY_POLL_S, deadline - now))

    ordered = _by_row_id(window)
    anchor = reply_row if reply_row is not None else (ordered[-1] if ordered else None)
    reply_text = ""
    if reply_row is not None:
        # A bare silence marker is a delivery decision, not a message: the sender never sees
        # NO_REPLY/[SILENT] as a real reply. Same rule as `dm` and the other Bot Mode doors.
        from gateway.response_filters import is_intentional_silence_response

        raw = str(reply_row.get("content") or "")
        reply_text = "" if is_intentional_silence_response(raw) else raw
    payload = {
        "peer": peer_name, "profile": profile, "session_id": session_id, "after": after,
        "status": status, "reply": reply_text, "reply_to": _reply_to(ordered, anchor),
        "message_id": _row_id(anchor) if anchor is not None else None,
        "rows_seen": len(window), "elapsed_s": round(time.monotonic() - started, 1),
    }
    if getattr(args, "json", False):
        print(json.dumps(payload))
    elif status == "completed":
        print(reply_text or "(no reply)")
    elif status == "pending":
        print(f"Peer '{peer_name}': the message is in its Bot Chat (session {session_id}) but "
              "its turn has not answered yet. Do NOT resend; ask again later.", file=sys.stderr)
    else:
        print(f"Peer '{peer_name}': no row after the cursor in session {session_id} — the "
              "message has not reached that transcript.", file=sys.stderr)
    return {"completed": 0, "pending": 3, "not_delivered": 1}[status]


def _peer_failure(peer_name: str, exc: Exception) -> int:
    """Print a peer HTTP rejection or transport failure to stderr; always exit 1.

    A refusal that means the DM path is broken is ALSO filed on the ops board: stderr reaches
    nobody who is not already looking, and the measured cost of that was a fleet-wide DM
    outage discovered by accident.
    """
    if isinstance(exc, urllib.error.HTTPError):
        detail = _http_error_detail(exc)
        print(f"Peer '{peer_name}' rejected the request (HTTP {exc.code}): {detail}",
              file=sys.stderr)
    else:
        detail = str(exc)
        print(f"Could not reach peer '{peer_name}': {exc}", file=sys.stderr)
    klass = _refusal_class(exc)
    if klass:
        _report_refusal(peer_name, klass, detail)
    return 1


def _is_read_timeout(exc: BaseException) -> bool:
    """True when *exc* is a socket read timeout, never a connection failure.

    ``urlopen`` wraps a timeout raised before the response headers in
    ``URLError``, but the one this lane hit fires mid-body: a synchronous peer
    turn can run for minutes, so the read timeout surfaces from ``resp.read()``
    as a bare ``socket.timeout`` — which *is* ``TimeoutError`` on Python 3.10+.
    Both shapes mean the same thing: the request was sent, the answer was lost.
    """
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return True
    return (isinstance(exc, urllib.error.URLError)
            and isinstance(getattr(exc, "reason", None), (TimeoutError, socket.timeout)))


class _DeliveryOutcomeUnknown(Exception):
    """A delivery POST timed out AND its idempotent replay did not answer.

    The first request may still have been admitted, so the caller must report
    ``unknown`` — never "unreachable", and never a blind resend.
    """

    def __init__(self, idempotency_key: str) -> None:
        super().__init__(idempotency_key)
        self.idempotency_key = idempotency_key


def _deliver_with_replay(url: str, key: str, *, body: dict, timeout: float,
                         headers: dict, idempotency_key: str,
                         deadline: float | None = None) -> dict:
    """POST one delivery, replaying it once on a read timeout (§D3).

    A read timeout on the *delivery* POST does not mean the peer was
    unreachable: the request may already be admitted, reserved and executing.
    Nothing may claim "unreachable" for that, so the identical request — same
    URL, same body, same idempotency key, wait budget pinned to
    ``_DM_REPLAY_WAIT_SECONDS`` — is re-issued once. Admission is idempotent on
    the key, so this cannot deliver twice: the server replays the reservation
    and returns the true envelope (the ``receipt``, or the settled result if
    the turn has since finished).

    ``deadline`` is the call's own ceiling: the replay runs ONLY if ITS whole
    allowance still fits inside what remains of it. A ceiling that lets the
    client run 30 s past itself is the defect this bound exists to remove — the
    read timeout has already spent the ceiling by the time it fires, so there
    the outcome is reported as ``unknown`` (exit 3, "accepted — collect it")
    rather than by spending another 30 s nobody budgeted for.
    """
    try:
        return _request(url, key, method="POST", body=body, timeout=timeout, headers=headers)
    except urllib.error.HTTPError:
        raise  # an HTTP status is an explicit answer, never a lost response
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        if not _is_read_timeout(exc):
            raise
    remaining = None if deadline is None else deadline - time.monotonic()
    if remaining is not None and remaining < _DM_REPLAY_TIMEOUT_S:
        raise _DeliveryOutcomeUnknown(idempotency_key)
    replay_headers = {**headers, "X-Hermes-Wait-Seconds": str(int(_DM_REPLAY_WAIT_SECONDS))}
    try:
        return _request(url, key, method="POST", body=body,
                        timeout=_DM_REPLAY_TIMEOUT_S if remaining is None
                        else min(_DM_REPLAY_TIMEOUT_S, remaining),
                        headers=replay_headers)
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError,
            RuntimeError) as exc:
        # Deliberately broad: the first POST may have been admitted, so *any*
        # failure to read the replay leaves this delivery's outcome unknown.
        # Reporting the peer as unreachable here is the exact misreport the
        # receipt contract exists to eliminate.
        raise _DeliveryOutcomeUnknown(idempotency_key) from exc


def _unknown_delivery_detail(idempotency_key: str) -> str:
    """``do not resend`` wording for a delivery whose outcome could not be read."""
    try:
        from tools.bot_delivery_queue import unknown_detail

        tail = unknown_detail(idempotency_key)
    except Exception:
        tail = f"outcome unknown; a resend must reuse idempotency_key {idempotency_key}"
    return f"do not resend — {tail}"


def _bot_mode_value(key: str, default):
    """Read one ``bot_mode`` key, falling back to ``default``."""
    try:
        from hermes_cli.config import load_config_readonly

        cfg = load_config_readonly() or {}
        value = (cfg.get("bot_mode") or {}).get(key, default)
    except Exception:
        value = default
    return default if value is None else value


def _peer_value(key: str, default):
    """Read one ``peer`` key, falling back to ``default``."""
    try:
        from hermes_cli.config import load_config_readonly

        cfg = load_config_readonly() or {}
        value = (cfg.get("peer") or {}).get(key, default)
    except Exception:
        value = default
    return default if value is None else value


def _resolve_wait_seconds(args) -> float:
    """``--wait`` sent as ``X-Hermes-Wait-Seconds`` (default ``peer.dm_wait_seconds``)."""
    raw = getattr(args, "wait_seconds", None)
    if raw is None:
        raw = _peer_value("dm_wait_seconds", DM_TIMEOUT_S)
    try:
        wait = float(raw)
    except (TypeError, ValueError):
        wait = float(DM_TIMEOUT_S)
    return max(1.0, wait)


def _sender_profile() -> str:
    """This host's profile name, sent as ``X-Hermes-Sender-Profile``."""
    env = (os.environ.get("HERMES_PROFILE") or "").strip()
    if env:
        return env
    try:
        from hermes_cli.profiles import get_active_profile_name

        return (get_active_profile_name() or "").strip() or "default"
    except Exception:
        return "default"


def _validate_idempotency_key(key: str) -> str:
    """1-255 characters, no CR/LF/NUL — the rule already used by ``peer run``."""
    key = (key or "").strip()
    if not key or len(key) > 255 or re.search(r"[\r\n\x00]", key):
        raise ValueError("Idempotency key must be 1-255 characters without control newlines.")
    return key


def _resolve_idempotency_key(args, *, sender_profile: str, target_profile: str,
                             session_id: str, message: str) -> str:
    """Explicit ``--idempotency-key``, else the shared §4.1 derivation.

    Must run *after* the Bot Chat session id is resolved: the derived key is
    bound to ``session_id``, so minting it earlier would make a retry that hit a
    different session a different logical message.
    """
    explicit = (getattr(args, "idempotency_key", None) or "").strip()
    if explicit:
        return _validate_idempotency_key(explicit)
    from hermes_cli.delivery_keys import derive_delivery_key

    try:
        window = int(_bot_mode_value("dedup_window_seconds", 900))
    except (TypeError, ValueError):
        window = 900
    return derive_delivery_key(sender_profile, target_profile, session_id, message,
                               dedup_window_seconds=window)


def _delivery_headers(idempotency_key: str, sender_profile: str, wait_seconds: float) -> dict:
    """The three delivery headers the client owns (§5.1)."""
    return {
        "Idempotency-Key": idempotency_key,
        "X-Hermes-Sender-Profile": sender_profile,
        "X-Hermes-Wait-Seconds": str(int(wait_seconds)),
    }


def _peer_error_body(exc: urllib.error.HTTPError) -> tuple[str, str]:
    """``(code, message)`` from an HTTPError body — a body can be read only once."""
    try:
        raw = exc.read().decode("utf-8", "replace")
    except Exception:
        return "", str(exc)
    try:
        parsed = json.loads(raw)
    except ValueError:
        return "", (raw[:200] or str(exc))
    if not isinstance(parsed, dict):
        return "", raw[:200]
    error = parsed.get("error")
    if isinstance(error, dict):
        return (str(error.get("code") or error.get("reason") or ""),
                str(error.get("message") or raw[:200]))
    return (str(parsed.get("reason") or parsed.get("code") or ""),
            str(parsed.get("message") or raw[:200]))


def _peer_refusal(peer_name: str, exc: urllib.error.HTTPError) -> int:
    """Report a refused/rejected request. A defect is exit 2, a delivery error 1."""
    code, message = _peer_error_body(exc)
    if code in _REFUSAL_ERROR_CODES:
        print(f"Peer '{peer_name}' refused the request: {message}", file=sys.stderr)
        return 2
    print(f"Peer '{peer_name}' rejected the request (HTTP {exc.code}): {message}",
          file=sys.stderr)
    return 1


def _emit_dm_envelope(args, envelope: dict, *, session_id: str, idempotency_key: str,
                      peer_name: str, target: str = "") -> int:
    """Print one delivery envelope and translate ENUM A into an exit code (§1.5).

    ENUM A is unchanged: ``receipt`` is SUCCESS and stays exit 0. What is new is
    that the receipt — a message the peer accepted and queued — also hands the
    caller the way to collect its reply, so "accepted" is never a dead end.
    """
    result = str(envelope.get("result") or "")
    status = str(envelope.get("status") or "")
    if getattr(args, "json", False):
        print(json.dumps(envelope))
    elif result == "delivered":
        print(str(envelope.get("reply") or "(no reply)"))
    elif result == "receipt":
        print(f"{status}: {envelope.get('detail') or ''}")
        print(f"session_id: {session_id}")
        print(f"idempotency_key: {idempotency_key}")
        print("Collect it with:")
        print(f"  hermes peer reply {target or peer_name} --session {session_id}")
    else:
        why = envelope.get("error") or envelope.get("detail") or envelope.get("reason") or result
        print(f"Peer '{peer_name}' {status or result}: {why}", file=sys.stderr)
    return RESULT_EXIT_CODES.get(result, 1)


def _emit_unknown_delivery(args, *, peer_name: str, profile: str | None, session_id: str,
                           idempotency_key: str, target: str = "", message: str = "",
                           base: str = "", key: str = "", deadline: float | None = None,
                           started: float | None = None) -> int:
    """Report an accepted-but-unverifiable delivery (§D3) without blaming the peer; exit 3.

    Both the delivery POST and its idempotent replay went unanswered, so the
    message may still have landed. The wording must never read as "the peer was
    unreachable" (it wasn't) and must warn against a blind resend.

    This is the same outcome the ceiling produces when it fires after the POST —
    "accepted, reply not collected" — so it reports through the SAME verdict and
    carries the same handle (``hermes peer reply``), including the row the
    confirm-read could pin OUR message to. Exit 3 is what makes it mechanically
    distinguishable from 1 ("nothing on the wire"): never resend either way.
    """
    detail = _unknown_delivery_detail(idempotency_key)
    row_id = None
    if base and key and session_id:
        row_id = _pin_message_row(base, key, session_id, message, deadline=deadline)
    elapsed_s = 0.0 if started is None else time.monotonic() - started
    if getattr(args, "json", False):
        payload = {
            "peer": peer_name, "profile": profile, "session_id": session_id,
            "after": row_id, "status": "pending",
            "delivery": "confirmed" if row_id is not None else "accepted",
            "elapsed_s": round(elapsed_s, 1),
            "idempotency_key": idempotency_key,
            "detail": detail,
        }
        print(json.dumps(payload))
        return 3
    return _emit_pending_delivery(args, peer_name=peer_name, profile=profile, target=target,
                                  session_id=session_id, row_id=row_id, elapsed_s=elapsed_s,
                                  idempotency_key=idempotency_key, unknown_detail=detail)


def _emit(args, payload: dict, text_lines: list[str]) -> int:
    if getattr(args, "json", False):
        print(json.dumps(payload))
    else:
        for line in text_lines:
            print(line)
    return 0


def _peer_add(args) -> int:
    name = (args.name or "").strip().lower()
    if not _PEER_NAME_RE.match(name):
        print(f"Invalid peer name: {name!r} (lowercase, digits, -, _; max 64)", file=sys.stderr)
        return 2
    url = (args.url or "").strip()
    if not url.lower().startswith(("http://", "https://")):
        print("Peer --url must be an http(s) gateway base URL, e.g. http://spark.lan:8377", file=sys.stderr)
        return 2
    peers = _load_peers()
    peers[name] = {"url": url.rstrip("/"), **({"note": args.note.strip()} if getattr(args, "note", "") else {})}
    _save_peers(peers)
    key = (getattr(args, "key", "") or "").strip()
    if key:
        from hermes_cli.config import save_env_value

        save_env_value(_peer_key_env(name), key)
        print(f"Peer '{name}' saved ({url}) — key stored as {_peer_key_env(name)} in ~/.hermes/.env")
    else:
        print(
            f"Peer '{name}' saved ({url}). No key given — set the peer's API_SERVER_KEY with:\n"
            f"  hermes peer add {name} --url {url} --key <key>\n"
            f"  (or add {_peer_key_env(name)}=<key> to ~/.hermes/.env)")
    return 0


def _peer_remove(args) -> int:
    name = (args.name or "").strip().lower()
    peers = _load_peers()
    if name not in peers:
        print(f"No peer named '{name}'.", file=sys.stderr)
        return 1
    peers.pop(name)
    _save_peers(peers)
    print(f"Peer '{name}' removed (its {_peer_key_env(name)} entry in .env is kept; delete it manually if unused).")
    return 0


def _peer_list(args) -> int:
    peers = _load_peers()
    if not peers:
        print("No peers registered. Add one: hermes peer add <name> --url http://host:port --key <API_SERVER_KEY>")
        return 0
    for name in sorted(peers):
        entry = peers[name] if isinstance(peers[name], dict) else {}
        has_key = "key set" if _peer_secret(name) else f"NO KEY ({_peer_key_env(name)} unset)"
        note = f" — {entry.get('note')}" if entry.get("note") else ""
        print(f"{name}\t{entry.get('url', '?')}\t[{has_key}]{note}")
    return 0


def _emit_unknown_run(args, peer_name: str, profile: str | None, run_id: str) -> int:
    """A 404 on the READ path is post-retention GC, not a delivery failure.

    Spec T6 / §7: a run's terminal row is deleted at the end of its retention
    window, so 404 means the record aged out — not that the delivery failed.
    Exit status stays 0 so a monitor keying on it cannot report a
    delivered-and-GC'd message as a failure.
    """
    if getattr(args, "json", False):
        print(json.dumps({
            "peer": peer_name, "profile": profile, "object": RUN_OBJECT,
            "result": "unknown", "status": "unknown", "run_id": run_id,
            "reason": "run_not_found", "retryable": False}))
    else:
        print(f"{run_id}: unknown — no such run "
              f"(retention window elapsed; this is not a delivery failure)")
    return 0


def _peer_run_ctl(args, action: str, peer_name: str, profile: str | None, base: str,
                  key: str) -> int:
    """``status`` / ``stop`` on an asynchronous run."""
    run_id = (getattr(args, "run_id", None) or "").strip()
    if not run_id:
        print("Run ID required.", file=sys.stderr)
        return 2
    stop = action == "stop"
    try:
        result = _request(
            f"{base}/v1/runs/{urllib.parse.quote(run_id, safe='')}" + ("/stop" if stop else ""),
            key, method="POST" if stop else "GET", body={} if stop else None)
    except urllib.error.HTTPError as exc:
        # A 404 on the read path is the retention window elapsing, never a
        # delivery signal. Every other status (and ``stop``, which mutates)
        # keeps today's failure semantics exactly.
        if not stop and exc.code == 404:
            return _emit_unknown_run(args, peer_name, profile, run_id)
        return _peer_failure(peer_name, exc)
    except (urllib.error.URLError, TimeoutError, OSError, RuntimeError) as exc:
        return _peer_failure(peer_name, exc)
    if getattr(args, "json", False):
        print(json.dumps({"peer": peer_name, "profile": profile, **result}))
        return 0
    print(f"{run_id}: {result.get('status', 'unknown')}")
    if not stop and result.get("output"):
        print(result["output"])
    elif not stop and result.get("error"):
        print(result["error"], file=sys.stderr)
    return 0


def _turn_body(message: str, *, message_key: str, **extra) -> dict:
    """Request body for one turn. ``author`` is added only when a dispatcher set HERMES_TURN_AUTHOR."""
    from agent.turn_author import turn_author_from_env

    body = {message_key: message, **extra}
    author = turn_author_from_env()
    if author is not None:
        body["author"] = author
    return body


def _peer_run(args, message: str, peer_name: str, profile: str | None, base: str, key: str) -> int:
    sender_profile = _sender_profile()
    wait_seconds = _resolve_wait_seconds(args)
    try:
        if _peer_run_durability(base, key) is not True:
            print(
                "Warning: this peer does not advertise restart-durable "
                "run replay; keep the run ID and avoid blind retries "
                "after a gateway restart.", file=sys.stderr)
        session_id = _ensure_bot_chat(base, key)
        idempotency_key = _resolve_idempotency_key(
            args, sender_profile=sender_profile, target_profile=profile or sender_profile,
            session_id=session_id, message=message)
        result = _request(
            f"{base}/v1/runs", key, method="POST",
            body=_turn_body(message, message_key="input", session_id=session_id),
            headers=_delivery_headers(idempotency_key, sender_profile, wait_seconds))
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except urllib.error.HTTPError as exc:
        return _peer_refusal(peer_name, exc)
    except (urllib.error.URLError, TimeoutError, OSError, RuntimeError) as exc:
        return _peer_failure(peer_name, exc)
    if result.get("object") == SEND_RESULT_OBJECT and result.get("result") == "receipt":
        envelope = dict(result)
        envelope.setdefault("peer", peer_name)
        envelope.setdefault("profile", profile)
        return _emit_dm_envelope(args, envelope, session_id=session_id,
                                 idempotency_key=idempotency_key, peer_name=peer_name,
                                 target=str(getattr(args, "target", "") or peer_name))
    run_id = str(result.get("run_id") or result.get("delivery_id") or "")
    if not run_id:
        print(f"Peer '{peer_name}' did not return a run ID.", file=sys.stderr)
        return 1
    payload = {
        "peer": peer_name, "profile": profile, "session_id": session_id, "run_id": run_id,
        "status": result.get("status") or "started", "idempotency_key": idempotency_key,
        "replayed": bool(result.get("replayed", False))}
    replay = " (replayed)" if payload["replayed"] else ""
    return _emit(args, payload, [
        f"{run_id}: {payload['status']}{replay}", f"session_id: {session_id}",
        f"idempotency_key: {idempotency_key}"])


def _peer_dm(args, message: str, peer_name: str, profile: str | None, base: str, key: str) -> int:
    usage = _validate_dm_flags(args)
    if usage is not None:
        print(usage, file=sys.stderr)
        return 2
    target = str(getattr(args, "target", "") or peer_name)
    session_id = ""
    wait_seconds = _resolve_wait_seconds(args)
    # The wait ASKED OF the peer (--wait) and this client's own ceiling are two different
    # things: the peer runs the turn to completion whatever we do, so the ceiling only decides
    # how long WE are willing to hold the call open for the reply.
    ceiling = _dm_ceiling(args)
    sender_profile = _sender_profile()
    try:
        session_id = _ensure_bot_chat(base, key)
    except RuntimeError as exc:
        print(f"Peer '{peer_name}': {exc}", file=sys.stderr)
        return 1
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return _peer_failure(peer_name, exc)
    # The derived key is bound to the resolved session id, so it is minted here
    # rather than before the Bot Chat lookup (§4.1).
    try:
        idempotency_key = _resolve_idempotency_key(
            args, sender_profile=sender_profile, target_profile=profile or sender_profile,
            session_id=session_id, message=message)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    headers = _delivery_headers(idempotency_key, sender_profile, wait_seconds)
    # THE CEILING starts here: the delivery POST, any replay and the confirm-read all live
    # inside it, so a caller whose own bound is shorter than this client's is never killed
    # before the verdict is printed.
    started = time.monotonic()
    deadline = started + ceiling
    try:
        result = _deliver_with_replay(
            f"{base}/api/sessions/{urllib.parse.quote(session_id, safe='')}/chat", key,
            body=_turn_body(message, message_key="message"), timeout=ceiling,
            headers=headers, idempotency_key=idempotency_key, deadline=deadline)
    except urllib.error.HTTPError as exc:
        return _peer_refusal(peer_name, exc)
    except _DeliveryOutcomeUnknown:
        return _emit_unknown_delivery(
            args, peer_name=peer_name, profile=profile, session_id=session_id,
            idempotency_key=idempotency_key, target=target, message=message,
            base=base, key=key, deadline=deadline, started=started)
    except RuntimeError as exc:
        print(f"Peer '{peer_name}': {exc}", file=sys.stderr)
        return 1
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        # A timeout while awaiting the response, once the Bot Chat is known, means the peer took
        # this turn: the message is already in its Bot Chat and the gateway runs the turn to
        # completion regardless of this client, so reporting it unreachable makes the sender resend
        # and deliver it twice. urllib raises that timeout bare; one it wraps in URLError hit while
        # connecting or sending, so the request never arrived and "could not reach" is the truth.
        # The verdict carries the ACTUAL elapsed wait (never a hard-coded budget), pins OUR row
        # when the transcript shows it inside the remaining budget, and is exit 3 — "accepted,
        # reply not collected" — so it is mechanically distinct from 1, "nothing on the wire".
        if session_id and isinstance(exc, TimeoutError):
            return _emit_pending_delivery(
                args, peer_name=peer_name, profile=profile, target=target, session_id=session_id,
                row_id=_pin_message_row(base, key, session_id, message, deadline=deadline),
                elapsed_s=time.monotonic() - started, idempotency_key=idempotency_key)
        return _peer_failure(peer_name, exc)
    if result.get("object") == "hermes.session.chat.queued":
        # The peer's Bot Chat is open in its Desktop and that turn outlasted the peer's wait: the
        # message is in the open chat and is answered there, so a resend would run it twice.
        queued_in = result.get("session_id") or session_id
        return _emit(args, {"peer": peer_name, "profile": profile, "session_id": queued_in,
                            "status": result.get("status") or "queued", "delivery_id": result.get("delivery_id")},
                     [f"Peer '{peer_name}' has its Bot Chat open, so the message went into that chat (session "
                      f"{queued_in}) and is answered there. The reply cannot come back on this call. Do NOT resend.",
                      "Collect it with:",
                      f"  hermes peer reply {target} --session {queued_in}"])
    if result.get("object") == SEND_RESULT_OBJECT:
        envelope = dict(result)
        envelope.setdefault("peer", peer_name)
        envelope.setdefault("profile", profile)
        return _emit_dm_envelope(args, envelope, session_id=session_id,
                                 idempotency_key=idempotency_key, peer_name=peer_name,
                                 target=target)
    # Legacy peer without the delivery envelope: keep the pre-receipt reply shape.
    msg = result.get("message")
    reply = str(msg.get("content") or "") if isinstance(msg, dict) else ""
    # A successful bare silence marker is a delivery decision, not a message:
    # the turn stays in the peer's own transcript, the sending agent never
    # sees NO_REPLY/[SILENT] as a real reply. Same rule as the gateway's live
    # Bot Chat completion, the Desktop bot_relay.deliver RPC and the one-shot
    # local `hermes chat -Q` transport (tools/bot_mode_dm.py) — this is the
    # 4th Bot Mode delivery door and was missing the same check.
    from gateway.response_filters import is_intentional_silence_response
    if is_intentional_silence_response(reply):
        reply = ""
    payload = {"peer": peer_name, "profile": profile,
               "session_id": result.get("session_id") or session_id, "reply": reply,
               "idempotency_key": idempotency_key, "waited_seconds": float(wait_seconds)}
    return _emit(args, payload, [reply or "(no reply)"])


_REGISTRY_ACTIONS = {
    "add": _peer_add, "set": _peer_add, "remove": _peer_remove, "rm": _peer_remove,
    "list": _peer_list, "ls": _peer_list, None: _peer_list}


def cmd_peer(args) -> int:
    action = getattr(args, "peer_action", None)
    if action in _REGISTRY_ACTIONS:
        return _REGISTRY_ACTIONS[action](args)
    if action not in {"dm", "run", "status", "stop", "reply"}:
        print("Unknown peer action. See: hermes peer --help", file=sys.stderr)
        return 2
    usage = _validate_action_flags(action, args)
    if usage is not None:
        print(usage, file=sys.stderr)
        return 2
    try:
        peer_name, profile, peer, key = _resolve_peer_target(args.target)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except (LookupError, PermissionError) as exc:
        print(str(exc), file=sys.stderr)
        # A no-peer/no-key refusal IS the fleet's DM path failing - not a local typo to
        # swallow. It goes on the board under the peer the caller named.
        _report_refusal(_target_peer(args.target), _refusal_class(exc), str(exc))
        return 1
    base = _base_url(peer, profile)
    if action in {"status", "stop"}:
        return _peer_run_ctl(args, action, peer_name, profile, base, key)
    if action == "reply":
        return _peer_reply(args, peer_name, profile, base, key)
    message = _message_from_args(args)
    if not message:
        print("Message required (argument or stdin).", file=sys.stderr)
        return 2
    handler = _peer_run if action == "run" else _peer_dm
    return handler(args, message, peer_name, profile, base, key)


def build_peer_parser(subparsers) -> None:
    """Attach the ``peer`` subcommand to ``subparsers``."""
    parser = subparsers.add_parser(
        "peer", help="Bot-to-bot DMs across machines (peer Hermes gateways)",
        description="Register other Hermes gateways as peers and message their agents. "
            "'hermes peer dm <peer>[/<agent>] \"...\"' delivers into the remote "
            "agent's canonical Bot Chat over the peer's API server and prints "
            "the reply — the cross-machine twin of 'hermes -p <bot> chat'. "
            "'dm --timeout N' bounds THIS client's wait for that reply and 'dm --no-wait' "
            "returns as soon as the message is delivered; in either case a reply that does not "
            "come back on the call is collectable afterwards with 'hermes peer reply', which "
            "reads the peer's Bot Chat and never writes to it. "
            "The peer must run the api_server platform; its API_SERVER_KEY is "
            "stored locally as a credential in ~/.hermes/.env.",
        epilog=(
            "Examples:\n"
            "  hermes peer add spark --url http://spark.lan:8377 --key <API_SERVER_KEY>\n"
            "  hermes peer list\n"
            '  hermes peer dm spark "Message from 🤖 dixie (@dixie): disk status?"\n'
            '  hermes peer dm spark/researcher "..."   # named profile on a multiplexed peer\n'
            "  hermes peer dm spark --wait 30 \"...\"    # short wait; a busy peer returns a receipt\n"
            '  hermes peer dm spark "..." --timeout 30 # bound THIS client\'s wait for the reply\n'
            '  hermes peer dm spark "..." --no-wait     # deliver now, collect the reply later\n'
            "  hermes peer reply spark --session <sid> --after <row>   # collect it\n"
            "  hermes peer run spark --idempotency-key ticket-123 < long-task.txt\n"
            "  hermes peer status spark run_abc123\n"
            "  hermes peer stop spark run_abc123\n"
            "  hermes peer remove spark\n"
            "\n"
            "Exit codes: 0 the reply came back, or the peer accepted the message (a queued "
            "receipt, or a turn handed to an open Bot Chat); 1 delivery/peer error — nothing "
            "arrived; 2 usage or a refused request; 3 ACCEPTED but the reply was not collected "
            "on this call — the message IS in the peer's Bot Chat, so run the 'hermes peer "
            "reply' line printed with the verdict instead of resending it.\n"
            "The invariant: exit 3 means delivered — never resend; exit 1 means nothing "
            "arrived.\n"
            "Collecting a reply is the standard follow-up: 'hermes peer reply <peer> "
            "[--session <sid>] [--after <row_id>] [--wait N]' reads the peer's own transcript "
            "(it performs NO POST), so a reply that missed the dm's wait is never lost.\n"
            "--wait W is the budget this client ASKS THE PEER FOR ('X-Hermes-Wait-Seconds', "
            f"default peer.dm_wait_seconds, {DM_TIMEOUT_S}); the client's own ceiling on the "
            f"call is --timeout (default {DM_DEFAULT_TIMEOUT_S}), because the shipped default "
            "budget is 600 s and the client adds 30 s of slack, so a bare `dm` outlives every "
            "ordinary caller bound (an agent terminal cap, a cron shim, `timeout N`) and the "
            "caller sees silence. `--wait 600` still buys the long inline wait; `--timeout` "
            "sets the client ceiling exactly.\n"
            "`--wait 1` is the receipt idiom against an envelope-capable peer; `--no-wait` is "
            "the same idea for a legacy peer that has no receipt to return.\n"
            "A post-retention 'hermes peer status' 404 is reported as unknown, exit 0 — "
            "it is not a delivery failure.\n"
            "Budget --timeout with a startup margin: 'hermes <subcommand>' costs seconds of "
            "launcher/bootstrap before the client does any work (1.9 s measured warm "
            "2026-10-14, 8-11 s cold), so budget --timeout with that margin — the margin is "
            "spent BEFORE the network phase, so it is the CALLER's bound that must cover it."),
        formatter_class=argparse.RawDescriptionHelpFormatter)
    peer_sub = parser.add_subparsers(dest="peer_action")

    add_p = peer_sub.add_parser("add", aliases=["set"], help="Register (or update) a peer gateway")
    add_p.add_argument("name", help="Peer name (lowercase slug, e.g. spark, homelab)")
    add_p.add_argument("--url", required=True, help="Peer gateway base URL, e.g. http://spark.lan:8377")
    add_p.add_argument("--key", default="", help="The peer's API_SERVER_KEY (stored in ~/.hermes/.env)")
    add_p.add_argument("--note", default="", help="Optional description")

    peer_sub.add_parser("list", aliases=["ls"], help="List registered peers")

    rm_p = peer_sub.add_parser("remove", aliases=["rm"], help="Remove a peer")
    rm_p.add_argument("name", help="Peer name")

    def _remote(name: str, help: str, *, run_id: bool):
        sp = peer_sub.add_parser(name, help=help)
        sp.add_argument("target", help="<peer> or <peer>/<agent> (named profile on a multiplexed peer)")
        if run_id:
            sp.add_argument(
                "run_id",
                help="Run ID or delivery ID returned by 'hermes peer run'/'hermes peer dm'")
        else:
            sp.add_argument("message", nargs="?", default=None, help="Message text (or stdin)")
            sp.add_argument(
                "--idempotency-key", default=None,
                help="Stable retry key; a retry MUST reuse it (derived from the message when omitted)")
            if name == "dm":
                budget = sp.add_mutually_exclusive_group()
                budget.add_argument(
                    "--wait", dest="wait_seconds", type=float, default=None, metavar="W",
                    help="Seconds of reply budget to ASK THE PEER FOR "
                         f"(default: peer.dm_wait_seconds, {DM_TIMEOUT_S}); an envelope-capable "
                         "peer answers with a receipt when its turn outlasts it, and --wait 1 is "
                         "the receipt idiom. This is not this client's own ceiling: see --timeout.")
                budget.add_argument(
                    "--timeout", type=int, default=None, metavar="N",
                    help="Seconds THIS client waits for the peer's reply before it prints the "
                         f"'accepted, collect it later' verdict (default: {DM_DEFAULT_TIMEOUT_S}, "
                         "exit 3). The old 600 s default outlived every ordinary caller bound — "
                         "an agent terminal cap, a cron shim, `timeout N` — which made silence "
                         "the default answer; pass --timeout 600 for a long inline wait, or use "
                         "run/status. Never below 5 s, so the request actually leaves; budget the "
                         "~10 s launcher margin on top.")
                budget.add_argument(
                    "--no-wait", dest="no_wait", action="store_true",
                    help="Deliver and return at once; never waits for the reply, and works "
                         "against a peer that has no receipt to return. Collect the reply with "
                         "'hermes peer reply'.")
            else:
                sp.add_argument(
                    "--wait", dest="wait_seconds", type=float, default=None,
                    help="Seconds to wait for a reply before a queued receipt is returned "
                         f"(default: peer.dm_wait_seconds, {DM_TIMEOUT_S})")
        sp.add_argument("--json", action="store_true", default=False, help="Emit a JSON result")

    _remote("dm", "Message an agent on a peer gateway; returns its reply, else a queued receipt",
            run_id=False)
    _remote("run", "Start a long peer turn asynchronously and return its run ID", run_id=False)
    _remote("status", "Read the status and final output of an asynchronous peer run", run_id=True)
    _remote("stop", "Stop one asynchronous peer run without affecting another turn", run_id=True)

    reply_p = peer_sub.add_parser(
        "reply", help="Collect the reply a 'peer dm' could not wait for (read-only)")
    reply_p.add_argument(
        "target", help="<peer> or <peer>/<agent> (named profile on a multiplexed peer)")
    reply_p.add_argument(
        "--session", default=None,
        help="Session id to read (default: the peer's canonical Bot Chat; a read never creates one)")
    reply_p.add_argument(
        "--after", type=int, default=None, metavar="ROW_ID",
        help="Only consider rows newer than this transcript row id — the row 'peer dm' pinned")
    reply_p.add_argument(
        "--wait", type=int, default=0, metavar="N",
        help="Poll for up to N seconds while the peer's turn finishes (default 0: a single read)")
    reply_p.add_argument("--json", action="store_true", default=False, help="Emit a JSON result")

    parser.set_defaults(func=cmd_peer)
