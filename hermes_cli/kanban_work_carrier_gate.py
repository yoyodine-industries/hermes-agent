"""The work-carrier gate: a LOOP-shaped job may not be filed as a standalone card.

RULING (platform-stl, 2026-11-05). The defect class is not a bad card, it is the wrong
CARRIER: an agent with multi-phase work - phases learned as it goes, more than one repo, a
deploy, a proof after the deploy - reaches for ``kanban create`` because a card is the
reflex, and the work then lives as a row nobody loops over. Measured instance: a four-filer
fix across two repos was filed as one card (``t_e217fc62``) and had to be re-homed into a
``yaan-looping`` project mid-flight; the same reflex produced 160 duplicate filings of 50
unchanged conditions on the platform board.

WHAT THE ESTATE ALREADY RULES: the board is the state of record, not a work queue. A project
carries L1/L2/L3 in ``LOOPING/task_list.md``; the board carries ONE card per L1 (``yaan-looping``).
The carrier is chosen by the SHAPE of the work, and choosing by habit is the failure mode.

WHY A GATE AND NOT A SKILL: "a rule in a skill did not stop it; this writer makes the rule an
invariant" (``looping_state_write.py``). A written rule is advice; a refusal at the door changes
the reflex, because the reflex is what the door answers.

THE RULE, deliberately deterministic - no model, no prose reading:

* A card filed WITHOUT a project is REFUSED when its title or body carries a STRONG
  loop-shape marker: a ``LOOPING/`` path or ``task_list.md``, the words ``workstream`` or
  ``milestone``, an L-id (``PFK-1.2``, ``PROJ-L1.2.3``), a leading ``L1:``/``L2:``/``L3:``
  label, or ``phase <n>``.
* A card filed WITH a project is never refused by this gate: it is already anchored to the
  loop's own project, which is what the gate exists to force.
* A genuine FINDING that merely mentions a milestone (a defect in the milestone UI, say) is
  filed by declaring the carrier explicitly: a line ``CARRIER: card`` in the body. The
  declaration rides with the card, so the exception is auditable rather than invisible.

WHAT IS DELIBERATELY NOT A TRIGGER: the bare word "phase" or "stage" as an ordinary noun in a
defect report, or any other ordinary prose. The markers above are WRITTEN ARTEFACTS of a loop
(an id, a task_list path, a workstream heading) - a card citing one is either carrying loop
state or quoting it, and the declaration covers the quoting case.
"""
from __future__ import annotations

import re
from typing import Optional

#: Written artefacts of a LOOP. Each is a strong signal: an L-id, a task_list path, a
#: workstream/milestone heading, an explicit L1/L2/L3 label, or "phase <n>".
LOOP_SHAPE_MARKERS = (
    re.compile(r"LOOPING/", re.I),
    re.compile(r"\btask_list\.md\b", re.I),
    re.compile(r"\bworkstream\b", re.I),
    re.compile(r"\bmilestones?\b", re.I),
    re.compile(r"\b[A-Z]{2,}-\d+\.\d+(?:\.\d+)?\b"),          # PFK-1.2, PROJ-L1.2.3
    re.compile(r"(?m)^\s*L[123]\s*[:\-\u2014]"),               # a leading "L1: ..." label
    re.compile(r"\bphase\s+\d\b", re.I),
)

#: The explicit escape: a finding that quotes loop vocabulary declares its carrier.
CARRIER_DECLARATION = re.compile(r"(?im)^\s*CARRIER:\s*card\s*$")

REMEDY = """\
file the LOOP instead, then the ONE card per L1:
  hermes project create {slug} --name "<project>"       # the loop's home + LOOPING/task_list.md
  hermes kanban create "<L1 title>" --project {slug} --assignee <lane>
  # then write L2/L3 (ids, owners, evidence) into <project>/LOOPING/task_list.md
a finding that only QUOTES loop vocabulary declares it: put `CARRIER: card` on its own line in the body."""

REFUSAL_CODE = "loop_shaped_work"


def _marker_hit(title: str, body: str) -> Optional[str]:
    text = f"{title or ''}\n{body or ''}"
    for pattern in LOOP_SHAPE_MARKERS:
        match = pattern.search(text)
        if match:
            return match.group(0).strip()
    return None


def check(*, title: str, body: str = "", project: Optional[str] = None,
          slug_hint: str = "<project>") -> Optional[str]:
    """Return a refusal message, or None when the filing may proceed.

    Pure function: no I/O, no store reads, no model. It is called at the FILING door before
    any row is written, so a refusal costs the filer one turn and writes nothing.
    """
    if str(project or "").strip():
        # Anchored to a project: this IS the loop's card. Nothing to refuse.
        return None
    if CARRIER_DECLARATION.search(body or ""):
        # Declared as a card on purpose; the declaration rides in the body, auditable.
        return None
    hit = _marker_hit(title, body)
    if not hit:
        return None
    return (
        f"LOOP REFUSED ({REFUSAL_CODE}) -- this filing carries a loop artefact ({hit!r}) "
        "but names no project.\n"
        "Multi-phase work is carried by a yaan-looping project: the board holds ONE card per "
        "L1, and L2/L3 live in LOOPING/task_list.md. Filing it as a standalone card is how "
        "work loses its loop and the board becomes a work queue.\n"
        + REMEDY.format(slug=slug_hint)
    )
