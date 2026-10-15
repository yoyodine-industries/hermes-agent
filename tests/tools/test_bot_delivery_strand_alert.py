"""Tests for the stranded-delivery growth alarm (DoD 4; card t_703af8d1).

Covers the census rule, the per-seat growth test, the ONE-keyed-card guarantee and
its recovery, the log line, and the landing of the evaluation inside the LIVE
drainer's own sweep.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

from gateway.platforms import api_server_bot_delivery as drainer
from tools import bot_delivery_queue as q
from tools import bot_delivery_strand_alert as alert

DEFAULTS = dict(q._DEFAULTS)
TTL_NS = int(DEFAULTS["delivery_queue_ttl_seconds"] * 1e9)


@pytest.fixture(autouse=True)
def _pin_config(monkeypatch):
    monkeypatch.setattr(q, "_cfg", lambda key, **kw: DEFAULTS[key])
    yield


def _did(n: int) -> str:
    return "%032x" % n


def _record(n: int, *, status: str, waited_ns: int = 0, reason: str = "queued_expired",
            now_ns: int | None = None) -> dict:
    now_ns = time.time_ns() if now_ns is None else now_ns
    created = now_ns - waited_ns
    return {
        "delivery_id": _did(n),
        "status": status,
        "target_profile": "bravo",
        "sender_profile": "alpha",
        "created_at": created,
        "updated_at": created,
        "attempts_log": [],
        "reason": reason,
        "sequence": n,
    }


def _write(path: Path, record: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
    return path


def _spool(home: Path, n: int, *, status: str, waited_ns: int = 0, **kw) -> Path:
    record = _record(n, status=status, waited_ns=waited_ns, **kw)
    sub = {"queued": q.QUEUE_DIR, "running": q.CLAIMED_DIR}.get(status, q.SETTLED_DIR)
    return _write(Path(home) / "runtime" / q.DELIVERY_DIR_NAME / sub
                  / f"{record['delivery_id']}.json", record)


class FakeBoard:
    """A board that behaves like ``create_task``: same key -> SAME row, never a second."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.rows: dict[str, str] = {}

    def __call__(self, *, title, body, assignee, priority, board, idempotency_key) -> str:
        self.calls.append({"title": title, "body": body, "assignee": assignee,
                           "priority": priority, "board": board,
                           "idempotency_key": idempotency_key})
        if idempotency_key in self.rows:
            return self.rows[idempotency_key]
        tid = "t_%08x" % (len(self.rows) + 1)
        self.rows[idempotency_key] = tid
        return tid


@pytest.fixture
def fleet(tmp_path, monkeypatch):
    """A root home with a routable seat and one named lane, and a pinned band module."""
    root = tmp_path / "hermes"
    (root / "profiles" / "bravo").mkdir(parents=True)
    (root / "profiles" / "bravo" / "config.yaml").write_text("model: {}\n", encoding="utf-8")
    monkeypatch.setattr(alert, "_band_priority", lambda *a, **k: (790000, ""))
    return root


def _log_lines(monkeypatch) -> list[tuple]:
    lines: list[tuple] = []
    monkeypatch.setattr(q, "_log", lambda event, record=None, **f: lines.append((event, f)))
    return lines


# ---------------------------------------------------------------- the census
def test_a_queued_record_past_the_ttl_is_stranded(fleet, tmp_path):
    _spool(fleet, 1, status="queued", waited_ns=TTL_NS * 3)
    assert q.seat_strand_census(fleet)["stranded"] == 1


