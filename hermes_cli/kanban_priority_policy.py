"""Board-scoped priority policy: the priority a card is BORN with, from its routes.

A card's priority is decided at birth, not corrected afterwards. The kernel asks the
board's configured policy for the value at the INSERT, so a row never exists outside
its band and no filing surface - the tool verb, the CLI, a decomposer, another lane's
agent - can place a card somewhere else. Correcting a card after the fact means there
is a window in which the wrong value is what every reader sees, and it needs a sweep
that has to re-derive what the right value was.

WHO OWNS THE NUMBERS. This module carries no band table and no clause: it is
mechanism. A board names its policy in ``board.json``:

    "priority_policy": {"module": "/abs/path/to/policy.py", "function": "band_birth"}

and this module loads that callable through ``importlib.util.spec_from_file_location``
and calls it as ``fn(requested, assignee, board, title, body)``. The callable's record is
what the card is born with: the kernel stores ``record["applied"]`` and attaches the
record VERBATIM to the ``created`` event under the ``priority_policy`` key, and only when
``record["clamped"]`` says the value moved - a card the policy left alone keeps today's
byte-identical event. A policy's extra keys (``tier``, ``qualification``, ...) are the
provenance the operator needs to read a banding decision back; they ride inside that one
key, which is why a policy cannot collide with a key the kernel writes on the event.

WHY A FILE PATH. A path is read from disk on each filing, so a policy that is edited or
redeployed takes effect without a restart of whatever called ``create_task``, and a
board can name a copy the fleet deploys as a unit. There is no package-name form: a
dotted name would resolve against the *caller's* ``sys.path``, which differs between the
gateway, a CLI invocation and a test run - the same board would then get different
policies depending on who filed the card. The path must be absolute and must exist;
a board's wiring is never resolved relative to a working directory.

FAIL-OPEN, ALWAYS. A policy that is configured but cannot be used - missing file, import
error, no such callable, a raise inside the callable, a record with no usable ``applied``
- must never take a filing down with it: the card keeps the caller's priority, one warning
per broken policy is logged per process, and the ``created`` event carries
``{"status": "unavailable", "error": "<one line>"}`` so the broken wiring is VISIBLE
instead of silent. A policy that is merely not configured is a legitimate state, not an
error: unset and blank are today's behaviour exactly - no warning, no event key.

That split is the point. A report that only prints cannot tell "nothing to do" from
"never ran", so the unconfigured case stays quiet and the unusable case is recorded on the
card itself. Nothing here is allowed to raise out of a filing.

The callable is host-local and written by the fleet's own tooling, and it is loaded and
called IN PROCESS: no subprocess, no thread with a timeout, no sandbox. A filing is on the
hot path of every lane, and a policy is code the operator wired deliberately - the honest
boundary is "trusted local code", stated rather than pretended.

A MATCHING READER IS NOT A POLICY. This module decides nothing about what the number
means; two boards may name two different policies and that is intended - one wiring point
per board, and the board is the thing that owns its bands.

THE DOMAIN IS THE KERNEL'S, THE BANDS ARE THE FLEET'S. This module also carries the one
set of numbers the kernel itself owns: the ordinary domain a card may be filed in, the
reserved tranche only a designation may reach, and the clamp that brings an out-of-domain
value back inside. They live here - not in a board's policy file and not in the kernel -
because they bound EVERY board and EVERY policy, and a bound that lives in a per-board
policy is a bound that board can disagree with. A policy still owns its own bands; this
module owns only the outer edge those bands sit inside.

THE FLOOR IS THE BOARD'S, READ THROUGH ITS OWN POLICY. A board wired with a floor trims
its ordinary domain to its top band: no card on it may sit below the floor, and the
reserved tranche above stays designation-only. The floor is either a value inside the
ordinary domain (``-1000..900000``) or exactly ``TRANCHE_FLOOR`` (990000) - the ONE
reserved-tranche value a board may name as its floor, so a top board can trim its ordinary
domain to the tranche boundary without opening the tranche itself. On such a board a row AT
the floor is admitted without a marker (the board's own declaration is the evidence), while
every other value in the reserved tranche stays designation-only, on every board. The number
is NOT here and NOT in the kernel - it is the board's own policy module's
``band_floor(board)``, so the fleet's band module both lifts a card to the floor at birth and
answers the kernel's refusals with the same number. Nothing is paraphrased: one number, one
home. This module only asks (:func:`board_floor`); the doors act on the answer:

* door 2, a deliberate re-rank: below the floor is REFUSED, and the refusal names the floor;
* door 3, the armed storage guard: a raw write that CHANGES a value to below the floor is
  aborted in the database itself, and the floor is baked into the trigger at ARM time, so a
  board whose floor changed is re-armed by its wiring step rather than by a redeploy;
* door 5, a revoke: the value the designation ledger recorded is clamped to what the board
  allows AFTER the revocation, so releasing a designation can never abort on the number the
  ledger happens to hold.

``band_floor`` is OPTIONAL in a policy module, and absent answers ``None``: no floor, every
door inert, byte-identical to the behaviour before the clause existed. A module that defines
it but cannot answer with a usable number is UNUSABLE WIRING, and is as loud as every other
unusable policy here - because a floor that failed to load would otherwise be
indistinguishable from a board that never had one.
"""

