"""Substrate admission --- the ONE probe both spawn doors ask before admitting work.

Card ``t_72e9bf03`` (design by ``platform-stl``), implemented by ``t_0ae49984``.

THE FAULT THIS EXISTS FOR (measured 2026-10-26, live on this host). ``/Volumes/yoyofs1``
returned ``EPERM`` on ``open()``/``readdir()`` while ``stat()``, ``os.path.exists()`` and
``os.path.isdir()`` all returned **OK**. Three consequences:

* **F1** every cheap liveness check is green during the fault, so any probe built on
  ``stat``/``exists``/``isdir`` is blind to it. **The probe must PERFORM the operation.**
* **F2** the fleet's health surface ran *from* the denied volume and reported 17/17 green.
* **F3** every sanctioned raise path (``escalate``, the escalation ledger, the operator
  ladder) lived on the denied volume, so a guard whose signal rides them is silent in
  exactly the episode that needs it. Hence invariant **I5**: the guard must not depend on
  any path it guards --- map, records, card, DM and log are all boot-volume.

WHAT THIS MODULE IS. One module, three doors, one signal:

* door 1 --- the dispatcher tick (``kanban_db_dispatch.dispatch_once``): a ``tier: global``
  denial refuses the whole tick;
* door 2 --- the cron tick (``cron.scheduler_tick``): same, before the advance so no fire is
  lost;
* door 3 --- per-unit admission (a card's resolved workspace, a cron job's own script and
  workdir): a denial holds THAT unit and siblings proceed (invariant **I2**).

DOCTRINE (ruling R3 in the design; do not soften any of these):

* a substrate denial gates ADMISSION, never ACCEPTANCE --- **I1**: no card is failed,
  blocked or failure-counted, no cron job takes ``last_status: error``, no workspace is
  created, no claim is written;
* **I3** fail-closed for workers (they spawn), fail-open for observers (hooks, mirrors,
  and this module's own map reader --- a broken map is a recorded finding, never a stop);
* **I4** ``indeterminate`` (``EMFILE``/``ENFILE``/``EINTR``/``ETIMEDOUT``/``EAGAIN``, and
  any errno this module has not classified) NEVER refuses. A guard that stops the fleet on
  a transient is worse than the condition it watches;
* **I6** a refusal is BOUNDED: one card, one DM, one log line per EPISODE. Streaks are
  recomputed every tick; delivery is not (the ``delivery`` block is the bound).

KILL SWITCH. ``HERMES_SUBSTRATE_GATE=off`` turns the guard into a RECORDER: it still probes
and still writes ``substrate_status.json``, and it refuses nothing. That is step 1 of the
rollback in the design, so it must not require a restart of anything else --- the env is
read per call, not at import.
"""

from __future__ import annotations

import errno as _errno
import hashlib
import itertools
import json
import logging
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

log = logging.getLogger(__name__)

#: --- the greppable names ---------------------------------------------------------------
KIND = "substrate_denied"
#: The one greppable string. ``str(SubstrateDenied(...))`` always starts with it, and every
#: log line the doors emit carries it, so ``grep substrate-denied-yield:`` finds the episode.
PREFIX = "substrate-denied-yield:"

GATE_ENV = "HERMES_SUBSTRATE_GATE"
GATE_OFF = frozenset({"off", "0", "false", "no", "disabled", "none"})

MAP_FILENAME = "substrate_roots.yaml"
STATUS_FILENAME = "substrate_status.json"
#: The map's own schema version, and the record's. The record is v2 (the streak/episode half
#: added by the skew gate); the substrate record carries the SAME schema plus
#: ``matrix``/``held``/``guards_blind``/``cleared_at``/``duration_s``.
MAP_VERSION = 1
SCHEMA_VERSION = 2

#: Steady-state cost: N roots, one open + one readdir each, reused for 15 s. A 60 s
#: dispatcher tick and a 60 s cron tick therefore cost ~N syscalls each, not 2N.
FRESH_WITHIN = 15.0

