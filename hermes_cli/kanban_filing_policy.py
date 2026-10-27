"""The filing budget — the pre-create policy check at the kernel's ONE filing door.

The estate admits work faster than it can land it, and every producer that filed a card did
so through a surface that had no reason to know the ratio. So the budget is enforced where a
row is actually born — ``kanban_db.create_task`` — rather than in each producer module: a
producer that calls the CLI, the tool verb or the store API cannot opt out by wiring, and
"ungoverned" stops being the state a new surface gets by doing nothing.

WHAT THIS MODULE IS NOT. It carries no roster, no band table, no window and no counter: it is
mechanism. The roster of surfaces, the precedence bands and the pool's own instrument are
DATA, read from a policy file:

    {"surfaces": {"<created_by>": {"class": "governed" | "exempt", ...}, ...},
     "precedence": {"1": {...}, "2": {...}, ...},
     "unknown_creator": {"class": "governed", "precedence": 5},
     "pool": {"instrument": "/abs/path/to/instrument.py",
              "config": "/abs/path/to/its/config.json",
              "births": "count_births", "merges": "count_merges",
              "window_hours": 24, "repos": [...], "budget_s": 20}}

A HOST WITH NO POLICY FILE IS THE PRE-EXISTING UPSTREAM BEHAVIOUR, byte for byte: the hook
returns ``None`` before it does anything else, nothing is printed, no event key is written,
and no module outside the kernel is imported. That is what makes this change upstream-able.
What "nothing" costs on an inert host, stated accurately: :func:`policy_path` runs one env
lookup and two ``isfile`` stats per create, so the hook is not free, only behaviourally
inert (no reading, no event key, no estate import).

THE POOL IS ONE COUNTER, NOT A PER-SURFACE ALLOWANCE. A surface does not get a budget; it
draws from the pool. The pool is the measured landings — `merges_last_24h` over the declared
repos and the same 24 h window, read from the estate's EXISTING instrument (the rate-matched
door's ``count_merges``), never from a second counter kept here. The births side is that same
instrument's own ``count_births``, so the two numbers a refusal prints are the two numbers
the door's verdict already compares. Whatever the instrument says is what the pool is; this
module neither divides it, ratchets it, nor trends it.

PRECEDENCE INSIDE THE POOL. The bands are the policy's, in the ruling's order: keyed
remediation (costs nothing — the kernel's idempotency answers it before this hook runs), the
SEV1/alarm class (exempt, never budgeted), defects, designs, then notices and everything
else. A create is refused when the pool is spent FOR ITS BAND: the budget is spent in band
order, so a lane's notices can never strand a defect, and an earlier band is never refused
because a later one over-filed. Surfaces with class ``governed`` draw; an ``exempt`` row is
never refused (it still states its reading so the audit can see it).

AN IDENTITY IS ONLY REFUSED IF THE POLICY NAMES ITS CLASS. The kernel's idempotency
early-return only answers a row whose status is not ``archived``, so before this leg a
producer that ticks on a schedule re-filed the same identity the moment its card closed —
measured on the board as 344 rows over 133 keys with 211 of them excess on the
``pr-disposition:`` family alone. A ledger of closures nobody consults is not a dedupe, so
the door reads the BOARD: when the create carries an idempotency key and no LIVE row holds
it but a CLOSED row created inside the window does, the filing is refused and the refusal
NAMES the duplicate (``duplicate-closed-key``, with the closed card's id and status in the
line and the record).

BUT A KEY IS NOT ALWAYS AN EPISODE. Most keyed families on the estate name a CONDITION that
legitimately recurs — ``failure:<signature>``, ``dirty-shared-tree|<tree>``,
``landing-refused:<repo>#<n>`` — and their producers treat a refusal as "not filed", so a
leg that refused them would silence live faults rather than dedupe an episode (measured at
the first review: 1,199 condition-shaped re-filings against 6 episode-shaped, and the
estate's own filer split — PR #2273 — decides the same way). So the leg is bounded by DATA:
a key belongs to the CLASS before its first ``:`` or ``|``, and only a class the policy
declares under ``closure_leg.identities`` is ever refused. A key class the policy does not
name is ADMITTED exactly as a host with no closure leg admits it, a key with no separator
has no class at all so nothing can declare it, and a policy that declares nothing refuses
nothing — fail-open, the same stance as the unconfigured-host case below. The declared set
is read from the file on every create, so adding or retiring a family is a data edit.

The caller that genuinely means to re-open a DECLARED identity says so — ``resurrect=true``
— and is then judged by the pool like any other birth. The check is a board read, not an
instrument read: it works when the pool is unmeasurable, and it cannot refuse the exempt
classes above.

A REFUSAL IS AN ANSWER, NOT AN EXCEPTION. The ruling is explicit: the hook does not raise, it
does not file a card explaining that it refused, and it does not drop the create silently. It
returns :class:`FilingRefusal` — a ``str`` whose value IS the printed line

    refused: budget spent (births=N merges=M window=24h surface=<created_by> precedence=<n>)

or, for a re-filed identity,

    refused: duplicate-closed-key (key=K closed=t_x status=archived window=24h surface=S
    precedence=n; pass resurrect=true to file it again)

so a caller that prints the answer names the refusal, and a caller that treats an answer as
an id gets a string that is visibly not one. The line is also written to stderr (and to the
``kanban_filing_policy`` log channel) at the moment of the refusal, so a producer that reads
only its own output still sees it. Refusals NEVER consume the pool's budget twice and never
write a row.

FAIL-OPEN WHERE THE INSTRUMENT IS CONCERNED, AND VISIBLE. A policy that cannot be read, an
instrument that cannot be imported, a counter that raises or times out: none of that may take
a filing down or silently refuse the estate's work — the create is ADMITTED and the record
carries ``{"status": "unmeasured", "error": ...}``, which the ``created`` event writes and a
warning prints once per process. The same split the priority policy uses: the unconfigured
case is silent, the unusable case is recorded. A MEASURED refusal is the only refusal.

TRUSTED LOCAL CODE, STATED RATHER THAN PRETENDED. The instrument is a path the policy names
and it is imported and called IN PROCESS on the filing path (the house precedent: a filing is
on every lane's hot path, so no subprocess, no sandbox). ``sys.path`` is restored after the
call, so the estate's own ``scripts/`` directory cannot shadow a kernel import for the rest of
the process.

THE MOMENT. The hook runs after the idempotency early-return (a keyed refresh of an identity
the board already holds is not a birth and never draws on the pool) and before any write, so a
refused filing leaves the database untouched. Its order inside the hook is: policy read → the
declared identity classes (DATA) → exempt classes (never refused) → the closure-dedupe leg
(a board fact, no instrument needed, and only for a key whose class the policy declares) →
the pool reading (the only part that can be unmeasurable, and the only part that fails open).

A KNOWN ASYMMETRY, STATED RATHER THAN HIDDEN. :func:`_consumed` attributes existing births by
THE SURFACE's roster precedence while the decision uses the create's RAISED band, so a
band-raised create (a defect filed by the design lane) is judged against a pool whose spend
side excludes its own surface's history. It can only admit more, never refuse more, and it has
no effect while the earlier bands are over-spent; it is left as measured rather than quietly
"fixed" into a second attribution rule.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("kanban.filing_policy")

#: Explicit path to a policy file. Set by a probe, a test, or a host that keeps its policy
#: somewhere other than the two defaults below.
ENV_POLICY = "HERMES_FILING_POLICY"

#: The defaults, relative to the Hermes root. A host with neither file has no policy and the
#: hook is inert.
DEFAULT_NAMES = ("filing_policy.json", os.path.join("config", "filing_policy.json"))

#: The name the ``created`` event carries the consulted policy's record under.
EVENT_KEY = "filing_policy"

#: The band a create is judged in when neither its surface nor its own words say otherwise.
FALLBACK_PRECEDENCE = 5

#: The refusal line's one format. Exported so a reader (and a test) can match it exactly
#: instead of re-typing it; a second format would be a second contract.
REFUSAL_FORMAT = ("refused: budget spent (births=%d merges=%d window=%dh surface=%s "
                  "precedence=%d)")

#: The OTHER refusal a create can meet, and its own format: a keyed identity whose live rows
#: are gone but whose CLOSED row is still inside the window. Distinct on purpose — a producer
#: told "budget spent" when the truth is "you already filed this" would wait for landings that
#: would never let it through.
DUPLICATE_FORMAT = ("refused: duplicate-closed-key (key=%s closed=%s status=%s window=%dh "
                    "surface=%s precedence=%d; pass resurrect=true to file it again)")

#: The statuses that mean "this row is closed" to the dedupe leg. ``done`` never reaches the
#: hook at all (the kernel's early return answers any non-archived row), so ``archived`` is the
#: status this actually refuses today; ``done`` is listed because the leg must be right about
#: the board, not about the kernel's early return.
CLOSED_STATUSES = ("done", "archived")

#: The separators that END an identity key's class token: ``pr-disposition:<ref>`` is an
#: episode of the class ``pr-disposition``; ``dirty-shared-tree|<tree>`` is an observation of
#: the class ``dirty-shared-tree``. A key carrying neither separator has NO class, so no policy
#: can declare it and the closure leg can never refuse it.
IDENTITY_SEPARATORS = (":", "|")

#: The policy block that declares which identity classes the closure leg may refuse on. ABSENT,
#: ``null`` or empty is a policy that declares nothing — and a policy that declares nothing
#: refuses nothing. The fail-open direction is deliberate: the pathological failure of this leg
#: is a live fault filed under a recurring key never reaching the board.
DECLARED_BLOCK = "closure_leg"

#: The key inside :data:`DECLARED_BLOCK` holding the classes.
DECLARED_KEY = "identities"

#: The record's note when the leg stood aside because the key's class is not declared. Named
#: on the card so an audit can tell "admitted because the pool had room" from "admitted
#: because this family is not an episode".
UNDECLARED = "undeclared-identity"


def identity_class(key: Optional[str]) -> Optional[str]:
    """The identity CLASS a key belongs to: the token before its first separator, or ``None``.

    ``pr-disposition:yaan-platform#2268`` → ``pr-disposition``;
    ``dirty-shared-tree|/opt/x|abc123`` → ``dirty-shared-tree``;
    ``plain-key`` → ``None`` (no class, so nothing can declare it).
    """
    text = str(key or "").strip()
    cuts = [text.find(sep) for sep in IDENTITY_SEPARATORS if sep in text]
    if not cuts or min(cuts) <= 0:
        return None
    return text[:min(cuts)].strip() or None


def declared_identities(policy: dict) -> dict:
    """The identity classes this policy lets the closure leg refuse on: ``{class: row}``.

    DATA, never a list in this module. Accepted shapes are a mapping (``{"pr-disposition":
    {...}}``) or a plain list of class names; a declaration written with its separator
    (``"pr-disposition:"``) is normalised to the class token, so the data author does not have
    to know which spelling the parser reads. Anything else declares nothing.
    """
    block = policy.get(DECLARED_BLOCK)
    if not isinstance(block, dict):
        return {}
    rows = block.get(DECLARED_KEY)
    if isinstance(rows, (list, tuple)):
        rows = {str(name): {} for name in rows}
    if not isinstance(rows, dict):
        return {}
    declared: dict = {}
    for name, row in rows.items():
        token = str(name).strip()
        for sep in IDENTITY_SEPARATORS:
            token = token.rstrip(sep).strip()
        if token:
            declared[token] = row if isinstance(row, dict) else {}
    return declared


class FilingRefusal(str):
    """The refusal ANSWER: its value is the line the caller prints and records.

    A ``str`` on purpose. ``create_task`` is id-shaped for every one of its callers, and the
    ruling forbids raising out of a filing, so the refusal has to travel as a value. Being a
    ``str`` means a caller that prints it names the refusal verbatim; being FALSY means
    ``if not task_id:`` — the idiom a caller already writes — catches it, and a caller that
    skips that check still gets a string that cannot be mistaken for a card id.
    """

    __slots__ = ("record",)
    refused = True

    def __new__(cls, line: str, record: Optional[dict] = None):
        obj = super().__new__(cls, line)
        obj.record = record or {}
        return obj

    def __bool__(self) -> bool:  # a refusal is never a successful filing
        return False

    def __repr__(self) -> str:  # keep logs honest: never quote it as a task id
        return "FilingRefusal(%s)" % str.__repr__(self)


def is_refusal(value: Any) -> bool:
    """Is ``value`` a refusal answer (as opposed to a card id)?"""
    return isinstance(value, FilingRefusal)


def policy_path(root: Optional[str] = None) -> Optional[str]:
    """The policy file this host is governed by, or ``None``.

    ``HERMES_FILING_POLICY`` wins when it names a readable file; otherwise the two defaults
    under the Hermes root are tried in order. A path that does not exist is not an error —
    "this host has no policy" is the pre-existing behaviour, not a broken configuration.
    """
    explicit = (os.environ.get(ENV_POLICY) or "").strip()
    if explicit:
        return explicit if os.path.isfile(explicit) else None
    base = Path(root) if root else _hermes_root()
    for name in DEFAULT_NAMES:
        candidate = base / name
        if candidate.is_file():
            return str(candidate)
    return None


def _hermes_root() -> Path:
    from hermes_constants import get_default_hermes_root

    return Path(get_default_hermes_root())


def load(path: str) -> tuple[Optional[dict], Optional[str]]:
    """``(policy, error)`` — the parsed file, or the one line saying why it cannot be used."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            policy = json.load(fh)
    except Exception as exc:  # unreadable, not JSON, a directory: all "cannot be used"
        return None, "%s: %s" % (type(exc).__name__, exc)
    if not isinstance(policy, dict):
        return None, "policy is %s, not an object" % type(policy).__name__
    return policy, None