from __future__ import annotations

import importlib.util
import logging
import os
import re
from typing import Any, Callable, NamedTuple, Optional

__all__ = [
    "POLICY_KEY", "DEFAULT_FUNCTION", "FLOOR_FUNCTION", "PolicyError", "PriorityOutOfDomain",
    "Verdict", "ORDINARY_MIN", "ORDINARY_MAX", "MAX_BAND_TOP", "TRANCHE_FLOOR", "TRANCHE_TOP",
    "DESIGNATED_PRIORITY", "MAX_PRIORITY",
    "in_ordinary", "in_tranche", "board_is_wired", "domain_reason", "clamp_to_domain",
    "normalize_spec", "load_module", "load_callable", "priority_for_create", "board_floor",
]

_log = logging.getLogger(__name__)

#: The ``board.json`` key a board names its policy under.
POLICY_KEY = "priority_policy"

#: The callable a spec means when it names no function.
DEFAULT_FUNCTION = "band_birth"

#: The OPTIONAL callable a policy module may define to state its board's FLOOR: the lowest
#: priority a card on that board may hold. Absent means "no floor here" - the state of every
#: policy written before the clause existed, and of every board that never opted in.
FLOOR_FUNCTION = "band_floor"

#: Policy locations already warned about, so a broken policy on a board says so ONCE per
#: process instead of once per card: a filing loop must not turn a wiring mistake into a
#: log flood.
_WARNED: set[str] = set()


class Verdict(NamedTuple):
    """What a card is born with, and what the ``created`` event should carry.

    ``applied`` is the stored priority. ``record`` is the value for the event's
    ``priority_policy`` key, or ``None`` when the event must keep today's shape.
    """

    applied: int
    record: Optional[dict]

_FUNCTION_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_MODULE_NAME_RE = re.compile(r"[^A-Za-z0-9_]")

# A policy module's path -> (module object, (mtime_ns, size)) of the file it came from.
# Cached because a filing happens far more often than a policy changes, and keyed with the
# file's stamp so an edited or redeployed policy is picked up on the next filing rather than
# at the next process start. ONE cache for the MODULE rather than one per callable: the birth
# callable and the floor reader must be attributes of the SAME copy, or a redeploy landing
# between the two reads would band a card by one policy and place its floor by another.
_MODULES: dict[str, tuple[Any, tuple[int, int]]] = {}


class PolicyError(RuntimeError):
    """A board's configured priority policy could not be used.

    Raised by the VALIDATORS - :func:`normalize_spec`, :func:`load_callable`, and the CLI
    and reader that call them - so bad wiring is refused while someone is looking at it.
    A FILING never sees one: :func:`priority_for_create` catches everything and reports an
    unusable policy as a verdict, because no create may fail on a policy's account.
    """


