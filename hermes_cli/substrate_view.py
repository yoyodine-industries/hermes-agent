"""Boot-volume READ of the substrate authority record — the ONE mirror (design §3.6).

The substrate admission gate writes ONE authority record,
``<fleet home>/substrate_status.json`` (``hermes_cli/substrate_access.py``), and every
human-facing surface must show THAT record, not a second opinion. This module is the
single reader those surfaces share: ``hermes doctor`` (``doctor_substrate``), the
dashboard (``/api/substrate``, ``/api/status``) and the gateway API
(``:8644 GET /substrate``).

Rules this module is built to keep (card t_cc599eea, design t_72e9bf03 §3.6, §6):

* **Read-only, and it refuses nothing.** No write, no probe, no spawn. A reader that
  could refuse would be a fourth door nobody designed.
* **Boot volume only.** Every path it touches lives on the boot volume (the record,
  the degradation ledger, ``config.yaml``). It never stats, opens or lists anything
  under ``/Volumes/yoyofs1`` or ``/opt/hermes_prod``: the whole point is to answer
  "is the runtime's substrate reachable" *while the volume is denying*, in well under
  a second. Nothing here can block on a hung mount.
* **``unknown`` is a first-class answer, never green.** An absent, unreadable or stale
  record is ``state: unknown`` with the reason and the last known state — because
  "no tick has ever recorded a verdict" and "we saw a denial 6 hours ago and nothing
  since" are both *not* healthy. The :9501 API's 17/17 green during a total substrate
  denial on 2026-10-26 is the cautionary measurement this rule exists for.
* **A counted fail-open is a degradation.** ``guards_blind`` folds
  ``hook-degradation.jsonl`` / ``hook-degradation.last`` into the same view, so the
  guards that are silently failing open are visible next to the substrate state
  instead of only in a ledger nobody reads.

``guards_blind`` counts ARMED ROWS, not bodies: one unreadable ``guard-shared-trees.py``
blinds four ``pre_tool_call`` rows (terminal, write_file, patch, skill_manage), and each
of those four is its own silent observation loss, so the honest report is "4 of 5".
"""

from __future__ import annotations

import datetime
import json
import os
import time
from pathlib import Path
from typing import Any, Iterable, Optional

#: The authority record's filename, inside the FLEET home (never a profile home: two
#: homes would mean two authorities, see ``substrate_access.hermes_home``).
STATUS_FILENAME = "substrate_status.json"

#: Where the fail-open launcher records a degraded hook body (``hook-launcher.py``).
LEDGER_FILENAME = "hook-degradation.jsonl"
STAMP_FILENAME = "hook-degradation.last"

#: A record older than this is ``unknown``, never ``ok``. The writers are the two doors'
#: HEARTBEAT (``substrate_access.HEARTBEAT_EVERY`` = 60 s on every healthy pass, plus the
#: refusal path, which rewrites every refused tick) and a recovery; 15 min is ~15 missed
#: heartbeats, well past "one slow tick" and far short of "the gate stopped running hours ago".
#: After card ``t_253a5e55`` this window means exactly what it says: silence means no door ran.
STALE_AFTER = 900.0

#: A degradation record this recent means "that body is blind NOW". The launcher
#: throttles to one record per 60 s, so a steady outage keeps refreshing it; the
#: window is generous because a rarely-invoked row (``skill_manage``) may not be
#: called for minutes.
BLIND_WITHIN = 900.0

#: Bounded reads: the tail of the ledger and at most this many of its last lines.
LEDGER_MAX_BYTES = 256 * 1024
LEDGER_MAX_LINES = 400

#: At most this many denied roots are rendered; the count is always exact.
RENDERED_ROOTS = 8

#: Suffixes a launcher-armed row may carry after the launcher script.
_LAUNCHER_MARKERS = ("hook-launcher.py", "-launcher.py")


# ---------------------------------------------------------------------------------------
# Homes — two, deliberately, and they can differ
# ---------------------------------------------------------------------------------------


def fleet_home() -> Path:
    """The FLEET home holding the ONE authority record and the ONE degradation ledger.

    Resolved through ``hermes_constants.get_default_hermes_root()`` for the same reason
    ``substrate_access.hermes_home`` is: a profile-scoped process
    (``HERMES_HOME=.../profiles/<name>``) must read the same authority as the gateway.
    """
    try:
        from hermes_constants import get_default_hermes_root

        return Path(get_default_hermes_root())
    except Exception:  # noqa: BLE001 - a missing constant must not break the mirror
        env = (os.environ.get("HERMES_HOME") or "").strip()
        return Path(env) if env else Path.home() / ".hermes"


