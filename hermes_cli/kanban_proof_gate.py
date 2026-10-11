"""Deploy-proof gate: a ``landed`` card may not close on evidence from before the landing.

The contract of a ``landed`` card is: a real production RUN (or service probe) dated
AFTER the artifact's landing, AND the artifact the handoff advertises is the bytes that
are actually live. This module is the clause engine behind
``hermes_cli.kanban_db._gate_deploy_proof``; it takes the INNER proof dict (not the
``{"proof": {...}}`` wrapper) and answers tri-state: admitted, or refused by name.

Proof shape (all keys optional except ``artifacts``)::

    {
      "artifacts": [
        {"path": "/abs/path/on/disk", "sha256": "<64 hex>"},
        {"path": "/abs/path/on/disk", "commit": "<40 hex, or >=7 hex prefix>"},
        {"path": "/abs/path/on/disk", "commit": "<40 hex>", "sha256": "<64 hex>"}
      ],
      "landing": {"ref": "<landed sha | card id | pr number>"},
      # a LEDGER-LESS artifact (a live-tree override no departure ever delivered) declares
      # its class and pays for it with a probe that names its observations:
      #   "landing": {"ref": "<what the override ships>", "source": "live-tree"},
      #   "probe":   {"at": "<iso8601>", "result": "ok",
      #               "observations": ["<behaviour 1>", "<behaviour 2>", "<behaviour 3>"]}
      "run":     {"store": "yoyoflow|card", "id": "...", "task": "t_..."},
      #   ``id`` is a ``runs.id`` for store=yoyoflow, or a ``task_runs`` ROW id for
      #   store=card (never the card id); ``task`` is the card id, optional for card-store.
      #   A card-store run is green only once outcome=completed, so an in-flight card cannot
      #   cite its own run — use the probe form or a green yoyoflow run for a card's own close.
      "probe":   {"at": "<iso8601>", "result": "ok",
                  "observations": ["<optional for a train dest, REQUIRED ledger-less>"]}
    }

Cause vocabulary (greppable, one ``cause`` per refusal):

  ``no_proof`` / ``malformed_proof``  nothing to judge
  ``artifact_missing``                a declared path is not a readable file
  ``landing_unresolved``              no ledger row supports the landing ref
  ``landing_not_admitted``            the matching landing row was refused
  ``landing_live_tree_denied``        a live-tree declaration for bytes the ledger says a
                                      departure delivers — prove them on the train instead
  ``run_unknown`` / ``no_run_or_probe``  the named run/probe cannot be resolved
  ``proof_predates_landing``          the run predates the artifact — the DoD clause
  ``run_not_green``                   the run finished red
  ``live_tree_unproven``              a ledger-less landing whose proof carries no probe, or
                                      a probe with no named observations, or only a card turn
  ``artifact_hash_mismatch``          declared sha256 / merged blob != the bytes on disk
  ``artifact_blob_unresolved``        a declared ``blob`` pin could not be resolved in git
  ``artifact_unpinned``               a declared artifact names no DURABLE REFERENT the gate can
                                      re-resolve at handoff (ruling t_6f582c2b): ``path`` (+
                                      ``sha256``) alone is not enough on a landed card

CLAUSE ORDER (deliberate, see :data:`_CAUSE_PRECEDENCE`). The clauses are EVALUATED in the
order a landed card owes them — LANDING first, then the RUN/PROBE, then the ARTIFACT's
identity — and every clause that can be evaluated IS evaluated, with every failure reported
in ``Verdict.failures``; ``cause`` names the one the worker should act on first. The
identity clauses (``artifact_*``) are named LAST, because their documented remedy is a
re-pin of the declared hash — a clause whose remedy is a re-pin must never pre-empt the
clause that says the evidence is from before the landing. A stale pinned hash therefore
reports the timing failure and lists the mismatch beside it, instead of masking the DoD
clause behind an artifact complaint.

One structural exception, named first and stated here so it is never a surprise:
``no_proof``, ``malformed_proof`` and ``artifact_missing`` say there is nothing to judge —
no proof block, no artifact list, or a declared path that is not a readable file. Those
three name the cause, because no clause about bytes can be acted on until the path is
fixed, but they no longer RETURN early: the landing and run clauses are still evaluated and
reported beside them (that early return was the defect — a landed card could be refused on
an artifact complaint with the timestamps never considered).

TWO EVIDENCE FORMS FOR AN ARTIFACT. ``sha256`` pins the live bytes and goes stale on the
next departure; ``blob: "<commit>:<path in repo>"`` pins the MERGED blob and does not go
stale. It is resolved with git in the artifact's own work tree, or in the ``repo`` the
entry declares when the deployed path is not itself inside a checkout, and the live file
must EQUAL the blob at that commit — so a handoff can no longer be admitted on bytes that
were built from some later commit. A bare ``commit``, and the dest's own
``$DEST/.deployed-from`` stamp (repo, commit, tree, deployed-at) that ``deploy.sh`` writes,
are recorded as supporting evidence in ``facts.artifacts``; neither refuses on its own.

A DURABLE REFERENT IS REQUIRED (ruling t_6f582c2b, D1/D2). ``path`` + ``sha256`` describes a
file at one instant; when the bytes are delivered by a live-tree override a later payload can
overwrite them, so "never deployed" and "landed then superseded" become indistinguishable and
the card cannot be re-audited. Every declared artifact must therefore name at least ONE record
OUTSIDE the working tree that the gate can re-resolve at handoff. Any of these suffices:

  * R1 ``blob`` — the existing merged-blob pin; the referent is the git object.
  * R2 ledger — the proof's ``landing.ref`` resolves to an ADMITTED deploy-ledger landing row
    (existing machinery, unchanged): a train-delivered artifact proves itself on the train.
  * R3 ``record`` — for the ledger-less live-tree class, two kinds in v1:
      - ``{"kind": "override-manifest", "id": "<repo-relative path>"}``: a row
        ``| <path> | <sha256> |`` in ``~/.hermes/yoyoflow/untracked-overrides/MANIFEST.md``
        equal to the pin's declared hash, and (when the byte copy exists) a copy under that
        directory with the same hash.
      - ``{"kind": "payload-record", "id": "<card>"}``: the card's delta record
        ``~/.hermes/yoyoflow/r5-preroll/<card>-payload.diff`` with its ``.sha256`` sidecar,
        the sidecar equal to the record's content hash.

An artifact with none of these is refused ``artifact_unpinned``, naming the path, its live
sha256 and the remedy. A card WORKSPACE is not a referent (evidence bundles are reaped), and
the armed override set's ``.meta`` rows are not byte records (their ``sha256`` is the diff
section's hash and their ``b_blob`` does not resolve in the partial clone).

AUTHORING A PAYLOAD. Two rules, both measured on the gate's own witness cards (t_29f0035a /
t_141ff55e), because both were bitten while the gate was being proven:

  * NEVER hardcode the artifact's ``sha256`` in a card body or a worker instruction. Resolve
    it at fire time (``shasum -a 256 <path>``). A pinned literal is a MEASUREMENT, not a
    fact: a dest redeployed twice in one evening turned an authored hash stale within hours,
    and the resulting ``artifact_hash_mismatch`` masked the clause the card was testing.
  * PICK a landing ref whose resolved landing is known to precede the named run. Match the
    ref against ``kind == "landing"`` rows and read that row's ``at``; a plain DEPLOY row, or
    a same-sha re-deploy, can date the artifact later than its author ever saw, and the card
    is then refused as predating a landing it never knew about.

``sha256`` is OPTIONAL on an artifact. Naming the path with a ledger landing ref is the
DEPLOY-STAMP form and cannot go stale; ``blob: "<commit>:<path>"`` is the merged-blob form.
Declare ``sha256`` only when the live bytes themselves are the claim.

LANDING RESOLUTION. The landing instant is the LATER of two legs, both read from the
deploy ledger:

  * the LANDING leg — the earliest ADMITTED row with ``kind == "landing"`` naming the
    ref ("these bytes have been on trunk since this merge"). A row with no ``kind`` is a
    DEPLOY row, never a landing;
  * the ARRIVAL leg — when the ref's own bytes were deployed to a dest that is an
    ANCESTOR of the declared artifact, the earliest ``at`` of the trailing run of
    same-sha arrivals at that dest ("the run must have had these bytes to run"). Rows are
    scoped to the deepest ancestor dest, so a deploy of the same sha to some other dest
    cannot date this artifact; a row the gate refused (``allowed: false``) deployed
    nothing and never counts; a re-deploy of IDENTICAL bytes does not re-date the arrival
    (the trailing run spans it), while a deploy of different bytes in between does.

With no landing block at all, the artifact's own stamp, else its mtime, supplies the
instant — the fallback the gate has always had, with the stamp preferred to the
filesystem.

LEDGER-LESS ARTIFACTS (the ``"source": "live-tree"`` class). Some deployed bytes are never
delivered by a departure: a fork checkout or a live-tree override is edited in place and no
ledger row can ever name its merge. For those, an unresolvable landing ref used to be an
un-satisfiable contract — the card could not close however true its evidence was. The class
is now expressible, and it is paid for, not waived:

  * the declaration is REFUSED (``landing_live_tree_denied``) when ANY ledger row names a
    ``dest`` that is an ancestor of the declared artifact: those bytes ARE train-delivered,
    and a train unit proves itself on the train. The declaration cannot be used to escape a
    ledger leg that exists;
  * when the ledger has no such dest, the landing instant is the artifact's own bytes'
    arrival — its ``$DEST/.deployed-from`` stamp, else its mtime;
  * the proof must be a live probe dated AFTER that instant, carrying a non-empty
    ``observations`` list: the named behaviours the probe actually exercised, recorded in
    ``facts.probe_observations`` so a third party reads the observations, not a summary. A
    probe that names nothing, or only a card-store run (a card turn is evidence about a
    card, not about live bytes), is refused ``live_tree_unproven``;
  * if the refs DO resolve in the ledger, the ledger leg wins and the declaration is merely
    recorded (``facts.live_tree_ignored``) — stronger evidence is never discarded.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

LANDED_CONTRACT = "landed"

DEFAULT_LEDGER = "/opt/hermes_prod/deploy-ledger.jsonl"
DEFAULT_RUN_STORE = "/opt/hermes_prod/run_details/yoyoflow-state.db"

# The durable byte records for a LIVE-TREE override (ruling t_6f582c2b, R3). Resolved
# lazily so a test drives them from a temp tree and never reads the live records.
DEFAULT_OVERRIDES = os.path.join(os.path.expanduser("~"), ".hermes", "yoyoflow",
                                 "untracked-overrides")
DEFAULT_PREROLL = os.path.join(os.path.expanduser("~"), ".hermes", "yoyoflow", "r5-preroll")

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_CARD_TOKEN = re.compile(r"t_[0-9a-f]{4,}")


def _overrides_root(override: Optional[str] = None) -> str:
    return (override or os.environ.get("HERMES_PROOF_OVERRIDES")
            or DEFAULT_OVERRIDES)


def _preroll_root(override: Optional[str] = None) -> str:
    return (override or os.environ.get("HERMES_PROOF_PREROLL")
            or DEFAULT_PREROLL)

GREEN_RUN_STATUSES = frozenset({"completed", "success", "succeeded", "ok", "green", "done"})
#: The engine's OUTCOME vocabulary — `pass` iff ``runs.verdict`` is exactly "pass"
#: (``yoyoflow/engine.py::resolve_outcome``, ``docs/hook-outcomes.md``). Kept separate from
#: ``GREEN_RUN_STATUSES`` because status and outcome are different facts: a verdict may only
#: REVOKE a green status, never manufacture one (the engine's ``status-failed`` dominates rule).
GREEN_RUN_VERDICTS = frozenset({"pass"})
GREEN_CARD_OUTCOMES = frozenset({"completed"})
GREEN_PROBE_RESULTS = frozenset({"ok", "pass", "passed", "green", "healthy", "up"})

_CAUSES = (
    "no_proof",
    "malformed_proof",
    "artifact_missing",
    "artifact_hash_mismatch",
    "artifact_blob_unresolved",
    "artifact_unpinned",
    "landing_unresolved",
    "landing_not_admitted",
    "landing_live_tree_denied",
    "no_run_or_probe",
    "run_unknown",
    "proof_predates_landing",
    "run_not_green",
    "live_tree_unproven",
)

# Named first when several clauses fail at once. The identity clauses are last: their
# remedy is a re-pin, which must never be what a worker is told to do while a timing
# failure is what is actually wrong. ``artifact_missing`` is first of all because it is
# STRUCTURAL — a declared path that is not a readable file leaves nothing about bytes to
# judge, so that path is the subject — but it no longer short-circuits the evaluation: the
# landing and the run are still resolved and reported beside it.
_CAUSE_PRECEDENCE = (
    "artifact_missing",
    "proof_predates_landing",
    "landing_unresolved",
    "landing_not_admitted",
    "landing_live_tree_denied",
    "run_unknown",
    "no_run_or_probe",
    "live_tree_unproven",
    "run_not_green",
    # Within the identity group: an unresolvable pin means the comparison could not be
    # established at all, so it outranks a mismatch of bytes that WERE comparable; and a
    # pin with NO durable referent is the species "nothing could be established" (ruling
    # t_6f582c2b), ranking above a byte mismatch for the same reason. A gate that
    # dribbles requirements over successive handoffs teaches bypass, so the whole
    # requirement is stated on the first refusal.
    "artifact_blob_unresolved",
    "artifact_unpinned",
    "artifact_hash_mismatch",
)

_LEDGER_LINE_CAP = 200_000

# The deploy stamp sits at the dest root; the walk up from a declared artifact path is
# bounded so a path with no ancestor dest cannot scan the filesystem.
_STAMP_NAME = ".deployed-from"
_STAMP_MAX_WALK = 8

PROOF_SHAPE = (
    'metadata={"proof": {"artifacts": [{"path": "<abs path>", "sha256": "<live hash>"}], '
    '"landing": {"ref": "<ledger landed_sha | card id | pr>"}, '
    '"run": {"store": "yoyoflow|card", "id": "<runs.id | task_runs row id (card)>"}}} '
    '(or "probe": {"at": "<iso-8601>", "result": "ok"} for a service) '
    '(store=card reads ``id`` as a task_runs ROW id, never the card id, and only an '
    'already-completed run is green — a card cannot cite its own in-flight run) '
    '(for bytes NO departure delivers — a live-tree override — declare '
    '"landing": {"ref": "<what the override ships>", "source": "live-tree"} and pay for it '
    'with a probe dated after the bytes\' own arrival carrying "observations": '
    '["<behaviour 1>", "<behaviour 2>"]: the named behaviours it exercised) '
    '(for a pin that does not go stale, declare {"path": "<abs path>", "blob": '
    '"<merged commit>:<path in repo>", "repo": "<abs work tree, only when the '
    'deployed path is not itself inside a checkout>"} instead of the hash: the live '
    'file must BE the blob at that commit) '
    '(EVERY artifact must also name a DURABLE REFERENT the gate can re-resolve, or it is '
    'refused "artifact_unpinned": a "blob" above, or an ADMITTED ledger landing ref for the '
    'bytes, or for the ledger-less live-tree class a "record": '
    '{"kind": "override-manifest", "id": "<repo-relative path>"} (its untracked-overrides '
    'MANIFEST row + byte copy carry these bytes) or {"kind": "payload-record", "id": '
    '"<card>"} (the r5-preroll <card>-payload.diff + .sha256). A bare "path" + "sha256" is '
    'refused on a landed card)'
)


def is_landed_contract(value: Optional[str]) -> bool:
    """True when the card was authored to prove itself after its landing."""
    return (value or "local-only").strip().lower() == LANDED_CONTRACT


@dataclass
class Verdict:
    """The gate's answer: ``ok`` plus (on refusal) the clause that failed.

    ``failures`` carries EVERY clause that failed, not only the one named in ``cause``:
    a refusal must never hide a second, differently-fixed defect behind the first.
    """

    ok: bool
    cause: Optional[str] = None
    detail: str = ""
    facts: dict = field(default_factory=dict)
    failures: list = field(default_factory=list)

    def fail(self, clause: str, detail: str) -> None:
        """Record a failed clause. ``ok``/``cause`` are settled by :meth:`settle`."""
        self.failures.append({"clause": clause, "detail": detail})

    def settle(self) -> "Verdict":
        if not self.failures:
            self.ok = True
            self.cause = None
            return self
        self.ok = False
        by_clause = {failure["clause"]: failure for failure in self.failures}
        for clause in _CAUSE_PRECEDENCE:
            if clause in by_clause:
                self.cause = clause
                self.detail = by_clause[clause]["detail"]
                return self
        # A failure with no declared precedence is still a refusal, and still named.
        self.cause = self.failures[0]["clause"]
        self.detail = self.failures[0]["detail"]
        return self

    def as_metadata(self) -> dict:
        payload: dict = {"ok": bool(self.ok)}
        if self.cause:
            payload["cause"] = self.cause
        if self.detail:
            payload["detail"] = self.detail
        if self.facts:
            payload["facts"] = self.facts
        if self.failures:
            payload["failures"] = list(self.failures)
        return payload


def _iso(epoch: Optional[float]) -> Optional[str]:
    if epoch is None:
        return None
    return datetime.fromtimestamp(float(epoch), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(value: Any) -> Optional[float]:
    """Epoch seconds from an epoch number or an ISO-8601 string; None when unparsable."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    if text.replace(".", "", 1).isdigit():
        return float(text)
    candidate = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        stamp = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.timestamp()