#: Consecutive refused ticks for ONE signature before the acting card is filed. Inherited
#: verbatim from the skew gate (``TICK_YIELD_ACTOR_THRESHOLD``): one refusal is churn, three
#: in a row is a stall.
THRESHOLD = 3

#: The probe's writable scratch directory, inside each root the map gives ``w``. It is created
#: once and carries a ``.gitignore`` of ``*`` so a prod tree's ``git status`` stays clean while
#: the probe writes there every 15 s.
PROBE_DIRNAME = ".substrate-probe"

#: Classification. DENIED is the design's measured set; INDETERMINATE is the transient set.
#: An errno in NEITHER set is treated as indeterminate (I4, fail open) and logged.
DENIED_ERRNOS = frozenset(
    {"EPERM", "EACCES", "EROFS", "ENOENT", "ESTALE", "EIO", "ENOTDIR", "ENOSPC", "EDQUOT"}
)
INDETERMINATE_ERRNOS = frozenset({"EMFILE", "ENFILE", "EINTR", "ETIMEDOUT", "EAGAIN"})

#: DM pair per episode (invariant I5: boot-resident gateway, never ``escalate``).
DEFAULT_DM_TARGETS = ("yoyodine/default", "yoyodine/platform-stl")
DM_TIMEOUT_S = 30

#: Bounded read fan-out for picking a file to ``open()`` on a root that declares no sentinel.
_READ_FANOUT = 64
_probe_seq = itertools.count(1)


# ---------------------------------------------------------------------------------------
# Readings and the report
# ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class RootSpec:
    """One root the map declares: where it is, what operation it must support, and its tier.

    ``tier`` is the blast-radius control and it is **data**, not code --- a mis-scoped root is
    a one-line map change:

    * ``global`` --- every spawn needs it (boot-resident essentials only). A denial refuses the
      whole tick;
    * ``unit``   --- a specific unit's own root; a denial holds THAT unit, siblings proceed;
    * ``advisory`` --- nothing spawns into it; a denial is recorded, never refuses anything.
    """

    path: str
    caps: tuple[str, ...]
    tier: str
    owner: str
    label: str
    #: Optional known file (relative to ``path``) to ``open()``. When empty the read probe picks
    #: the first regular file it meets (bounded fan-out), so every ``r`` root still performs a
    #: real ``open(2)`` --- which is the whole point (F1).
    sentinel: str = ""


@dataclass(frozen=True)
class RootReading:
    """The outcome of ONE operation on ONE root.

    ``errno`` is the NAME (``EPERM``), never the number: it is the thing the matrix, the
    signature and a human all read.
    """

    path: str
    op: str
    ok: bool
    errno: Optional[str]
    ms: float
    tier: str

    @property
    def verdict(self) -> str:
        """``ok`` / ``denied`` / ``indeterminate`` --- the classification, in one word."""
        if self.ok or not self.errno:
            return "ok"
        if self.errno in DENIED_ERRNOS:
            return "denied"
        return "indeterminate"

    @property
    def denied(self) -> bool:
        return self.verdict == "denied"

    def as_matrix_row(self) -> dict:
        """The row the record and the acting card quote."""
        return {
            "path": self.path,
            "op": self.op,
            "errno": "ok" if self.ok else self.errno,
            "tier": self.tier,
        }


@dataclass
class ProbeReport:
    """One probe pass: when it ran, every reading it took, and whether it was cached."""

    at: float
    readings: list[RootReading] = field(default_factory=list)
    cached: bool = False
    findings: list[str] = field(default_factory=list)
    """Named non-fatal problems from the pass itself (``map_unreadable: ...``). A finding is
    never a refusal --- but it is never silent either (I3)."""

    @property
    def denied(self) -> list[RootReading]:
        """Every reading that is a DENIAL. Empty is the healthy answer."""
        return [r for r in self.readings if r.denied]

    @property
    def indeterminate(self) -> list[RootReading]:
        """Transient failures. Logged, counted as evidence, NEVER a refusal (I4)."""
        return [r for r in self.readings if r.verdict == "indeterminate"]

    def held_by_tier(self, tier: str) -> list[RootReading]:
        """The denials belonging to one tier --- what a ``global`` door asks for."""
        return [r for r in self.denied if r.tier == tier]

    @property
    def denied_global(self) -> list[RootReading]:
        return self.held_by_tier("global")

    def matrix(self) -> list[dict]:
        return [r.as_matrix_row() for r in self.readings]

    def summary(self) -> str:
        """One deterministic sentence for a log line or a DM body."""
        bad = self.denied
        if not bad:
            return "substrate readable on every declared root"
        causes: dict[str, int] = {}
        for r in bad:
            causes[f"{r.errno}@{r.op}"] = causes.get(f"{r.errno}@{r.op}", 0) + 1
        parts = ", ".join(f"{k} x{v}" for k, v in sorted(causes.items()))
        return f"{len(bad)} denied reading(s) on {len({r.path for r in bad})} root(s): {parts}"