def local_home() -> Path:
    """The home whose ``config.yaml`` hook registry this process actually observes.

    Profile homes carry their own rows (measured 2026-10-26: the root home arms 6 launcher
    rows — 5 ``pre_tool_call`` + 1 ``kanban_task_blocked`` — and each of the 8 profile
    homes arms 5: 4 + 1, because the root home carries the uv-live-venv row a profile does
    not), and a profile process is the one that can go blind in its own way — so the
    registry is read locally while the record stays fleet-wide.
    """
    env = (os.environ.get("HERMES_HOME") or "").strip()
    return Path(env) if env else fleet_home()


def status_path(path: Any = None) -> Path:
    """The authority record's path (or the explicit one a caller supplies)."""
    return Path(path) if path is not None else fleet_home() / STATUS_FILENAME


def ledger_path() -> Path:
    return fleet_home() / "ops-checks" / LEDGER_FILENAME


def stamp_path() -> Path:
    return fleet_home() / "ops-checks" / STAMP_FILENAME


# ---------------------------------------------------------------------------------------
# Small, never-raising primitives
# ---------------------------------------------------------------------------------------


def _epoch(value: Any) -> Optional[float]:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        pass
    try:
        return datetime.datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


def _iso(stamp: Any) -> Optional[str]:
    """Epoch -> local ISO-8601 with offset, the same shape the record writes."""
    seconds = _epoch(stamp)
    if seconds is None:
        return None
    try:
        return datetime.datetime.fromtimestamp(seconds).astimezone().isoformat(timespec="seconds")
    except (OverflowError, OSError, ValueError):
        return None


def _age(stamp: Any, now: float) -> Optional[float]:
    seconds = _epoch(stamp)
    if seconds is None:
        return None
    return round(max(0.0, now - seconds), 1)


def _read_record(path: Path) -> tuple[dict, Optional[str]]:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}, "absent"
    except OSError as exc:
        return {}, f"unreadable: {exc.__class__.__name__}: {exc}"
    except UnicodeDecodeError as exc:  # a binary blob is not a record
        return {}, f"unparseable: {exc.__class__.__name__}"
    try:
        doc = json.loads(raw)
    except ValueError as exc:
        return {}, f"unparseable: {exc}"
    if not isinstance(doc, dict):
        return {}, "unparseable: not a JSON object"
    return doc, None


def _tail_lines(path: Path, max_bytes: int = LEDGER_MAX_BYTES) -> Optional[list[str]]:
    """The last *max_bytes* of a file as lines. ``None`` when it cannot be read."""
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            if size > max_bytes:
                fh.seek(size - max_bytes)
                fh.readline()  # drop the partial first line
            blob = fh.read()
    except (FileNotFoundError, OSError):
        return None
    return blob.decode("utf-8", "replace").splitlines()


def _degradations(now: float) -> tuple[dict[str, dict], Optional[str], Optional[float]]:
    """Group the ledger by the body it names.

    Returns ``({body: run}, ledger_state, stamp_epoch)`` where each run is
    ``{"first": epoch, "last": epoch, "reason": str}`` for the CURRENT contiguous run of
    that body. ``ledger_state`` is ``None`` (read), ``"absent"`` or an error string.
    """
    lines = _tail_lines(ledger_path())
    ledger_state: Optional[str] = None
    if lines is None:
        ledger_state = "absent" if not ledger_path().exists() else "unreadable"
        lines = []
    records: list[dict] = []
    for line in lines[-LEDGER_MAX_LINES:]:
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict):
            continue
        stamp = _epoch(record.get("ts"))
        body = record.get("target")
        if stamp is None or not isinstance(body, str) or not body.strip():
            continue
        records.append({"at": stamp, "body": body.strip(), "reason": record.get("reason")})

    runs: dict[str, dict] = {}
    for record in sorted(records, key=lambda r: r["at"]):
        run = runs.get(record["body"])
        if run is None or (record["at"] - run["last"]) > BLIND_WITHIN:
            runs[record["body"]] = {
                "first": record["at"],
                "last": record["at"],
                "reason": record["reason"],
                "records": 1,
            }
        else:
            run["last"] = record["at"]
            run["reason"] = record["reason"] or run["reason"]
            run["records"] = int(run.get("records") or 0) + 1
    current = {body: run for body, run in runs.items() if (now - run["last"]) <= BLIND_WITHIN}

    stamp: Optional[float] = None
    try:
        stamp = _epoch(stamp_path().read_text(encoding="utf-8").strip())
    except (FileNotFoundError, OSError):
        stamp = None
    return current, ledger_state, stamp


