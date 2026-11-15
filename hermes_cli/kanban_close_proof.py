"""Two deterministic readings of a CLOSE: is it a retirement, and did its proof pass.

Design ruling ESC-36b70d83894494d2 (platform-stl, card t_1da139e3). Both readings
exist because a close is currently a hand-typed sentence: the board counted
``t_fd2c802f`` as work performed at 0s while its summary said "SUPERSEDED by
t_b3efee10" and nothing had been written. Neither reading asks a model, and
neither interprets prose beyond a fixed vocabulary and a declared line.

RETIRE CLAIM
    A handoff whose words say "superseded by t_xxxxxxxx" is a RETIREMENT claim.
    The board's retirement door is ``archive --reason <token>``: it is excluded
    from done counts by construction and recorded as its own state. A completion
    carrying that vocabulary is precisely the false green this module refuses.

CLOSE PROOF
    A card may declare ONE line in its body::

        proof: cmd=<shell command> [expect=<rc, default 0>] [timeout=<s, default 60>] [cwd=<dir>]
        proof: path=<file-or-glob> [min_bytes=<N, default 1>]

    ``cmd`` is executed with the shell; ``path`` is a filesystem existence/size
    check (a glob passes when ANY match meets ``min_bytes``). The refusal names
    the command and its output, so the worker is told what failed rather than
    that something did.
"""

from __future__ import annotations

import glob
import os
import re
import subprocess
import tempfile
import time
from typing import Any, Optional

#: The CLOSED retire vocabulary (ruling §3). Fixed strings, no inference: a
#: summary that matches one of these AND names a card id is a retirement claim.
RETIRE_VOCABULARY = (
    "superseded",
    "duplicate of",
    "redirected to",
    "folded into",
    "replaced by",
    "closed moot by",
)

#: Card ids on every board: ``t_`` + eight lowercase hex characters.
_CARD_REF = re.compile(r"\bt_[0-9a-f]{8}\b")

#: The declared proof line, tolerant of the markdown a card body carries: an
#: optional bullet/quote prefix and backticks around the line.
_BULLET = re.compile(r"^[-*>]\s+")

#: ``expect=`` / ``timeout=`` / ``cwd=`` follow the command; the command itself
#: may contain spaces, so the split is on these named keys and not on spaces.
_CMD_TAIL_SPLIT = re.compile(r"\s+(?=(?:expect|timeout|cwd)\s*=)", re.IGNORECASE)
_PATH_TAIL_SPLIT = re.compile(r"\s+(?=(?:min_bytes)\s*=)", re.IGNORECASE)

DEFAULT_TIMEOUT = 60
MAX_TIMEOUT = 600
MAX_TAIL_CHARS = 400
PROOF_KEYS = ("proof", "proof_required")


def _clean_line(raw: str) -> str:
    line = (raw or "").strip()
    line = _BULLET.sub("", line)
    if line.startswith("`") and line.endswith("`") and len(line) > 2:
        line = line.strip("`").strip()
    return line


def find_retire_claim(*texts: Optional[str]) -> Optional[dict[str, str]]:
    """The retire claim in ANY of ``texts`` (usually result, summary), or ``None``.

    BOTH conditions are required, exactly as ruled: a fixed-vocabulary phrase AND
    another card id. A close that merely mentions a card ("see t_abc12345 for the
    evidence") is not a retirement, and a retirement that names no successor
    ("superseded by a newer generation") gives the board nothing to point at.
    """
    for text in texts:
        if not text:
            continue
        low = str(text).lower()
        for phrase in RETIRE_VOCABULARY:
            if phrase not in low:
                continue
            match = _CARD_REF.search(str(text))
            if match:
                return {"phrase": phrase, "card": match.group(0)}
    return None


def proof_required(body: Optional[str], metadata: Optional[dict]) -> bool:
    """``True`` when the card DECLARES that a close must carry a proof.

    Two spellings, both deliberate: a body line ``proof_required: true`` (the
    author's declaration, survives every close) and a ``proof_required`` key on
    the closing metadata (a caller's declaration for this close).
    """
    if isinstance(metadata, dict) and metadata.get("proof_required") is True:
        return True
    for raw in (body or "").splitlines():
        line = _clean_line(raw)
        low = line.lower()
        if low.startswith("proof_required:"):
            value = low.split(":", 1)[1].strip()
            if value in ("true", "yes", "1", "required"):
                return True
    return False


