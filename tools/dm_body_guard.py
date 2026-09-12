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

The SAME rule covers content-bearing tool arguments on the write path (``content``,
``file_content``, ``new_string``), where a cut at authoring time lands as a partial FILE and a
success status: a skill write shipped a 167-byte fragment ending ``message:t...[truncated]``,
and upstream NousResearch/hermes-agent#83714 writes the literal marker into the target file for
multi-line ``new_string``. ``content_refusal`` / ``guard_tool_content_arguments`` are that half;
both halves share ``find_truncation_marker`` so one rule cannot drift from the other.

Pure stdlib, no import-time side effects (see ``yaan-import-side-effects``).
"""

from __future__ import annotations

import re

#: How far back from the end of the body to look. A truncation marker is the LAST thing
#: written, so a short window is always enough; anything older is quotation.
TAIL_WINDOW = 64

#: How far back from the end of a content payload to look. Deliberately generous: a content
#: argument is routinely cut inside a docstring or a long prose paragraph, and a few residual
#: lines can follow the marker, so the window has to cover far more than the marker itself.
#: Do not tighten — this window is the only thing between a cut payload and a partial write.
CONTENT_TAIL_WINDOW = 400

#: Tool-argument names that carry AUTHORED CONTENT — bytes destined for a file — and so are
#: refused when their tail is a truncation marker. ``new_string`` is written into the file by
#: ``patch``. ``old_string`` is deliberately ABSENT: it is a search pattern, so a cut there
#: simply fails to match and the patch errors loudly on its own — gating it would only add
#: false refusals.
CONTENT_ARG_FIELDS = ("content", "file_content", "new_string")

#: End-anchored, bracketed truncation marker. Covers the forms seen in the wild and those
#: written by harnesses: ``[truncated]``, ``[...truncated]``, ``[truncated 4096 chars]``,
#: ``<truncated>``, ``(truncated)``, ``…[truncated]``, ``... [TRUNCATED]``. The bracket or
#: angle/paren wrapper is REQUIRED so ordinary prose is never refused.
_MARKER_RE = re.compile(
    r"(?:\.{2,}|…)?\s*[\[\(<]\s*(?:\.{2,}\s*)?truncat\w*(?:\s[^\]\)>]{0,40})?[\]\)>]\s*$",
    re.IGNORECASE,
)


def find_truncation_marker(body: str, *, window: int = TAIL_WINDOW) -> str | None:
    """Return the end-of-body truncation marker (e.g. ``"[truncated]"``), else ``None``.

    Only an end-anchored, bracketed marker counts: truncation loses the tail, so a marker
    in the middle of a body is a quotation, not evidence of a cut. ``window`` is the number
    of trailing characters searched; a content payload passes the wider
    ``CONTENT_TAIL_WINDOW``.
    """
    tail = str(body or "")[-window:]
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


def content_refusal(field: str, value: str) -> str | None:
    """Return the refusal for a content-bearing tool argument cut at authoring time, else ``None``.

    The marker is LITERAL TEXT in the argument, so what arrived is a partial payload, not a
    deliberately small one — and the marker leaves the serialized JSON valid, so no downstream
    check can catch it. Writing it would ship the corruption with a success status, so the
    refusal lands before anything touches the filesystem. Callers pass the argument's own name
    so the message points at the field that arrived cut.
    """
    text = str(value or "")
    marker = find_truncation_marker(text, window=CONTENT_TAIL_WINDOW)
    if not marker:
        return None
    length = len(text.strip())
    return (
        f"REFUSED: the {field!r} argument ends in a truncation marker {marker!r} ({length} chars). "
        "This is a PARTIAL payload cut where the tool call was AUTHORED, not a size limit — nothing "
        "rejected it for length, its tail is simply missing. NOTHING was written: no file was created "
        "and no file was modified. Re-send the full content. If it is long, create the file first with "
        "a short chunk and append the rest with patch instead of one oversized argument. Notify "
        "yoyodine-majordomo that a tool argument arrived truncated."
    )


def guard_tool_content_arguments(args) -> str | None:
    """Return the first refusal for a truncation-marked content argument in ``args``, else ``None``.

    The single entry point every content-writing handler calls before its first side effect.
    Walks dicts and lists of dicts (``skill_manage`` takes an ``operations`` array) and checks
    every key in ``CONTENT_ARG_FIELDS`` whose value is a ``str``; anything else is ignored, so
    an op's non-content fields never trip it.
    """
    if isinstance(args, dict):
        for key, value in args.items():
            if key in CONTENT_ARG_FIELDS and isinstance(value, str):
                refusal = content_refusal(key, value)
                if refusal is not None:
                    return refusal
            if isinstance(value, (dict, list, tuple)):
                refusal = guard_tool_content_arguments(value)
                if refusal is not None:
                    return refusal
    elif isinstance(args, (list, tuple)):
        for item in args:
            refusal = guard_tool_content_arguments(item)
            if refusal is not None:
                return refusal
    return None


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