class SubstrateDenied(Exception):
    """A ``tier: global`` root denied an operation: the tick must not admit work.

    Sibling of the tree-skew refusal --- same episode/streak/delivery machinery, different
    ``kind`` --- so a refused tick is never a silent one and never a false green.
    """

    kind = KIND

    def __init__(self, report: ProbeReport, *, door: str = "dispatcher") -> None:
        self.report = report
        self.door = door
        self.signature = signature(report)
        super().__init__(
            f"{PREFIX} {report.summary()}; refusing to admit work "
            f"(signature {self.signature or 'unknown'}, door {door})."
        )


# ---------------------------------------------------------------------------------------
# The map --- boot-volume data, fail-open
# ---------------------------------------------------------------------------------------


@dataclass
class RootsMap:
    """The parsed map. ``specs`` is what the probe walks; ``findings`` says what went wrong."""

    path: str
    specs: list[RootSpec] = field(default_factory=list)
    findings: list[str] = field(default_factory=list)
    hold_catchup_hours: float = 6.0


def hermes_home() -> Path:
    """``<HERMES_HOME>``, else ``~/.hermes``. Never raises.

    Resolved through :func:`hermes_constants.get_default_hermes_root`, NOT through the ambient
    ``HERMES_HOME``, for the same reason ``kanban_home()`` is: this record, this map and this
    episode are FLEET-wide, and a profile-scoped process (``HERMES_HOME=.../profiles/<name>``)
    must still read and write the ONE authority on the launch home. Two homes would mean two
    streaks, two episodes and two cards for one stall.
    """
    try:
        from hermes_constants import get_default_hermes_root

        return Path(get_default_hermes_root())
    except Exception:  # noqa: BLE001 - a tree without the constant keeps working
        return Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes"))


def map_path(path: Any = None) -> Path:
    """The map's location, or the explicit one a caller (or a test) supplies."""
    if path is not None:
        return Path(path)
    return hermes_home() / MAP_FILENAME


def status_path(path: Any = None) -> Path:
    """The AUTHORITY record's location, or the explicit one a caller supplies."""
    if path is not None:
        return Path(path)
    return hermes_home() / STATUS_FILENAME


_TIERS = ("global", "unit", "advisory")


def _spec_from_row(row: Any, findings: list[str], *, origin: str) -> Optional[RootSpec]:
    """One map row -> :class:`RootSpec`, or ``None`` with a finding. Tolerant by design."""
    if not isinstance(row, dict):
        findings.append(f"map_row_not_a_mapping: {row!r}")
        return None
    path = str(row.get("path") or "").strip()
    if not path:
        findings.append(f"map_row_without_path: {origin}")
        return None
    caps = row.get("caps") or ["r"]
    if isinstance(caps, str):
        caps = [caps]
    caps = tuple(str(c).strip().lower() for c in caps if str(c).strip())
    caps = tuple(c for c in caps if c in ("r", "w"))
    if not caps:
        findings.append(f"map_row_without_caps: {path}")
        return None
    tier = str(row.get("tier") or "unit").strip().lower()
    if tier not in _TIERS:
        # An unknown tier is the ONE scope mistake that could silently widen the blast radius,
        # so it degrades to the NARROWEST tier and says so.
        findings.append(f"map_row_unknown_tier: {path}={tier!r} -> unit")
        tier = "unit"
    return RootSpec(
        path=path,
        caps=caps,
        tier=tier,
        owner=str(row.get("owner") or "platform"),
        label=str(row.get("label") or Path(path).name or path),
        sentinel=str(row.get("sentinel") or "").strip(),
    )


