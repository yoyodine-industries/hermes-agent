"""The inbox -> card provenance block — DESIGN section 4 of the seat-directory convention.

A worker that files a card FROM its seat's inbox (``<domain>/<profile>/TASKS.md``) must
carry the item's own words onto the card, or the linkage cannot be checked afterwards: the
item gets struck from the inbox and nothing on the card proves it was ever there. This
module builds that block, and REFUSES to build it when

  * the seat's ``TASKS.md`` does not exist, or
  * the named item text is not in it.

Filing an item from an inbox that does not hold it is the drop, one step earlier than the
nightly audit that would otherwise catch it days later (``profile_inbox_check.py``, the
``profile-inbox-audit`` maintenance lane).

The gate lives at ``kanban_db.create_task`` — the one moment EVERY filing surface reaches —
so the CLI's ``--from-inbox`` and the ``kanban_create`` tool share it instead of each
re-deciding. :class:`InboxRefusal` is a ``ValueError`` on purpose: the kernel's refusal
idiom (and the tool door's structured error envelope) already maps it, so a second
translation layer per door would only be a place for the two doors to disagree.

Fleet-specific by design: the roots below are this host's estate, not a general
convention. It rides the override set for that reason — upstream has no seat directories.
"""
from __future__ import annotations

import re
from pathlib import Path

#: The roots that hold ``<domain>/<profile>/TASKS.md``. The ONE place the convention's
#: geography is written; a second estate root is an edit here, never a caller's argument.
INBOX_ROOTS = ("/opt/hermes_sandbox",)

#: The section heading the audit reads. Provenance outside this section is malformed (see
#: ``profile_inbox_check.py``'s ``inbox_section``), so the block is written under it.
HEADING = "## Inbox item"


class InboxRefusal(ValueError):
    """The filing cannot claim inbox provenance. The message is shown to the filer verbatim."""


def _norm(text: str) -> str:
    """Whitespace-collapsed, casefolded, markdown-noise-stripped comparison form.

    The seat files are hand-written tables and bullets: the same item reads ``**wire the
    thing**`` in one column and plain text in a caller's flag. Comparison normalises that
    noise; the BLOCK still quotes the caller's own words verbatim.
    """
    return re.sub(r"\s+", " ", re.sub(r"[`*_>#|\u25d0\u2298\u25cf\u2611]", " ", str(text))).strip().casefold()


def seat_file(seat: str, roots: tuple[str, ...] | None = None) -> Path:
    """``<domain>/<profile>`` -> the seat's live TASKS.md, or :class:`InboxRefusal`.

    The seat is a two-segment name because the block's ``inbox:`` line carries it that way
    and the audit parses it that way; anything else is refused rather than guessed at.
    """
    name = str(seat or "").strip().strip("/")
    parts = [p for p in name.split("/") if p]
    if len(parts) != 2 or any(p in (".", "..") for p in parts):
        raise InboxRefusal(
            f"seat {seat!r} is not <domain>/<profile> (the form the card's block carries)"
        )
    tried = []
    for root in roots or INBOX_ROOTS:
        path = Path(root) / parts[0] / parts[1] / "TASKS.md"
        if path.is_file():
            return path
        tried.append(str(path))
    raise InboxRefusal(
        "no seat inbox at " + ", ".join(tried)
        + " — an item cannot be filed from an inbox that does not exist"
    )


def provenance_block(seat: str, item: str, roots: tuple[str, ...] | None = None) -> str:
    """The ``## Inbox item`` block, or :class:`InboxRefusal` when the seat cannot prove it."""
    words = " ".join(str(item or "").split())
    if not words:
        raise InboxRefusal("the item's own words are empty: the block would prove nothing")
    path = seat_file(seat, roots)
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:                                                # pragma: no cover
        raise InboxRefusal(f"cannot read {path}: {exc}") from exc
    if _norm(words) not in _norm(text):
        raise InboxRefusal(
            f"the item is not in {path}: {words[:120]!r} — the drop, one step earlier "
            "(an item that the seat file no longer holds cannot be filed as if it did)"
        )
    return f"{HEADING}\n\ninbox: {seat}\n> {words}\n"


def with_inbox_provenance(body: str | None, seat: str,
                          item: str, roots: tuple[str, ...] | None = None) -> str:
    """The card body with the provenance block FIRST, so no later edit can bury it.

    Idempotent for a re-filed body (a retry that hands back the same opening post): a body
    that already OPENS with this exact block is returned unchanged rather than carrying two
    blocks the audit would then have to disambiguate.
    """
    block = provenance_block(seat, item, roots=roots)
    rest = str(body or "").strip("\n")
    if rest.startswith(block):
        return rest
    return block if not rest else block + "\n" + rest
