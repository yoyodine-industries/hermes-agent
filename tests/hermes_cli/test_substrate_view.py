"""The substrate MIRROR: one read, three surfaces (card t_cc599eea, design §3.6).

The rule under test is not "the mirror is pretty" — it is that the mirror cannot lie in
the one direction that matters: **it must never show green when the substrate is
unmeasured**, and it must show the fail-open guards next to the state. :9501 reported
17/17 green through a total substrate denial on 2026-10-26; every assertion here exists so
that the boot-resident surfaces cannot repeat it.

Fixtures write REAL files (a record, a ledger, a ``config.yaml``) into a temp home and
point the reader's two homes at it, so the parsing is exercised end to end rather than
mocked.
"""

from __future__ import annotations

import json
import time

import pytest

from hermes_cli import substrate_view as sv


# ---------------------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------------------


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """A throwaway fleet+local home with neither a record nor a ledger."""
    (tmp_path / "ops-checks").mkdir()
    monkeypatch.setattr(sv, "fleet_home", lambda: tmp_path)
    monkeypatch.setattr(sv, "local_home", lambda: tmp_path)
    return tmp_path


SHARED_TREES = "/opt/hermes_prod/yaan-majordomo/ops-checks/guard-shared-trees.py"
UV_GUARD = "/opt/hermes_prod/yaan-majordomo/ops-checks/guard-uv-live-venv.py"


def _registry(home, rows):
    lines = ["hooks:", "  pre_tool_call:"]
    for matcher, body in rows:
        lines += [
            f"    - matcher: {matcher}",
            "      command: python3 /Users/hermes_user/.hermes/ops-checks/hook-launcher.py " + body,
            "      fail_closed: false",
        ]
    (home / "config.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _record(home, **overrides):
    now = time.time()
    record = {
        "version": 2,
        "yielded": False,
        "kind": "substrate_admitted",
        "door": "dispatch",
        "sig": None,
        "streak": 0,
        "first_tick": now - 60,
        "last_tick": now - 5,
        "threshold": 3,
        "episode": None,
        "delivery": None,
        "matrix": [],
        "held": [],
        "guards_blind": 0,
        "guards_total": 0,
        "guards": [],
        "read_findings": [],
        "cleared_at": None,
        "duration_s": None,
        "recorded_at": now - 5,
    }
    record.update(overrides)
    path = home / sv.STATUS_FILENAME
    path.write_text(json.dumps(record), encoding="utf-8")
    return path


def _denied_record(home, *, age_s: float = 5.0, **overrides):
    now = time.time()
    return _record(
        home,
        yielded=True,
        kind="substrate_denied",
        door="cron",
        sig="deadbeef",
        streak=4,
        first_tick=now - 1800,
        last_tick=now - age_s,
        episode="deadbeef-1",
        held=[{"board": "platform", "units": 2}],
        matrix=[
            {"path": "/opt/hermes_prod/yaan-platform", "op": "r", "ok": False,
             "errno": "EPERM", "ms": 0.2, "tier": "unit"},
            {"path": "/Users/hermes_user/.hermes", "op": "r", "ok": True,
             "errno": None, "ms": 0.1, "tier": "global"},
        ],
        recorded_at=now - age_s,
        **overrides,
    )


def _degradation(home, body, *, age_s: float = 10.0, reason="body unreadable: EPERM"):
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(time.time() - age_s))
    line = json.dumps(
        {
            "ts": stamp,
            "marker": "HOOK-DEGRADED",
            "target": body,
            "tried": ["/opt/hermes_prod/x", "/Volumes/yoyofs1/x"],
            "reason": reason,
            "pid": 4242,
        }
    )
    with (home / "ops-checks" / sv.LEDGER_FILENAME).open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


# ---------------------------------------------------------------------------------------
# 1. unknown is a first-class answer — and never green
# ---------------------------------------------------------------------------------------


def test_no_record_is_unknown_and_never_ok(home):
    view = sv.read()
    assert view["state"] == "unknown"
    assert view["reason"] == "record_absent"
    assert view["record"]["present"] is False
    assert "no tick has ever recorded a verdict" in view["detail"]
    assert sv.summary(view)["state"] == "unknown"


def test_an_unparseable_record_is_unknown_not_ok(home):
    (home / sv.STATUS_FILENAME).write_text("{not json", encoding="utf-8")
    view = sv.read()
    assert view["state"] == "unknown"
    assert view["reason"] == "record_unparseable"


