"""Tests for the sender-side truncation guard.

The rule's whole value is its narrowness: it must fire on a body whose tail is a truncation
marker, and must NOT fire on prose that merely mentions truncation — a guard that refuses
good messages gets bypassed and the incident recurs.
"""

import pytest

from tools.dm_body_guard import (
    DEFAULT_REPORT_TARGET,
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


def test_refusal_names_the_report_target_and_defaults_to_a_neutral_one():
    refusal = truncation_refusal("analysis follows. [truncated]", report_to="the duty reviewer")
    assert refusal is not None
    assert "the duty reviewer" in refusal

    # The rule carries no deployment's identifiers: unset, it names nobody in particular.
    unnamed = truncation_refusal("analysis follows. [truncated]")
    assert unnamed is not None
    assert DEFAULT_REPORT_TARGET in unnamed
    assert "the duty reviewer" not in unnamed


def test_complete_body_is_not_refused():
    assert truncation_refusal("all five anchors matched; the patch is clean") is None


def test_guard_keeps_the_callers_cap_authoritative():
    assert guard_outbound_body("hi") is None
    assert "too long" in guard_outbound_body("x" * 11, max_chars=10)
    assert "required" in guard_outbound_body("   ")
    assert "REFUSED" in guard_outbound_body("x" * 5 + " [truncated]", max_chars=16000)
