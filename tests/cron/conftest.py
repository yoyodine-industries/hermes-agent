"""Cron-test fixtures.

Provides a default ``HERMES_MODEL`` for cron run_job tests so each one
doesn't have to spell out a model. The global conftest blanks
HERMES_MODEL hermetically; without this autouse fixture every cron test
that exercises ``run_job`` would hit the fail-fast guard added in
``cron/scheduler.py`` (see issue #23979) and have to be rewritten.

Tests that specifically need ``HERMES_MODEL`` unset — model-resolution
edge cases — call ``monkeypatch.delenv("HERMES_MODEL", raising=False)``
inside the test, which overrides this fixture's value for that scope.
"""

from pathlib import Path

import pytest

#: Production's lifecycle-guard refusal ledger. Every lane reads it; no test may write it.
_PROD_REFUSAL_LEDGER = Path("/opt/hermes_prod/run_details/lifecycle-guard-refusals.jsonl")


def _prod_refusal_ledger_fingerprint() -> tuple[int, int] | None:
    """Size + mtime of the production refusal ledger, or None where it does not exist (CI)."""
    try:
        stat = _PROD_REFUSAL_LEDGER.stat()
    except OSError:
        return None
    return (stat.st_size, stat.st_mtime_ns)


@pytest.fixture(scope="session", autouse=True)
def _the_cron_suite_never_writes_the_production_refusal_ledger():
    """Measure, across the whole suite run, that PRODUCTION's refusal ledger never moved.

    The per-test pin above is the mechanism; this is the assertion that says the mechanism
    holds for every module in the tree, including ones that build the guard themselves instead
    of going through a shared fixture. Size + mtime rather than a line count, so a truncation
    or a rewrite fails here too.
    """
    before = _prod_refusal_ledger_fingerprint()
    yield
    after = _prod_refusal_ledger_fingerprint()
    assert after == before, (
        f"this suite wrote to the PRODUCTION refusal ledger {_PROD_REFUSAL_LEDGER} "
        f"(before={before}, after={after}); an arm is building the guard without the "
        "LIFECYCLE_GUARD_REFUSAL_LEDGER pin"
    )


@pytest.fixture()
def make_cron_provider():
    """Factory for minimal CronScheduler test doubles.

    ``make_cron_provider(register_job=...)`` returns a real ``CronScheduler``
    subclass instance whose ``register_job`` is the given callable — so tests
    exercising the creation-registration contract share one stub instead of
    redefining inline spy/failing classes, and an ABC rename breaks them
    loudly instead of silently passing a duck-type.
    """
    from cron.scheduler_provider import CronScheduler

    def _make(register_job=None, name="stub"):
        class _StubProvider(CronScheduler):
            @property
            def name(self):  # pragma: no cover - trivial
                return name

            def start(self, stop_event, **kw):  # pragma: no cover - unused
                pass

            def register_job(self, job):
                if register_job is not None:
                    return register_job(job)
                return None

        return _StubProvider()

    return _make


@pytest.fixture(autouse=True)
def _no_managed_store(tmp_path, monkeypatch):
    """Point PM's store at an empty dir: script runs must not select the host install's
    dependency venv (POSIX cron scripts run on it when a store is committed)."""
    monkeypatch.setenv("HERMES_RUNTIME_DIR", str(tmp_path / "no-pm-store"))


@pytest.fixture(autouse=True)
def _default_cron_test_model(monkeypatch):
    """Pin a default HERMES_MODEL so cron run_job tests have a resolvable model."""
    monkeypatch.setenv("HERMES_MODEL", "test-cron-default-model")
    yield


@pytest.fixture(autouse=True)
def _refusal_ledger_never_reaches_production(tmp_path, monkeypatch):
    """No arm in this tree may append to PRODUCTION's lifecycle-guard refusal ledger.

    ``cron.lifecycle_guard`` defaults its refusal ledger to
    /opt/hermes_prod/run_details/lifecycle-guard-refusals.jsonl, so every arm that makes the
    guard refuse writes a row into the LIVE audit trail unless something pins the env. That is
    not hypothetical: runs of the guard suites left 86 fixture rows in the production ledger
    (2026-10-25T07:06-07:09Z and 2026-11-03T07:34-07:36Z, pids 28464/34409/35058/35572/47802/
    47921/48343, every command a pytest tmp path) - half the evidence "a refusal leaves a row"
    is meant to provide. A module-level pin only protects the module that remembers to use it;
    this one protects every arm, including arms added later.
    """
    monkeypatch.setenv("LIFECYCLE_GUARD_REFUSAL_LEDGER", str(tmp_path / "refusals.jsonl"))
    yield


@pytest.fixture(autouse=True)
def _reset_session_context_vars():
    """Restore session ContextVars around cron tests that call run_job directly.

    Production confines each cron run to a copied context, but direct unit tests
    share the pytest context. ``run_job`` intentionally clears ordinary session
    variables to explicit empty values, which would otherwise shadow legacy env
    fallbacks used by later approval tests in the same process.
    """
    from gateway.session_context import _UNSET, _VAR_MAP

    def _reset_all():
        for var in _VAR_MAP.values():
            var.set(_UNSET)

    _reset_all()
    yield
    _reset_all()