def _hash_file(path: str) -> Optional[str]:
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


def _ledger_path(override: Optional[str]) -> str:
    return override or os.environ.get("HERMES_PROOF_LEDGER") or DEFAULT_LEDGER


def _run_store_path(override: Optional[str]) -> str:
    return override or os.environ.get("HERMES_PROOF_RUN_STORE") or DEFAULT_RUN_STORE


def _ledger_rows(path: str) -> Iterable[dict]:
    try:
        handle = open(path, "r", encoding="utf-8", errors="replace")
    except OSError:
        return
    with handle:
        for line in handle:
            line = line.strip()
            if not line or len(line) > _LEDGER_LINE_CAP:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                yield row


# -- refs, rows, stamps -------------------------------------------------------


def _ref_names(ref: Any, *candidates: Any) -> bool:
    """Does ``ref`` name one of these row fields? Exact, or a >=7 char sha prefix."""
    text_ref = str(ref or "").strip()
    if not text_ref:
        return False
    for candidate in candidates:
        text = str(candidate or "").strip()
        if not text:
            continue
        if text_ref == text:
            return True
        if len(text_ref) >= 7 and text.startswith(text_ref):
            return True
    return False


def _row_at(row: dict) -> Optional[float]:
    return parse_ts(row.get("at") or row.get("row_recorded"))