def roots(path: Any = None) -> RootsMap:
    """Read and validate the map. **Fail-open**: a broken map returns no specs plus a named
    finding --- it never stops the fleet, and it is never silent (design §3.1.3, I3)."""
    target = map_path(path)
    result = RootsMap(path=str(target))
    try:
        raw = target.read_text(encoding="utf-8")
    except OSError as exc:
        result.findings.append(f"map_unreadable: {target}: {_errno_name(exc) or exc}")
        return result
    try:
        import yaml  # local import: a tree without PyYAML must not fail to import this module

        doc = yaml.safe_load(raw)
    except Exception as exc:  # noqa: BLE001 - a broken map is a finding, never a raise
        result.findings.append(f"map_unparseable: {target}: {exc.__class__.__name__}: {exc}")
        return result
    if doc is None:
        result.findings.append(f"map_empty: {target}")
        return result
    if not isinstance(doc, dict):
        result.findings.append(f"map_not_a_mapping: {type(doc).__name__}")
        return result
    version = doc.get("version")
    if version is not None and str(version) != str(MAP_VERSION):
        result.findings.append(f"map_version_unknown: {version!r} (reader is v{MAP_VERSION})")
    try:
        result.hold_catchup_hours = float(doc.get("hold_catchup_hours") or 6.0)
    except (TypeError, ValueError):
        result.findings.append(
            f"map_hold_catchup_hours_unreadable: {doc.get('hold_catchup_hours')!r}"
        )
    rows = doc.get("roots")
    if not isinstance(rows, list):
        result.findings.append("map_without_roots_list")
        return result
    for row in rows:
        spec = _spec_from_row(row, result.findings, origin=str(target))
        if spec is not None:
            result.specs.append(spec)
    if not result.specs:
        result.findings.append("map_declares_no_probeable_root")
    for finding in result.findings:
        log.warning("substrate: %s", finding)
    return result


# ---------------------------------------------------------------------------------------
# The probe --- an operation, never metadata (F1)
# ---------------------------------------------------------------------------------------


def _errno_name(exc: BaseException) -> Optional[str]:
    """The errno's NAME, or ``None`` for a non-OSError. Never raises."""
    code = getattr(exc, "errno", None)
    if code is None:
        return None
    return _errno.errorcode.get(int(code), f"E{code}")


def _read_op(root: str, spec: RootSpec) -> None:
    """``open(2)`` a file on the root **and** take one ``readdir`` step.

    Both halves matter: the measured fault denied each independently, and a probe that only did
    one of them would be blind to the other. ``stat``/``exists``/``isdir`` are never the signal
    here --- they are green during this fault (F1).
    """
    first_file: Optional[str] = None
    if spec.sentinel:
        first_file = os.path.join(root, spec.sentinel)
    with os.scandir(root) as it:  # the readdir syscall itself
        for idx, entry in enumerate(it):
            if first_file is not None or idx >= _READ_FANOUT:
                break
            try:
                if entry.is_file(follow_symlinks=False):
                    first_file = entry.path
                    break
            except OSError:
                continue
    if first_file is not None:
        with open(first_file, "rb") as fh:  # the open(2) syscall
            fh.read(1)