class PriorityOutOfDomain(ValueError):
    """A value the domain does not allow, raised by the doors that must REFUSE.

    The edit door raises this instead of writing, so a caller's bug is not hidden behind a
    silent re-rank. The create door never raises it: a filing is clamped and recorded,
    because a card that never lands is worse than a card that lands at the domain edge.
    """


#: The ordinary domain. Every ordinary write - create, edit, a sweep, a lane's own re-rank -
#: lands inside it. ``900000`` is the highest band top in the fleet, so it is the highest a
#: card may be filed at without a designation.
ORDINARY_MIN = -1000
ORDINARY_MAX = 900000
#: The highest band top, in the band table's own vocabulary.
MAX_BAND_TOP = ORDINARY_MAX
#: The reserved tranche, and the ONE value inside it a designation may assign.
TRANCHE_FLOOR = 990000
TRANCHE_TOP = 999999
DESIGNATED_PRIORITY = TRANCHE_FLOOR
#: Nothing sits above this, and only the designation door reaches it.
MAX_PRIORITY = TRANCHE_TOP


def in_ordinary(value: Any) -> bool:
    """Is *value* inside the ordinary domain (``-1000..900000``)?"""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return False
    return ORDINARY_MIN <= number <= ORDINARY_MAX


def in_tranche(value: Any) -> bool:
    """Is *value* inside the reserved tranche (``990000..999999``)?"""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return False
    return TRANCHE_FLOOR <= number <= TRANCHE_TOP


def domain_reason(value: int) -> str:
    """One line naming why *value* is not an ordinary priority - the refusal/record text."""
    if in_tranche(value):
        return ("the reserved tranche %d-%d is designated, never requested"
                % (TRANCHE_FLOOR, TRANCHE_TOP))
    if value > ORDINARY_MAX:
        return "above the maximum band (%d)" % MAX_BAND_TOP
    return "below the documented floor (%d)" % ORDINARY_MIN


def clamp_to_domain(applied: int, record: Optional[dict], floor: Optional[int] = None) -> tuple:
    """``(priority, record)`` with ``priority`` inside the ordinary domain.

    The create door's half of the domain. A value outside the ordinary domain is brought to
    the domain edge and the clamp is merged into the record the ``created`` event carries
    under ``priority_policy``, because a clamp the filer cannot see is how a board fills with
    values no guard ever produced. An in-domain value comes back with the record untouched,
    so a card the policy placed correctly keeps today's byte-identical event.

    ``floor`` is the board's declared floor (``kanban_priority_policy.board_floor``), when the
    caller has it, and it buys exactly ONE value the domain would otherwise clamp: a ``floor``
    ON the tranche boundary (:data:`TRANCHE_FLOOR`) admits a value equal to it, because the
    board's own declaration is the evidence and the policy's lift at birth must survive the
    clamp. A floor inside the ordinary domain changes nothing here (such a value is in-domain
    already), and every other out-of-domain value is still clamped and recorded. Defaulting
    ``floor`` to ``None`` keeps every caller that has no floor byte-identical.
    """
    value = int(applied)
    if in_ordinary(value):
        return value, record
    if floor is not None and value == int(floor) == TRANCHE_FLOOR:
        return value, record
    clamped = ORDINARY_MAX if value > ORDINARY_MAX else ORDINARY_MIN
    merged = dict(record or {})
    merged["applied"] = clamped
    merged["clamped"] = True
    merged["domain"] = {
        "requested": value,
        "applied": clamped,
        "bounds": [ORDINARY_MIN, ORDINARY_MAX],
        "reason": domain_reason(value),
    }
    return clamped, merged


