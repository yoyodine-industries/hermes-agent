"""t-37b8e0c562: a queue_full refusal row persists the cap that fired, and nothing derives one.

The defect these pin: ``build_envelope`` fell back to ``limit_kind or LIMIT_PER_PROFILE``, so a
row that persisted NO cap printed ``32 per profile`` -- a specific, actionable value invented from
absence, and the reason an operator went to raise the wrong limit twice.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from tools import bot_delivery_queue as q


def _did(n: int) -> str:
    return f"ab{n:030d}"


def _claimed(home, n=1):
    """Admit and claim one record, so it is settle-able."""
    q.admit(
        home, sender_profile="alpha", target_profile="bravo", target_session_id="sess-1",
        idempotency_key=f"peer-{_did(n)}", fingerprint=f"fp-{_did(n)}",
        delivery_id=_did(n), message=f"hello {n}",
    )
    claimed = q.claim_next(home, target_profile="bravo", lease_ok=True,
                           now_ns=time.time_ns())
    assert claimed is not None
    return claimed


def _settled_path(home, n):
    return Path(home) / "runtime" / q.DELIVERY_DIR_NAME / q.SETTLED_DIR / f"{_did(n)}.json"


def _base_record(**over):
    base = {
        "delivery_id": _did(1),
        "idempotency_key": "peer-1",
        "target_profile": "bravo",
        "sender_profile": "alpha",
        "status": q.STATUS_FAILED,
        "reason": "queue_full",
        "created_at": time.time_ns(),
        "updated_at": time.time_ns(),
    }
    base.update(over)
    return base


def test_a_row_with_no_limit_kind_renders_unknown_and_never_the_per_profile_default():
    """(c) THE REGRESSION: absence renders ``unknown``. This cannot pass by falling back --
    the historical default is exactly what the assertion forbids."""
    detail = q.build_envelope(_base_record())["detail"]
    assert q.CAP_UNKNOWN_LITERAL in detail
    assert "per_profile" not in detail
    assert "per_sender" not in detail
    assert "pending from this sender" not in detail


def test_the_envelope_prints_exactly_the_three_literals():
    """(b) Three literals and no fourth: per_profile, per_sender, unknown."""
    assert q.queue_full_cap_literal(_base_record(limit_kind=q.LIMIT_PER_PROFILE)) \
        == f"cap per_profile ({q.max_per_profile()})"
    assert q.queue_full_cap_literal({"limit_kind": q.LIMIT_PER_SENDER, "limit": 8}) \
        == "cap per_sender (8)"
    assert q.queue_full_cap_literal(_base_record()) == q.CAP_UNKNOWN_LITERAL
    # A persisted VALUE is used verbatim; the KIND is still never inferred.
    assert q.queue_full_cap_literal({"limit_kind": q.LIMIT_PER_PROFILE, "limit": 99}) \
        == "cap per_profile (99)"
    assert q.queue_full_cap_literal({"limit": 99}) == q.CAP_UNKNOWN_LITERAL


def test_a_queue_full_settlement_must_name_the_cap_that_fired(tmp_path):
    """(a) The cap is a REQUIRED key at the writer: a refusal row may not be written blind."""
    claimed = _claimed(tmp_path)
    with pytest.raises(ValueError) as excinfo:
        q.settle(tmp_path, claimed["delivery_id"], status=q.STATUS_FAILED, reply=None,
                 error="Bot Chat admission refused: queue_full", reason="queue_full")
    assert "limit_kind" in str(excinfo.value)
    assert not _settled_path(tmp_path, 1).exists()


def test_a_queue_full_settlement_persists_kind_limit_and_schema(tmp_path):
    claimed = _claimed(tmp_path)
    settled = q.settle(tmp_path, claimed["delivery_id"], status=q.STATUS_FAILED, reply=None,
                       error="queue full", reason="queue_full",
                       limit_kind=q.LIMIT_PER_SENDER, limit=8)
    assert settled["limit_kind"] == q.LIMIT_PER_SENDER
    assert settled["limit"] == 8
    assert settled["schema"] == q.REFUSAL_SCHEMA_V2
    stored = json.loads(_settled_path(tmp_path, 1).read_text())
    assert stored["schema"] == q.REFUSAL_SCHEMA_V2
    assert stored["limit_kind"] == q.LIMIT_PER_SENDER
    assert "cap per_sender (8)" in q.build_envelope(stored)["detail"]


def test_a_session_budget_class_is_recorded_and_never_rendered_as_a_delivery_cap(tmp_path):
    """The admission FIFO classes are session budgets, not delivery caps: persisted under
    ``limit_class``, and still ``cap unknown`` on the envelope -- naming a delivery cap that was
    not the one that fired is the same misdirection the default caused."""
    claimed = _claimed(tmp_path)
    settled = q.settle(tmp_path, claimed["delivery_id"], status=q.STATUS_FAILED, reply=None,
                       error="Bot Chat admission refused: queue_full", reason="queue_full",
                       limit_class="automation_fifo")
    assert settled["limit_class"] == "automation_fifo"
    assert settled["limit_kind"] is None
    detail = q.build_envelope(settled)["detail"]
    assert q.CAP_UNKNOWN_LITERAL in detail
    assert "per_profile" not in detail and "per_sender" not in detail


def test_the_refusal_census_line_counts_the_window_and_never_a_bare_total(tmp_path):
    """(d) N, how many persisted a kind, and the pre-fix window with its own instants."""
    home = tmp_path / "spool-home"
    for n in (1, 2, 3):
        claimed = _claimed(home, n)
        if n == 1:
            q.settle(home, claimed["delivery_id"], status=q.STATUS_FAILED, reply=None,
                     error="queue full", reason="queue_full",
                     limit_kind=q.LIMIT_PER_PROFILE, limit=32)
        else:
            q.settle(home, claimed["delivery_id"], status=q.STATUS_FAILED, reply=None,
                     error="queue full", reason="queue_full", limit_class="automation_fifo")
    # one PRE-FIX row: written by the old writer, carrying no kind and no schema
    legacy = json.loads(_settled_path(home, 1).read_text())
    legacy.pop("limit_kind", None)
    legacy.pop("schema", None)
    legacy["delivery_id"] = _did(9)
    _settled_path(home, 9).write_text(json.dumps(legacy))

    census = q.refusal_kind_census(home)
    assert census["queue_full"] == 4
    assert census["limit_kind_persisted"] == 1
    assert census["cap_unknown"] == 3
    line = q.refusal_kind_census_line(census)
    assert line.startswith("queue_full 4 · limit_kind persisted 1 · "
                           "cap-unknown (pre-fix window) 3, first ")
    assert ".. last " in line and line.endswith("Z")
    assert census["by_kind"]["per_profile"] == 1
    assert census["by_kind"]["class:automation_fifo"] == 2
    assert census["by_kind"][q.REFUSAL_SCHEMA_V1_UNKINDED] == 1


def test_an_empty_spool_says_so_instead_of_printing_a_zero_total(tmp_path):
    line = q.refusal_kind_census_line(q.refusal_kind_census(tmp_path))
    assert line == ("queue_full 0 · limit_kind persisted 0 · "
                    "cap-unknown (pre-fix window) 0, no window")


def test_the_post_deploy_probe_catches_a_cap_it_should_have_caught():
    """(e) The probe is an assertion, so prove it FAILS on the rows it exists to catch: a
    post-deploy row with no cap, a row whose envelope invents one from absence, and a clean row."""
    import importlib.util
    from pathlib import Path as _Path
    spec = importlib.util.spec_from_file_location(
        "probe_refusal_kinds",
        _Path(__file__).resolve().parents[2] / "scripts" / "probe_delivery_refusal_kinds.py")
    probe = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(probe)

    clean = _base_record(limit_kind=q.LIMIT_PER_SENDER, limit=8, schema=q.REFUSAL_SCHEMA_V2)
    assert probe.check([("seat", clean)]) == []

    unkinded = _base_record(schema=q.REFUSAL_SCHEMA_V2)
    problems = probe.check([("seat", unkinded)])
    assert any("NO cap recorded" in p for p in problems)

    unstamped = _base_record(limit_kind=q.LIMIT_PER_PROFILE, limit=32)
    problems = probe.check([("seat", unstamped)])
    assert any(q.REFUSAL_SCHEMA_V2 in p for p in problems)

    # A row that DOES carry per_profile but whose detail would print per_sender: the probe reads
    # the envelope, so a render-side regression is caught by the same check.
    class _Lying(dict):
        pass

    lying = _base_record(limit_kind=q.LIMIT_PER_PROFILE, limit=32, schema=q.REFUSAL_SCHEMA_V2)
    original = q.build_envelope
    try:
        def _swapped(record, *a, **kw):
            env = original(record, *a, **kw)
            env["detail"] = env["detail"].replace("cap per_profile", "cap per_sender")
            return env
        q.build_envelope = _swapped
        problems = probe.check([("seat", lying)])
    finally:
        q.build_envelope = original
    assert any("does not print" in p for p in problems)