def _is_admitted(row: dict) -> bool:
    if row.get("admission") not in (None, "admitted"):
        return False
    return row.get("exit") in (None, 0)


def _is_ancestor_dest(dest: Any, artifact_path: str) -> bool:
    """Is ``dest`` the artifact's own directory, or one of its ancestors?"""
    text = str(dest or "").strip().rstrip("/")
    if not text:
        return False
    target = os.path.abspath(artifact_path)
    return target == text or target.startswith(text + os.sep)


def _read_stamp(path: str) -> Optional[dict]:
    fields: dict = {}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                key, _, value = line.partition(":")
                key = key.strip().lower()
                value = value.strip()
                if key and value:
                    fields[key] = value
    except OSError:
        return None
    return fields or None


def _stamp_for(path: str) -> tuple:
    """The nearest ancestor ``.deployed-from`` stamp of ``path`` -> (path, fields)."""
    directory = os.path.dirname(os.path.abspath(path))
    for _ in range(_STAMP_MAX_WALK):
        candidate = os.path.join(directory, _STAMP_NAME)
        if os.path.isfile(candidate):
            return candidate, _read_stamp(candidate)
        parent = os.path.dirname(directory)
        if parent == directory:
            break
        directory = parent
    return None, None


# -- landing resolution -------------------------------------------------------


def _landing_leg(refs: list, rows: list) -> tuple:
    """The earliest ADMITTED ``kind == "landing"`` row naming a ref.

    -> (epoch | None, refusal | None). A deploy row (no ``kind``) is never a landing: it
    records an arrival of bytes at a dest, not the merge that authorised them.
    """
    matches = [
        row for row in rows
        if row.get("kind") == "landing"
        and any(_ref_names(ref, row.get("landed_sha"), row.get("sha"), row.get("card"),
                           row.get("pr")) for ref in refs)
    ]
    if not matches:
        return None, None
    admitted = [row for row in matches if _is_admitted(row)]
    timed = [(at, row) for at, row in ((_row_at(row), row) for row in admitted)
             if at is not None]
    if timed:
        return min(timed, key=lambda pair: pair[0])[0], None
    if admitted:
        return None, {
            "clause": "landing_unresolved",
            "detail": (f"the landing row for {', '.join(str(ref) for ref in refs)} carries "
                       "no parsable ``at`` timestamp"),
        }
    row = matches[0]
    return None, {
        "clause": "landing_not_admitted",
        "detail": (
            f"the landing row for {', '.join(str(ref) for ref in refs)} is not an admitted "
            f"landing (admission={row.get('admission')!r}, exit={row.get('exit')!r}, "
            f"at={row.get('at')!r})"
        ),
    }


def _arrival_leg(refs: list, artifact_paths: list, rows: list) -> tuple:
    """Earliest arrival of the ref's own bytes at the declared artifact's dest.

    -> (epoch | None, facts). Scoped to the DEEPEST ancestor dest of each declared
    artifact, so a deploy of the same sha to some other dest cannot date this artifact.
    A row the gate refused deployed nothing. Where the ledger records which rows wrote
    the dest's stamp, only those rows describe its bytes. Per dest the rows are read in
    file order and the earliest ``at`` of the TRAILING run of same-sha arrivals is the
    answer: identical bytes re-deployed do not re-date the arrival, different bytes in
    between do (the bytes changed, then came back).
    """
    deploys = [
        row for row in rows
        if row.get("kind") is None
        and row.get("allowed") is not False
        and str(row.get("dest") or "").strip()
    ]
    if not deploys:
        return None, {}
    best: Optional[tuple] = None
    for artifact_path in artifact_paths:
        dests = sorted(
            {str(row["dest"]).rstrip("/") for row in deploys
             if _is_ancestor_dest(row["dest"], artifact_path)},
            key=lambda dest: (dest.count(os.sep), len(dest)),
            reverse=True,
        )
        if not dests:
            continue
        dest = dests[0]
        seq = [row for row in deploys if str(row["dest"]).rstrip("/") == dest]
        stamped = [row for row in seq if row.get("stamped") is True]
        if stamped:
            seq = stamped
        for ref in refs:
            run_start: Optional[float] = None
            for row in seq:
                at = _row_at(row)
                if at is not None and _ref_names(ref, row.get("sha")):
                    run_start = at if run_start is None else min(run_start, at)
                else:
                    run_start = None  # different bytes arrived here: the run restarts
            if run_start is not None and (best is None or run_start > best[0]):
                best = (run_start, {"arrival_dest": dest, "arrival_ref": str(ref)})
    if best is None:
        return None, {}
    return best[0], {"arrival_at": _iso(best[0]), **best[1]}


_LIVE_TREE_SOURCES = frozenset({
    "live-tree", "live_tree", "livetree", "live tree", "override", "live-override",
    "live_override", "checkout", "fork", "untracked", "ledger-less", "ledgerless",
})


