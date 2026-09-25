"""Tests for issue #87033 — the cronjob tool must surface gateway liveness.

The builtin cron ticker only runs inside the gateway process. Before the
fix, ``cronjob(action="create")`` returned a clean success even with no
gateway running, so the agent confidently told the user a recurring task
was scheduled while the job could never fire. The CLI already warned
(``hermes cron list`` / ``hermes cron status``); the agent path did not.

Contract pinned here:

* create with a live gateway → ``gateway_running: true``, no warning;
* create with no gateway → ``gateway_running: false`` + explicit warning
  telling the model the job is saved but will not fire yet;
* non-builtin scheduler providers are exempt (they fire without the gateway);
* a failed liveness probe stays neutral (``gateway_running: null``) instead
  of claiming either way.
"""

from __future__ import annotations

import json

import pytest


@pytest.fixture
def hermes_env(tmp_path, monkeypatch):
    """Isolate HERMES_HOME for each test so jobs don't leak."""
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "cron").mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))

    import importlib

    import hermes_constants
    importlib.reload(hermes_constants)
    import cron.jobs
    importlib.reload(cron.jobs)
    import cron.scheduler
    importlib.reload(cron.scheduler)

    return home


def _create_job() -> dict:
    from tools.cronjob_tools import cronjob

    return json.loads(
        cronjob(
            action="create",
            schedule="every 10m",
            prompt="say hi",
            name="liveness-probe-job",
            deliver="local",
        )
    )


def _list_jobs() -> dict:
    from tools.cronjob_tools import cronjob

    return json.loads(cronjob(action="list"))

class TestCreateSurfacesGatewayLiveness:
    def test_create_with_gateway_running_has_no_warning(self, hermes_env):
        with patch_liveness(provider="builtin", pids=[12345]) as patches:
            result = _create_job()

        assert result["success"] is True
        assert result["gateway_running"] is True
        assert "warning" not in result

    def test_create_without_gateway_warns_not_scheduled(self, hermes_env):
        with (
            patch_liveness(provider="builtin", pids=[]),
        ):
            result = _create_job()

        assert result["success"] is True, (
            "the job itself is still created successfully"
        )
        assert result["gateway_running"] is False
        assert result.get("warning"), "the model must be told the job won't fire (#87033)"

    def test_non_builtin_provider_is_exempt(self, hermes_env):
        """External schedulers (e.g. Chronos) fire without the gateway —
        no false alarm may be raised for them."""
        with patch_liveness(provider="chronos", pids=[]):
            result = _create_job()

        assert result["success"] is True
        assert result["gateway_running"] is True
        assert "warning" not in result

    def test_failed_probe_stays_neutral(self, hermes_env):
        """If liveness cannot be determined, say nothing either way."""
        with patch_liveness(provider=None, pids=[]):  # probe raises → None
            result = _create_job()

        assert result["success"] is True
        assert result["gateway_running"] is None
        assert "warning" not in result


class TestListSurfacesGatewayLiveness:
    """The `list` action has the same silent-inert-job failure mode as
    create (#87033): an agent inspecting jobs with no gateway running must
    learn they are not firing, not just see a clean list."""

    def _list_jobs(self) -> dict:
        from tools.cronjob_tools import cronjob

        return json.loads(cronjob(action="list"))

    def test_list_with_gateway_running_has_no_warning(self, hermes_env):
        _create_job()  # ensure at least one job exists
        with patch_liveness(provider="builtin", pids=[12345]):
            result = self._list_jobs()

        assert result["success"] is True
        assert result["count"] >= 1
        assert result["gateway_running"] is True
        assert "warning" not in result

    def test_list_without_gateway_warns_jobs_inert(self, hermes_env):
        _create_job()
        with patch_liveness(provider="builtin", pids=[]):
            result = self._list_jobs()

        assert result["success"] is True
        assert result["gateway_running"] is False
        assert result.get("warning"), "the model must be told the listed jobs won't fire (#87033)"

    def test_list_empty_without_gateway_stays_quiet(self, hermes_env):
        """Nothing scheduled + no gateway → no alarm; there is nothing inert."""
        with patch_liveness(provider="builtin", pids=[]):
            result = self._list_jobs()

        assert result["success"] is True
        assert result["count"] == 0
        assert "warning" not in result

    def test_list_non_builtin_provider_is_exempt(self, hermes_env):
        _create_job()
        with patch_liveness(provider="chronos", pids=[]):
            result = self._list_jobs()

        assert result["success"] is True
        assert result["gateway_running"] is True
        assert "warning" not in result


# ---------------------------------------------------------------------------


from contextlib import ExitStack


