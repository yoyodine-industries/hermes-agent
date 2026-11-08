"""Which cron execution the current code runs inside, for plugins (``ctx.current_cron_execution()``).

The scheduler binds it once it has won the execution's ``claimed`` → ``running`` transition (or a
restart-safe worker adopted it) and resets it when the fire ends. It is a ContextVar, so it follows
the agent onto the threads the scheduler already runs it on via ``contextvars.copy_context()`` and
never leaks to another concurrently firing job or profile. Model output cannot reach it: tool
subprocesses do not share the interpreter. Delegated children read ``None`` — they are not the
scheduled occurrence.
"""
from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any, Mapping, Optional


@dataclass(frozen=True)
class CronExecution:
    job_id: str
    job_name: str
    execution_id: str
    source: str  # "builtin" (ticker), "direct" (manual run / dashboard Run now), provider name...
    scheduled_instant: Optional[str]  # the occurrence fired; None for an off-schedule run
    started_at: Optional[str]
    profile: str


_CURRENT: ContextVar[Optional[CronExecution]] = ContextVar("hermes_cron_execution", default=None)


def enter_cron_execution(job: Mapping[str, Any], execution_id: str, record: Mapping[str, Any]) -> Token:
    """Bind this fire's identity from *record*, the ledger row of the running transition."""
    from hermes_cli.profiles import current_profile_name

    return _CURRENT.set(CronExecution(
        job_id=str(job["id"]),
        job_name=str(job.get("name") or job["id"]),
        execution_id=str(execution_id),
        source=str(record.get("source") or ""),
        scheduled_instant=record.get("scheduled_instant"),
        started_at=record.get("started_at"),
        profile=current_profile_name("default") or "default",
    ))


def exit_cron_execution(token: Optional[Token]) -> None:
    if token is not None:
        _CURRENT.reset(token)


def current_cron_execution() -> Optional[CronExecution]:
    from agent.delegation_context import is_delegated_child_context

    return None if is_delegated_child_context() else _CURRENT.get()


# --- The occurrence identity a fire EXPORTS to its children ----------------
#
# The contextvar above serves in-process plugins. A child process (a ``no_agent`` script,
# a pre-run gate script, a restart-safe worker) shares no interpreter with the scheduler
# and cannot re-derive the identity: ``create_execution`` records the SCHEDULER's pid, so a
# child cannot recognise its own ledger row, and picking a row by nearest window would put
# resolution logic in the consumer. So the fire exports the identity as env vars, once,
# from the job snapshot the scheduler dispatched — and the CONSUMER decides what an absent
# value means (a manual/off-schedule fire legitimately has no occurrence).


def cron_execution_env(job: Mapping[str, Any]) -> dict[str, str]:
    """The occurrence identity of *job*, as the env a fire's child processes receive.

    ``_scheduled_instant`` is the occurrence fired, VERBATIM as the ledger holds it
    (``cron.occurrences.scheduled_instant`` stays its only normaliser) — never the wall
    clock. An absent instant is the single readable "off-schedule / manual fire" signal,
    so a key whose value is None or empty is OMITTED rather than exported empty: a
    present-but-empty variable is indistinguishable from a real one and would be a trap.
    Only ids, an instant and a source travel here — never a secret.
    """
    values = {
        "HERMES_CRON_JOB_ID": job.get("id"),
        "HERMES_CRON_EXECUTION_ID": job.get("execution_id"),
        "HERMES_CRON_SCHEDULED_INSTANT": job.get("_scheduled_instant"),
        "HERMES_CRON_SOURCE": job.get("source"),
    }
    return {name: str(value) for name, value in values.items() if value not in (None, "")}


#: The names above, as the child-env bridge below owns them.
CRON_IDENTITY_ENV_NAMES = (
    "HERMES_CRON_JOB_ID",
    "HERMES_CRON_EXECUTION_ID",
    "HERMES_CRON_SCHEDULED_INSTANT",
    "HERMES_CRON_SOURCE",
)


def cron_identity_child_env(env: dict[str, str]) -> dict[str, str]:
    """Overlay THIS context's fire identity onto a child environment, in place.

    The third export path, and the one that carries the agent's own tool children: a script
    and an external worker each overlay :func:`cron_execution_env` on an env they build,
    but an in-process agent fire's tools inherit the scheduler's ``os.environ``, which never
    held the identity. The ContextVar bound on the ``claimed`` → ``running`` win does hold
    it, on the very thread the agent runs on — so read it where the child env is built
    (``tools.environments.local._finalize_child_env``) and it is correct for concurrent
    fires, where a process-global write would hand one job's children another job's row.

    A bound fire is AUTHORITATIVE over anything inherited, like the gateway session bridge
    beside it: a value the fire does not have (an off-schedule fire's occurrence) is
    REMOVED rather than inherited from a stale process env, because presence is what a
    consumer reads. With no fire bound there is nothing to say, so the env is untouched —
    that is the external-worker path, which sets its own four names explicitly.
    """
    current = current_cron_execution()
    if current is None:
        return env
    values = {
        "HERMES_CRON_JOB_ID": current.job_id,
        "HERMES_CRON_EXECUTION_ID": current.execution_id,
        "HERMES_CRON_SCHEDULED_INSTANT": current.scheduled_instant,
        "HERMES_CRON_SOURCE": current.source,
    }
    for name in CRON_IDENTITY_ENV_NAMES:
        value = values[name]
        if value in (None, ""):
            env.pop(name, None)
        else:
            env[name] = str(value)
    return env