def parse_proof(body: Optional[str]) -> Optional[dict[str, Any]]:
    """The card body's declared proof spec, or ``None``.

    A malformed declaration is NOT silently ignored: it returns a spec with
    ``error`` set, so the close refuses with ``proof_failed`` naming the line
    instead of quietly treating an unparseable proof as no proof at all (that
    silence is the defect class this module exists for).
    """
    for raw in (body or "").splitlines():
        line = _clean_line(raw)
        if not line.lower().startswith("proof:"):
            continue
        rest = line.split(":", 1)[1].strip()
        if rest.startswith("`") and rest.endswith("`") and len(rest) > 2:
            rest = rest.strip("`").strip()
        return _parse_spec(rest)
    return None


def _int_param(value: str, name: str, default: int, *, low: int, high: Optional[int]) -> Any:
    raw = (value or "").strip()
    if raw == "":
        return default
    try:
        n = int(raw)
    except ValueError:
        return f"{name}={raw!r} is not an integer"
    if n < low:
        return f"{name}={n} is below the minimum {low}"
    if high is not None and n > high:
        return f"{name}={n} is above the maximum {high}"
    return n


def _parse_spec(rest: str) -> dict[str, Any]:
    low = rest.lower()
    if low.startswith("cmd="):
        parts = _CMD_TAIL_SPLIT.split(rest)
        cmd = parts[0][len("cmd="):].strip()
        spec: dict[str, Any] = {
            "kind": "cmd", "cmd": cmd, "expect": 0,
            "timeout": DEFAULT_TIMEOUT, "cwd": None,
        }
        for part in parts[1:]:
            key, _sep, value = part.partition("=")
            key = key.strip().lower()
            value = value.strip()
            if key == "expect":
                got = _int_param(value, "expect", 0, low=0, high=255)
                if isinstance(got, str):
                    return {"kind": "cmd", "cmd": cmd, "error": got}
                spec["expect"] = got
            elif key == "timeout":
                got = _int_param(value, "timeout", DEFAULT_TIMEOUT, low=1, high=MAX_TIMEOUT)
                if isinstance(got, str):
                    return {"kind": "cmd", "cmd": cmd, "error": got}
                spec["timeout"] = got
            elif key == "cwd":
                spec["cwd"] = value or None
        if not spec["cmd"]:
            return {"kind": "cmd", "cmd": "", "error": "proof: cmd= is empty"}
        return spec
    if low.startswith("path="):
        parts = _PATH_TAIL_SPLIT.split(rest)
        pattern = parts[0][len("path="):].strip()
        spec = {"kind": "path", "path": pattern, "min_bytes": 1}
        for part in parts[1:]:
            key, _sep, value = part.partition("=")
            if key.strip().lower() == "min_bytes":
                got = _int_param(value, "min_bytes", 1, low=0, high=None)
                if isinstance(got, str):
                    return {"kind": "path", "path": pattern, "error": got}
                spec["min_bytes"] = got
        if not spec["path"]:
            return {"kind": "path", "path": "", "error": "proof: path= is empty"}
        return spec
    return {"kind": "unknown", "error": (
        "proof: must start with cmd= or path= (got "
        f"{rest[:80]!r})")}


def _tail(text: str, limit: int = MAX_TAIL_CHARS) -> str:
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    if not lines:
        return ""
    line = lines[-1]
    return line if len(line) <= limit else line[: limit - 1] + "…"


def _base_dir(spec_cwd: Optional[str], base_dir: Optional[str]) -> Optional[str]:
    if spec_cwd:
        return os.path.expanduser(spec_cwd)
    if base_dir and os.path.isdir(base_dir):
        return base_dir
    return None


