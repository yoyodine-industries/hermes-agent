"""Sender-side guard for outbound bot DMs: a body that is already truncated is refused.

Why this exists (2026-09-12): two peer DMs arrived cut mid-sentence, one ending in a bare
``[truncated]`` marker. The cut happened where the body was authored — upstream of the send
path — and every downstream check passed, because the marker left the serialized tool
arguments *valid* JSON and a shortened string literal still parses. The recipient could not
tell a truncated body from a terse one, so a partial instruction was acted on as if whole.

This moves the failure to the sender: a body whose tail is a truncation marker is refused
before delivery, loudly, and nothing is queued. The sender resends the tail instead of
shipping a partial message the recipient has no way to detect.

Deliberately narrow: only an end-anchored, bracketed marker counts. Truncation loses the
tail, so a marker in the middle of a body is a quotation — messages *about* truncation are
legitimate — and the bare word "truncated" in prose is not a marker. A cut with no marker at
all has no detectable signature and is not guessed at here: a heuristic over "looks cut" would
refuse good messages, and a guard that cries wolf gets bypassed.

Pure stdlib, no import-time side effects (see ``yaan-import-side-effects``).
"""

from __future__ import annotations

import re

#: How far back from the end of the body to look. A truncation marker is the LAST thing
#: written, so a short window is always enough; anything older is quotation.
TAIL_WINDOW = 64

#: End-anchored, bracketed truncation marker. Covers the forms seen in the wild and those
#: written by harnesses: ``[truncated]``, ``[...truncated]``, ``[truncated 4096 chars]``,
#: ``<truncated>``, ``(truncated)``, ``…[truncated]``, ``... [TRUNCATED]``. The bracket or
#: angle/paren wrapper is REQUIRED so ordinary prose is never refused.
_MARKER_RE = re.compile(
    r"(?:\.{2,}|…)?\s*[\[\(<]\s*(?:\.{2,}\s*)?truncat\w*(?:\s[^\]\)>]{0,40})?[\]\)>]\s*$",
    re.IGNORECASE,
)


def find_truncation_marker(body: str) -> str | None:
    """Return the end-of-body truncation marker (e.g. ``"[truncated]"``), else ``None``.

    Only an end-anchored, bracketed marker counts: truncation loses the tail, so a marker
    in the middle of a body is a quotation, not evidence of a cut.
    """
    tail = str(body or "")[-TAIL_WINDOW:]
    match = _MARKER_RE.search(tail)
    if not match:
        return None
    # Report the bracketed token itself: a leading ellipsis is context, not the marker.
    return match.group(0).strip().lstrip(".… \t") or None


def truncation_refusal(body: str) -> str | None:
    """Return the refusal for a truncation-marked body, else ``None``.

    One shared rule for every outbound DM surface (the ``message_agent`` tool and the
    ``hermes peer dm`` / ``peer run`` CLI) so the two cannot drift apart. Callers decide how
    to surface the string: a tool error payload, or stderr plus a non-zero exit.
    """
    marker = find_truncation_marker(body)
    if not marker:
        return None
    length = len(str(body or "").strip())
    return (
        f"REFUSED: this message body ends in a truncation marker {marker!r} ({length} chars). "
        "It is a PARTIAL body, not the message — it was cut before it reached the send path. "
        "NOTHING was sent and nothing was queued, so the recipient has not seen it. Re-send "
        "the full text: lead with the conclusion, and if the content is long, write it to a "
        "file and send the path instead of pasting it."
    )


def guard_outbound_body(body: str, *, max_chars: int | None = None) -> str | None:
    """Return a refusal string for a body that must not be sent, else ``None``.

    Order: empty, over-long (only when ``max_chars`` is given), truncation marker. The cap is
    a parameter rather than a constant here so the caller's existing limit stays
    authoritative and its wording does not change under this guard.
    """
    text = str(body or "")
    stripped = text.strip()
    if not stripped:
        return "message is required — compose what you want to say to that agent."
    if max_chars is not None and len(stripped) > max_chars:
        return (f"message too long ({len(stripped)} chars > {max_chars}). "
                "Send the essentials; share large content as a file path instead.")
    return truncation_refusal(stripped)
