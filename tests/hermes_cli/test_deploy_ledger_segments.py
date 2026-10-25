"""Reader contract for the rotated deploy ledger (card t_1b4d6bac, deliverable 2).

The ledger is proof of work that has already landed: gate.py check 2 and the kanban proof gate
both decide by reading its rows. So the arms below are about one property - a rotation must be
INVISIBLE to a reader - plus the negative one that nothing is ever truncated.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from hermes_cli import deploy_ledger


def _write(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")


@pytest.fixture()
def ledger(tmp_path):
    anchor = tmp_path / "deploy-ledger.jsonl"
    _write(anchor, [{"kind": "landing", "sha": "aaa111", "allowed": True}])
    return anchor


def test_segments_of_a_fresh_ledger_is_just_the_anchor(ledger):
    assert deploy_ledger.segments(str(ledger)) == [str(ledger)]
    assert deploy_ledger.read_rows(str(ledger)) == [{"kind": "landing", "sha": "aaa111", "allowed": True}]


def test_rotation_renames_the_anchor_and_keeps_every_row(ledger):
    rotated = deploy_ledger.rotate(str(ledger), min_bytes=1, now=datetime(2026, 10, 25, 3, 15, tzinfo=timezone.utc))

    assert rotated == "deploy-ledger.jsonl.bak-20261025T031500Z"
    assert not ledger.exists(), "the anchor is RENAMED, never copied-and-emptied"
    assert ledger.with_name(rotated).stat().st_size > 0, "rotation must not truncate"
    # the row is still readable, through the set, with the anchor absent
    assert deploy_ledger.read_rows(str(ledger)) == [{"kind": "landing", "sha": "aaa111", "allowed": True}]


def test_a_landing_written_after_the_rotation_is_read_after_the_older_rows(ledger):
    deploy_ledger.rotate(str(ledger), min_bytes=1, now=datetime(2026, 10, 25, 3, 15, tzinfo=timezone.utc))
    _write(ledger, [{"kind": "landing", "sha": "bbb222", "allowed": True}])

    rows = deploy_ledger.read_rows(str(ledger))
    assert [row["sha"] for row in rows] == ["aaa111", "bbb222"], "append order, oldest segment first"
    assert deploy_ledger.segments(str(ledger))[-1] == str(ledger), "the anchor is always last (newest)"


def test_two_rotations_inside_one_second_do_not_collide(ledger):
    first = deploy_ledger.rotate(str(ledger), min_bytes=1, now=datetime(2026, 10, 25, 3, 15, tzinfo=timezone.utc))
    _write(ledger, [{"kind": "landing", "sha": "ccc333"}])
    second = deploy_ledger.rotate(str(ledger), min_bytes=1, now=datetime(2026, 10, 25, 3, 15, tzinfo=timezone.utc))

    assert first != second
    assert [row["sha"] for row in deploy_ledger.read_rows(str(ledger))] == ["aaa111", "ccc333"]


def test_the_rotation_writes_an_anchor_record_beside_the_ledger(ledger):
    """The house anchor shape, so both ledgers in /opt/hermes_prod are one auditable family."""
    deploy_ledger.rotate(str(ledger), min_bytes=1, now=datetime(2026, 10, 25, 3, 15, tzinfo=timezone.utc))

    record = json.loads(deploy_ledger.anchor_record_path(str(ledger)).read_text(encoding="utf-8"))
    assert record["segment"] == "deploy-ledger.jsonl.bak-20261025T031500Z"
    assert record["ledger"] == str(ledger)
    assert record["lines"] == 1 and record["bytes"] > 0
    assert len(record["sha256"]) == 64


def test_rotation_respects_the_size_floor_and_never_touches_a_small_ledger(ledger):
    assert deploy_ledger.rotate(str(ledger), min_bytes=10 * 1024 * 1024) is None
    assert ledger.exists() and ledger.stat().st_size > 0


def test_rotation_of_a_missing_anchor_is_a_no_op(tmp_path):
    assert deploy_ledger.rotate(str(tmp_path / "nope.jsonl"), min_bytes=0) is None


def test_unrelated_siblings_are_not_mistaken_for_segments(ledger):
    sibling = ledger.with_name("deploy-ledger.jsonl.tmp")
    sibling.write_text('{"kind": "landing", "sha": "zzz"}\n', encoding="utf-8")
    notes = ledger.with_name("deploy-ledger-notes.jsonl")
    notes.write_text('{"kind": "landing", "sha": "yyy"}\n', encoding="utf-8")

    assert deploy_ledger.segments(str(ledger)) == [str(ledger)]
    assert [row["sha"] for row in deploy_ledger.read_rows(str(ledger))] == ["aaa111"]


def test_the_proof_gate_reader_resolves_the_set(ledger, monkeypatch):
    """The gate's own reader, not a copy of it: a rotated landing row must still be found."""
    from hermes_cli import kanban_proof_gate as gate

    deploy_ledger.rotate(str(ledger), min_bytes=1, now=datetime(2026, 10, 25, 3, 15, tzinfo=timezone.utc))
    _write(ledger, [{"kind": "landing", "sha": "ddd444"}])

    rows = list(gate._ledger_rows(str(ledger)))
    assert [row["sha"] for row in rows] == ["aaa111", "ddd444"]
