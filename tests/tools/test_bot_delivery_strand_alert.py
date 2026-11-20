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


@pytest.fixture(autouse=True)
def _board_read_says_live(monkeypatch):
    """The ledger's self-check defaults to "the card is alive" in these unit tests.

    The real seam opens the board (``hermes_cli.kanban_db``); a unit test must not, and
    a board that does not exist here would read as a FAILED read and suppress filing.
    The two tests that exercise the self-check pass their own ``read_card``.
    """
    monkeypatch.setattr(alert, "_read_card_status", lambda card_id, **kw: "todo",
                        raising=False)
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


# ---------------------------------------------------------------------------
# the windowed count (card t_330bee02, platform-stl ruling 2026-11-14): the meter
# must be able to reach zero, or recovery is unreachable and the guard is one-shot
# ---------------------------------------------------------------------------
HOUR_NS = int(3600 * 1e9)
WINDOW = 86400          # the ruling's default; the knob is exercised separately


def test_expired_history_outside_the_window_is_reported_but_not_stranded(fleet):
    """The 1192-record case: old settled records are HISTORY, not strands."""
    _spool(fleet, 1, status="expired", waited_ns=8 * 86400 * int(1e9))
    _spool(fleet, 2, status="expired", waited_ns=59 * 86400 * int(1e9))
    seat = q.seat_strand_census(fleet)
    assert (seat["stranded"], seat["expired"], seat["expired_within_window"]) == (0, 2, 0)


def test_one_expired_record_inside_the_window_is_stranded_once(fleet):
    _spool(fleet, 1, status="expired", waited_ns=HOUR_NS)
    seat = q.seat_strand_census(fleet)
    assert seat["expired_within_window"] == 1
    assert seat["expired"] == 1
    assert seat["stranded"] == 1, "counted twice?"
    assert seat["pending_over_ttl"] == 0 and seat["claimed_stuck"] == 0


def test_stranded_is_the_windowed_sum_of_its_three_terms(fleet):
    _spool(fleet, 1, status="expired", waited_ns=HOUR_NS)                 # inside window
    _spool(fleet, 2, status="expired", waited_ns=30 * 86400 * int(1e9))   # history
    _spool(fleet, 3, status="queued", waited_ns=TTL_NS * 3)               # over TTL
    seat = q.seat_strand_census(fleet)
    assert seat["stranded"] == (seat["pending_over_ttl"] + seat["claimed_stuck"]
                                + seat["expired_within_window"]) == 2
    assert seat["expired"] == 2


def test_an_expired_record_with_no_clock_is_reported_and_never_hidden(fleet):
    path = _spool(fleet, 1, status="expired")
    record = json.loads(path.read_text(encoding="utf-8"))
    record.pop("updated_at", None)
    record.pop("created_at", None)
    path.write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
    seat = q.seat_strand_census(fleet)
    assert seat["expired_undated"] == 1
    assert seat["expired"] == 1
    assert seat["stranded"] == 0, "an undated record must not be guessed into the window"


def test_the_expire_window_is_the_env_knob(fleet, monkeypatch):
    monkeypatch.setenv("HERMES_STRAND_EXPIRE_WINDOW_SECONDS", "60")
    _spool(fleet, 1, status="expired", waited_ns=HOUR_NS)
    seat = q.seat_strand_census(fleet)
    assert (seat["stranded"], seat["expired_within_window"]) == (0, 0)
    assert q.strand_census(fleet)["window_seconds"] == 60.0


def test_the_window_makes_recovery_reachable(fleet, monkeypatch):
    """The same record, aged past the window: the count returns to zero on its own.

    Today's code cannot pass this: ``expired`` was terminal and permanent, so the
    ledger's incident could never close and ``generation`` never advanced.
    """
    _log_lines(monkeypatch)
    board = FakeBoard()
    path = _spool(fleet, 1, status="expired", waited_ns=HOUR_NS)

    first = alert.alert(fleet, create=board)
    assert (first["fleet_total"], first["generation"], first["recovery"]) == (1, 0, False)

    record = json.loads(path.read_text(encoding="utf-8"))
    aged = time.time_ns() - 2 * 86400 * int(1e9)
    record["created_at"] = record["updated_at"] = aged
    path.write_text(json.dumps(record, sort_keys=True), encoding="utf-8")

    after = alert.alert(fleet, create=board)
    assert after["fleet_total"] == 0
    assert after["recovery"] is True
    assert after["generation"] == 1 and after["card_id"] is None
    assert len(board.calls) == 1, "recovery filed a card"
    assert after["census"]["totals"]["expired"] == 1, "history stopped being reported"