def run_proof(spec: dict[str, Any], *, base_dir: Optional[str] = None) -> dict[str, Any]:
    """EXECUTE the declared proof; returns the record the close stores.

    ``verified`` is the whole verdict. For ``cmd`` it is ``rc == expect``; for
    ``path`` it is "a match exists whose size is at least ``min_bytes``". A
    timeout, a missing directory, an unparseable spec and a raised OSError all
    land as ``verified: False`` with a ``reason`` a worker can act on — never as
    an exception that would leave the close looking like an infrastructure fault.
    """
    started = time.time()
    record: dict[str, Any] = {"kind": spec.get("kind"), "verified": False,
                              "declared": _render_declaration(spec)}
    if spec.get("error"):
        record["reason"] = spec["error"]
        record["duration_ms"] = int((time.time() - started) * 1000)
        return record

    if spec["kind"] == "path":
        pattern = spec["path"]
        cwd = _base_dir(None, base_dir)
        target = pattern
        if not os.path.isabs(target):
            target = os.path.join(cwd, target) if cwd else os.path.abspath(target)
        try:
            matches = [p for p in glob.glob(target, recursive=True)
                       if os.path.isfile(p) and os.path.getsize(p) >= spec["min_bytes"]]
        except OSError as exc:                       # pragma: no cover - defensive
            matches = []
            record["reason"] = f"path check raised {exc}"
        record.update({
            "path": pattern,
            "resolved": target,
            "min_bytes": spec["min_bytes"],
            "match_count": len(matches),
            "matches": matches[:5],
            "verified": bool(matches),
        })
        if not matches and "reason" not in record:
            record["reason"] = (f"no file matching {pattern!r} with at least "
                                f"{spec['min_bytes']} byte(s)")
        record["duration_ms"] = int((time.time() - started) * 1000)
        return record

    cwd = _base_dir(spec.get("cwd"), base_dir)
    record.update({"cmd": spec["cmd"], "expect": spec["expect"], "cwd": cwd})
    if spec.get("cwd") and not os.path.isdir(os.path.expanduser(spec["cwd"])):
        record["reason"] = f"cwd {spec['cwd']!r} does not exist"
        record["duration_ms"] = int((time.time() - started) * 1000)
        return record
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace") as sink:
        try:
            proc = subprocess.run(
                spec["cmd"], shell=True, cwd=cwd,
                stdout=sink, stderr=subprocess.STDOUT, timeout=spec["timeout"],
            )
            rc: Optional[int] = proc.returncode
        except subprocess.TimeoutExpired:
            rc = None
            record["reason"] = f"timed out after {spec['timeout']}s"
        except OSError as exc:                       # pragma: no cover - defensive
            rc = None
            record["reason"] = f"could not run the proof: {exc}"
        sink.flush()
        end = sink.tell()
        sink.seek(max(0, end - 65536))
        output = sink.read()
    record["rc"] = rc
    record["output_tail"] = _tail(output)
    if rc is not None:
        record["verified"] = rc == spec["expect"]
        if not record["verified"]:
            record["reason"] = (f"exit code {rc}, expected {spec['expect']}"
                                + (f": {record['output_tail']}" if record["output_tail"] else ""))
    record["duration_ms"] = int((time.time() - started) * 1000)
    return record


def _render_declaration(spec: dict[str, Any]) -> str:
    """The one-line spelling echoed into the result: ``cmd=...`` or ``path=...``."""
    if spec.get("kind") == "cmd":
        tail = []
        if spec.get("expect", 0) != 0:
            tail.append(f"expect={spec['expect']}")
        if spec.get("cwd"):
            tail.append(f"cwd={spec['cwd']}")
        return "proof: cmd=" + (spec.get("cmd") or "") + (" " + " ".join(tail) if tail else "")
    if spec.get("kind") == "path":
        tail = f" min_bytes={spec['min_bytes']}" if spec.get("min_bytes", 1) != 1 else ""
        return "proof: path=" + (spec.get("path") or "") + tail
    return "proof: (malformed)"


def echo_line(record: dict[str, Any]) -> str:
    """``[proof rc=0 cmd=...]`` — the bounded claim appended to the close's result."""
    kind = record.get("kind")
    if kind == "cmd":
        rc = record.get("rc")
        return f"[proof rc={rc if rc is not None else 'timeout'} cmd={record.get('cmd', '')}]"
    if kind == "path":
        return (f"[proof matches={record.get('match_count', 0)} "
                f"path={record.get('path', '')}]")
    return f"[proof unverified: {record.get('reason', 'malformed declaration')}]"
