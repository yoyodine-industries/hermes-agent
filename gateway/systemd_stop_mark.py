"""Mark a supervisor-ordered stop as planned so SIGTERM exits cleanly.

Runs as systemd ``ExecStop=`` before ``KillSignal=SIGTERM``. A raw
``systemctl restart/stop hermes-gateway`` bypasses the ``hermes gateway``
CLI, so no takeover (``--replace``) or planned-stop marker exists when the
SIGTERM lands; the gateway classifies it as an unexpected kill, exits
non-zero, and the journal records ``Failed with result exit-code`` even
though the restart was intentional.

The unit's ``Environment=HERMES_HOME=`` is inherited here, so the shared
:func:`gateway.status.write_planned_stop_marker` helper lands the marker in
the right profile home with PID + start-time identity. Best-effort and
never blocking: any failure returns 0 so the stop/restart proceeds.
"""

from __future__ import annotations

import os
import sys


def _resolve_target_pid(argv: list[str] | tuple[str, ...]) -> int | None:
    """Explicit ``argv[1]``, else systemd's ``$MAINPID``. None when unknown.

    No PID-file fallback on purpose: marking the recorded PID without proof
    it is the process systemd is about to signal could mislabel an unrelated
    shutdown as planned.
    """
    raw: object = None
    if len(argv) > 1 and argv[1]:
        raw = argv[1]
    else:
        raw = os.environ.get("MAINPID")
    try:
        pid = int(str(raw).strip())
    except (TypeError, ValueError, AttributeError):
        return None
    return pid if pid > 0 else None


def main(argv: list[str] | tuple[str, ...] | None = None) -> int:
    """Write the attributed planned-stop request for the supervised PID. Always 0."""
    try:
        pid = _resolve_target_pid(list(argv) if argv is not None else sys.argv)
        if pid is None:
            return 0
        from gateway.stop_attribution import write_stop_request

        # Called from the unit's ExecStop=, so the requester IS this helper process; its argv names
        # systemd as the origin and ``delivery`` says so, because the SIGTERM that follows belongs
        # to systemd, not to the helper.
        write_stop_request(pid, kind="operator", reason_code="systemd_stop", delivery="systemd",
                           reason_text="systemd ExecStop= for the gateway unit",
                           lull_gate="not_consulted")
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