def board_is_wired(spec: Any) -> bool:
    """Does this raw ``priority_policy`` value name a policy at all?

    ``None`` and blank are the UNWIRED case - the key is absent, or the wiring action wrote ``""``
    to clear it - and every door in the domain stays inert for such a board. A dict counts as
    wired even when it is malformed: "configured but unusable" must not be read as "not
    configured", or the domain would quietly switch itself off on a typo.
    """
    if spec is None:
        return False
    if isinstance(spec, str):
        return bool(spec.strip())
    return True


def _module_name(path: str) -> str:
    """A stable, filename-free module name for ``path`` (unique per absolute path)."""
    safe = _MODULE_NAME_RE.sub("_", os.path.basename(path)) or "policy"
    return "_kanban_priority_policy_%s_%d" % (safe, abs(hash(os.path.abspath(path))) % (10 ** 12))


def normalize_spec(raw: Any) -> Optional[dict]:
    """``{module, function}`` for a board's raw ``priority_policy`` value, or ``None``.

    ``None``/``null``/blank means the board has no policy and the caller's value stands
    (the inert case - see the module docstring). A string is shorthand for
    ``{"module": <string>}``. Anything else - including a dict with no module - is
    malformed and raises: a configured key that cannot be understood must not be read as
    "no policy", because that downgrade is invisible.

    Shape and existence are validated here (so a writer can refuse a spec before it is
    stored); whether the callable itself loads is :func:`load_callable`'s question.
    """
    if raw is None:
        return None
    if isinstance(raw, str):
        if not raw.strip():
            # Blank: the board set nothing here, so this is the inert case exactly - the
            # same answer as an absent key, and deliberately not an error to shout about.
            return None
        raw = {"module": raw}
    if not isinstance(raw, dict):
        raise PolicyError(
            "board %s must be an object like {\"module\": \"/abs/path/policy.py\", "
            "\"function\": \"band_birth\"} or null, got %r" % (POLICY_KEY, type(raw).__name__)
        )
    module = str(raw.get("module") or "").strip()
    function = str(raw.get("function") or "").strip() or DEFAULT_FUNCTION
    if not module:
        raise PolicyError("%s names no module (set %s.module)" % (POLICY_KEY, POLICY_KEY))
    if not _FUNCTION_RE.match(function):
        raise PolicyError(
            "%s.function %r is not a callable name" % (POLICY_KEY, function)
        )
    if not os.path.isabs(module):
        raise PolicyError(
            "%s.module must be an absolute path (got %r); a relative path would resolve "
            "against whichever working directory filed the card" % (POLICY_KEY, module)
        )
    if not module.endswith(".py"):
        raise PolicyError("%s.module must name a .py file (got %r)" % (POLICY_KEY, module))
    if not os.path.isfile(module):
        raise PolicyError("%s.module %r is not an existing file" % (POLICY_KEY, module))
    return {"module": module, "function": function}


def load_module(spec: dict) -> Any:
    """The policy MODULE a normalised ``spec`` names, or raise :class:`PolicyError`.

    Loaded from the file itself by path, which is what makes a policy redeploy visible
    without a restart; the module is re-read whenever its stamp changes. THE one loader:
    :func:`load_callable` and :func:`board_floor` both come through it, so a board's floor
    and the value it bands a filing to are always attributes of the same copy.
    """
    module_path = spec["module"]
    try:
        st = os.stat(module_path)
    except OSError as exc:
        raise PolicyError(
            "%s.module %r cannot be read (%s)" % (POLICY_KEY, module_path, exc)
        ) from exc
    stamp = (st.st_mtime_ns, st.st_size)
    cached = _MODULES.get(module_path)
    if cached is not None and cached[1] == stamp:
        return cached[0]
    try:
        file_spec = importlib.util.spec_from_file_location(_module_name(module_path), module_path)
        if file_spec is None or file_spec.loader is None:
            raise ImportError("no loader for %s" % module_path)
        module = importlib.util.module_from_spec(file_spec)
        file_spec.loader.exec_module(module)
    except Exception as exc:
        # Deliberately fatal: an unloadable policy means every card on the board would
        # land unbanned, and a silent fallback would hide it until someone audits.
        raise PolicyError(
            "%s.module %r could not be loaded (%s: %s)"
            % (POLICY_KEY, module_path, exc.__class__.__name__, exc)
        ) from exc
    _MODULES[module_path] = (module, stamp)
    return module