def _write_op(root: str) -> None:
    """``O_CREAT|O_EXCL`` a scratch file under ``<root>/.substrate-probe/`` and unlink it.

    The directory is created once and carries ``.gitignore`` = ``*`` so a prod checkout stays
    clean to ``git status`` while the probe writes there every 15 s.
    """
    scratch = os.path.join(root, PROBE_DIRNAME)
    if not os.path.isdir(scratch):
        try:
            os.makedirs(scratch, exist_ok=True)
            with open(os.path.join(scratch, ".gitignore"), "w", encoding="utf-8") as fh:
                fh.write("*\n")
        except OSError:
            # The probe below is what reports the failure -- deliberately not swallowed here.
            pass
    name = os.path.join(scratch, f"{os.getpid()}-{next(_probe_seq)}")
    fd = os.open(name, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        os.close(fd)
    finally:
        try:
            os.unlink(name)
        except OSError:
            pass


def probe(spec: RootSpec) -> list[RootReading]:
    """Perform the spec's declared operations. One reading per capability; **never raises**.

    An operation this module does not know is skipped, not guessed at.
    """
    readings: list[RootReading] = []
    for op in spec.caps:
        started = time.perf_counter()
        failure: Optional[str] = None
        try:
            if op == "r":
                _read_op(spec.path, spec)
            elif op == "w":
                _write_op(spec.path)
            else:
                continue
        except OSError as exc:
            failure = _errno_name(exc) or f"E{exc.errno}"
        readings.append(
            RootReading(
                path=spec.path,
                op=op,
                ok=failure is None,
                errno=failure,
                ms=round((time.perf_counter() - started) * 1000.0, 3),
                tier=spec.tier,
            )
        )
    return readings


#: Per-(path, op, tier) reading cache. The TTL is what keeps two 60 s tickers at ~N syscalls.
_CACHE: dict[tuple[str, str, str], tuple[float, RootReading]] = {}


def _cached_probe(
    spec: RootSpec, *, fresh_within: float, use_cache: bool
) -> tuple[list[RootReading], bool]:
    readings: list[RootReading] = []
    all_cached = True
    now = time.monotonic()
    for op in spec.caps:
        key = (spec.path, op, spec.tier)
        hit = _CACHE.get(key) if use_cache else None
        if hit is not None and (now - hit[0]) <= fresh_within:
            readings.append(hit[1])
            continue
        all_cached = False
        one = RootSpec(spec.path, (op,), spec.tier, spec.owner, spec.label, spec.sentinel)
        for reading in probe(one):
            _CACHE[key] = (now, reading)
            readings.append(reading)
    return readings, all_cached


def clear_cache() -> None:
    """Drop the TTL cache (tests, and an operator forcing a fresh reading)."""
    _CACHE.clear()


def probe_all(
    tier: Optional[str] = None,
    *,
    path: Any = None,
    fresh_within: float = FRESH_WITHIN,
    use_cache: bool = True,
) -> ProbeReport:
    """Probe every root the map declares (optionally one tier only). Never raises.

    The map's findings ride on the report so the signal can carry them: a broken map is
    ``denied=[]`` (fail open) with a named finding, never a refusal.
    """
    declared = roots(path)
    specs = [s for s in declared.specs if tier is None or s.tier == tier]
    readings: list[RootReading] = []
    cached_flags: list[bool] = []
    for spec in specs:
        got, was_cached = _cached_probe(spec, fresh_within=fresh_within, use_cache=use_cache)
        readings.extend(got)
        cached_flags.append(was_cached)
    report = ProbeReport(
        at=time.time(),
        readings=readings,
        cached=bool(cached_flags) and all(cached_flags),
        findings=list(declared.findings),
    )
    for reading in report.indeterminate:
        log.warning(
            "substrate: indeterminate reading %s op=%s errno=%s -- NOT a refusal (I4)",
            reading.path, reading.op, reading.errno,
        )
    return report


def unit_substrate(
    paths: Iterable[Any],
    *,
    caps: Sequence[str] = ("r", "w"),
    tier: str = "unit",
    fresh_within: float = FRESH_WITHIN,
    use_cache: bool = True,
) -> ProbeReport:
    """Probe a UNIT's own roots (a card's resolved workspace, a job's script/workdir).

    Ad-hoc specs, not map rows: door 3 asks about paths the map cannot know (a worktree is
    minted per card). Same classification, same cache, same fail-open rules.
    """
    readings: list[RootReading] = []
    cached_flags: list[bool] = []
    seen: set[tuple[str, str]] = set()
    for raw in paths:
        if not raw:
            continue
        text = str(raw)
        if (text, tier) in seen:
            continue
        seen.add((text, tier))
        spec = RootSpec(
            path=text, caps=tuple(caps), tier=tier, owner="platform",
            label=Path(text).name or text,
        )
        got, was_cached = _cached_probe(spec, fresh_within=fresh_within, use_cache=use_cache)
        readings.extend(got)
        cached_flags.append(was_cached)
    return ProbeReport(
        at=time.time(),
        readings=readings,
        cached=bool(cached_flags) and all(cached_flags),
    )


def signature(report: ProbeReport) -> str:
    """``sha256(<sorted "path\\top\\terrno" of every DENIED reading>)[:16]``.

    **Aggregate by cause, not by count** (design §3.6): two ticks that deny the same roots are
    one episode however many roots there are, and three distinct causes are three signatures.
    Stable across pids and times --- nothing about *when* or *who* is in it. Empty string when
    nothing is denied (a signature of "nothing" would be a lie).
    """
    rows = sorted(f"{r.path}\t{r.op}\t{r.errno}" for r in report.denied)
    if not rows:
        return ""
    return hashlib.sha256("\n".join(rows).encode("utf-8")).hexdigest()[:16]


def episode_identity(sig: str, first_tick: float) -> str:
    """The episode: the signature plus the window's start (the skew gate's own rule)."""
    return f"{sig}:{int(first_tick)}"


# ---------------------------------------------------------------------------------------
# The kill switch
# ---------------------------------------------------------------------------------------


def gate_enabled() -> bool:
    """``False`` when ``HERMES_SUBSTRATE_GATE`` is off: probe and record, refuse nothing.

    Read PER CALL, deliberately: the rollback must not require a restart of anything but the
    process that reads it.
    """
    return (os.environ.get(GATE_ENV) or "").strip().lower() not in GATE_OFF


def refusal_for(report: ProbeReport, *, tier: str = "global") -> Optional[list[RootReading]]:
    """The denial a door must refuse on, or ``None``.

    The kill switch and I4 live HERE, so no door can get either rule wrong.
    """
    if not gate_enabled():
        return None
    held = report.held_by_tier(tier)
    return held or None


# ---------------------------------------------------------------------------------------
# The ONE record (authority)
# ---------------------------------------------------------------------------------------


def _read_json(path: Path) -> dict:
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return doc if isinstance(doc, dict) else {}


def read_status(path: Any = None) -> dict:
    """The authority record as a dict, or ``{}``. Unreadable is "no prior episode"."""
    return _read_json(status_path(path))


def _write_status(payload: dict, path: Any = None) -> str:
    """Atomic write of the authority record. Best effort, never silent."""
    target = status_path(path)
    try:
        from utils import atomic_json_write

        target.parent.mkdir(parents=True, exist_ok=True)
        atomic_json_write(target, payload, mode=0o600)
        return str(target)
    except Exception as exc:  # noqa: BLE001 - an unwritable record must not take a tick down
        log.warning("substrate: could not write the record to %s: %s", target, exc)
        return f"(record unwritable: {exc})"


def merge_delivery(delivery: dict, path: Any = None) -> str:
    """Record a delivery attempt on the CURRENT episode's authority record.

    Deliberately NOT routed through :func:`record`: that would re-derive the streak, and a second
    write inside one refusal would count the same tick twice. A record that vanished is left alone
    and the returned text says so.
    """
    target = status_path(path)
    prior = _read_json(target)
    if not prior.get("yielded"):
        return "(no record to carry the delivery)"
    prior["delivery"] = delivery
    return _write_status(prior, target)


def _iso_epoch(value: Any) -> Optional[float]:
    """``2026-10-26T15:08:12-0400`` -> epoch. ``None`` when unparseable (never raises)."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        import datetime

        return datetime.datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


def guards_blind(since: Optional[float] = None) -> dict:
    """The fail-open hook degradations, folded into the episode (design §3.5 / §6).

    Delegates the parsing to :mod:`hermes_cli.substrate_view`, which is the ONE reader
    every mirror shares (``hermes doctor``, ``/api/substrate``, the gateway's
    ``/substrate``): two parsers would eventually disagree about what "blind" means.
    The result is ``{blind, total, items, ...}`` — ``blind`` counts ARMED ROWS whose
    body is failing open right now, ``total`` counts the armed rows in this home.

    ``since`` is accepted for call-site compatibility and deliberately NOT applied: an
    ongoing fail-open is current whether it began before or during this episode, and
    filtering by ``first_tick`` would hide a guard that went blind first and stayed
    blind. Never raises.
    """
    try:
        from hermes_cli.substrate_view import blind_guards

        return blind_guards()
    except Exception as exc:  # noqa: BLE001 - a degraded reader must not break the record
        log.warning("substrate: could not fold the hook degradation ledger: %s", exc)
        return {"blind": 0, "total": None, "items": [], "ledger_state": "unreadable",
                "registry_state": "unreadable"}


def record(
    report: ProbeReport,
    *,
    door: str,
    held: Optional[list[dict]] = None,
    path: Any = None,
    now: Optional[float] = None,
    yielded: Optional[bool] = None,
) -> tuple[str, dict]:
    """Fold this pass into the AUTHORITY record. ``(path, payload)``.

    The streak is recomputed every tick from the SIGNATURE (so it survives a respawn, and two
    doors refusing the same cause continue ONE window); the ``delivery`` block is carried
    forward untouched inside an episode, which is what bounds the DM to one per episode (I6).

    ``yielded`` overrides the default ``bool(report.denied)`` for the ONE case where a denial is
    real but nothing is being refused: a per-unit HOLD on a green global tier (door 3). The unit is
    counted in ``held`` -- the record names it -- while the door state stays honest, so a held card
    never reads as "the host is refusing to spawn" and the retirement sweep does not fire on it.
    """
    target = status_path(path)
    prior = _read_json(target)
    stamp = float(now if now is not None else time.time())
    sig = signature(report)
    same = bool(prior.get("yielded")) and prior.get("sig") == sig and bool(sig)
    if same:
        streak = int(prior.get("streak") or 0) + 1
        first_tick = float(prior.get("first_tick") or stamp)
    else:
        streak = 1
        first_tick = stamp
    episode = episode_identity(sig, first_tick)
    delivery = prior.get("delivery") if prior.get("episode") == episode else None
    guards = guards_blind()
    payload = {
        "version": SCHEMA_VERSION,
        "yielded": bool(report.denied) if yielded is None else bool(yielded),
        "kind": KIND,
        "door": door,
        "sig": sig,
        "streak": streak,
        "first_tick": first_tick,
        "last_tick": stamp,
        "threshold": THRESHOLD,
        "episode": episode,
        "delivery": delivery,
        "matrix": report.matrix(),
        "held": list(held or prior.get("held") or []),
        "guards_blind": int(guards.get("blind") or 0),
        "guards_total": guards.get("total"),
        "guards": list(guards.get("items") or []),
        "read_findings": list(report.findings),
        "cleared_at": None,
        "duration_s": None,
        "recorded_at": stamp,
    }
    return _write_status(payload, target), payload


def record_cleared(
    *,
    door: str,
    report: Optional[ProbeReport] = None,
    released: Optional[list[str]] = None,
    path: Any = None,
    now: Optional[float] = None,
) -> tuple[str, dict]:
    """Write the RECOVERY RECEIPT into the record: ``cleared_at`` + ``duration_s`` + the
    cleared matrix + the units released. A no-op (left as it is) when no episode is open."""
    target = status_path(path)
    prior = _read_json(target)
    if not prior.get("yielded"):
        return str(target), prior
    stamp = float(now if now is not None else time.time())
    try:
        first = float(prior.get("first_tick") or stamp)
    except (TypeError, ValueError):
        first = stamp
    payload = dict(prior)
    payload["yielded"] = False
    payload["cleared_at"] = stamp
    payload["duration_s"] = round(max(0.0, stamp - first), 3)
    payload["cleared_door"] = door
    payload["cleared_matrix"] = (
        report.matrix() if report is not None else prior.get("matrix") or []
    )
    payload["released"] = list(released or [])
    payload["recorded_at"] = stamp
    return _write_status(payload, target), payload


# ---------------------------------------------------------------------------------------
# The ONE DM pair per episode (I5: boot-resident, never ``escalate``)
# ---------------------------------------------------------------------------------------


def _hermes_executable() -> Optional[str]:
    """The ``hermes`` CLI to DM with, or ``None``. Boot volume only: ``sys.executable``'s
    sibling first (the running install), then ``PATH``."""
    import shutil

    try:
        sibling = Path(sys.executable).with_name("hermes")
        if sibling.is_file() and os.access(sibling, os.X_OK):
            return str(sibling)
    except Exception:  # noqa: BLE001
        pass
    return shutil.which("hermes")


def _matrix_sentence(matrix: Sequence[dict]) -> str:
    """One deterministic clause out of the matrix: ``EPERM on r x3 root(s)``."""
    causes: dict[str, int] = {}
    for row in matrix:
        if row.get("errno") in (None, "ok"):
            continue
        key = f"{row.get('errno')} on {row.get('op')}"
        causes[key] = causes.get(key, 0) + 1
    if not causes:
        return "no denial in the matrix"
    return "; ".join(f"{k} x{v} root(s)" for k, v in sorted(causes.items()))


def deliver_once(
    payload: dict,
    *,
    door: str,
    targets: Sequence[str] = DEFAULT_DM_TARGETS,
    path: Any = None,
    timeout: int = DM_TIMEOUT_S,
) -> dict:
    """ONE ``hermes peer run`` per target per EPISODE. Returns the ``delivery`` block.

    ``payload["delivery"]`` is the bound (DEC-2.4 pattern): once it carries an ``at`` for THIS
    episode, nothing is retried --- a fourth, fifth and fiftieth refusal cannot re-notify.
    Best effort: a failure is RECORDED with its named error and never raises.
    """
    existing = payload.get("delivery") or {}
    if existing.get("at"):
        return existing
    exe = _hermes_executable()
    line = (
        f"{PREFIX} {payload.get('sig') or 'unknown'} episode {payload.get('episode') or '?'} "
        f"streak {payload.get('streak')}/{(payload.get('threshold') or THRESHOLD)} door {door}: "
        f"{_matrix_sentence(payload.get('matrix') or [])}. Record: {status_path(path)}. "
        f"Nothing is being admitted; the acting card is on the ops board."
    )
    delivery: dict = {"at": float(time.time()), "door": door, "attempts": [], "line": line}
    for target in targets:
        attempt: dict = {"target": target, "state": "unavailable", "error": None}
        if exe is None:
            attempt["error"] = "the hermes CLI was not found on the boot volume"
        else:
            try:
                proc = subprocess.run(
                    [exe, "peer", "run", target, line],
                    capture_output=True, text=True, encoding="utf-8", errors="replace",
                    timeout=timeout,
                )
                attempt["state"] = "sent" if proc.returncode == 0 else "failed"
                if proc.returncode != 0:
                    attempt["error"] = (proc.stderr or proc.stdout or "").strip()[:400]
                attempt["returncode"] = proc.returncode
            except Exception as exc:  # noqa: BLE001 - a DM failure is recorded, never raised
                attempt["error"] = f"{exc.__class__.__name__}: {exc}"
        delivery["attempts"].append(attempt)
    delivery["state"] = (
        "sent" if all(a["state"] == "sent" for a in delivery["attempts"]) else "partial"
    )
    return delivery


__all__ = [
    "DENIED_ERRNOS",
    "FRESH_WITHIN",
    "INDETERMINATE_ERRNOS",
    "KIND",
    "PREFIX",
    "THRESHOLD",
    "ProbeReport",
    "RootReading",
    "RootSpec",
    "RootsMap",
    "SubstrateDenied",
    "clear_cache",
    "deliver_once",
    "episode_identity",
    "gate_enabled",
    "guards_blind",
    "hermes_home",
    "map_path",
    "merge_delivery",
    "probe",
    "probe_all",
    "read_status",
    "record",
    "record_cleared",
    "refusal_for",
    "roots",
    "signature",
    "status_path",
    "unit_substrate",
]