def _declares_live_tree(landing: dict) -> bool:
    """Does the handoff declare the LEDGER-LESS class (bytes no departure delivers)?

    Explicit and greppable on purpose: the class buys the artifact's own mtime as its
    landing instant, so it is never inferred and never a default.
    """
    if not isinstance(landing, dict):
        return False
    for key in ("live_tree", "ledger_less"):
        if landing.get(key) is True:
            return True
    for key in ("source", "class", "kind", "provenance", "delivery"):
        value = landing.get(key)
        if value is None:
            continue
        if str(value).strip().lower() in _LIVE_TREE_SOURCES:
            return True
    return False


def _dest_rows_for(artifact_paths: list, rows: list) -> list:
    """The ledger rows whose ``dest`` contains a declared artifact, in file order.

    These are the rows that make a live-tree declaration a CANDIDATE misdeclaration. They
    are not automatically a refusal: see :func:`_train_dest_for`.
    """
    found = []
    for row in rows:
        dest = row.get("dest")
        if not dest:
            continue
        for path in artifact_paths:
            if _is_ancestor_dest(dest, path):
                found.append({"dest": str(dest), "artifact": str(path),
                              "sha": row.get("sha"), "at": row.get("at"),
                              "stamped": row.get("stamped")})
                break
    return found


_CHECKOUT_CACHE: dict = {}