# --- the roster and the bands ---------------------------------------------------------------

def _surface_row(policy: dict, created_by: Optional[str]) -> tuple[str, dict]:
    """``(surface, row)`` for the filing lane. An unknown creator is GOVERNED, never exempt."""
    surface = (created_by or "").strip() or "unknown"
    rows = policy.get("surfaces") or {}
    row = rows.get(surface)
    if isinstance(row, dict):
        return surface, row
    fallback = policy.get("unknown_creator")
    if not isinstance(fallback, dict):
        fallback = {"class": "governed", "precedence": FALLBACK_PRECEDENCE}
    # The row keeps the creator it was asked about, so the refusal names the real surface.
    return surface, dict(fallback)


def _expired(row: dict, today: Optional[str] = None) -> bool:
    """Has this exemption's re-justification date passed? (ISO date, compared as a date.)"""
    expiry = str(row.get("expires") or "").strip()
    if not expiry:
        return False
    stamp = today or time.strftime("%Y-%m-%d")
    return expiry < stamp


def _band_for(policy: dict, row: dict, title: Optional[str], body: Optional[str]) -> tuple[int, Optional[dict]]:
    """``(band, spec)`` for a create: the lowest matching band, else the surface's own.

    The card's own words can RAISE its precedence (a defect filed by the design lane is a
    defect), never lower it: a marker for a later band is ignored when the surface already
    sits earlier in the order.

    A band declares what it looks for as ``markers`` (case-insensitive substrings) and/or
    ``patterns`` (regular expressions), and where it looks with ``scope``: ``title`` (the
    default - the card's own declaration of what it is) or ``text`` (title and body, the form
    the estate's SEV1 declaration already takes). The band table is DATA: a pattern that the
    estate already owns is mirrored into it and pinned by the estate's own suite, never
    re-invented here.
    """
    bands = policy.get("precedence") or {}
    full = "%s\n%s" % (title or "", body or "")
    floor = int(row.get("precedence") or policy.get("default_precedence") or FALLBACK_PRECEDENCE)
    ordered: list[tuple[int, dict]] = []
    for key, spec in bands.items():
        number = _number(key)
        if number is not None:
            ordered.append((number, spec if isinstance(spec, dict) else {}))
    for number, spec in sorted(ordered):
        scope = full if str(spec.get("scope") or "title").lower() == "text" else (title or "")
        haystack = scope.casefold()
        markers = [str(m).casefold() for m in (spec.get("markers") or []) if str(m).strip()]
        if markers and any(marker in haystack for marker in markers):
            return (min(number, floor) if floor else number), spec
        for pattern in spec.get("patterns") or []:
            try:
                if re.search(str(pattern), scope):
                    return (min(number, floor) if floor else number), spec
            except re.error:
                # A pattern the policy cannot compile is the policy's problem, not the
                # filing's: skip THAT band and keep judging the others (a malformed band must
                # not become a hole a later band's marker slips through... it is skipped, and
                # the record's ``policy`` path lets an auditor find the bad row).
                continue
    own = bands.get(str(floor))
    return floor, own if isinstance(own, dict) else {}