class _LivenessPatches:
    """Context manager patching the provider/gateway-pid probes.

    Also pins the gateway runtime lock probe to *inactive* by default so
    these tests are deterministic even when a real gateway (holding the
    real lock) runs on the developer's machine — the lock-first check in
    ``_builtin_gateway_liveness`` would otherwise short-circuit to True
    and mask the pid-scan behavior under test. Pass ``lock_active=True``
    to exercise the lock-first path itself.

    The two host-process witnesses read HOST-WIDE state (the host-role
    rendezvous record and the per-home runtime record), so they are pinned
    the same way and for the same reason: they must answer for the code
    under test, never for a real gateway on the machine running the suite.
    ``host_gateway``/``runtime_pid`` state what a live gateway would report;
    ``blind_witnesses=True`` makes both unreadable.
    """

    def __init__(self, *, provider, pids, lock_active=False, host_gateway=None,
                 runtime_pid=None, blind_witnesses=False):
        self._provider = provider
        self._pids = pids
        self._lock_active = lock_active
        self._host_gateway = host_gateway
        self._runtime_pid = runtime_pid
        self._blind_witnesses = blind_witnesses

    def __enter__(self):
        from unittest.mock import patch

        self._stack = ExitStack()

        def _fake_provider_name():
            if self._provider is None:
                raise RuntimeError("probe failure")
            return self._provider

        self._stack.enter_context(
            patch(
                "hermes_cli.cron._active_cron_provider_name",
                side_effect=_fake_provider_name,
            )
        )
        self._stack.enter_context(
            patch(
                "hermes_cli.gateway.find_gateway_pids",
                return_value=list(self._pids),
            )
        )
        self._stack.enter_context(
            patch(
                "hermes_cli.gateway.named_profile_served_by_running_multiplexer",
                return_value=False,
            )
        )
        self._stack.enter_context(
            patch(
                "gateway.status.is_gateway_runtime_lock_active",
                return_value=self._lock_active,
            )
        )
        if self._blind_witnesses:
            self._stack.enter_context(
                patch(
                    "hermes_cli.gateway.host_multiplexer_serving",
                    side_effect=OSError("host-role record unreadable"),
                )
            )
            self._stack.enter_context(
                patch(
                    "gateway.status.get_runtime_status_running_pid",
                    side_effect=OSError("runtime record unreadable"),
                )
            )
        else:
            self._stack.enter_context(
                patch(
                    "hermes_cli.gateway.host_multiplexer_serving",
                    return_value=self._host_gateway,
                )
            )
            self._stack.enter_context(
                patch(
                    "gateway.status.get_runtime_status_running_pid",
                    return_value=self._runtime_pid,
                )
            )
        return self

    def __exit__(self, *exc):
        return self._stack.__exit__(*exc)


def patch_liveness(*, provider, pids, lock_active=False, host_gateway=None,
                   runtime_pid=None, blind_witnesses=False):
    return _LivenessPatches(
        provider=provider,
        pids=pids,
        lock_active=lock_active,
        host_gateway=host_gateway,
        runtime_pid=runtime_pid,
        blind_witnesses=blind_witnesses,
    )