def test_a_card_closed_on_the_board_reopens_the_incident(fleet, monkeypatch):
    """D3: a terminal card must not silence the ledger while the backlog stands."""
    _log_lines(monkeypatch)
    board = FakeBoard()
    _spool(fleet, 1, status="expired", waited_ns=HOUR_NS)
    first = alert.alert(fleet, create=board)

    statuses = {first["card_id"]: "archived"}

    def reader(card_id, **_kw):
        return statuses.get(card_id, "todo")

    second = alert.alert(fleet, create=board, read_card=reader)
    assert second["closed_card"] == first["card_id"]
    assert second["generation"] == 1 and second["card_read_failed"] is False
    assert board.calls[-1]["idempotency_key"] == "peer-strand-alert:1"
    assert second["card_id"] not in (None, first["card_id"])

    third = alert.alert(fleet, create=board, read_card=reader)
    assert third["card_id"] == second["card_id"]
    assert len(board.rows) == 2, "a repeat evaluation minted a second row for generation 1"


def test_a_card_row_that_is_gone_is_the_same_as_a_closed_one(fleet, monkeypatch):
    _log_lines(monkeypatch)
    board = FakeBoard()
    _spool(fleet, 1, status="expired", waited_ns=HOUR_NS)
    first = alert.alert(fleet, create=board)

    second = alert.alert(fleet, create=board, read_card=lambda card_id, **_kw: None)
    assert second["closed_card"] == first["card_id"]
    assert second["generation"] == 1
    assert board.calls[-1]["idempotency_key"] == "peer-strand-alert:1"


def test_a_failing_board_read_files_nothing_and_keeps_the_card(fleet, monkeypatch):
    """D3: a board hiccup must never mint a duplicate card or orphan the live one."""
    lines = _log_lines(monkeypatch)
    board = FakeBoard()
    _spool(fleet, 1, status="expired", waited_ns=HOUR_NS)
    first = alert.alert(fleet, create=board)
    calls_before = len(board.calls)

    _spool(fleet, 2, status="expired", waited_ns=HOUR_NS)      # the backlog GREW

    def boom(card_id, **_kw):
        raise RuntimeError("the board is locked")

    second = alert.alert(fleet, create=board, read_card=boom)
    assert second["card_read_failed"] is True
    assert second["card_id"] == first["card_id"], "a failed read orphaned the live card"
    assert len(board.calls) == calls_before, "a failed read filed blind"
    assert second["fleet_total"] == 2
    assert [event for event, _ in lines][-1] == "strand_alert"
    assert lines[-1][1]["stranded"] == 2


def test_the_card_body_names_the_window_its_terms_and_the_instant(fleet, monkeypatch):
    board = FakeBoard()
    _spool(fleet, 1, status="expired", waited_ns=HOUR_NS)                # inside window
    _spool(fleet, 2, status="expired", waited_ns=30 * 86400 * int(1e9))  # history
    _spool(fleet, 3, status="queued", waited_ns=TTL_NS * 3)              # over TTL

    verdict = alert.alert(fleet, create=board)
    body = board.calls[0]["body"]
    assert verdict["fleet_total"] == 2
    assert f"expired in the last {WINDOW}s" in body
    assert f"= 1 over-TTL queued + 0 claimed stuck + 1 expired in the last {WINDOW}s" in body
    assert f"expired: 2 total / 1 within the last {WINDOW}s" in body
    assert "`expired_undated`: 0" in body
    assert "read at: " in body and "epoch seconds" in body
    assert f"window {WINDOW}s" in body


def test_the_log_line_carries_the_windowed_terms(fleet, monkeypatch):
    lines = _log_lines(monkeypatch)
    _spool(fleet, 1, status="expired", waited_ns=HOUR_NS)
    _spool(fleet, 2, status="expired", waited_ns=30 * 86400 * int(1e9))
    alert.alert(fleet, create=FakeBoard())
    fields = lines[0][1]
    assert (fields["stranded"], fields["expired"], fields["expired_within_window"]) == (1, 2, 1)
    assert fields["expired_undated"] == 0
    assert fields["window_seconds"] == WINDOW


def test_the_one_command_census_prints_its_window(fleet, capsys):
    _spool(fleet, 1, status="expired", waited_ns=HOUR_NS)
    _spool(fleet, 2, status="expired", waited_ns=30 * 86400 * int(1e9))
    assert alert._main([str(fleet)]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["stranded"] == 1
    assert printed["expired"] == 2 and printed["expired_within_window"] == 1
    assert printed["window_seconds"] == WINDOW
    assert printed["seats"]["default"]["expired_within_window"] == 1
    assert printed["read_at"] > 0
