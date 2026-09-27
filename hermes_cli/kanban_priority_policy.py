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
"""

from __future__ import annotations

import importlib.util
import logging
import os
import re
from typing import Any, Callable, NamedTuple, Optional

__all__ = [
    "POLICY_KEY", "DEFAULT_FUNCTION", "PolicyError", "Verdict",
    "normalize_spec", "load_callable", "priority_for_create",
]

_log = logging.getLogger(__name__)

#: The ``board.json`` key a board names its policy under.
POLICY_KEY = "priority_policy"

#: The callable a spec means when it names no function.
DEFAULT_FUNCTION = "band_birth"

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

# (module path, function name) -> (callable, (mtime_ns, size)) of the file it came from.
# Cached because a filing happens far more often than a policy changes, and keyed with
# the file's stamp so an edited or redeployed policy is picked up on the next filing
# rather than at the next process start.
_CACHE: dict[tuple[str, str], tuple[Callable[..., Any], tuple[int, int]]] = {}


class PolicyError(RuntimeError):
    """A board's configured priority policy could not be used.

    Raised by the VALIDATORS - :func:`normalize_spec`, :func:`load_callable`, and the CLI
    and reader that call them - so bad wiring is refused while someone is looking at it.
    A FILING never sees one: :func:`priority_for_create` catches everything and reports an
    unusable policy as a verdict, because no create may fail on a policy's account.
    """


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


def load_callable(spec: dict) -> Callable[..., Any]:
    """The policy callable a normalised ``spec`` names, or raise :class:`PolicyError`.

    Loaded from the file itself by path, which is what makes a policy redeploy visible
    without a restart. A spec that has already been normalised is accepted as-is; the
    module is re-read whenever its stamp changes.
    """
    module_path = spec["module"]
    function_name = spec["function"]
    try:
        st = os.stat(module_path)
    except OSError as exc:
        raise PolicyError(
            "%s.module %r cannot be read (%s)" % (POLICY_KEY, module_path, exc)
        ) from exc
    stamp = (st.st_mtime_ns, st.st_size)
    key = (module_path, function_name)
    cached = _CACHE.get(key)
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
    fn = getattr(module, function_name, None)
    if not callable(fn):
        raise PolicyError(
            "%s.module %r has no callable %r" % (POLICY_KEY, module_path, function_name)
        )
    _CACHE[key] = (fn, stamp)
    return fn


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