def _number(key) -> Optional[int]:
    try:
        return int(key)
    except (TypeError, ValueError):
        return None


def _costs_nothing(spec: dict) -> bool:
    return bool(spec.get("zero_cost"))


def _exempt_band(spec: dict) -> bool:
    return bool(spec.get("exempt"))


# --- the pool: the estate's own instrument --------------------------------------------------

_INSTRUMENT_CACHE: dict[str, Any] = {}


def _load_instrument(path: str):
    """Import the instrument named by the policy (cached per path, per process)."""
    cached = _INSTRUMENT_CACHE.get(path)
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location("_filing_pool_instrument", path)
    if spec is None or spec.loader is None:
        raise ImportError("no importable module at %s" % path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _INSTRUMENT_CACHE[path] = module
    return module


def _reading(policy: dict, now: Optional[float] = None) -> tuple[Optional[dict], Optional[str]]:
    """``(reading, error)`` — the pool, measured by the declared instrument.

    The reading is ``{"births", "births_by_surface", "merges", "window_hours", "repos"}``.
    Every failure is a NAMED error rather than a zero: a pool of 0 births would refuse the
    estate, and a pool of 0 merges would refuse it too — an unreadable door is not a budget.
    """
    pool = policy.get("pool")
    if not isinstance(pool, dict):
        return None, "policy has no 'pool' block"
    path = str(pool.get("instrument") or "").strip()
    config = str(pool.get("config") or "").strip()
    if not path or not config:
        return None, "policy's 'pool' block names no instrument and config"
    window_hours = int(pool.get("window_hours") or policy.get("window_hours") or 24)
    since = int(now or time.time()) - window_hours * 3600
    births_fn = str(pool.get("births") or "count_births")
    merges_fn = str(pool.get("merges") or "count_merges")
    before = list(sys.path)
    try:
        module = _load_instrument(path)
        cfg = module.load_config(config)
        budget = None
        if hasattr(module, "Budget"):
            budget = module.Budget(float(pool.get("budget_s") or 20.0))
        births = getattr(module, births_fn)(since, config=cfg, budget=budget)
        names = [str(n) for n in (pool.get("repos") or []) if str(n).strip()]
        if not names:
            for flow in (cfg.get("flows") or {}).values():
                for name in (flow or {}).get("watch") or []:
                    if name not in names:
                        names.append(str(name))
        if not names:
            return None, "no watched repos: neither the policy nor the instrument declares any"
        merges = getattr(module, merges_fn)(since, names, cfg, budget=budget)
    except Exception as exc:  # a broken instrument admits the filing, loudly (see docstring)
        return None, "%s: %s" % (type(exc).__name__, exc)
    finally:
        sys.path[:] = before
    if "births_by_surface" not in births or not isinstance(births.get("births_by_surface"), dict):
        # THE SPEND SIDE MUST BE ATTRIBUTED, OR THE POOL IS UNMEASURABLE. An instrument that
        # counts births but cannot say WHO filed them would make every band look unspent, and
        # the door would admit the estate while printing a ratio it never applied. A missing
        # breakdown is a broken instrument (a stale deploy of the counter, most likely), not a
        # pool of zero - so it fails like every other unmeasurable reading: admitted, named.
        return None, ("the instrument's %s returns no births_by_surface: the pool's spend side "
                      "cannot be attributed to a surface" % births_fn)
    return {
        "births": int(births.get("births") or 0),
        "births_by_surface": dict(births.get("births_by_surface") or {}),
        "merges": int(merges.get("merges") or 0),
        "window_hours": window_hours,
        "repos": names,
        "refs": merges.get("refs"),
    }, None


def _consumed(policy: dict, reading: dict, band: int) -> int:
    """Births the pool's budget has already paid for, at or before ``band``.

    Exempt surfaces (the operator's own filing is not estate throughput) and zero-cost bands
    (keyed remediation — which the kernel answers before this hook anyway) never draw.
    """
    total = 0
    by_surface = reading.get("births_by_surface") or {}
    for surface, count in by_surface.items():
        row = (policy.get("surfaces") or {}).get(surface) or policy.get("unknown_creator") or {}
        if str(row.get("class") or "governed") == "exempt":
            continue
        spec = (policy.get("precedence") or {}).get(str(int(row.get("precedence") or FALLBACK_PRECEDENCE))) or {}
        if _exempt_band(spec) or _costs_nothing(spec):
            continue
        if int(row.get("precedence") or FALLBACK_PRECEDENCE) <= band:
            total += int(count or 0)
    return total


# --- a keyed identity that was already closed ------------------------------------------------

def _dedupe_window(policy: dict) -> int:
    """The window the closure leg looks back over: the policy's own, in hours."""
    pool = policy.get("pool")
    if not isinstance(pool, dict):
        pool = {}
    return int(policy.get("window_hours") or pool.get("window_hours") or 24)


def _closed_duplicate(conn, key: str, since: float) -> Optional[dict]:
    """The CLOSED row that already holds ``key`` inside the window, or ``None``.

    A board read, deliberately: the ledger of closures is the board itself, so the leg works
    on a host whose instrument is unreadable and needs no second store to keep in step.
    ``conn`` is the connection the row would land on, i.e. the destination board.
    """
    if conn is None:
        return None
    try:
        row = conn.execute(
            "SELECT id, status, created_at FROM tasks WHERE idempotency_key = ? "
            "AND status IN (%s) AND created_at >= ? "
            "ORDER BY created_at DESC LIMIT 1"
            % ",".join("?" for _ in CLOSED_STATUSES),
            (str(key), *CLOSED_STATUSES, int(since)),
        ).fetchone()
    except Exception as exc:  # a board read that cannot answer is NOT a duplicate
        log.warning("filing policy: duplicate-key lookup failed, admitting: %s", exc)
        return None
    if row is None:
        return None
    return {"closed": row[0], "status": row[1], "created_at": row[2]}


# --- the door -------------------------------------------------------------------------------

def evaluate(conn, *, created_by: Optional[str] = None, title: Optional[str] = None,
             body: Optional[str] = None, root: Optional[str] = None,
             now: Optional[float] = None, idempotency_key: Optional[str] = None,
             resurrect: bool = False) -> Optional[dict]:
    """The decision for one create: ``None`` when this host has no policy (inert), else a record.

    The record's ``status`` is the whole contract:

      ``admitted``   the filing proceeds; ``reason`` says why (``exempt``, ``sev1``,
                     ``under-pool``, ``disabled``);
      ``refused``    the pool is spent for the create's band, or the key's live rows are gone
                     and its CLOSED row is still inside the window (``reason`` is
                     ``budget-spent`` / ``duplicate-closed-key``; ``line`` is the printed refusal);
      ``unmeasured`` the policy or the instrument could not be read — ADMITTED, with ``error``.

    ``idempotency_key`` is the create's own dedup identity: with one, the closure leg asks the
    BOARD whether that identity was already born and closed inside the window — but only when
    the key's CLASS (the token before its first ``:``/``|``) is declared in the policy's
    ``closure_leg.identities``. An undeclared class is admitted and the record says so
    (``closure_leg_skipped: undeclared-identity``); a policy declaring nothing declares no
    class, so nothing is refused on a key. ``resurrect`` is the caller SAYING it means to
    re-open a DECLARED identity — the leg is skipped, and the filing is then judged by the
    pool like any other birth.

    ``conn`` is the board the row will land on: the closure leg reads it (the budget does not,
    it reads the estate's instrument).
    """
    path = policy_path(root)
    if not path:
        return None
    policy, error = load(path)
    if error or policy is None:
        return _unmeasured(path, error or "policy is not readable")
    if not bool(policy.get("enabled", True)):
        return {"status": "admitted", "reason": "disabled", "policy": path}

    surface, row = _surface_row(policy, created_by)
    surface_class = str(row.get("class") or "governed")
    band, spec = _band_for(policy, row, title, body)

    if surface_class == "exempt":
        record = _admitted("exempt", path, surface, band)
        record["exemption"] = {k: row.get(k) for k in ("owner", "why", "expires") if k in row}
        if _expired(row, None):
            # An exemption past its re-justification date is STILL honoured — refusing an
            # exempt surface is the failure this class exists to prevent — but it is stated,
            # so the estate's own gate can go red on the lapse instead of a filing being lost.
            record["exemption_expired"] = True
        return record
    if _exempt_band(spec):
        return _admitted("sev1", path, surface, band)

    # THE CLOSURE LEG. Before the pool, because it is a BOARD fact and not an instrument
    # reading: it must answer even when the instrument cannot, and it must not spend 1.2 s
    # importing one to learn that the identity was already filed and closed. It runs only for
    # a keyed identity whose CLASS the policy declares (see the module docstring): every other
    # key is a condition that recurs by design, or carries no class at all, and is admitted.
    reopened = None
    undeclared = False
    if idempotency_key:
        window_hours = _dedupe_window(policy)
        if identity_class(idempotency_key) not in declared_identities(policy):
            undeclared = True
        else:
            duplicate = _closed_duplicate(conn, idempotency_key,
                                          int(now or time.time()) - window_hours * 3600)
            if duplicate and not resurrect:
                state = {
                    "status": "refused", "reason": "duplicate-closed-key", "policy": path,
                    "surface": surface, "precedence": band,
                    "idempotency_key": str(idempotency_key),
                    "identity_class": identity_class(idempotency_key),
                    "closed_id": duplicate["closed"], "closed_status": duplicate["status"],
                    "closed_at": duplicate["created_at"], "window_hours": window_hours,
                }
                state["line"] = DUPLICATE_FORMAT % (idempotency_key, duplicate["closed"],
                                                    duplicate["status"], window_hours,
                                                    surface, band)
                state["why"] = ("idempotency key %r already born and closed as %s (%s) inside "
                                "the %dh window; pass resurrect=true to file it again"
                                % (idempotency_key, duplicate["closed"], duplicate["status"],
                                   window_hours))
                return state
            if duplicate:
                # The caller SAID it means to re-open the identity: the leg steps aside, the
                # filing is judged by the pool like any other birth, and the card records that
                # a closed row was deliberately superseded (an auditor reading the pool's
                # arithmetic should not have to guess why the same key appears twice inside
                # one window).
                reopened = duplicate

    reading, error = _reading(policy, now)
    if error or reading is None:
        state = _unmeasured(path, error or "the pool could not be measured", surface=surface,
                            band=band)
        if undeclared:
            state["closure_leg_skipped"] = UNDECLARED
        return state
    consumed = _consumed(policy, reading, band)
    state = {
        "status": "admitted" if consumed < reading["merges"] else "refused",
        "reason": "under-pool" if consumed < reading["merges"] else "budget-spent",
        "policy": path, "surface": surface, "precedence": band,
        "births": consumed, "merges": reading["merges"],
        "window_hours": reading["window_hours"], "births_total": reading["births"],
    }
    if undeclared:
        state["closure_leg_skipped"] = UNDECLARED
    if reopened:
        state["resurrected_from"] = {"id": reopened["closed"], "status": reopened["status"]}
    if consumed < reading["merges"]:
        return state
    state["line"] = REFUSAL_FORMAT % (consumed, reading["merges"], reading["window_hours"],
                                      surface, band)
    state["why"] = ("births in precedence bands <= %d have spent the pool the landings "
                    "measure (%d of %d; %d work identities in the window)"
                    % (band, consumed, reading["merges"], reading["births"]))
    return state


def _admitted(reason: str, path: str, surface: str, band: int) -> dict:
    return {"status": "admitted", "reason": reason, "policy": path,
            "surface": surface, "precedence": band}


def _unmeasured(path: str, error: str, *, surface: Optional[str] = None,
                band: Optional[int] = None) -> dict:
    record = {"status": "unmeasured", "policy": path, "error": str(error)}
    if surface is not None:
        record["surface"] = surface
    if band is not None:
        record["precedence"] = band
    return record


def apply(conn, *, created_by: Optional[str] = None, title: Optional[str] = None,
          body: Optional[str] = None, root: Optional[str] = None,
          now: Optional[float] = None, idempotency_key: Optional[str] = None,
          resurrect: bool = False) -> tuple[Optional[FilingRefusal], Optional[dict]]:
    """Consult the policy at the create door: ``(refusal, record)``.

    ``(None, None)`` is the inert host — nothing read, nothing printed, nothing recorded.
    ``(refusal, record)`` means the create MUST NOT proceed; ``record`` is the refusal's own
    (already printed here). ``(None, record)`` means admitted, with the record the ``created``
    event rides.
    """
    record = evaluate(conn, created_by=created_by, title=title, body=body, root=root, now=now,
                      idempotency_key=idempotency_key, resurrect=resurrect)
    if record is None:
        return None, None
    if record.get("status") == "refused":
        line = record["line"]
        print(line, file=sys.stderr)
        # INFO, not WARNING: the print above IS the contract, and a host that has a stderr
        # handler on the root logger would otherwise show a producer two identical lines.
        log.info("%s", line)
        return FilingRefusal(line, record), record
    if record.get("status") == "unmeasured":
        # Visible, never fatal (the priority policy's own rule): one warning per process.
        message = "filing policy unmeasured — admitted: %s" % record.get("error")
        print(message, file=sys.stderr)
        log.warning("%s", message)
    return None, record