# ---------------------------------------------------------------------------------------
# The hook registry — which rows are armed through a fail-open launcher
# ---------------------------------------------------------------------------------------


def _hook_rows(config_path: Optional[Path] = None) -> tuple[list[dict], Optional[str]]:
    """Every launcher-armed hook row in this home's registry. ``(rows, error)``.

    A row is ``{row, event, matcher, command, hook, body}``. Only rows whose command
    invokes a launcher are returned: those are the ones that can fail open silently,
    which is what ``guards_blind`` is about. A row that names a body directly is a
    different failure mode (CPython's own exit 2 reading as a veto) and is not counted
    here.
    """
    path = Path(config_path) if config_path is not None else local_home() / "config.yaml"
    try:
        import yaml

        doc = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return [], "absent"
    except Exception as exc:  # noqa: BLE001 - yaml import/parse must never break the read
        return [], f"unreadable: {exc.__class__.__name__}: {exc}"
    if not isinstance(doc, dict):
        return [], "unparseable: not a mapping"
    hooks = doc.get("hooks")
    if not isinstance(hooks, dict):
        return [], "no hooks block"

    rows: list[dict] = []
    for event, entries in hooks.items():
        if not isinstance(entries, list):
            continue
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict):
                continue
            command = entry.get("command")
            if not isinstance(command, str) or not command.strip():
                continue
            tokens = command.split()
            launcher = next(
                (tok for tok in tokens if tok.rsplit("/", 1)[-1].endswith(_LAUNCHER_MARKERS)),
                None,
            )
            if launcher is None:
                continue
            after = tokens[tokens.index(launcher) + 1:] if launcher in tokens else []
            body = next(
                (tok for tok in reversed(after)
                 if tok.rsplit("/", 1)[-1].endswith(".py") and tok != launcher),
                None,
            )
            matcher = entry.get("matcher")
            row = f"{event}[{index}]" + (f":{matcher}" if isinstance(matcher, str) and matcher else "")
            rows.append(
                {
                    "row": row,
                    "event": str(event),
                    "matcher": matcher if isinstance(matcher, str) else None,
                    "command": command,
                    "hook": (body.rsplit("/", 1)[-1][: -3] if body else None),
                    "body": body,
                }
            )
    return rows, None


def _blind_rows(now: float, config_path: Optional[Path] = None) -> dict:
    """The degradation set folded against the registry: ``{blind, total, items, ...}``."""
    tried, ledger_state, stamp = _degradations(now)
    rows, registry_error = _hook_rows(config_path)
    items: list[dict] = []
    for row in rows:
        body = row.get("body")
        run = tried.get(body) if body else None
        if run is None:
            continue
        items.append(
            {
                "hook": row["hook"],
                "row": row["row"],
                "body": body,
                "since": _iso(run["first"]),
                "reason": run.get("reason"),
                "last": _iso(run["last"]),
                "observations": int(run.get("records") or 1),
            }
        )
    blind_bodies = sorted(set(tried) - {row.get("body") for row in rows if row.get("body")})
    unattributed = []
    for body in blind_bodies:
        run = tried.get(body) or {}
        unattributed.append(
            {
                "hook": body.rsplit("/", 1)[-1],
                "row": "(unregistered)",
                "body": body,
                "since": _iso(run.get("first")),
                "reason": run.get("reason"),
                "last": _iso(run.get("last")),
                "observations": int(run.get("records") or 1),
            }
        )
    # A fresh stamp with no fresh ledger record: SOMETHING failed open in the last
    # window and the throttle/rotation hid which body. Counted as one unattributed
    # degradation, never as 0 — "a guard is blind and we do not know which" is not healthy.
    stamp_fresh = stamp is not None and (now - stamp) <= BLIND_WITHIN
    entries = items + unattributed
    if stamp_fresh and not entries:
        entries.append(
            {
                "hook": "(unknown)",
                "row": "(unknown)",
                "body": "(unattributed)",
                "since": _iso(stamp),
                "reason": "a hook degraded and the ledger names no current body (throttled or rotated)",
                "last": _iso(stamp),
                "observations": 1,
            }
        )
    return {
        "blind": len(entries),
        "total": len(rows) if not registry_error else None,
        "rows_armed": len(rows),
        "items": entries,
        "ledger": str(ledger_path()),
        "ledger_state": ledger_state or "read",
        "registry_state": registry_error or "read",
        "degradation_seen": stamp_fresh or bool(tried),
        "last_degradation": _iso(stamp),
    }


# ---------------------------------------------------------------------------------------
# The ONE view
# ---------------------------------------------------------------------------------------


