"""Contract test for the dashboard's archive receipt.

The archive seam refuses an *anonymous* archive: a PATCH/POST that flips a card to
``archived`` must carry a machine-readable ``reason`` (and a ``destination`` of
``<board>/<card>`` for the reasons whose whole meaning is "another card inherits
this work"). Without the receipt the API answers 400 ``reason_missing`` — so a
dashboard door that forgets it does not corrupt anything, it just becomes a button
that never works. That is exactly the kind of breakage nobody notices, hence this
test.

The bundle is committed plain JS with no build step (see its header comment), so
the shipped call sites ARE the source: asserting on the file is asserting on the
delivered artifact. The bundle also keeps its own copy of the reason table — it
cannot import Python — and this test is what keeps that copy equal to
``kanban_db.ARCHIVE_REASONS``.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb

BUNDLE = (
    Path(__file__).resolve().parents[2]
    / "plugins" / "kanban" / "dashboard" / "dist" / "index.js"
)

_REASON_ROW = re.compile(
    r'\{\s*value:\s*"(?P<value>[^"]+)"\s*,'
    r'\s*label:\s*"[^"]*"\s*,'
    r'\s*needsDestination:\s*(?P<needs>true|false)\s*\}'
)


def _bundle_source() -> str:
    assert BUNDLE.exists(), f"dashboard bundle missing: {BUNDLE}"
    return BUNDLE.read_text(encoding="utf-8")


def _reason_rows() -> list[tuple[str, bool]]:
    """The bundle's ARCHIVE_REASONS table as ``[(value, needsDestination), ...]``."""
    src = _bundle_source()
    start = src.find("const ARCHIVE_REASONS = [")
    assert start >= 0, "the bundle no longer declares an ARCHIVE_REASONS table"
    end = src.find("];", start)
    assert end > start, "unterminated ARCHIVE_REASONS table"
    block = src[start:end]
    return [(m.group("value"), m.group("needs") == "true") for m in _REASON_ROW.finditer(block)]


def _assert_ordered(src: str, pairs: list[tuple[str, str]], door: str) -> None:
    """``pairs`` must appear in the file in the given order — that is what proves the
    receipt is collected *before* the request is built at that door."""
    cursor = 0
    for needle, why in pairs:
        idx = src.find(needle, cursor)
        assert idx >= 0, f"{door}: {why} — marker not found: {needle!r}"
        cursor = idx + len(needle)


def test_bundle_declares_the_reason_table():
    rows = _reason_rows()
    assert rows, "ARCHIVE_REASONS table parsed empty"
    # No duplicate tokens: a duplicated entry would make the numbered prompt lie
    # about which reason the human picked.
    values = [v for v, _ in rows]
    assert len(values) == len(set(values)), f"duplicate ARCHIVE_REASONS entries: {values}"


def test_bundle_reason_table_matches_backend():
    backend = list(getattr(kb, "ARCHIVE_REASONS", None) or ())
    if not backend:
        pytest.skip("kanban_db.ARCHIVE_REASONS not present in this tree (archive seam not landed yet)")
    assert [v for v, _ in _reason_rows()] == backend, (
        "the dashboard's ARCHIVE_REASONS table has drifted from kanban_db.ARCHIVE_REASONS; "
        "a reason the bundle offers but the backend refuses is a 400 for the human"
    )


def test_bundle_destination_reasons_match_backend():
    needing = set(getattr(kb, "ARCHIVE_REASONS_NEEDING_DESTINATION", None) or ())
    if not needing:
        pytest.skip("kanban_db.ARCHIVE_REASONS_NEEDING_DESTINATION not present in this tree")
    bundle_needing = {v for v, needs in _reason_rows() if needs}
    assert bundle_needing == needing, (
        "the reasons that demand a destination differ between the bundle and the backend; "
        "one side would ask for a link the other does not want"
    )


def test_single_archive_door_collects_a_receipt():
    """Door 1 — the shared move dispatch (drag / menu / any moveTask to 'archived')."""
    src = _bundle_source()
    _assert_ordered(src, [
        ("const performMoveTask = useCallback", "the shared move dispatch exists"),
        ('if (newStatus === "archived")', "the archived move is recognised"),
        ("const receipt = requestArchiveReceipt(count);", "it collects the receipt"),
        ("const finalPatch = summary", "the receipt rides the PATCH body"),
    ], "performMoveTask")
    assert 'body: JSON.stringify(finalPatch)' in src, (
        "the single-task PATCH body is no longer the receipt-bearing finalPatch"
    )


def test_bulk_move_to_archived_uses_the_archive_door():
    """Door 2 — a bulk move to 'archived'.

    The bulk *status* verb has no 'archived' handler server-side (archiving is a
    receipt-bearing operation, not a status verb), so the bulk move must ride
    ``archive: true`` or every card in the batch is refused as an unknown status.
    """
    src = _bundle_source()
    assert "const bulkBody = newStatus === \"archived\"" in src, (
        "the bulk move no longer routes 'archived' through the bulk archive door"
    )
    assert "Object.assign({ ids: ids, archive: true }, finalPatch)" in src, (
        "the bulk archive body no longer carries archive:true alongside the receipt"
    )
    assert "body: JSON.stringify(bulkBody)" in src, "the bulk move posts something other than bulkBody"


def test_bulk_archive_button_collects_a_receipt():
    """Door 3 — the bulk toolbar's Archive button (``{ archive: true }``)."""
    src = _bundle_source()
    _assert_ordered(src, [
        ("const applyBulk = useCallback", "the bulk apply path exists"),
        ("if (patch && patch.archive)", "a bulk archive is recognised"),
        ("const receipt = requestArchiveReceipt(count);", "it collects the receipt"),
        ("finalPatch = Object.assign({}, patch, receipt);", "the receipt is merged into the body"),
    ], "applyBulk")


def test_drawer_archive_action_collects_a_receipt():
    """Door 4 — the task drawer's Archive button (a PATCH through doPatch)."""
    src = _bundle_source()
    _assert_ordered(src, [
        ("function withArchiveReceipt(patch)", "the drawer has an archive-receipt step"),
        ('if (!patch || patch.status !== "archived") return patch;', "it only fires for an archive"),
        ("const receipt = requestArchiveReceipt(1);", "it collects the receipt"),
    ], "withArchiveReceipt")
    _assert_ordered(src, [
        ("function applyPatch(patch)", "the drawer's PATCH builder exists"),
        ("const receipted = withArchiveReceipt(finalPatch);", "it applies the receipt step"),
        ("if (!receipted) return Promise.resolve();", "a cancelled receipt aborts the PATCH"),
        ("body: JSON.stringify(receipted)", "the receipted patch is what gets sent"),
    ], "TaskDrawer.applyPatch")


def test_receipt_prompt_treats_cancel_as_a_no_op():
    """A cancelled prompt must mean "nothing was archived", never an anonymous archive."""
    src = _bundle_source()
    _assert_ordered(src, [
        ("function requestArchiveReceipt(count)", "the collector exists"),
        ("if (answer === null) return null;", "cancelling the reason returns null"),
        ("if (entered === null) return null;", "cancelling the destination returns null"),
        ("return receipt;", "a complete receipt is returned"),
    ], "requestArchiveReceipt")