def test_a_record_nobody_refreshed_is_unknown_with_its_last_known_state(home):
    _denied_record(home, age_s=sv.STALE_AFTER + 60)
    view = sv.read()
    assert view["state"] == "unknown"
    assert view["reason"] == "record_stale"
    # The stale record said DENIED: a reader must be able to see that, and must not
    # promote it back to a current verdict.
    assert view["record"]["last_known_state"] == "denied"
    assert view["record"]["stale"] is True
    assert "stale after" in view["detail"]


def test_a_stale_record_names_the_true_cause_and_the_way_to_measure_it(home, capsys):
    """A stale record means UNMEASURED — a door stopped running, and the hint must say so.

    ``substrate_access.heartbeat`` rewrites the authority record on every healthy pass (bounded
    to ``HEARTBEAT_EVERY``, card ``t_253a5e55``), so silence past ``STALE_AFTER`` is no longer
    "nothing was denied lately": it is "no door has run". ``guards_blind`` is folded in at write
    time, so a record already carries it and needs no live fold. The older hint — "if neither has
    run since the record was written" — named a cause the record cannot have, and on 2026-10-26
    it pointed readers at the scheduler while the substrate was the fault.
    """
    _denied_record(home, age_s=sv.STALE_AFTER + 60)
    detail = sv.read()["detail"]
    assert "no door has run" in detail, "the detail must state what a stale record means"
    assert "only when a denial is observed" not in detail, (
        "a healthy tick DOES write the record now; the old string is the lie this test outlives"
    )
    assert "not been measured" not in detail

    finding = _doctor_row()
    out = capsys.readouterr().out
    assert "Substrate UNKNOWN" in out
    assert "probe_all(use_cache=False)" in out, "the hint must name the on-demand measurement"
    assert "if neither has run since" not in out, "the false cause is gone from the doctor row"
    assert not finding.manual_issues, "an unmeasured substrate is still not a fault to escalate"


def test_a_fresh_admitted_record_is_ok(home):
    _record(home)
    view = sv.read()
    assert view["state"] == "ok"
    assert view["reason"] is None
    assert view["record"]["age_s"] is not None


# ---------------------------------------------------------------------------------------
# 2. denied carries the evidence the acting card and the mirrors need
# ---------------------------------------------------------------------------------------


def test_a_denied_record_names_the_roots_errno_episode_and_held_units(home):
    _denied_record(home)
    view = sv.read()
    assert view["state"] == "denied"
    assert view["door"] == "cron"
    assert view["episode"] == "deadbeef-1"
    assert view["streak"] == 4 and view["threshold"] == 3
    assert view["roots_denied"] == 1
    assert view["roots"][0]["errno"] == "EPERM"
    assert view["roots"][0]["tier"] == "unit"
    assert view["held_count"] == 1
    assert view["since"] is not None


# ---------------------------------------------------------------------------------------
# 3. guards_blind — the counted fail-open, folded into the same view
# ---------------------------------------------------------------------------------------


def test_one_blind_body_blinds_every_row_that_uses_it(home):
    _registry(home, [("terminal", UV_GUARD), ("write_file", SHARED_TREES), ("patch", SHARED_TREES)])
    _record(home)
    _degradation(home, SHARED_TREES)
    view = sv.read()
    assert view["state"] == "ok"
    assert view["guards_total"] == 3
    # guard-shared-trees blinds TWO armed rows; the uv guard is intact.
    assert view["guards_blind"] == 2
    assert {item["row"] for item in view["guards"]} == {"pre_tool_call[1]:write_file",
                                                       "pre_tool_call[2]:patch"}
    assert {(item["hook"], item["body"]) for item in view["guards"]} == {("guard-shared-trees", SHARED_TREES)}
    assert all(item["reason"] and item["since"] for item in view["guards"])


def test_a_degradation_older_than_the_window_is_not_current(home):
    _registry(home, [("write_file", SHARED_TREES)])
    _record(home)
    _degradation(home, SHARED_TREES, age_s=sv.BLIND_WITHIN + 120)
    view = sv.read()
    assert view["guards_blind"] == 0


def test_repeat_degradations_of_one_body_keep_their_first_sighting(home):
    _registry(home, [("write_file", SHARED_TREES)])
    _record(home)
    _degradation(home, SHARED_TREES, age_s=300)
    _degradation(home, SHARED_TREES, age_s=120)
    view = sv.read()
    assert view["guards_blind"] == 1
    item = view["guards"][0]
    assert item["observations"] == 2
    # `since` is when it went blind, not when it was last seen.
    assert item["since"] != item["last"]


def test_a_degradation_of_a_body_no_row_names_is_counted_unregistered(home):
    _registry(home, [("terminal", UV_GUARD)])
    _record(home)
    _degradation(home, "/opt/hermes_prod/yaan-majordomo/ops-checks/guard-undeployed.py")
    view = sv.read()
    assert view["guards_blind"] == 1
    assert view["guards"][0]["row"] == "(unregistered)"