def test_a_queued_record_inside_the_ttl_is_not_stranded(fleet):
    _spool(fleet, 1, status="queued", waited_ns=TTL_NS // 2)
    seat = q.seat_strand_census(fleet)
    assert (seat["queued"], seat["pending_over_ttl"], seat["stranded"]) == (1, 0, 0)


def test_expired_is_stranded_and_ambiguous_is_surfaced_but_never_stranded(fleet):
    _spool(fleet, 1, status="expired", waited_ns=TTL_NS * 2)
    _spool(fleet, 2, status="ambiguous", reason="runtime_offline")
    _spool(fleet, 3, status="failed", reason="turn_error")
    _spool(fleet, 4, status="delivered")
    seat = q.seat_strand_census(fleet)
    assert seat["stranded"] == 1
    assert (seat["expired"], seat["ambiguous"], seat["failed"], seat["delivered"]) == (1, 1, 1, 1)


def test_the_oldest_stranded_record_carries_its_age_and_path(fleet):
    _spool(fleet, 1, status="expired", waited_ns=TTL_NS * 2)
    _spool(fleet, 2, status="queued", waited_ns=TTL_NS * 9)
    seat = q.seat_strand_census(fleet)
    assert seat["oldest_delivery_id"] == _did(2)
    assert seat["oldest_path"].endswith(f"{_did(2)}.json")
    assert seat["oldest_age_seconds"] >= TTL_NS * 9 / 1e9 - 5


def test_the_fleet_census_covers_the_routable_seat_and_every_live_lane(fleet):
    _spool(fleet, 1, status="expired", waited_ns=TTL_NS * 2)              # default seat
    _spool(fleet / "profiles" / "bravo", 2, status="expired", waited_ns=TTL_NS * 2)
    census = q.strand_census(fleet)
    assert sorted(census["seats"]) == ["bravo", "default"]
    assert census["totals"]["stranded"] == 2
    assert q.stranded_breakdown(census) == "bravo:1,default:1"


def test_spool_homes_is_the_profile_list_predicate(fleet):
    homes = dict(q.spool_homes(fleet))
    assert homes["default"] == fleet
    assert homes["bravo"] == fleet / "profiles" / "bravo"
    # a scratch dir that is not a profile id, and a marker-less dir, are not lanes
    (fleet / "profiles" / "NotAProfile").mkdir()
    (fleet / "profiles" / "charlie").mkdir()
    assert sorted(dict(q.spool_homes(fleet))) == ["bravo", "default"]


# ---------------------------------------------------------------- growth + the card
def test_a_first_evaluation_with_a_backlog_grows_and_files_one_card(fleet, monkeypatch):
    lines = _log_lines(monkeypatch)
    _spool(fleet / "profiles" / "bravo", 1, status="expired", waited_ns=TTL_NS * 2)
    board = FakeBoard()

    result = alert.alert(fleet, board="migrations", assignee="platform-stl", create=board)

    assert result["grown_seats"] == ["bravo"]
    assert result["fleet_total"] == 1
    assert len(board.calls) == 1
    call = board.calls[0]
    assert call["idempotency_key"] == "peer-strand-alert:0"
    assert call["board"] == "migrations" and call["assignee"] == "platform-stl"
    assert call["title"] == alert.STRAND_ALERT_TITLE
    assert "stranded: 1" in call["body"] and "bravo" in call["body"]
    assert "oldest stranded record" in call["body"]
    assert "peer-strand-alert:0" in call["body"]
    assert [e for e, _ in lines] == ["strand_alert"]
    assert lines[0][1]["stranded"] == 1 and lines[0][1]["grown"] == 1
    assert lines[0][1]["card"] == result["card_id"]


def test_a_repeat_evaluation_returns_the_same_card_and_writes_no_second_row(fleet, monkeypatch):
    lines = _log_lines(monkeypatch)
    _spool(fleet, 1, status="expired", waited_ns=TTL_NS * 2)
    board = FakeBoard()

    first = alert.alert(fleet, create=board)
    second = alert.alert(fleet, create=board)

    assert second["card_id"] == first["card_id"]
    assert len(board.rows) == 1, "a sustained backlog minted a second row"
    assert len(board.calls) == 1, "a steady backlog re-filed"
    assert lines[1][1]["stranded"] == 1 and lines[1][1]["grown"] == 0
    assert lines[1][1]["card"] == first["card_id"]


def test_growth_again_in_the_same_incident_returns_the_same_card_id(fleet, monkeypatch):
    """The key, not the ledger, is what makes a growth storm impossible."""
    _log_lines(monkeypatch)
    _spool(fleet, 1, status="expired", waited_ns=TTL_NS * 2)
    board = FakeBoard()
    first = alert.alert(fleet, create=board)

    _spool(fleet, 2, status="expired", waited_ns=TTL_NS * 2)
    second = alert.alert(fleet, create=board)

    assert second["grown_seats"] == ["default"] and second["fleet_total"] == 2
    assert second["card_id"] == first["card_id"]
    assert len(board.calls) == 2 and len(board.rows) == 1
    assert board.calls[0]["idempotency_key"] == board.calls[1]["idempotency_key"]


def test_recovery_stops_filing_and_the_next_incident_gets_its_own_card(fleet, monkeypatch):
    _log_lines(monkeypatch)
    board = FakeBoard()
    planted = _spool(fleet, 1, status="expired", waited_ns=TTL_NS * 2)
    first = alert.alert(fleet, create=board)

    planted.unlink()                       # the strand drained / was remediated
    drained = alert.alert(fleet, create=board)
    assert drained["fleet_total"] == 0 and drained["recovery"] is True
    assert len(board.calls) == 1, "recovery filed anyway"
    assert drained["generation"] == 1

    _spool(fleet, 2, status="expired", waited_ns=TTL_NS * 2)
    again = alert.alert(fleet, create=board)
    assert len(board.calls) == 2
    assert board.calls[1]["idempotency_key"] == "peer-strand-alert:1"
    assert again["card_id"] != first["card_id"]


def test_a_failing_board_never_breaks_the_evaluation_and_is_retried(fleet, monkeypatch):
    lines = _log_lines(monkeypatch)
    _spool(fleet, 1, status="expired", waited_ns=TTL_NS * 2)

    def boom(**_kwargs):
        raise RuntimeError("the board is locked")

    first = alert.alert(fleet, create=boom)
    assert first["card_id"] is None and first["filed"] is False
    assert lines[0][1]["card"] == "-"
    assert "stranded" in lines[0][1]

    board = FakeBoard()
    second = alert.alert(fleet, create=board)
    assert second["card_id"] is not None and len(board.rows) == 1


def test_an_incident_that_keeps_growing_keeps_the_same_key_across_ticks(fleet, monkeypatch):
    _log_lines(monkeypatch)
    board = FakeBoard()
    for n in (1, 2, 3):
        _spool(fleet, n, status="expired", waited_ns=TTL_NS * 2)
        alert.alert(fleet, create=board)
    assert len(board.rows) == 1 and len(board.calls) == 3


# ---------------------------------------------------------------- priority ladder
def test_priority_is_asked_of_the_board_band_module_never_typed(monkeypatch, tmp_path):
    mod = tmp_path / "bands_probe.py"
    mod.write_text(
        "def band_birth(requested, assignee, board, title, body):\n"
        "    assert assignee == 'platform-stl'\n"
        "    return {'applied': 812345, 'band': 'platform-other'}\n",
        encoding="utf-8",
    )
    from hermes_cli import kanban_db as kb

    monkeypatch.setattr(kb, "_board_meta_for",
                        lambda board: {"priority_policy": {"module": str(mod),
                                                           "function": "band_birth"}})
    assert alert._band_priority("migrations", assignee="platform-stl",
                                title="t", body="b") == (812345, "")


def test_an_unreadable_ladder_is_recorded_not_invented(monkeypatch):
    from hermes_cli import kanban_db as kb

    monkeypatch.setattr(kb, "_board_meta_for", lambda board: {})
    priority, note = alert._band_priority("migrations", assignee="platform-stl",
                                          title="t", body="b")
    assert priority == 0 and "no priority policy" in note


# ---------------------------------------------------------------- the live sweep
def test_the_alert_runs_inside_the_drainers_own_sweep(fleet, monkeypatch):
    calls: list = []

    def _spy(home, **kw):
        calls.append((home, kw))
        return {}

    async def _one_tick(*_a, **_k):
        # the sweep sleeps FIRST, so let the first tick through and stop on the second
        ticks.append(1)
        if len(ticks) > 1:
            raise asyncio.CancelledError()
        return None

    ticks: list = []
    monkeypatch.setattr(alert, "evaluate_and_alert", _spy)
    monkeypatch.setattr(drainer.asyncio, "sleep", _one_tick)
    monkeypatch.setattr(q, "sweep_delivery_queue", lambda home, **kw: 0)
    monkeypatch.setattr(drainer, "drain_once", _no_drain)
    from tools import bot_mode_probe

    monkeypatch.setattr(bot_mode_probe, "_default_home", lambda: str(fleet))

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(drainer.sweep_loop(object()))

    assert calls == [(fleet, {})]


async def _no_drain(*_a, **_k) -> int:
    return 0


def test_the_sweep_hook_is_the_landed_loop_not_a_shadow_runner():
    """The alarm is CALLED from the live module, not merely importable."""
    source = Path(drainer.__file__).read_text(encoding="utf-8")
    assert "strand_alert.evaluate_and_alert(home)" in source
    assert "bot_delivery_strand_alert" in source


def test_the_one_command_census_prints_the_same_number_the_alarm_logs(fleet, monkeypatch, capsys):
    _spool(fleet, 1, status="expired", waited_ns=TTL_NS * 2)
    assert alert._main([str(fleet)]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["stranded"] == q.strand_census(fleet)["totals"]["stranded"] == 1
    assert printed["seats"]["default"]["stranded"] == 1