class TestRuntimeLockFirstLiveness:
    """The gateway runtime lock is the primary liveness signal (#95947).

    ``find_gateway_pids`` can transiently return empty while the gateway is
    up (right after a restart) and excludes the current PID by design
    (#13242), so a single-process gateway probed as dead while its own
    ticker was firing (#94143 class). The lock is held for exactly the
    gateway's lifetime and short-circuits to True before the pid scan.
    """

    def test_lock_active_reports_alive_despite_empty_pid_scan(self, hermes_env):
        """The reported false alarm: lock held, pid scan empty → alive."""
        _create_job()
        with patch_liveness(provider="builtin", pids=[], lock_active=True):
            from tools.cronjob_tools import cronjob

            result = json.loads(cronjob(action="list"))

        assert result["success"] is True
        assert result["gateway_running"] is True
        assert "warning" not in result

    def test_lock_inactive_falls_back_to_pid_scan(self):
        from unittest.mock import patch

        import hermes_cli.cron as cron_cli

        with (
            patch("hermes_cli.cron._active_cron_provider_name", return_value="builtin"),
            patch("gateway.status.is_gateway_runtime_lock_active", return_value=False),
            patch("hermes_cli.gateway.find_gateway_pids", return_value=[424242]),
        ):
            assert cron_cli._builtin_gateway_liveness() is True

    def test_no_lock_no_pids_is_false(self):
        from unittest.mock import patch

        import hermes_cli.cron as cron_cli

        with (
            patch("hermes_cli.cron._active_cron_provider_name", return_value="builtin"),
            patch("gateway.status.is_gateway_runtime_lock_active", return_value=False),
            patch("hermes_cli.gateway.find_gateway_pids", return_value=[]),
            patch(
                "hermes_cli.gateway.named_profile_served_by_running_multiplexer",
                return_value=False,
            ),
        ):
            assert cron_cli._builtin_gateway_liveness() is False

    def test_lock_probe_failure_still_falls_back(self):
        """A crashing lock probe must not poison the tri-state helper —
        the pid scan still decides (the outer except returns None only
        when both probes fail)."""
        from unittest.mock import patch

        import hermes_cli.cron as cron_cli

        with (
            patch("hermes_cli.cron._active_cron_provider_name", return_value="builtin"),
            patch(
                "gateway.status.is_gateway_runtime_lock_active",
                side_effect=OSError("lock probe failed"),
            ),
            patch("hermes_cli.gateway.find_gateway_pids", return_value=[424242]),
        ):
            assert cron_cli._builtin_gateway_liveness() is True

    def test_running_multiplexer_counts_as_alive_for_named_profile(self):
        """A satellite needs its own fresh heartbeat as well as a live multiplexer."""
        from unittest.mock import patch

        from cron.jobs import record_ticker_heartbeat
        import hermes_cli.cron as cron_cli

        record_ticker_heartbeat(success=True)
        with (
            patch("hermes_cli.cron._active_cron_provider_name", return_value="builtin"),
            patch("gateway.status.is_gateway_runtime_lock_active", return_value=False),
            patch("hermes_cli.gateway.find_gateway_pids", return_value=[]),
            patch(
                "hermes_cli.gateway.named_profile_served_by_running_multiplexer",
                return_value=True,
            ),
        ):
            assert cron_cli._builtin_gateway_liveness() is True

    def test_no_multiplexer_and_no_pids_is_still_false(self):
        """Both host-process witnesses answer "no live gateway" → absence is provable."""
        from unittest.mock import patch

        import hermes_cli.cron as cron_cli

        with (
            patch("hermes_cli.cron._active_cron_provider_name", return_value="builtin"),
            patch("gateway.status.is_gateway_runtime_lock_active", return_value=False),
            patch("hermes_cli.gateway.find_gateway_pids", return_value=[]),
            patch(
                "hermes_cli.gateway.named_profile_served_by_running_multiplexer",
                return_value=False,
            ),
            # Pinned for the same reason the lock probe is: these read host-wide state, so on
            # a machine that runs a real gateway they would answer for THAT process.
            patch("hermes_cli.gateway.host_multiplexer_serving", return_value=None),
            patch("gateway.status.get_runtime_status_running_pid", return_value=None),
        ):
            assert cron_cli._builtin_gateway_liveness() is False



class TestHostProcessLiveness:
    """A host-owned gateway is not "not a satellite" (blind-guard class).

    ``named_profile_served_by_running_multiplexer`` answers the narrow "is this a SATELLITE of
    the default's multiplexer" and is hard-False for ``default`` — the profile that owns the
    shared host process — so a probe that read its False as "no gateway" reported a healthy
    gateway as absent. Measured on the live host while this failed: ``GET /health`` 200, the
    launchd gateway PID alive for hours, ``gateway_state.json`` naming that same PID, and
    ``cronjob(action="create")`` still answering ``gateway_running: false`` with a "will NOT
    fire" warning — an absence the probe had no evidence for.
    """

    def test_live_host_process_counts_as_alive_for_default(self, hermes_env):
        """The regression, through the tool: default profile, blind pid scan, live host record."""
        _create_job()  # a listed job is what would be reported inert
        with patch_liveness(provider="builtin", pids=[], host_gateway=object()):
            result = _list_jobs()

        assert result["gateway_running"] is True
        assert "warning" not in result

    def test_live_host_process_is_alive_without_pid_scan(self, hermes_env):
        import hermes_cli.cron as cron_cli

        with patch_liveness(provider="builtin", pids=[], host_gateway=object()):
            assert cron_cli._builtin_gateway_liveness() is True

    def test_runtime_record_alone_counts_as_alive(self, hermes_env):
        """The status.py fallback: a host record nobody can read, but a live runtime record."""
        import hermes_cli.cron as cron_cli

        with patch_liveness(provider="builtin", pids=[], runtime_pid=4242):
            assert cron_cli._builtin_gateway_liveness() is True

    def test_unreadable_witnesses_are_unknown_never_absent(self, hermes_env):
        """A probe that cannot see the gateway reports unknown, not "not running"."""
        import hermes_cli.cron as cron_cli

        with patch_liveness(provider="builtin", pids=[], blind_witnesses=True):
            assert cron_cli._builtin_gateway_liveness() is None

    def test_unreadable_witnesses_warn_about_nothing(self, hermes_env):
        """…and the tool says nothing either way rather than declaring the jobs inert."""
        _create_job()
        with patch_liveness(provider="builtin", pids=[], blind_witnesses=True):
            result = _list_jobs()

        assert result["gateway_running"] is None
        assert "warning" not in result

    def test_both_witnesses_reporting_no_owner_is_false(self, hermes_env):
        """The #87033 warning must survive: a proven absence still says so."""
        _create_job()
        with patch_liveness(provider="builtin", pids=[]):
            result = _list_jobs()

        assert result["gateway_running"] is False
        assert result.get("warning"), "the model must still be told the jobs won't fire"
