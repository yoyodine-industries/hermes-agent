"""``hermes doctor``: the emergency-stop row — is a stop armed, how wide, and holding what.

v3 §6.9 asks the doctor to render mode, lane set, the DEFCON board designation, the held-card
count and any fail-safe read defect. The row is INFORMATIONAL: a stop is a deliberate operator
state, never a fault, so it records nothing on the ``Finding`` — a held fleet still exits
``hermes doctor`` clean, it just cannot read as an idle one.
"""

from __future__ import annotations

from hermes_cli.doctor_report import Finding, check_ok, check_warn, doctor_check


@doctor_check("Emergency stop state could not be read")
def _check_emergency_stop(should_fix: bool, f: Finding) -> None:
    """Render the effective stop state, held cards included.

    The body is ``hermes status``'s own banner rather than a second formatter: the two surfaces
    answer the same question, and a doctor that formatted the state itself could disagree with
    ``hermes status`` about whether a hold is total — the misreading (§6.9) both rows exist to
    prevent. ``check_warn``, not ``check_fail``: the row carries an operator action to notice,
    not an issue to fix.
    """
    from hermes_cli.status import _estop_status_line

    line = _estop_status_line()
    if line is None:
        check_ok("Emergency stop", "clear — `hermes pause` arms one")
        return
    check_warn(line)