def _checkout_state(directory: str, relpath: str) -> tuple:
    """(HEAD sha | None, clean: bool | None) for the checkout *directory* (memoised)."""
    key = (directory, relpath)
    if key in _CHECKOUT_CACHE:
        return _CHECKOUT_CACHE[key]
    state: tuple = (None, None)
    try:
        head = subprocess.run(["git", "-C", directory, "rev-parse", "HEAD"],
                              capture_output=True, text=True, timeout=30)
        if head.returncode == 0 and head.stdout.strip():
            status = subprocess.run(["git", "-C", directory, "status", "--porcelain", "--",
                                     relpath], capture_output=True, text=True, timeout=30)
            state = (head.stdout.strip().lower(), not status.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        state = (None, None)
    _CHECKOUT_CACHE[key] = state
    return state


def _row_is_dest_provenance(row: dict, artifact_path: str) -> bool:
    """Does this deploy row account for the artifact's LIVE bytes at its dest?

    A row from another revision does not. Three ways a row binds: it wrote the dest's own
    ``.deployed-from`` stamp; the dest's stamp names its sha; or the dest is a clean
    checkout sitting at its commit. Anything else is a historical arrival of DIFFERENT
    bytes into a shared directory — measured on the fork checkout 2026-09-27: its only
    ledger row is a 2026-09-21 arrival whose sha is not the checkout's HEAD, and the dest
    carries no deploy stamp at all, so the row says nothing about today's bytes.
    """
    sha = str(row.get("sha") or "").strip().lower()
    if not sha:
        return False
    if row.get("stamped") is True:
        return True
    _stamp_path, stamp = _stamp_for(artifact_path)
    stamp_sha = str((stamp or {}).get("commit") or "").strip().lower()
    if stamp_sha and (stamp_sha.startswith(sha) or sha.startswith(stamp_sha)):
        return True
    head, clean = _checkout_state(os.path.dirname(os.path.abspath(artifact_path)),
                                  os.path.basename(artifact_path))
    if head and clean and (head.startswith(sha) or sha.startswith(head)):
        return True
    return False


def _train_dest_for(artifact_paths: list, rows: list) -> Optional[tuple]:
    """(dest, artifact, row) for a departure that DELIVERS these bytes — else ``None``.

    An ancestor ``dest`` alone is NOT a misdeclaration. Denying the ledger-less class on
    any dest row that merely contains the path makes the class unprovable for exactly the
    artifact the ledger happened to touch once: the remedy the refusal names (prove it on
    the train) would be unavailable, which is the un-satisfiable-gate defect in another
    costume. The row must be the dest's CURRENT provenance (see
    :func:`_row_is_dest_provenance`); the rest are recorded as
    ``facts['live_tree_dest_rows']`` so a third party can see what was considered.
    """
    for row in rows:
        dest = row.get("dest")
        if not dest:
            continue
        for path in artifact_paths:
            if _is_ancestor_dest(dest, path) and _row_is_dest_provenance(row, path):
                return str(dest), path, row
    return None


def _bytes_landing(artifact_paths: list) -> tuple:
    """(epoch | None, facts) — the landing instant read off the artifact's own bytes.

    The dest's deploy stamp wins over the filesystem: a stamp records the departure, an
    mtime records whoever last touched the file.
    """
    if artifact_paths:
        stamp_path, stamp = _stamp_for(artifact_paths[0])
        stamp_at = None
        if stamp:
            stamp_at = parse_ts(stamp.get("deployed")) or parse_ts(stamp.get("at"))
        if stamp_at is not None:
            return stamp_at, {
                "landing_source": "deploy-stamp", "stamp": stamp_path,
                "stamp_commit": stamp.get("commit"),
            }
    mtimes = [os.path.getmtime(path) for path in artifact_paths if os.path.exists(path)]
    if mtimes:
        return max(mtimes), {"landing_source": "mtime"}
    return None, {}


def _landing_time(proof: dict, artifact_paths: list, rows: list, *,
                  artifacts_readable: bool = True) -> tuple:
    """(epoch | None, landing facts, refusal | None) — the instant the deployed bytes landed.

    With a landing ref: the LATER of the landing leg and the arrival leg. With no landing
    block: the artifact's own deploy stamp, else its mtime. With a ref the ledger cannot
    resolve AND a declaration of the ledger-less class: the artifact's own arrival, and the
    class's obligations are judged by the caller (see the module docstring).
    """
    landing = proof.get("landing")
    landing = landing if isinstance(landing, dict) else {}
    refs = [str(landing[key]) for key in ("ref", "sha", "landed_sha", "card", "pr")
            if landing.get(key)]
    live_tree = _declares_live_tree(landing)
    if refs or live_tree:
        landing_at, refusal = _landing_leg(refs, rows)
        arrival_at, arrival_facts = _arrival_leg(refs, artifact_paths, rows)
        if landing_at is None and arrival_at is None:
            if live_tree:
                denied = _train_dest_for(artifact_paths, rows)
                if denied is not None:
                    dest, path, row = denied
                    return None, {
                        "live_tree_denied": {"dest": dest, "artifact": path,
                                             "row_sha": row.get("sha"), "row_at": row.get("at"),
                                             "row_stamped": row.get("stamped")}}, {
                        "clause": "landing_live_tree_denied",
                        "detail": (
                            f"the handoff declares these bytes ledger-less, but the deploy "
                            f"ledger's row at {row.get('at')!r} (sha {row.get('sha')!r}) is the "
                            f"current provenance of the dest {dest!r}, which contains {path!r}: "
                            f"a train-delivered artifact proves itself on the train (name the "
                            f"landing row, or the deploy row that arrived at that dest), and "
                            f"the ledger-less class cannot be declared to escape a ledger leg "
                            f"that exists"),
                    }
                stamp_at, stamp_facts = _bytes_landing(artifact_paths)
                facts = {"refs": refs, "landing_refs_unresolved_in_ledger": refs or None,
                         # rows into a shared dir whose bytes are NOT these bytes: recorded,
                         # never a refusal (a stale arrival cannot deny the class)
                         "live_tree_dest_rows": _dest_rows_for(artifact_paths, rows) or None}
                facts.update(stamp_facts)
                # AFTER the stamp facts: they carry their own ``landing_source`` (the stamp
                # or the mtime they read the instant from) and the class is what binds here.
                facts["landing_source"] = "live-tree"
                facts["live_tree"] = True
                facts["live_tree_undated_by"] = stamp_facts.get("landing_source")
                if stamp_at is None:
                    return None, facts, {
                        "clause": "landing_unresolved",
                        "detail": (f"the ledger-less landing at {artifact_paths[0]!r} cannot "
                                   "be dated: no readable artifact exists to supply the "
                                   "bytes' own arrival"),
                    }
                return stamp_at, facts, None
            if refusal is not None:
                return None, {}, refusal
            return None, {}, {
                "clause": "landing_unresolved",
                "detail": (
                    f"no admitted landing row in the deploy ledger names "
                    f"{', '.join(refs)}, and no deploy row of those bytes records an "
                    f"arrival at {artifact_paths[0]!r} or a parent of it; a landing "
                    f"reference must name a landing row (landed_sha, card, or PR). If no "
                    "departure delivers these bytes at all, declare them ledger-less: "
                    "\"landing\": {\"ref\": \"<what the override ships>\", "
                    "\"source\": \"live-tree\"} with a probe that names its observations"
                ),
            }
        facts = {"landing_source": "ledger", "refs": refs}
        if live_tree:
            facts["live_tree_declared"] = True
            facts["live_tree_ignored"] = "the ledger resolves these bytes"
        if landing_at is not None:
            facts["landing_row_at"] = _iso(landing_at)
        facts.update(arrival_facts)
        # Both legs bind: the run must be after the merge AND after the bytes arrived.
        return max(landing_at or 0.0, arrival_at or 0.0), facts, refusal

    stamp_at, stamp_facts = _bytes_landing(artifact_paths)
    if stamp_at is not None:
        return stamp_at, stamp_facts, None
    if not artifacts_readable:
        # The artifact clause refuses this proof by name; a second refusal derived from the
        # same unreadable path is noise, so the undated landing is recorded as a fact.
        return None, {"landing_source": None,
                      "landing_undated": "no readable artifact to date the landing"}, None
    return None, {}, {
        "clause": "landing_unresolved",
        "detail": "no landing record was named and no deployed artifact exists to date "
                  "the landing",
    }


# -- artifacts ----------------------------------------------------------------


def _artifacts(proof: dict):
    """[(path, declared sha256, declared commit, declared blob, declared repo, record)] —
    None if malformed. ``record`` is the R3 durable-referent dict (or None)."""
    raw = proof.get("artifacts")
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        return None
    entries = []
    for item in raw:
        if isinstance(item, str):
            entries.append((item, None, None, None, None, None))
            continue
        if not isinstance(item, dict) or not item.get("path"):
            return None
        declared = item.get("sha256") or item.get("hash")
        commit = item.get("commit") or item.get("provenance")
        blob = item.get("blob")
        if blob and not commit:
            # ``blob`` is "<commit>:<path in repo>": the commit half is the provenance
            # half, and the pair is resolved with git in _blob_bytes.
            commit, _, _ = str(blob).partition(":")
        repo = item.get("repo") or item.get("repo_path")
        record = item.get("record")
        entries.append((
            str(item["path"]),
            str(declared).strip().lower() if declared else None,
            str(commit).strip().lower() if commit else None,
            str(blob).strip() if blob else None,
            str(repo).strip() if repo else None,
            record if isinstance(record, dict) else None,
        ))
    return entries or None


_BLOB_TIMEOUT = 15


def _checkout_of(path: str) -> Optional[str]:
    """The git work tree ``path`` lives in -> its root, or None when it is in none."""
    current = os.path.dirname(os.path.abspath(path))
    for _ in range(_STAMP_MAX_WALK + 1):
        if os.path.exists(os.path.join(current, ".git")):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    return None


def _blob_bytes(blob: str, artifact_path: str, repo: Optional[str]) -> tuple:
    """Resolve ``<commit>:<path in repo>`` -> (sha256 of the blob | None, facts, detail).

    The work tree is the declared ``repo``, else the checkout the deployed artifact
    itself lives in — and with no declared ``repo`` the in-repo path is read off the
    artifact, so the comparison is ``git show <commit>:<the deployed file's own path>``.
    A declared ``repo`` may name the in-repo path explicitly; the bytes comparison is
    what validates that naming. Every git failure is this clause's own cause
    (``artifact_blob_unresolved``), never a hash complaint: an unreadable pin is not
    evidence that the bytes are wrong.
    """
    commit, _, relpath = blob.partition(":")
    commit, relpath = commit.strip(), relpath.strip().lstrip("/")
    if not commit or not relpath:
        return None, {}, (
            f"the blob pin {blob!r} is not \"<commit>:<path in repo>\"")
    work_tree = os.path.abspath(repo) if repo else _checkout_of(artifact_path)
    if not work_tree or not os.path.isdir(work_tree):
        return None, {}, (
            f"no git work tree holds {artifact_path} to resolve the blob pin {blob!r} "
            f"in: declare \"repo\": \"<abs path to the work tree>\"")
    if not repo:
        relpath = os.path.relpath(os.path.abspath(artifact_path), work_tree)
    try:
        result = subprocess.run(
            ["git", "-C", work_tree, "show", f"{commit}:{relpath}"],
            capture_output=True, timeout=_BLOB_TIMEOUT, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return None, {}, (
            f"the blob pin {blob!r} could not be resolved in {work_tree}: {exc}")
    if result.returncode != 0:
        why = (result.stderr or b"").decode("utf-8", "replace").strip().splitlines()
        return None, {}, (
            f"{commit[:16]}:{relpath} is not a blob in {work_tree}: "
            f"{why[0] if why else f'git exited {result.returncode}'}")
    return hashlib.sha256(result.stdout).hexdigest(), {
        "blob_repo": work_tree, "blob_path": relpath, "blob_commit": commit,
    }, None


# -- durable referents (ruling t_6f582c2b, D1/D2) ------------------------------


def _manifest_rows(manifest_path: str) -> Optional[dict]:
    """{repo-relative path: sha256} from an untracked-overrides MANIFEST.md, or None."""
    try:
        with open(manifest_path, "r", encoding="utf-8", errors="replace") as handle:
            text = handle.read()
    except OSError:
        return None
    rows: dict = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [cell.strip().strip("`").strip()
                 for cell in stripped.strip("|").split("|")]
        if len(cells) < 2:
            continue
        key, value = cells[0], cells[1].lower()
        if key and _HEX64.match(value):
            rows[key] = value
    return rows


def _override_manifest_referent(path: str, declared: Optional[str], rid: str,
                                root: str) -> tuple:
    """R3 override-manifest: the MANIFEST row (and copy, when present) must carry these bytes."""
    norm = os.path.normpath(rid)
    if os.path.isabs(norm) or norm.startswith("..") or norm in (".", ""):
        return False, {}, (
            f"the override-manifest \"id\" must be a repo-relative path, not {rid!r}")
    manifest = os.path.join(root, "MANIFEST.md")
    rows = _manifest_rows(manifest)
    if rows is None:
        return False, {}, f"the untracked-overrides MANIFEST cannot be read: {manifest}"
    row_sha = rows.get(rid)
    if not row_sha:
        return False, {"record_kind": "override-manifest", "record_id": rid}, (
            f"the untracked-overrides MANIFEST has no row for {rid!r}: {manifest}")
    facts = {"record_kind": "override-manifest", "record_id": rid,
             "record_manifest": manifest, "record_row_sha256": row_sha}
    if declared and row_sha != declared:
        return False, facts, (
            f"the MANIFEST row for {rid!r} reads {row_sha[:16]} but the pin declares "
            f"{declared[:16]}")
    copy = os.path.join(root, rid)
    if os.path.isfile(copy):
        copy_sha = _hash_file(copy)
        facts["record_copy"] = copy
        facts["record_copy_sha256"] = copy_sha
        expect = declared or row_sha
        if copy_sha != expect:
            return False, facts, (
                f"the byte copy at {copy} reads {(copy_sha or '')[:16]} but the pin "
                f"declares {expect[:16]}")
    return True, facts, None


def _payload_record_referent(path: str, declared: Optional[str], rid: str,
                             root: str) -> tuple:
    """R3 payload-record: the card's delta record + its .sha256 sidecar must both check out."""
    name = rid if rid.endswith(".diff") else rid + "-payload.diff"
    if os.path.isabs(name) or "/" in name or os.sep in name:
        return False, {}, (
            f"the payload-record \"id\" must be a bare record name, not {rid!r}")
    stem = name[:-len(".diff")]
    if not _CARD_TOKEN.search(stem):
        return False, {}, (
            f"the payload record's name {name!r} does not name a card "
            "(expected <card>-payload.diff)")
    record_path = os.path.join(root, name)
    if not os.path.isfile(record_path):
        return False, {"record_kind": "payload-record", "record_id": rid}, (
            f"the payload record does not exist: {record_path}")
    record_sha = _hash_file(record_path)
    facts = {"record_kind": "payload-record", "record_id": rid,
             "record_path": record_path, "record_sha256": record_sha}
    sidecar = record_path + ".sha256"
    try:
        with open(sidecar, "r", encoding="utf-8", errors="replace") as handle:
            side_text = handle.read().strip()
    except OSError:
        return False, facts, f"the payload record has no .sha256 sidecar: {sidecar}"
    tokens = side_text.split()
    side_sha = tokens[0].lower() if tokens else ""
    facts["record_sidecar"] = sidecar
    facts["record_sidecar_sha256"] = side_sha or None
    if not _HEX64.match(side_sha) or side_sha != record_sha:
        return False, facts, (
            f"the .sha256 sidecar ({side_sha[:16] or 'empty'}) does not equal the record's "
            f"content hash ({(record_sha or '')[:16]})")
    return True, facts, None


def _record_referent(path: str, declared: Optional[str], record: dict, *,
                     overrides_root: Optional[str] = None,
                     preroll_root: Optional[str] = None) -> tuple:
    """(admitted, facts, detail) for an R3 ``record`` — fails CLOSED on anything unreadable."""
    kind = str(record.get("kind") or "").strip().lower()
    rid = record.get("id")
    rid = str(rid).strip() if rid is not None else ""
    if not kind or not rid:
        return False, {}, "the artifact's \"record\" must name both a \"kind\" and an \"id\""
    if kind in ("override-manifest", "override_manifest", "manifest"):
        return _override_manifest_referent(path, declared, rid, _overrides_root(overrides_root))
    if kind in ("payload-record", "payload_record", "payload"):
        return _payload_record_referent(path, declared, rid, _preroll_root(preroll_root))
    return False, {}, (
        f"the artifact's record kind {kind!r} is not one the gate can read (use "
        "\"override-manifest\" or \"payload-record\")")


def _durable_referent(path: str, declared: Optional[str], blob_present: bool,
                      blob_resolved: bool, record: Optional[dict], *,
                      ledger_bound: bool, overrides_root: Optional[str],
                      preroll_root: Optional[str]) -> tuple:
    """(admitted, facts, detail) — R1 blob OR R2 ledger OR R3 record for one artifact."""
    if blob_resolved:
        return True, {"referent": "blob"}, None
    if ledger_bound:
        return True, {"referent": "ledger"}, None
    if record is not None:
        admitted, facts, detail = _record_referent(
            path, declared, record, overrides_root=overrides_root,
            preroll_root=preroll_root)
        return admitted, ({"referent": "record", **facts} if admitted else facts), detail
    if blob_present:
        return False, {}, ("the declared \"blob\" does not resolve "
                           "(see the artifact_blob_unresolved clause)")
    return False, {}, ("no \"blob\", no admitted ledger landing ref and no \"record\" "
                       "is declared")


def _artifact_refusals(entries: list, facts: dict, *, ledger_bound: bool = False,
                       overrides_root: Optional[str] = None,
                       preroll_root: Optional[str] = None) -> list:
    """Every artifact clause: the declared bytes must BE the live artifact, and the pin
    must name a DURABLE REFERENT the gate can re-resolve at handoff (ruling t_6f582c2b)."""
    refusals = []
    for path, declared, commit, blob, repo, record in entries:
        actual = _hash_file(path)
        if actual is None:
            refusals.append(dict(clause="artifact_missing", detail=(
                f"the named deployed artifact could not be read: {path}")))
            continue
        row: dict = {"live": actual, "declared": declared or actual}
        if declared and declared != actual:
            refusals.append(dict(clause="artifact_hash_mismatch", detail=(
                f"the deployed artifact is not the bytes this handoff declared: {path} "
                f"live {actual[:16]} != declared {declared[:16]}")))
        if commit or blob:
            row["declared_commit"] = commit
            # The dest's own stamp is recorded as provenance EVIDENCE, never as a clause:
            # the bytes comparison below is what decides, and artifacts deployed outside
            # the train carry no stamp at all.
            stamp_path, stamp = _stamp_for(path)
            stamp_commit = str((stamp or {}).get("commit") or "").strip().lower()
            row["stamp"] = stamp_path
            row["stamp_commit"] = stamp_commit or None
            row["stamp_at"] = (stamp or {}).get("deployed") or (stamp or {}).get("at")
        blob_resolved = False
        if blob:
            row["declared_blob"] = blob
            if repo:
                row["declared_repo"] = repo
            blob_hash, blob_facts, detail = _blob_bytes(blob, path, repo)
            if blob_hash is None:
                refusals.append(dict(clause="artifact_blob_unresolved", detail=(
                    f"the artifact's merged blob cannot be checked: {detail}")))
            else:
                blob_resolved = True
                row.update(blob_facts)
                row["blob_sha256"] = blob_hash
                if blob_hash != actual:
                    refusals.append(dict(clause="artifact_hash_mismatch", detail=(
                        f"the deployed artifact is not the merged blob this handoff "
                        f"declared: {path} live {actual[:16]} != "
                        f"{blob_facts['blob_commit'][:16]}:{blob_facts['blob_path']} "
                        f"({blob_hash[:16]})")))
        admitted, referent_facts, referent_detail = _durable_referent(
            path, declared, bool(blob), blob_resolved, record,
            ledger_bound=ledger_bound, overrides_root=overrides_root,
            preroll_root=preroll_root)
        row.update(referent_facts)
        if not admitted:
            refusals.append(dict(clause="artifact_unpinned", detail=(
                f"the deployed artifact names no durable referent: {path} (live sha256 "
                f"{actual}). {referent_detail}. Remedy: pin the blob at a commit that "
                f"carries these bytes (\"blob\": \"<commit>:<path in repo>\"), or name the "
                f"record that carries this path (\"record\": {{\"kind\": "
                f"\"override-manifest\"|\"payload-record\", \"id\": ...}}), writing the "
                f"record first when it does not exist yet")))
        facts["artifacts"][path] = row
    return refusals


# -- run records --------------------------------------------------------------


def _card_run(conn: Optional[sqlite3.Connection], spec: dict) -> tuple:
    if conn is None:
        return None, {}, {
            "clause": "run_unknown",
            "detail": "a card-store run record needs the board database, which this "
                      "caller did not supply",
        }
    run_id = spec.get("id")
    task_id = spec.get("task")
    sql = "SELECT id, task_id, outcome, started_at, ended_at FROM task_runs WHERE id = ?"
    params: tuple = (run_id,)
    if task_id:
        sql += " AND task_id = ?"
        params = (run_id, task_id)
    try:
        row = conn.execute(sql, params).fetchone()
    except sqlite3.Error as exc:
        return None, {}, {"clause": "run_unknown",
                          "detail": f"the board database could not be read: {exc}"}
    if row is None:
        return None, {}, {
            "clause": "run_unknown",
            "detail": f"run {run_id!r} for card {task_id or '<any>'} is not in this "
                      "board's task_runs — for store=card the id must be a task_runs "
                      "ROW id (the run's own numeric id), never the card id",
        }
    keys = row.keys()
    outcome = str(row["outcome"] or "").strip().lower() if "outcome" in keys else ""
    stamp = parse_ts(row["ended_at"] if "ended_at" in keys else None) or \
        parse_ts(row["started_at"] if "started_at" in keys else None)
    if stamp is None:
        return None, {}, {"clause": "run_unknown",
                          "detail": f"run {run_id!r} carries no timestamp"}
    facts = {"run_id": run_id, "run_store": "card", "run_task": task_id or row["task_id"],
             "run_outcome": outcome or None, "finished_at": _iso(stamp)}
    return ({
        "at": stamp,
        "green": outcome in GREEN_CARD_OUTCOMES,
        "label": f"card run {row['task_id']}:{run_id} ({outcome or 'no outcome'})",
    }, facts, None)


def _store_run(path: str, run_id: Any) -> tuple:
    if not os.path.exists(path):
        return None, {}, {"clause": "run_unknown",
                          "detail": f"the run store {path} does not exist"}
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
    except sqlite3.Error as exc:
        return None, {}, {"clause": "run_unknown",
                          "detail": f"the run store {path} could not be opened: {exc}"}
    try:
        try:
            row = conn.execute(
                "SELECT id, workflow_name, status, started_at, finished_at, verdict "
                "FROM runs WHERE id = ?", (run_id,),
            ).fetchone()
        except sqlite3.Error as exc:
            return None, {}, {"clause": "run_unknown",
                              "detail": f"the run store {path} could not be read: {exc}"}
    finally:
        conn.close()
    if row is None:
        return None, {}, {"clause": "run_unknown",
                          "detail": f"run {run_id!r} is not in {path}"}
    status = str(row["status"] or "").strip().lower()
    verdict = str(row["verdict"] or "").strip().lower()
    started = parse_ts(row["started_at"])
    stamp = parse_ts(row["finished_at"]) or started
    if stamp is None:
        return None, {}, {"clause": "run_unknown",
                          "detail": f"run {run_id!r} carries no timestamp"}
    facts = {"run_id": row["id"], "run_store": os.path.basename(path),
             "run_workflow": row["workflow_name"], "run_status": status or None,
             "run_verdict": verdict or None, "started_at": _iso(started),
             "finished_at": _iso(stamp)}
    return ({
        "at": stamp,
        "green": (status in GREEN_RUN_STATUSES
                  and (not verdict or verdict in GREEN_RUN_VERDICTS)),
        "label": f"run {row['id']} ({row['workflow_name']}, "
                 f"{verdict or status or 'no status'})",
    }, facts, None)


_OBSERVATION_CAP = 20


def _observations(value: Any) -> list:
    """The named observations a ledger-less probe must carry, normalised and bounded.

    A string is one observation; a dict keeps its own fields so a structured observation
    stays readable in the facts. Bounded (20 entries, 500 characters each) so a handoff
    cannot turn ``facts`` into a log dump.
    """
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return []
    named = []
    for item in list(value)[:_OBSERVATION_CAP]:
        if isinstance(item, dict):
            name = str(item.get("observation") or item.get("name") or item.get("what")
                       or "").strip()
            detail = str(item.get("detail") or item.get("evidence") or item.get("result")
                         or "").strip()
            text = f"{name}: {detail}" if name and detail else (name or detail)
            if not text:
                text = json.dumps(item, sort_keys=True, default=str)
        else:
            text = str(item)
        text = " ".join(text.split())
        if text:
            named.append(text[:500])
    return named


def _proof_record(proof: dict, conn: Optional[sqlite3.Connection], run_store_path: str) -> tuple:
    """(record, facts, refusal) for the run record or probe the handoff offers as proof."""
    run = proof.get("run")
    if isinstance(run, dict) and run.get("id") is not None:
        store = str(run.get("store") or "yoyoflow").strip().lower()
        if store == "card":
            return _card_run(conn, run)
        if store in ("yoyoflow", "run-store", "run_store"):
            return _store_run(run_store_path, run.get("id"))
        return None, {}, {
            "clause": "malformed_proof",
            "detail": f"unknown run store {store!r}; use \"yoyoflow\" for a scheduled "
                      "workflow run or \"card\" for a card turn",
        }
    probe = proof.get("probe")
    if isinstance(probe, dict) and probe:
        stamp = parse_ts(probe.get("at") or probe.get("finished_at") or probe.get("started_at"))
        if stamp is None:
            return None, {}, {
                "clause": "malformed_proof",
                "detail": "probe needs a parsable ``at`` timestamp (ISO-8601)",
            }
        result = str(probe.get("result") or "").strip().lower()
        facts = {"probe_at_claimed": probe.get("at"), "probe_at": _iso(stamp),
                 "probe_result": result or None,
                 "probe_path": probe.get("path") or probe.get("url"),
                 "probe_observations": _observations(probe.get("observations"))}
        return ({
            "at": stamp,
            "green": result in GREEN_PROBE_RESULTS,
            "label": f"probe ({result or 'no result'})",
        }, facts, None)
    return None, {}, {
        "clause": "no_run_or_probe",
        "detail": "the proof carries no run record and no probe: a landing is not a run. "
                  "Name the run that exercised the deployed bytes (run.store "
                  "\"yoyoflow\" or \"card\") or the post-deploy probe that exercised them",
    }


def witness(payload: Any, conn: Optional[sqlite3.Connection] = None, *,
            run_store_path: Optional[str] = None) -> Verdict:
    """Judge a bare run/probe WITNESS: no artifact, no landing, one question.

    ``evaluate`` answers a ``landed`` card's question — does this proof cover the deployed
    artifact, and is the evidence newer than its landing. The completion evidence gate
    (``hermes_cli/kanban_gate_invariants.py``) asks a smaller one: is the run or probe this
    handoff names RESOLVABLE, and is it GREEN. Same stores, same refusal vocabulary, fewer
    clauses — so a cause read off an evidence refusal means what it means off a proof refusal.
    """
    if not isinstance(payload, dict) or not payload:
        return Verdict(False, "no_run_or_probe",
                       f"the evidence names no run and no probe; {PROOF_SHAPE}")
    record, facts, refusal = _proof_record(payload, conn, _run_store_path(run_store_path))
    if refusal is not None:
        return Verdict(False, refusal["clause"], refusal["detail"], facts)
    if record is None:
        return Verdict(False, "no_run_or_probe",
                       f"the evidence names no run and no probe; {PROOF_SHAPE}")
    facts = dict(facts)
    facts["witness"] = record["label"]
    facts["witness_at"] = _iso(record["at"])
    facts["witness_kind"] = "run" if payload.get("run") else "probe"
    if not record["green"]:
        return Verdict(False, "run_not_green",
                       f"{record['label']} is not green: a run that did not succeed is not "
                       f"evidence that the work was done, so a card may not close on it", facts)
    return Verdict(True, None, "", facts)


# -- the gate -----------------------------------------------------------------


def evaluate(
    proof: Any,
    conn: Optional[sqlite3.Connection] = None,
    *,
    ledger_path: Optional[str] = None,
    run_store_path: Optional[str] = None,
    overrides_root: Optional[str] = None,
    preroll_root: Optional[str] = None,
) -> Verdict:
    """Judge a ``landed`` card's proof block against the stores, never against its prose.

    Every clause that can be evaluated is evaluated; ``cause`` names the one to act on
    first, and ``failures`` lists all of them (see ``_CAUSE_PRECEDENCE``).
    """
    if proof is None:
        return Verdict(False, "no_proof", (
            f"this card is authored ``landed``: its completion must carry the deployed "
            f"artifact, its landing, and a run or probe dated after it. Pass {PROOF_SHAPE}"))
    if not isinstance(proof, dict):
        return Verdict(False, "malformed_proof", f"proof must be an object; {PROOF_SHAPE}")
    entries = _artifacts(proof)
    if not entries:
        return Verdict(False, "malformed_proof", (
            f"proof.artifacts must name at least one deployed artifact path; {PROOF_SHAPE}"))

    facts: dict = {"artifacts": {}}
    verdict = Verdict(True, None, "", facts)
    paths = [path for path, _declared, _commit, _blob, _repo, _record in entries]
    readable = [path for path in paths if os.path.isfile(path)]

    # 1. LANDING — the DoD clause itself, and the instant the run below must postdate. It is
    #    resolved FIRST: an artifact complaint (a wrong path, a stale pin) must never be
    #    reported while the timestamps went unconsidered.
    rows = list(_ledger_rows(_ledger_path(ledger_path)))
    landing_at, landing_facts, refusal = _landing_time(
        proof, paths, rows, artifacts_readable=bool(readable))
    facts.update(landing_facts)
    if refusal is not None:
        verdict.fail(refusal["clause"], refusal["detail"])
    if landing_at is not None:
        facts["landed_at"] = _iso(landing_at)
    landing = proof.get("landing")
    if isinstance(landing, dict) and landing:
        facts["landing_ref"] = {key: landing[key] for key in
                                ("ref", "sha", "landed_sha", "card", "pr") if landing.get(key)}

    # 2. RUN / PROBE — the evidence the contract is about: after the landing, green, and (for
    #    a ledger-less landing) carrying the named observations that class owes.
    record, record_facts, refusal = _proof_record(
        proof, conn, _run_store_path(run_store_path))
    if refusal is not None:
        verdict.fail(refusal["clause"], refusal["detail"])
    if record is not None:
        facts.update(record_facts)
        facts["proof_at"] = _iso(record["at"])
        facts["proof_kind"] = "run" if proof.get("run") else "probe"
        if landing_at is not None and record["at"] <= landing_at:
            verdict.fail("proof_predates_landing", (
                f"the proof predates the landing: {record['label']} at "
                f"{_iso(record['at'])} is at or before the landing at {_iso(landing_at)}. "
                "Evidence from before the landing proves the PREVIOUS bytes — record a "
                "run dated after the deployed bytes landed"))
        if not record["green"]:
            verdict.fail("run_not_green", (
                f"the proof of record did not succeed: {record['label']}"))
        if facts.get("landing_source") == "live-tree":
            kind = facts.get("proof_kind")
            subjects = ", ".join(paths) or "<no path>"
            if kind == "run" and facts.get("run_store") == "card":
                verdict.fail("live_tree_unproven", (
                    f"the landing at {subjects} is ledger-less, so a card-store run is not "
                    f"evidence about it: {record['label']} says a card turn finished, not "
                    "that the live bytes behave. Probe the deployed behaviour and name what "
                    "you observed"))
            elif kind == "probe" and not facts.get("probe_observations"):
                verdict.fail("live_tree_unproven", (
                    f"the landing at {subjects} is ledger-less, so its probe must name the "
                    "behaviours it exercised: add \"observations\": [\"<behaviour 1>\", "
                    "\"<behaviour 2>\", ...] to the probe. An unnamed probe is a claim, and "
                    "a claim is not a probe"))

    # 3. ARTIFACT IDENTITY — last, because the remedy for these clauses is a re-pin of the
    #    declared hash, and a re-pin must never be what a worker is told to do while the
    #    evidence above is what is actually wrong.
    ledger_bound = str(facts.get("landing_source") or "") == "ledger"
    for refusal in _artifact_refusals(
            entries, facts, ledger_bound=ledger_bound,
            overrides_root=overrides_root, preroll_root=preroll_root):
        verdict.fail(refusal["clause"], refusal["detail"])

    verdict.settle()
    if verdict.ok:
        verdict.detail = (
            f"{record['label']} at {_iso(record['at'])} is after the landing at "
            f"{_iso(landing_at)} and covers the deployed artifact")
    return verdict


def render(task_id: str, verdict: Verdict) -> str:
    """The refusal text a worker or operator reads: the clause, the numbers, the fix."""
    if verdict.ok:
        return f"proof gate: {task_id} — {verdict.detail}"
    parts = [f"proof gate refused {task_id} [{verdict.cause}]: {verdict.detail}."]
    others = [failure for failure in verdict.failures if failure["clause"] != verdict.cause]
    if others:
        parts.append(
            "Also failing, and not fixed by the first clause (every one of these has to "
            "clear): " + "; ".join(f"[{f['clause']}] {f['detail']}" for f in others) + ".")
    parts.append(
        "Re-pinning the artifact hash cannot clear a timing failure: a stale pin beside a "
        "predating run is still a predating run, and the run is what the contract is about.")
    parts.append(
        f"Nothing changed — the card is still in flight. Retry kanban_complete with "
        f"{PROOF_SHAPE}; if the unit is genuinely not deployed, `hermes kanban set-contract "
        f"{task_id} local-only --reason '<why>'` releases the fence first.")
    return " ".join(parts)


# -- the read-only pin audit (ruling t_6f582c2b, D3(iii)) ---------------------


def audit_pin(entry: Any, *, ledger_refs: Iterable = (), rows: Iterable = (),
              overrides_root: Optional[str] = None,
              preroll_root: Optional[str] = None) -> dict:
    """READ-ONLY tri-state of one declared artifact pin.

    Returns exactly one of ``resolves`` / ``superseded`` / ``unverifiable`` with the facts
    that decided it. It never writes, never reads the board store, and never raises on a
    legacy pin carrying none of the new fields — such a pin is ``unverifiable`` by
    definition.

      * ``resolves``   — a durable referent (a git ``blob``, an admitted ledger landing
        row, or a readable ``record``) resolves AND the live bytes still match the pin;
      * ``superseded`` — a durable referent resolves but the live bytes have moved on:
        "landed then superseded", distinguishable from "never landed";
      * ``unverifiable`` — no durable referent was recorded. The live bytes are NOT a
        referent (a later payload can overwrite them), so this is recorded as a property
        of the pin's CLASS, never as a finding against the card.
    """
    if isinstance(entry, str):
        item: dict = {"path": entry}
    elif isinstance(entry, dict):
        item = entry
    else:
        return {"state": "unverifiable", "path": None, "declared": None, "live": None,
                "referent": None, "reasons": ["the pin is not a path string or an object"]}
    path = str(item.get("path") or "").strip()
    declared = item.get("sha256") or item.get("hash")
    declared = str(declared).strip().lower() if declared else None
    blob = item.get("blob")
    blob = str(blob).strip() if blob else None
    repo = item.get("repo") or item.get("repo_path")
    repo = str(repo).strip() if repo else None
    record = item.get("record")
    record = record if isinstance(record, dict) else None

    reasons: list = []
    live = _hash_file(path) if path else None
    if live is None and path:
        reasons.append(f"the pinned path is not a readable file: {path}")

    referent = None
    referent_hash = None
    if blob:
        blob_hash, _blob_facts, detail = _blob_bytes(blob, path, repo)
        if blob_hash is not None:
            referent, referent_hash = "blob", blob_hash
        elif detail:
            reasons.append(detail)
    if referent is None and ledger_refs:
        refs, row_list = list(ledger_refs), list(rows)
        landing_at, _ = _landing_leg(refs, row_list)
        bound = landing_at is not None
        if not bound:
            arrival_at, _ = _arrival_leg(refs, [path], row_list)
            bound = arrival_at is not None
        if bound:
            referent = "ledger"
    if referent is None and record is not None:
        try:
            admitted, rec_facts, detail = _record_referent(
                path, declared, record, overrides_root=overrides_root,
                preroll_root=preroll_root)
        except Exception as exc:  # a record check must never raise out of the audit
            admitted, rec_facts, detail = False, {}, f"the record could not be read: {exc}"
        if admitted:
            referent = "record"
            referent_hash = (rec_facts.get("record_row_sha256")
                             or rec_facts.get("record_sha256"))
        elif detail:
            reasons.append(detail)

    if referent is None:
        reasons.append("no durable referent was recorded for this pin")
        state = "unverifiable"
    else:
        live_match = bool(
            live is not None
            and ((declared is not None and live == declared)
                 or (referent_hash is not None and live == referent_hash)))
        state = "resolves" if live_match else "superseded"
        if not live_match:
            reasons.append("the live bytes no longer match the pinned identity")
    return {"state": state, "path": path or None, "declared": declared, "live": live,
            "referent": referent, "referent_sha256": referent_hash, "reasons": reasons}


def render_audit(task_id: str, audits: Iterable) -> str:
    """The tri-state census a reader sees: one mechanical, read-only line per pin."""
    lines = [f"pin audit {task_id}:"]
    for audit in audits:
        if not isinstance(audit, dict):
            lines.append(f"  [unverifiable] {audit!r}")
            continue
        lines.append(
            f"  [{audit.get('state')}] {audit.get('path')} "
            f"declared={(audit.get('declared') or '<none>')[:16]} "
            f"live={(audit.get('live') or '<none>')[:16]} "
            f"referent={audit.get('referent') or '<none>'}")
    return "\n".join(lines)