def _denied_roots(record: dict) -> list[dict]:
    """The FAILING roots out of the recorded matrix (``errno`` not ``ok``)."""
    matrix = record.get("matrix")
    out: list[dict] = []
    if not isinstance(matrix, list):
        return out
    for entry in matrix:
        if not isinstance(entry, dict):
            continue
        errno = entry.get("errno")
        if errno in (None, "", "ok", "Ok", "OK"):
            continue
        out.append(
            {
                "path": entry.get("path"),
                "op": entry.get("op"),
                "errno": errno,
                "tier": entry.get("tier"),
            }
        )
    return out


def blind_guards(*, now: Optional[float] = None, config_path: Any = None) -> dict:
    """The CURRENT fail-open degradation set, folded against this home's registry.

    ``{blind, total, items, ...}`` where ``blind`` counts armed ROWS whose body has a
    degradation record inside ``BLIND_WITHIN``, ``total`` counts the armed rows in the
    home's ``config.yaml`` (``None`` when that file cannot be read: unknown is not zero),
    and ``items`` is ``[{hook, row, body, since, reason, last, observations}]``.

    This is the shape ``substrate_access.record()`` folds into the episode record, so the
    authority record, the acting card, ``hermes doctor`` and both HTTP surfaces all read
    the same numbers.
    """
    return _blind_rows(float(now if now is not None else time.time()), config_path)


def read(
    *,
    record_path: Any = None,
    config_path: Any = None,
    now: Optional[float] = None,
    stale_after: float = STALE_AFTER,
) -> dict:
    """The single normalized view of the substrate, for every mirror. Never raises.

    ``state`` is one of ``ok`` / ``denied`` / ``unknown``; ``unknown`` always carries a
    ``reason`` and, when a record exists, ``last_known_state`` + its age so a reader can
    tell "never recorded" from "recorded a denial, then went quiet".

    ``guards_blind`` is the larger of the live ledger fold and the count the record itself
    carries (see :func:`_reconcile_guards`), so the mirror can neither hide a guard that
    just went blind nor contradict the card a human is reading.
    """
    stamp = float(now if now is not None else time.time())
    path = status_path(record_path)
    record, error = _read_record(path)
    age = _age(record.get("recorded_at"), stamp) if record else None

    if error == "absent":
        state, reason = "unknown", "record_absent"
    elif error and error.startswith("unparseable"):
        state, reason = "unknown", "record_unparseable"
    elif error:
        state, reason = "unknown", "record_unreadable"
    elif age is not None and age > stale_after:
        state, reason = "unknown", "record_stale"
    elif age is None:
        state, reason = "unknown", "record_unstamped"
    else:
        state, reason = ("denied" if record.get("yielded") else "ok"), None

    last_known = None
    if record:
        if record.get("yielded"):
            last_known = "denied"
        elif record.get("cleared_at"):
            last_known = "cleared"
        else:
            last_known = "admitted"

    denied = _denied_roots(record)
    guards = _blind_rows(stamp, config_path)
    blind, guard_items, guards_total, guards_source = _reconcile_guards(
        state, record, guards
    )
    held = record.get("held")
    held_list = held if isinstance(held, list) else []

    view = {
        "state": state,
        "reason": reason,
        "detail": _detail(state, reason, error, age, stale_after),
        "generated_at": _iso(stamp),
        "record": {
            "path": str(path),
            "present": bool(record),
            "error": error,
            "age_s": age,
            "stale": bool(age is not None and age > stale_after),
            "last_known_state": last_known,
            "version": record.get("version"),
            "recorded_at": _iso(record.get("recorded_at")),
        },
        "door": record.get("door"),
        "episode": record.get("episode"),
        "sig": record.get("sig"),
        "since": _iso(record.get("first_tick")),
        "last_tick": _iso(record.get("last_tick")),
        "streak": record.get("streak"),
        "threshold": record.get("threshold"),
        "duration_s": record.get("duration_s"),
        "cleared_at": _iso(record.get("cleared_at")),
        "held": [str(item) for item in held_list][:RENDERED_ROOTS],
        "held_count": len(held_list),
        "roots_denied": len(denied),
        "roots": denied[:RENDERED_ROOTS],
        "guards_blind": blind,
        "guards_total": guards_total,
        "guards": guard_items,
        "guards_source": guards_source,
        "guards_ledger_blind": int(guards.get("blind") or 0),
        "guards_ledger": guards["ledger"],
        "guards_state": {
            "ledger": guards["ledger_state"],
            "registry": guards["registry_state"],
            "last_degradation": guards["last_degradation"],
        },
        "read_findings": [str(item) for item in (record.get("read_findings") or [])][:RENDERED_ROOTS],
    }
    return view


