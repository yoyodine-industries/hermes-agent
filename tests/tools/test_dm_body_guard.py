"""Tests for the sender-side truncation guard.

The rule's whole value is its narrowness: it must fire on a body whose tail is a truncation
marker, and must NOT fire on prose that merely mentions truncation — a guard that refuses
good messages gets bypassed and the incident recurs.
"""

import pytest

from tools.dm_body_guard import (
    find_truncation_marker,
    guard_outbound_body,
    truncation_refusal,
)


@pytest.mark.parametrize("body,expected", [
    ("[truncated]", "[truncated]"),
    ("full sentence. [truncated]", "[truncated]"),
    ("... [truncated]", "[truncated]"),
    ("…[truncated]", "[truncated]"),
    ("[...truncated]", "[...truncated]"),
    ("message body\n[TRUNCATED]", "[TRUNCATED]"),
    ("<truncated>", "<truncated>"),
    ("(truncated)", "(truncated)"),
    ("[truncated at 4096 chars]", "[truncated at 4096 chars]"),
])
def test_end_anchored_markers_are_detected(body, expected):
    assert find_truncation_marker(body) == expected


@pytest.mark.parametrize("body", [
    "",
    "the log line is truncated.",
    "I am truncating the table for the report.",
    "the sender's stored argument is truncated, which is the whole finding",
    "the DMs looked like this: [truncated] and then they stopped arriving",
    "see [the truncated log] for detail",
    "[details omitted] deliberately, no machinery involved",
])
def test_prose_and_mid_body_quotations_are_not_markers(body):
    assert find_truncation_marker(body) is None


def test_refusal_names_the_marker_and_what_happened():
    refusal = truncation_refusal("analysis follows. [truncated]")
    assert refusal is not None
    assert "REFUSED" in refusal
    assert "'[truncated]'" in refusal
    assert "NOTHING was sent" in refusal


def test_complete_body_is_not_refused():
    assert truncation_refusal("all five anchors matched; the patch is clean") is None


def test_guard_keeps_the_callers_cap_authoritative():
    assert guard_outbound_body("hi") is None
    assert "too long" in guard_outbound_body("x" * 11, max_chars=10)
    assert "required" in guard_outbound_body("   ")
    assert "REFUSED" in guard_outbound_body("x" * 5 + " [truncated]", max_chars=16000)


# The measured boundary: a marker can carry a long trailing annotation, and the annotation is
# itself evidence of a real cut (a harness naming what it dropped). A window that stops short of
# the opening bracket silently misses the whole marker, so the window covers the bracketed token.
LONG_ANNOTATED_MARKER = (
    "[truncated: 8 of 19 nodes shown, full table at /opt/hermes_sandbox/adhoc/table.md]"
)


def test_long_annotated_marker_is_detected_not_missed():
    body = f"the node table was cut where it was written.\n{LONG_ANNOTATED_MARKER}"
    assert find_truncation_marker(body) == LONG_ANNOTATED_MARKER
    assert truncation_refusal(body) is not None


@pytest.mark.parametrize("body", [
    "reproduced from [output truncated by the harness]",
    "see [the truncated log] for detail",
    "both halves [share the rule, so the wording is truncated deliberately]",
])
def test_a_bracket_that_merely_mentions_truncation_is_not_a_marker(body):
    # Widening the annotation must not turn ordinary end-of-body prose into a refusal: the
    # bracketed token has to START with the marker word, not merely contain it.
    assert find_truncation_marker(body) is None


def test_end_anchored_quotation_is_refused_by_design():
    """The one accepted false refusal, pinned so it is not later filed as a new defect.

    Matching is end-anchored, so a body that ENDS with a bracketed marker as a quotation is
    refused. The refusal text carries the escape (re-send the text with the marker reworded).
    """
    body = "Status: the operator's own DM ended with [truncated]"
    assert find_truncation_marker(body) == "[truncated]"
    assert "Re-send" in truncation_refusal(body)


def test_refuse_truncated_body_raises_the_refusal_for_admission_seams():
    from tools.dm_body_guard import TruncatedBodyRefusal, refuse_truncated_body

    with pytest.raises(TruncatedBodyRefusal) as caught:
        refuse_truncated_body("harness state=OPEN.[truncated]")
    assert caught.value.refusal == str(caught.value)
    assert "REFUSED" in caught.value.refusal
    # An intact body is not an error: the seam stays usable for every normal delivery.
    assert refuse_truncated_body("all nine anchors matched") is None