def load_callable(spec: dict) -> Callable[..., Any]:
    """The policy callable a normalised ``spec`` names, or raise :class:`PolicyError`."""
    module_path = spec["module"]
    function_name = spec["function"]
    fn = getattr(load_module(spec), function_name, None)
    if not callable(fn):
        raise PolicyError(
            "%s.module %r has no callable %r" % (POLICY_KEY, module_path, function_name)
        )
    return fn


def board_floor(spec: Any, board: str = "") -> Optional[int]:
    """The floor ``board`` may not sit below, or ``None`` when that board has none.

    Resolved through the SAME route the birth decision uses - the board's ``priority_policy``
    module, loaded by path - so the lift at birth and the refusals at doors 2, 3 and 5 read
    ONE number from ONE home and cannot disagree. A floor a policy module states and an
    ``ORDINARY_MAX`` the kernel owns are different things: this is the board's own lower bound.

    ``None`` is the ordinary state - "no floor here" - and it has three routes, all inert:

    * the board carries no policy at all (an unwired board, or a blank key);
    * the policy module does not define ``band_floor`` (every policy written before the
      clause existed, and the fleet's band module on a board absent from its own table);
    * the module defines it and answers ``None`` for this board.

    A module that defines the reader and CANNOT answer a usable number is UNUSABLE WIRING and
    raises :class:`PolicyError`: the value must be an ``int`` inside the ordinary domain
    (``ORDINARY_MIN..ORDINARY_MAX``) or exactly :data:`TRANCHE_FLOOR` (990000) - the one
    reserved-tranche value a board may declare as its floor. Nothing else is accepted: the
    900001..989999 gap, the rest of the tranche (990001..999999) and anything above it stay
    refused. It is deliberately not a silent ``None``, because a floor that failed to load reads
    exactly like a board that never had one, and the doors are where a wrong number would land a
    card.
    """
    if not board_is_wired(spec):
        return None
    policy = normalize_spec(spec)
    if policy is None:
        return None
    where = "%s.%s" % (policy["module"], FLOOR_FUNCTION)
    fn = getattr(load_module(policy), FLOOR_FUNCTION, None)
    if fn is None:
        # Optional by design: absent means no floor, not broken wiring.
        return None
    if not callable(fn):
        raise PolicyError("%s.%s is not callable in %r"
                          % (POLICY_KEY, FLOOR_FUNCTION, policy["module"]))
    try:
        raw = fn(board or "")
    except Exception as exc:
        raise PolicyError("%s raised %s: %s"
                          % (where, exc.__class__.__name__, exc)) from exc
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise PolicyError("%s returned %r, expected an integer or None" % (where, raw))
    if not (in_ordinary(raw) or raw == TRANCHE_FLOOR):
        # The refusal names WHICH bound the value crossed, so the wiring author is told what to
        # do (move it into the domain, or name the tranche boundary exactly) rather than just
        # that it is wrong.
        if ORDINARY_MAX < raw < TRANCHE_FLOOR:
            crossed = ("%d sits in the gap between the ordinary top (%d) and the reserved "
                       "tranche floor (%d)" % (raw, ORDINARY_MAX, TRANCHE_FLOOR))
        elif raw > TRANCHE_FLOOR:
            crossed = ("%d is inside the reserved tranche (%d..%d) but is not its floor %d"
                       % (raw, TRANCHE_FLOOR, TRANCHE_TOP, TRANCHE_FLOOR))
        else:
            crossed = "%d is below the ordinary domain's floor (%d)" % (raw, ORDINARY_MIN)
        raise PolicyError(
            "%s returned %d, which is not a usable floor: a floor must be inside the ordinary "
            "domain (%d..%d) or exactly the tranche boundary %d - %s"
            % (where, raw, ORDINARY_MIN, ORDINARY_MAX, TRANCHE_FLOOR, crossed)
        )
    return int(raw)