def test_an_unreadable_registry_reports_an_unknown_total_not_zero(home):
    _record(home)
    _degradation(home, SHARED_TREES)
    view = sv.read()
    assert view["guards_total"] is None
    assert view["guards_blind"] == 1


def test_a_fresh_stamp_with_no_ledger_row_is_one_unattributed_degradation(home):
    _registry(home, [("terminal", UV_GUARD)])
    _record(home)
    (home / "ops-checks" / sv.STAMP_FILENAME).write_text(str(int(time.time() - 20)), encoding="utf-8")
    view = sv.read()
    assert view["guards_blind"] == 1
    assert view["guards"][0]["body"] == "(unattributed)"


def test_the_mirror_never_reports_fewer_blind_guards_than_the_record_claims(home):
    """The card quotes the record; the dashboard must not contradict it."""
    _registry(home, [("write_file", SHARED_TREES), ("terminal", UV_GUARD)])
    _record(home, guards_blind=2, guards_total=2, guards=[
        {"hook": "guard-shared-trees", "row": "pre_tool_call[0]:write_file",
         "body": SHARED_TREES, "since": "2026-10-26T16:02:00-04:00", "reason": "EPERM"},
        {"hook": "guard-shared-trees", "row": "pre_tool_call[1]:terminal",
         "body": SHARED_TREES, "since": "2026-10-26T16:02:00-04:00", "reason": "EPERM"},
    ])
    view = sv.read()
    assert view["guards_blind"] == 2
    assert view["guards_source"] == "record"
    assert view["guards_ledger_blind"] == 0
    assert len(view["guards"]) == 2


def test_the_mirror_never_hides_a_guard_that_just_went_blind(home):
    _registry(home, [("write_file", SHARED_TREES)])
    _record(home, guards_blind=0, guards_total=1, guards=[])
    _degradation(home, SHARED_TREES)
    view = sv.read()
    assert view["guards_blind"] == 1
    assert view["guards_source"] == "ledger"
    assert view["guards"][0]["row"] == "pre_tool_call[0]:write_file"


def test_the_mirror_counts_a_pre_leg_b_row_list_as_its_rows(home):
    """LEG A wrote the blind ROWS in ``guards_blind``; the acting card then reads "2 of 5".

    A mirror that understands only the int shape falls back to the live ledger and can print
    "0 of 1" beside that card -- the contradiction :func:`_reconcile_guards` exists to
    prevent, in the one older shape the record has actually carried on this host.
    """
    _registry(home, [("write_file", SHARED_TREES), ("patch", SHARED_TREES), ("terminal", UV_GUARD)])
    rows = [
        {"hook": "guard-shared-trees", "row": "pre_tool_call[0]:write_file",
         "body": SHARED_TREES, "since": "2026-10-26T16:02:00-04:00", "reason": "EPERM"},
        {"hook": "guard-shared-trees", "row": "pre_tool_call[1]:patch",
         "body": SHARED_TREES, "since": "2026-10-26T16:02:00-04:00", "reason": "EPERM"},
    ]
    _record(home, guards_blind=list(rows), guards_total=5)
    view = sv.read()
    assert view["guards_blind"] == 2
    assert view["guards_total"] == 5
    assert view["guards_source"] == "record"
    assert {item["row"] for item in view["guards"]} == {
        "pre_tool_call[0]:write_file",
        "pre_tool_call[1]:patch",
    }


def test_guards_blind_is_folded_into_the_authority_record(home, monkeypatch):
    """The record carries the count AND the rows — one number for cards, one list for detail."""
    from hermes_cli import substrate_access as sa

    _registry(home, [("terminal", UV_GUARD), ("write_file", SHARED_TREES)])
    _degradation(home, SHARED_TREES)
    monkeypatch.setattr(sa, "hermes_home", lambda: home)
    monkeypatch.setattr(sa, "map_path", lambda path=None: home / "substrate_roots.yaml")
    (home / "substrate_roots.yaml").write_text("global: []\nunits: {}\n", encoding="utf-8")

    report = sa.ProbeReport(at=time.time())
    target = home / sv.STATUS_FILENAME
    sa.record(report, door="dispatch", path=target)
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["guards_blind"] == 1
    assert payload["guards_total"] == 2
    assert payload["guards"][0]["hook"] == "guard-shared-trees"
    assert set(payload["guards"][0]) >= {"hook", "row", "body", "since", "reason"}


# ---------------------------------------------------------------------------------------
# 4. the surfaces: doctor row, dashboard payload, gateway payload
# ---------------------------------------------------------------------------------------


