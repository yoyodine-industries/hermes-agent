"""The dashboard backend runs the idle header-probe reaper (bounded per-profile store cache).

The fd release itself is covered in ``tests/hermes_state/test_header_probe_idle_release.py``;
this module pins the dashboard side: the configured idle window, one sweep per interval, and the
backend lifespan actually starting the loop (so a deployed dashboard is not merely capable of
releasing the probes, but running the release).
"""

import asyncio

import pytest

import hermes_cli.web_server_probe_reaper as reaper
import hermes_state_dbfile as dbfile


def test_idle_window_defaults_to_the_state_default(monkeypatch):
    monkeypatch.delenv("HERMES_STATE_PROBE_IDLE_SECONDS", raising=False)
    assert reaper.header_probe_idle_seconds() == dbfile.HEADER_PROBE_IDLE_SECONDS


@pytest.mark.parametrize("raw", ["", "  ", "not-a-number", "0", "-5"])
def test_idle_window_ignores_unusable_overrides(monkeypatch, raw):
    monkeypatch.setenv("HERMES_STATE_PROBE_IDLE_SECONDS", raw)
    assert reaper.header_probe_idle_seconds() == dbfile.HEADER_PROBE_IDLE_SECONDS


def test_idle_window_reads_the_override(monkeypatch):
    monkeypatch.setenv("HERMES_STATE_PROBE_IDLE_SECONDS", "90")
    assert reaper.header_probe_idle_seconds() == 90.0


def test_reap_once_uses_the_configured_window(monkeypatch):
    calls = []
    import hermes_state_registry

    monkeypatch.setenv("HERMES_STATE_PROBE_IDLE_SECONDS", "42")
    monkeypatch.setattr(hermes_state_registry, "release_idle_header_probes",
                        lambda seconds: calls.append(seconds) or 3)
    assert reaper.reap_idle_header_probes() == 3
    assert calls == [42.0]


def test_loop_keeps_reaping_after_a_failing_sweep(monkeypatch):
    """A release that raises must not stop the loop (the backend keeps serving and retries)."""
    sweeps = []

    def _flaky():
        sweeps.append(1)
        if len(sweeps) == 1:
            raise RuntimeError("boom")
        return 0

    async def _run():
        monkeypatch.setattr(reaper, "reap_idle_header_probes", _flaky)
        task = asyncio.create_task(
            reaper._header_probe_reaper_loop(interval_s=0.01, initial_delay_s=0)
        )
        for _ in range(200):
            await asyncio.sleep(0.01)
            if len(sweeps) >= 2:
                break
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(_run())
    assert len(sweeps) >= 2


def test_loop_reaps_every_interval_and_stops_when_cancelled(monkeypatch):
    sweeps = []

    async def _run():
        monkeypatch.setattr(reaper, "reap_idle_header_probes", lambda: sweeps.append(1) or 0)
        task = asyncio.create_task(
            reaper._header_probe_reaper_loop(interval_s=0.01, initial_delay_s=0)
        )
        for _ in range(200):
            await asyncio.sleep(0.01)
            if len(sweeps) >= 2:
                break
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(_run())
    assert len(sweeps) >= 2


def test_backend_lifespan_starts_the_reaper():
    """Wiring guard: the release only exists for the process if the lifespan starts the loop."""
    from pathlib import Path

    source = Path(reaper.__file__).with_name("web_server_app.py").read_text()
    assert "_header_probe_reaper_loop" in source
    assert "probe_reaper_task.cancel()" in source
