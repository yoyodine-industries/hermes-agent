"""``hermes doctor`` row for the substrate authority record (design §3.6, carrier 6).

One row, three states, and no fourth:

* ``denied`` — a tick cannot reach its substrate and is admitting nothing. RED, with the
  denied roots and their errno, the episode and its duration, the held units and the
  blind guards. This is the state the 2026-10-26 fault should have shown everywhere and
  showed nowhere.
* ``ok`` — the last tick reached every root it probed, and the record is fresh. GREEN.
* ``unknown`` — no record, an unreadable record, or a record that stopped being
  refreshed. YELLOW with the reason. **Never green**: "nobody has ever recorded a
  verdict" is not a clean bill of health, it is an unmeasured host, and the standing
  no-false-green rule exists because :9501 reported 17/17 green through a total
  substrate denial.

Read-only: this check opens the record, the degradation ledger and ``config.yaml`` on the
boot volume and nothing else. It cannot refuse work and it cannot write.
"""

from __future__ import annotations

from hermes_cli import substrate_view
from hermes_cli.doctor_report import (
    Finding,
    check_fail,
    check_info,
    check_ok,
    check_warn,
    doctor_check,
)

#: How many denied roots / blind guards to name before summarizing. A doctor row is a
#: pointer, not a report; the record itself carries the full matrix.
_SHOWN = 4

_RESTORE = (
    "Restore the substrate the ticks probe (a boot-resident root under "
    "/opt/hermes_prod is denying open(2)); the acting card for the episode is on the "
    "migrations board. `HERMES_SUBSTRATE_GATE=off` turns the gate into a recorder if "
    "spawning must continue while the volume is repaired."
)


def _guard_sentence(view: dict) -> str:
    total = view.get("guards_total")
    total_text = str(total) if total is not None else "?"
    return f"{view.get('guards_blind') or 0} of {total_text} launcher-armed hook row(s) blind"


def _show_guards(view: dict) -> None:
    for item in (view.get("guards") or [])[:_SHOWN]:
        check_info(
            f"blind guard {item.get('hook')} row {item.get('row')}: "
            f"{item.get('reason')} since {item.get('since')}"
        )


@doctor_check()
def _check_substrate(should_fix: bool, f: Finding) -> None:
    view = substrate_view.read()
    state = view.get("state")
    record = view.get("record") or {}

    if state == "denied":
        check_fail("Substrate REFUSED — ticks are admitting nothing", "")
        for root in (view.get("roots") or [])[:_SHOWN]:
            check_info(f"{root.get('path')}  op={root.get('op')}  errno={root.get('errno')}  tier={root.get('tier')}")
        if view.get("roots_denied", 0) > _SHOWN:
            check_info(f"... {view['roots_denied'] - _SHOWN} more denied root(s) in the record matrix")
        check_info(
            f"episode {view.get('episode')} since {view.get('since')} "
            f"(streak {view.get('streak')}/{view.get('threshold')}, door {view.get('door')}), "
            f"{view.get('held_count') or 0} unit(s) held"
        )
        check_info(f"guards: {_guard_sentence(view)}")
        _show_guards(view)
        f.manual_issues.append(f"Substrate denied: {view.get('detail')}. {_RESTORE}")
        return

    if state == "ok":
        check_ok("Substrate reachable — the last tick read every root it probed", "")
        check_info(f"record: {record.get('path')}  (age {record.get('age_s')}s, door {view.get('door')})")
        if view.get("guards_blind"):
            check_warn(
                "A fail-open guard is blind — observation is lost while ticks still run",
                f"({_guard_sentence(view)})",
            )
            _show_guards(view)
            hooks = ", ".join(sorted({str(item.get("hook")) for item in view.get("guards") or []}))
            f.manual_issues.append(
                f"{_guard_sentence(view)}: {hooks} — restore the hook body named in "
                f"{view.get('guards_ledger')} (the launcher fails open by design, so "
                "nothing else will report this)."
            )
        else:
            check_info(f"guards: {_guard_sentence(view)}")
        return

    # unknown — the honest answer when nothing has measured the substrate recently.
    reason = view.get("reason")
    check_warn("Substrate UNKNOWN — no fresh verdict", f"({reason} — {view.get('detail')})")
    check_info(f"record: {record.get('path')}  (present={record.get('present')}, error={record.get('error')})")
    if record.get("last_known_state"):
        check_info(
            f"last known state {record.get('last_known_state')} recorded "
            f"{record.get('age_s')}s ago — not a current verdict"
        )
    if reason == "record_stale":
        check_info(
            "unmeasured is not healthy, and the record says so: the two doors rewrite it at least "
            "every 60 s on a healthy pass, so a record this old means NO door has run since. "
            "Measure it now: python3 -c \"from hermes_cli import substrate_access as s; "
            "print(s.probe_all(use_cache=False).matrix())\""
        )
    check_info(f"guards: {_guard_sentence(view)}")
    _show_guards(view)


__all__ = ["_check_substrate"]