def _doctor_row():
    """The decorated check: `(should_fix) -> Finding` (the decorator supplies the Finding)."""
    from hermes_cli.doctor_substrate import _check_substrate

    return _check_substrate(False)


def test_the_doctor_row_is_red_on_a_denial(home, capsys):
    _denied_record(home)
    finding = _doctor_row()
    out = capsys.readouterr().out
    assert finding.manual_issues, "a denial with no doctor issue is the false green this card exists to kill"
    assert "Substrate REFUSED" in out
    assert "EPERM" in out and "deadbeef-1" in out


def test_the_doctor_row_is_green_when_the_record_is_fresh(home, capsys):
    _record(home)
    finding = _doctor_row()
    out = capsys.readouterr().out
    assert not finding.issues and not finding.manual_issues
    assert "Substrate reachable" in out


def test_the_doctor_row_is_yellow_not_green_with_no_record(home, capsys):
    finding = _doctor_row()
    out = capsys.readouterr().out
    assert "Substrate UNKNOWN" in out and "record_absent" in out
    assert "Substrate reachable" not in out, "an unmeasured substrate must never render green"
    assert not finding.manual_issues, "no record is not yet a fault to escalate"


def test_the_doctor_row_escalates_a_blind_guard_while_the_substrate_is_healthy(home, capsys):
    _registry(home, [("terminal", UV_GUARD), ("write_file", SHARED_TREES)])
    _record(home)
    _degradation(home, SHARED_TREES)
    finding = _doctor_row()
    out = capsys.readouterr().out
    assert "Substrate reachable" in out
    assert "blind" in out
    assert any("guard-shared-trees" in issue for issue in finding.manual_issues)


def test_the_public_summary_carries_the_count_and_no_host_paths(home):
    _registry(home, [("write_file", SHARED_TREES)])
    _denied_record(home)
    _degradation(home, SHARED_TREES)
    payload = sv.summary()
    assert payload["state"] == "denied"
    assert payload["guards_blind"] == 1
    assert payload["guards_total"] == 1
    blob = json.dumps(payload)
    assert "/opt/hermes_prod" not in blob and "/Volumes" not in blob
    assert "EPERM" not in blob


def test_the_dashboard_endpoint_serves_the_view_and_a_text_render(home):
    """The :8643 payload the SPA reads. Skipped only when the suite runs from a checkout
    under the real hermes home: tests/home_io_guard then refuses the pydantic plugin scan
    of the editable install's metadata, which importing the router triggers. That is an
    environment limit, not a property of the route — the same assertions run out of pytest
    in the card's verification script, and in CI outside ~/.hermes."""
    try:
        _assert_dashboard_surface(home)
    except AssertionError as exc:
        if "REAL hermes home" not in str(exc):
            raise
        pytest.skip(f"home_io_guard refuses the editable install's metadata read: {exc}")


def _assert_dashboard_surface(home):
    _registry(home, [("write_file", SHARED_TREES)])
    _denied_record(home)
    _degradation(home, SHARED_TREES)
    pytest.importorskip("fastapi")
    import fastapi
    from fastapi.testclient import TestClient

    from hermes_cli.web_routers.status import router

    app = fastapi.FastAPI()
    app.include_router(router)
    client = TestClient(app)

    body = client.get("/api/substrate").json()
    assert body["state"] == "denied"
    assert body["guards_blind"] == 1
    assert body["roots"][0]["errno"] == "EPERM"

    text = client.get("/api/substrate?format=text").text
    assert "substrate denied" in text
    assert "blind guards (1 of 1)" in text
    assert "guard-shared-trees" in text


def test_the_gateway_substrate_route_is_loopback_gated(home):
    pytest.importorskip("aiohttp")
    asyncio = pytest.importorskip("asyncio")

    from gateway.platforms.webhook import WebhookAdapter as WebhookPlatform

    _registry(home, [("write_file", SHARED_TREES)])
    _denied_record(home)
    _degradation(home, SHARED_TREES)

    class _Request:
        def __init__(self, remote):
            self.remote = remote
            self.query = {}

    platform = object.__new__(WebhookPlatform)
    loop = asyncio.new_event_loop()
    try:
        local = loop.run_until_complete(platform._handle_substrate(_Request("127.0.0.1")))
        remote = loop.run_until_complete(platform._handle_substrate(_Request("203.0.113.9")))
    finally:
        loop.close()

    local_body = json.loads(local.text)
    remote_body = json.loads(remote.text)
    assert local_body["state"] == "denied"
    assert local_body["roots"][0]["errno"] == "EPERM"
    # Off-box callers get the summary only: no host paths, no root names.
    assert remote_body["state"] == "denied"
    assert "roots" not in remote_body and "guards" not in remote_body