def _where(policy: Optional[dict], spec: Any) -> str:
    """The module a warning should name, best effort - the error must identify the wiring."""
    if policy:
        return "%s:%s" % (policy["module"], policy["function"])
    if isinstance(spec, str) and spec.strip():
        return spec.strip()
    if isinstance(spec, dict) and str(spec.get("module") or "").strip():
        return str(spec["module"]).strip()
    return POLICY_KEY


def _unavailable(requested: int, where: str, exc: Exception) -> Verdict:
    """The fail-open verdict: keep the caller's value, record why, warn once per process.

    The record names the WIRING as well as the failure, so a filer reading the card's own
    ``created`` event can see what to repair without going to the log - the log says it
    once per process, the record says it on every card that policy failed to place.
    """
    one_line = " ".join(str(exc).split()) or exc.__class__.__name__
    if where not in _WARNED:
        _WARNED.add(where)
        _log.warning(
            "kanban priority policy %s is unusable (%s); cards on this board keep the "
            "priority their filer asks for until it is fixed or the key is unwired",
            where, one_line,
        )
    return Verdict(int(requested), {"status": "unavailable", "policy": where, "error": one_line})


def priority_for_create(
    requested: int, *, assignee: str = "", board: str = "", title: str = "",
    body: str = "", spec: Any = None,
) -> Optional[Verdict]:
    """The verdict for a card being created, or ``None`` when the board has no policy.

    ``None`` is the inert case: the caller keeps ``requested`` and attaches nothing to the
    ``created`` event, byte-identical to a board that never opted in.

    Otherwise the verdict's ``applied`` is what the card is born with, and ``record`` is
    what the event carries under ``priority_policy`` - the policy's own record, when (and
    only when) the value moved. THIS FUNCTION DOES NOT RAISE: a spec that cannot be
    normalised, a module that will not load, a callable that raises and a record with no
    usable ``applied`` all yield ``requested`` unchanged plus an ``unavailable`` record and
    one warning per broken policy per process, because no create may fail on a policy's
    account and an unusable policy must be visible rather than silent.

    The callable is invoked exactly once, positionally, as ``fn(requested, assignee, board,
    title, body)``, so a policy ranks on the routes it can see at birth - lane, board, the
    card's own declaration line - and on nothing that does not exist yet.
    """
    try:
        policy = normalize_spec(spec)
    except PolicyError as exc:
        return _unavailable(requested, _where(None, spec), exc)
    if policy is None:
        return None
    try:
        fn = load_callable(policy)
    except PolicyError as exc:
        return _unavailable(requested, _where(policy, spec), exc)
    where = _where(policy, spec)
    try:
        record = fn(requested, assignee, board, title, body)
    except Exception as exc:
        return _unavailable(requested, where, PolicyError(
            "raised %s: %s" % (exc.__class__.__name__, exc)))
    if not isinstance(record, dict):
        return _unavailable(requested, where, PolicyError(
            "returned %s, expected a record" % type(record).__name__))
    if "applied" not in record:
        return _unavailable(requested, where, PolicyError("returned no 'applied' value"))
    try:
        applied = int(record["applied"])
    except (TypeError, ValueError) as exc:
        return _unavailable(requested, where, PolicyError("non-integer 'applied': %s" % exc))
    asked = int(requested)
    if applied == asked:
        # Nothing moved, so the row and its event stay exactly as they are today: the key
        # appears only on a card the policy actually placed.
        return Verdict(applied, None)
    verbatim = dict(record)
    # The record is attached because the value moved, so the flag is a fact about the card
    # rather than a policy's self-description - filled in only if the record omitted it.
    verbatim.setdefault("clamped", True)
    return Verdict(applied, verbatim)