def _reconcile_guards(state: str, record: dict, live: dict) -> tuple[int, list, Any, str]:
    """The blind-guard count a MIRROR may state, reconciled with the authority record.

    Two sources, one answer, and the rule is "never fewer blind guards than either source
    says":

    * the LIVE fold of the ledger (fresh: a guard that went blind 5 s ago is counted now);
    * the count the authority record itself carries (``guards_blind``), which is what the
      acting card and the dispatch handoff quote.

    Taking the larger keeps the mirror from contradicting the card a human is reading —
    "2 of 5 guards blind" on the card and "0" on the dashboard is a defect even when the
    guard recovered in between. Taking the live fold keeps a just-degraded guard visible
    before the next tick rewrites the record. A record that is absent/stale/unparseable
    contributes nothing (``unknown`` is not a claim).
    """
    live_blind = int(live.get("blind") or 0)
    live_items = list(live.get("items") or [])
    if state == "unknown":
        return live_blind, live_items, live.get("total"), "ledger"
    recorded = record.get("guards_blind")
    recorded_items = record.get("guards")
    if isinstance(recorded, (list, tuple)):
        # The LEG A build carried the ROWS in ``guards_blind`` and wrote no ``guards`` key.
        # The list's length is the count it claims and the rows are its detail: the same
        # guarantee, one older shape (see ``kanban_db_dispatch._blind_guard_count``).
        if not (isinstance(recorded_items, (list, tuple)) and recorded_items):
            recorded_items = recorded
        recorded = len(recorded)
    if isinstance(recorded, bool) or not isinstance(recorded, int) or recorded < 0:
        return live_blind, live_items, live.get("total"), "ledger"
    if recorded >= live_blind:
        total = record.get("guards_total")
        if total is None:
            total = live.get("total")
        items = [row for row in (recorded_items or []) if isinstance(row, dict)] \
            if isinstance(recorded_items, (list, tuple)) else []
        return recorded, items, total, "record"
    return live_blind, live_items, live.get("total"), "ledger"


def _detail(state: str, reason: Optional[str], error: Optional[str], age: Optional[float],
            stale_after: float) -> str:
    if state == "denied":
        return "a tick could not reach its substrate and is admitting nothing"
    if state == "ok":
        return f"the last tick reached every root it probed ({age:.0f}s ago)" if age is not None else "admitted"
    if reason == "record_absent":
        return "no tick has ever recorded a verdict (the record does not exist)"
    if reason == "record_unreadable":
        return f"the record exists but cannot be read ({error})"
    if reason == "record_unparseable":
        return f"the record is not the JSON document the gate writes ({error})"
    if reason == "record_stale":
        return (
            f"the last verdict is {age:.0f}s old: a healthy tick rewrites the record at least "
            "every 60 s, so no door has run since then — the substrate may well be readable, "
            f"but nothing has measured it (stale after {stale_after:.0f}s)"
        )
    if reason == "record_unstamped":
        return "the record carries no readable timestamp, so its freshness cannot be judged"
    return "unknown"


def summary(view: Optional[dict] = None, **kwargs: Any) -> dict:
    """The low-sensitivity half of the view: what a public probe may carry.

    No host paths, no root names, no pids — ``/api/status`` is in ``PUBLIC_API_PATHS``,
    and a gated bind serves it to any unauthenticated caller.
    """
    payload = view if view is not None else read(**kwargs)
    return {
        "state": payload.get("state"),
        "reason": payload.get("reason"),
        "episode": payload.get("episode"),
        "since": payload.get("since"),
        "held_count": payload.get("held_count"),
        "roots_denied": payload.get("roots_denied"),
        "guards_blind": payload.get("guards_blind"),
        "guards_total": payload.get("guards_total"),
        "generated_at": payload.get("generated_at"),
    }


def render_line(view: dict) -> str:
    """One deterministic sentence, for a log line or a card body."""
    guards = ""
    total = view.get("guards_total")
    if view.get("guards_blind"):
        guards = f", {view['guards_blind']} of {total if total is not None else '?'} guard(s) blind"
    return (
        f"substrate {view.get('state')}"
        + (f" ({view.get('reason')})" if view.get("reason") else "")
        + f": {view.get('detail')}"
        + guards
    )


__all__ = [
    "BLIND_WITHIN",
    "LEDGER_FILENAME",
    "STALE_AFTER",
    "STATUS_FILENAME",
    "blind_guards",
    "fleet_home",
    "ledger_path",
    "local_home",
    "read",
    "render_line",
    "stamp_path",
    "status_path",
    "summary",
]
