"""The cron store's write-time carrier gate — call site (a) of the carrier contract.

Card **t_a38b4a2f**; class, clauses and boundaries: `yaan-repo-control/docs/CARRIER-CONTRACT.md`.

A cron row's `script` is a registration: whatever file it resolves to is a live
launcher the scheduler runs by absolute path. This gate judges that file BEFORE the
row is persisted, so a shadow launcher is refused at the door instead of being found
by a census weeks later::

    hermes cron create --script oversight-loop.sh ...   -> refused, write not persisted

The predicate is **never re-implemented here**. `carrier.py` is loaded from the
control plane and its own `verdict()` / `parse_manifest_slots()` decide everything.
Deviating from the literal line in the card (`carrier.py --check FILE --registered`),
the registration is taken from the **manifest's carrier slots** rather than from the
row's name alone, because `--registered <name>` on a cron row's target also registers
WORKERS: measured 2026-09-30 over the live store, the name-only reading refused 11 of
26 rows (`config-snapshot.sh`, `board-store-backup.sh`, `maintenance-sweep.sh`, ... —
C2 "you are logic"), which is precisely the mistake `parse_manifest_slots` was written
to avoid ("a registration that guesses from the dest name refuses the wrong files").
The `*-shim.sh` source convention IS the registration, and it is what makes C2/C3
apply; every other live script is judged through the class-wide C1 — the clause that
catches the shadow — and is otherwise out-of-class, reported as such and never
refused.

Outcomes, recorded in a ledger row per judged file so the ledger answers "was this row
gated?" rather than leaving silence:

* predicate finding -> **refused**: the write raises, nothing is persisted.
* no finding       -> **clean**: allowed, recorded with the class the predicate read.
* could not measure (no control plane, no predicate, unreadable path, unresolved
  script) -> **unverified**: allowed and RECORDED LOUDLY. A check that could not run
  is not a pass, and a gate that dies cannot be obeyed. `HERMES_CARRIER_GATE=off` is
  the emergency waiver and it is recorded as `waived`, never silently.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)

#: The control plane that owns the predicate and the dest manifests.
DEFAULT_CONTROL_PLANE = "/opt/hermes_sandbox/yaan-repo-control"
CONTROL_PLANE_ENV = "HERMES_CARRIER_CONTROL_PLANE"
PREDICATE_NAME = "carrier.py"
MANIFEST_DIR = "manifests"
#: ``HERMES_CARRIER_GATE=off`` waives the gate; the waiver is recorded, never silent.
GATE_ENV = "HERMES_CARRIER_GATE"
LEDGER_NAME = "cron-carrier-gate.jsonl"

_WAIVER_VALUES = {"off", "0", "disabled", "false", "no"}

# Cached predicate module, keyed by (path, mtime_ns) so a control-plane update is
# picked up without a restart on the next write.
_PREDICATE: Optional[Any] = None
_PREDICATE_KEY: Optional[Tuple[str, int]] = None


class CarrierRefused(ValueError):
    """A cron write whose script target breaks the carrier contract."""


def control_plane() -> Path:
    """The control plane root: env override, else the live control checkout."""
    return Path(os.environ.get(CONTROL_PLANE_ENV) or DEFAULT_CONTROL_PLANE)


def predicate_path() -> Path:
    return control_plane() / PREDICATE_NAME


def ledger_path() -> Path:
    """``<HERMES_HOME>/logs/cron-carrier-gate.jsonl`` (``~/.hermes/logs`` for the default profile)."""
    return get_hermes_home() / "logs" / LEDGER_NAME


def _text(value: Any) -> str:
    return str(value).strip() if value else ""


def _load_predicate() -> Tuple[Optional[Any], Optional[str]]:
    """Import ``carrier.py`` from the control plane. Returns ``(module, error)``."""
    global _PREDICATE, _PREDICATE_KEY
    path = predicate_path()
    try:
        key = (str(path), path.stat().st_mtime_ns)
    except OSError as exc:
        return None, "control plane predicate %s is not readable (%s)" % (path, exc)
    if _PREDICATE is not None and _PREDICATE_KEY == key:
        return _PREDICATE, None
    try:
        spec = importlib.util.spec_from_file_location("yaan_carrier_contract", path)
        if spec is None or spec.loader is None:
            return None, "control plane predicate %s is not importable" % path
        module = importlib.util.module_from_spec(spec)
        # Register BEFORE exec_module: dataclasses resolves annotations through
        # sys.modules[cls.__module__].
        sys.modules["yaan_carrier_contract"] = module
        spec.loader.exec_module(module)
    except Exception as exc:  # pragma: no cover - a broken control plane
        return None, "control plane predicate %s failed to import (%s: %s)" % (
            path, type(exc).__name__, exc)
    _PREDICATE, _PREDICATE_KEY = module, key
    return module, None


def _manifest_slots(predicate: Any) -> Tuple[Dict[str, str], set, int]:
    """Union every manifest's carrier slots ``{dest_name: source}`` and ``preserve``d names.

    The cron write path knows the resolved PATH, not the dest it came from, so the
    registration is read by dest name across the control plane's manifests: a
    ``*-shim.sh``-sourced ``include`` anywhere is a carrier registration, which is the
    convention ``parse_manifest_slots`` publishes.
    """
    slots: Dict[str, str] = {}
    preserved: set = set()
    seen = 0
    manifest_dir = control_plane() / MANIFEST_DIR
    try:
        manifests = sorted(manifest_dir.glob("*.conf"))
    except OSError:  # pragma: no cover - unreadable control plane
        manifests = []
    for manifest in manifests:
        try:
            found, preserved_paths = predicate.parse_manifest_slots(manifest)
        except Exception as exc:  # pragma: no cover - malformed manifest
            logger.debug("carrier gate: %s unreadable: %s", manifest, exc)
            continue
        seen += 1
        slots.update(found)
        preserved |= preserved_paths
    return slots, preserved, seen


def _resolve_script_path(script: str) -> Tuple[Optional[Path], Optional[str]]:
    """The SCHEDULER's own resolver — the same one the fire path uses, by import.

    ``cron.scheduler_script._resolve_script_path`` is the single resolution the run
    path calls, so the gate and the fire agree by construction rather than by
    agreement between two copies.
    """
    from cron.scheduler_script import _resolve_script_path as scheduler_resolver

    return scheduler_resolver(script)


def _record(entry: Dict[str, Any]) -> None:
    """Append one ledger row (best effort: the ledger must never break a write)."""
    entry.setdefault("at", datetime.now(timezone.utc).isoformat())
    entry.setdefault("ledger", "cron-carrier-gate")
    try:
        path = ledger_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, sort_keys=True) + "\n")
    except Exception as exc:  # pragma: no cover - ledger is observability, not the gate
        logger.debug("carrier gate: could not append ledger row: %s", exc)


def _loud(message: str) -> None:
    """A finding the operator must see: the log AND stderr of the writing process."""
    logger.warning("carrier gate: %s", message)
    try:
        print("carrier gate: %s" % message, file=sys.stderr)
    except Exception:  # pragma: no cover - a closed stderr must not break the write
        pass


def _refusal_message(tool: str, field: str, path: Path, predicate_file: Path, verdict: Any) -> str:
    lines = [
        "Carrier contract refused this %s write: %s" % (tool, path),
        "",
        "The row's `%s` names a live launcher, so it is judged as a carrier "
        "(`%s`, declared by the dest manifest)." % (field, verdict.kind),
    ]
    for finding in verdict.findings:
        lines.append(
            "  %s line %d: %s\n      %s" % (finding.clause, finding.line, finding.text, finding.why)
        )
    lines += [
        "",
        "A carrier is declarations plus ONE exec whose target is an absolute path under "
        "/opt/hermes_prod, shipped by an `include scripts/X-shim.sh -> X.sh` line in its "
        "dest's manifest (docs/CARRIER-CONTRACT.md).",
        "Fix the carrier, remove the `%s` from the row, or point the row at a script that "
        "launches only prod bytes. Predicate: %s" % (field, predicate_file),
    ]
    return "\n".join(lines)


def _judge(predicate: Any, path: Path, slots: Dict[str, str], preserved: set) -> Any:
    """The predicate's own verdict for one resolved dest file.

    A manifest carrier slot is registered (C1, C2 and C3 apply). Anything else is read
    through the class-wide C1: a shell launcher that names the sandbox is the
    ``preserved-shadow`` class and refuses, and every other file is ``out-of-class`` —
    reported as a class, never as a pass.
    """
    text = path.read_text(encoding="utf-8", errors="replace")
    registered = path.name in slots
    shipped: Optional[bool] = None
    if registered:
        shipped = path.name not in preserved
    return predicate.verdict(text, path=str(path), registered=registered, shipped=shipped)


def enforce_carrier_contract(
    job_id: str,
    tool: str,
    script: Any = None,
    monitor_script: Any = None,
) -> None:
    """Gate one cron write's script targets. Raises :class:`CarrierRefused` on a finding.

    Called from the two write chokepoints (``cron.jobs.create_job`` / ``update_job``)
    BEFORE the record is persisted, so a refusal leaves ``jobs.json`` untouched. The
    fire path is never touched: a row that predates the gate still runs.
    """
    fields: List[Tuple[str, str]] = [
        (name, _text(value))
        for name, value in (("script", script), ("monitor_script", monitor_script))
        if _text(value)
    ]
    if not fields:
        return

    if (os.environ.get(GATE_ENV) or "").strip().lower() in _WAIVER_VALUES:
        for field, value in fields:
            _record({
                "tool": tool, "row_id": job_id, "field": field, "script": value,
                "resolved": None, "outcome": "waived",
                "why": "%s=%s — the carrier gate did not judge this row" % (
                    GATE_ENV, os.environ.get(GATE_ENV)),
            })
        _loud("WAIVED by %s: %s row %s not judged (%s)" % (
            GATE_ENV, tool, job_id, ", ".join("%s=%s" % fv for fv in fields)))
        return

    predicate, load_error = _load_predicate()
    if predicate is None:
        for field, value in fields:
            _record({
                "tool": tool, "row_id": job_id, "field": field, "script": value,
                "resolved": None, "outcome": "unverified", "why": load_error,
            })
        _loud("UNVERIFIED — %s row %s was allowed unjudged: %s" % (tool, job_id, load_error))
        return

    slots, preserved, manifest_count = _manifest_slots(predicate)

    for field, value in fields:
        resolved, resolve_error = _resolve_script_path(value)
        if resolved is None:
            _record({
                "tool": tool, "row_id": job_id, "field": field, "script": value,
                "resolved": None, "outcome": "unverified",
                "why": "unresolved: %s" % (resolve_error or "no path"),
            })
            _loud("UNVERIFIED — %s row %s: `%s=%s` did not resolve (%s); allowed unjudged" % (
                tool, job_id, field, value, resolve_error or "no path"))
            continue
        try:
            verdict = _judge(predicate, resolved, slots, preserved)
        except Exception as exc:
            _record({
                "tool": tool, "row_id": job_id, "field": field, "script": value,
                "resolved": str(resolved), "outcome": "unverified",
                "why": "unreadable: %s: %s" % (type(exc).__name__, exc),
            })
            _loud("UNVERIFIED — %s row %s: %s could not be read (%s); allowed unjudged" % (
                tool, job_id, resolved, exc))
            continue

        common = {
            "tool": tool, "row_id": job_id, "field": field, "script": value,
            "resolved": str(resolved), "class": verdict.kind,
            "clauses": [f.clause for f in verdict.findings],
            "manifests_read": manifest_count,
        }
        if verdict.ok:
            _record({**common, "outcome": "clean",
                     "why": "class=%s — no clause broken" % verdict.kind})
            continue
        _record({
            **common, "outcome": "refused",
            "why": "; ".join("%s line %d: %s" % (f.clause, f.line, f.why) for f in verdict.findings),
        })
        _loud("REFUSED — %s row %s: %s" % (tool, job_id, resolved))
        raise CarrierRefused(_refusal_message(tool, field, resolved, predicate_path(), verdict))
