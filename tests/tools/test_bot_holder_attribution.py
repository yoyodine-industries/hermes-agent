"""Tests: FLOck-free units for the holder attribution (t_f93b2489 §2-§5).

The holder rides three additive channels — the envelope's ``holder`` field, the human text, and
the gateway RPC data — and every one of them is provable without a turn lock: a fragment is a pure
function of the holder dict, ``read_turn_lock_holder`` is a file read, and the envelope projection
is ``build_receipt``. Those units run on every host, so a green lane here is real evidence rather
than a file of skips (the flock-dependent half lives in ``tests/tools/test_bot_turn_lock.py``,
declared ``posix``, and the bound-handler half in ``test_bot_relay_deliver_lane.py``).

Only the two liveness cases carry ``platforms("posix")``: they exercise the ``os.kill(pid, 0)``
probe, which cannot be reached on a host without ``fcntl`` (no holder record is ever published
there — the turn lock is a no-op) and has no meaning on one.
"""

from __future__ import annotations

import json
import os
import time

import pytest

from tools import bot_delivery_queue as delivery_queue
from tools import bot_mode_dm, bot_relay
from tools.bot_relay import TurnBusyError, holder_fragment, read_turn_lock_holder, turn_lock_path

#: The frozen §1.3 tuple, verbatim as it stood on the live tip before this change.
LEGACY_ENVELOPE_FIELDS = (
    "object", "result", "status", "status_detail", "send_id", "delivery_id", "idempotency_key",
    "replayed", "peer", "profile", "session_id", "queue_position", "attempts", "queued_seconds",
    "waited_seconds", "busy", "retryable", "retry_after_seconds", "reply", "error", "reason",
    "detail", "at",
)

TURN_HOLDER = {"kind": "turn_lock", "pid": 1234, "ppid": 1, "profile": "default", "host": "h",
               "started_at": 1.0, "age_seconds": 252.0, "alive": True}
LIVE_HOLDER = {"kind": "live_owner", "surface": "desktop", "session_id": "20261013_223229_f78ac5",
               "started_at": 1.0, "age_seconds": 252.0, "alive": True}


def _record(**over) -> dict:
    record = {"status": delivery_queue.STATUS_QUEUED,
              "status_detail": delivery_queue.STATUS_DETAIL_TARGET_BUSY,
              "delivery_id": "d1", "target_profile": "ops", "attempts": 0, "created_at": 1}
    record.update(over)
    return record


# ── §2 the ONE wording function ─────────────────────────────────────────────


def test_holder_fragment_wording_is_exact():
    assert holder_fragment(dict(TURN_HOLDER, age_seconds=42.0)) == "held by pid 1234 for 42s"
    assert holder_fragment(dict(TURN_HOLDER, age_seconds=252.0)) == "held by pid 1234 for 4m 12s"
    assert holder_fragment(dict(TURN_HOLDER, age_seconds=7620.0)) == "held by pid 1234 for 2h 07m"
    assert holder_fragment(dict(TURN_HOLDER, alive=False)) == "held by pid 1234, no longer running"
    assert holder_fragment({"kind": "turn_lock", "pid": 7, "alive": True}) == "held by pid 7"
    assert holder_fragment(LIVE_HOLDER) == (
        "seat held live by desktop (session 20261013_223229_f78ac5) for 4m 12s")


def test_holder_fragment_is_empty_for_an_unknown_holder():
    """Unknown is never invented: None, {}, and a pid-less record all yield ``""``."""
    assert holder_fragment(None) == ""
    assert holder_fragment({}) == ""
    assert holder_fragment({"kind": "turn_lock", "alive": True}) == ""
    assert holder_fragment({"kind": "turn_lock"}) == ""
    assert holder_fragment({"kind": "live_owner"}) == (
        "seat held live by another surface")  # surface unknown, still no fake pid


def test_holder_payload_drops_none_values_and_stays_empty_when_unknown():
    """§5.4: a holder-less error yields ``{}`` (and ``holder: null`` on the wire, never ``{}``)."""
    assert TurnBusyError("ops", 0.05).holder_payload() == {}
    assert TurnBusyError("ops", 0.05, {}).holder_payload() == {}
    # A record that names no process is unknown too, not an empty-shaped holder.
    assert TurnBusyError("ops", 0.05, {"kind": "turn_lock", "alive": True}).holder_payload() == {}

    err = TurnBusyError("ops", 0.05, dict(TURN_HOLDER, ppid=None, host=None))
    payload = err.holder_payload()
    assert payload["pid"] == 1234 and payload["kind"] == "turn_lock"
    assert "ppid" not in payload and "host" not in payload
    assert payload["alive"] is True and payload["age_seconds"] == 252.0


def test_live_owner_holder_is_projected_off_the_entry_the_caller_holds():
    """§4: the live-owner holder comes from fields of the registry entry — no new plumbing."""
    holder = bot_relay.holder_from_live_owner(
        {"session_id": "s1", "surface": "desktop", "pid": 4321, "started_at": 100.0,
         "lease_id": "l", "metadata": {"anything": True}}, now=352.0)
    assert holder == {"kind": "live_owner", "surface": "desktop", "session_id": "s1", "pid": 4321,
                      "started_at": 100.0, "age_seconds": 252.0, "alive": True}
    # An entry that names nobody (or a lookup that returns nothing) is unknown, not an error.
    assert bot_relay.holder_from_live_owner({}) is None
    assert bot_relay.holder_from_live_owner(None) is None
    assert bot_relay.holder_from_live_owner({"lease_id": "l"}) is None
    # pid is optional for a live owner; age is dropped when the entry has no usable start time.
    session_only = bot_relay.holder_from_live_owner({"session_id": "s1", "started_at": "junk"})
    assert session_only == {"kind": "live_owner", "session_id": "s1", "alive": True}
    assert holder_fragment(session_only) == "seat held live by another surface (session s1)"


# ── §2/§5.7 the envelope: one appended field, nothing else moved ─────────────


def test_envelope_field_order_is_a_one_field_appended_delta():
    """§5.7 schema-delta guard: the first 23 fields are today's tuple, byte for byte."""
    assert list(delivery_queue.ENVELOPE_FIELDS) == [*LEGACY_ENVELOPE_FIELDS, "holder"]
    assert delivery_queue.ENVELOPE_FIELDS[-1] == "holder"
    assert list(delivery_queue.RECORD_FIELDS) == [
        "delivery_id", "idempotency_key", "fingerprint", "sender_profile", "target_profile",
        "target_session_id", "message", "status", "status_detail", "attempts", "reoffer_count",
        "queue_position", "created_at", "updated_at", "claimed_at", "attempts_log", "reply",
        "error", "reason", "sequence"]  # the DURABLE record is untouched: holder is never persisted


def test_envelope_projects_the_holder_and_never_an_empty_object():
    with_holder = delivery_queue.build_envelope(_record(), holder=TURN_HOLDER)
    assert list(with_holder) == list(delivery_queue.ENVELOPE_FIELDS)
    assert with_holder["holder"] == TURN_HOLDER

    for empty in (None, {}):
        envelope = delivery_queue.build_envelope(_record(), holder=empty)
        assert envelope["holder"] is None, "unknown is None, never {}"


def test_envelope_without_a_holder_is_byte_identical_to_the_pre_holder_shape():
    """The whole point of "additive": with no holder the envelope carries `None` and no drift."""
    before = {key: None for key in LEGACY_ENVELOPE_FIELDS}
    after = delivery_queue.build_envelope(_record(), send_id="s", waited_seconds=0.0)
    assert list(after) == [*LEGACY_ENVELOPE_FIELDS, "holder"]
    assert {key: after[key] for key in LEGACY_ENVELOPE_FIELDS}.keys() == before.keys()
    assert after["status_detail"] == delivery_queue.STATUS_DETAIL_TARGET_BUSY, "byte-identical"
    assert after["detail"] == delivery_queue.DETAIL_RECEIPT


# ── §2/§5.2 the receipt: the text channel, appended at the END ───────────────


def test_receipt_appends_the_fragment_at_the_end_of_the_live_text():
    payload = delivery_queue.build_receipt(_record(), waited_seconds=0.0, holder=TURN_HOLDER)
    assert list(payload) == list(delivery_queue.ENVELOPE_FIELDS)
    assert payload["result"] == "receipt" and payload["status"] == "queued"
    assert payload["status_detail"] == delivery_queue.STATUS_DETAIL_TARGET_BUSY
    assert payload["busy"] is True and payload["attempts"] == 0
    assert payload["detail"] == (f"{delivery_queue.DETAIL_RECEIPT}"
                                 " — held by pid 1234 for 4m 12s")
    assert payload["detail"].startswith(delivery_queue.DETAIL_RECEIPT), "every live substring survives"
    assert payload["holder"]["pid"] == 1234


def test_receipt_without_a_holder_leaves_the_text_untouched():
    """§5.2 negative control: a free slot's receipt is unchanged — measured, never assumed."""
    plain = delivery_queue.build_receipt(_record(), waited_seconds=0.0, busy=False)
    assert plain["holder"] is None
    assert plain["detail"] == delivery_queue.DETAIL_RECEIPT
    assert plain["busy"] is False
    # An unreadable lockfile is UNKNOWN: that is not an error and it adds no fragment.
    unknown = delivery_queue.build_receipt(_record(), waited_seconds=0.0, holder={})
    assert unknown["holder"] is None
    assert unknown["detail"] == delivery_queue.DETAIL_RECEIPT


def test_live_owner_receipt_names_the_surface_in_the_text_and_the_field():
    """§5.6: the live-owner hold rides the SAME field and the same text channel."""
    payload = delivery_queue.build_receipt(
        _record(status_detail=delivery_queue.STATUS_DETAIL_LIVE_OWNER), waited_seconds=0.0,
        holder=LIVE_HOLDER)
    assert payload["holder"]["kind"] == "live_owner"
    assert payload["holder"]["surface"] == "desktop"
    assert payload["status_detail"] == delivery_queue.STATUS_DETAIL_LIVE_OWNER
    assert payload["busy"] is True and payload["attempts"] == 0
    assert payload["detail"].startswith(delivery_queue.DETAIL_HELD_LIVE)
    assert payload["detail"].endswith(
        " — seat held live by desktop (session 20261013_223229_f78ac5) for 4m 12s")

    unknown = delivery_queue.build_receipt(
        _record(status_detail=delivery_queue.STATUS_DETAIL_LIVE_OWNER), waited_seconds=0.0)
    assert unknown["holder"] is None
    assert unknown["detail"] == delivery_queue.DETAIL_HELD_LIVE


# ── §5.5 read_turn_lock_holder: unknown is never an error ────────────────────


def test_read_turn_lock_holder_returns_none_for_absent_empty_and_torn_records(tmp_path):
    root = tmp_path / "r"
    root.mkdir()
    assert read_turn_lock_holder(root, "default") is None  # absent

    path = turn_lock_path(root, "default")
    path.parent.mkdir(parents=True, exist_ok=True)
    for content in (b"", b"   \n", b"{not json", b"[1,2]", b'{"pid": "nope"}',
                    b'{"pid": 0}', b'{"pid": -3}', b'{"profile": "default"}'):
        path.write_bytes(content)
        assert read_turn_lock_holder(root, "default") is None, content
    # A directory where the lockfile should be is unreadable, not fatal.
    path.unlink()
    path.mkdir()
    assert read_turn_lock_holder(root, "default") is None


@pytest.mark.platforms("posix")
def test_read_turn_lock_holder_probes_liveness_instead_of_trusting_the_record(tmp_path):
    root = tmp_path / "r"
    path = turn_lock_path(root, "default")
    path.parent.mkdir(parents=True, exist_ok=True)

    # A record naming a LIVE process: this one. The probe says so.
    path.write_text(json.dumps({"pid": os.getpid(), "ppid": os.getppid(), "profile": "default",
                                "started_at": time.time() - 30, "host": "here"}), encoding="utf-8")
    holder = read_turn_lock_holder(root, "default")
    assert holder["kind"] == "turn_lock" and holder["pid"] == os.getpid()
    assert holder["alive"] is True
    assert 30 <= holder["age_seconds"] < 40
    assert holder["host"] == "here"

    # A record claiming a live pid while the process is gone is reported as NOT running (that is
    # the operator's reclaim signal), and one with an unparsable start has no age at all.
    assert bot_relay._pid_alive(2_147_483_647) is False  # top of the pid_t range, nothing there
    path.write_text(json.dumps({"pid": 2_147_483_647, "profile": "default"}), encoding="utf-8")
    stale = read_turn_lock_holder(root, "default")
    assert stale["alive"] is False and stale["kind"] == "turn_lock"
    assert "started_at" not in stale and "age_seconds" not in stale
    assert holder_fragment(stale) == "held by pid 2147483647, no longer running"

    # A pid beyond the host's pid_t range is UNKNOWN, not a crash: the read never raises, and an
    # unanswerable probe leaves `alive` out entirely (unknown ≠ dead, unknown ≠ a holder with no
    # liveness at all — the fragment still names the pid).
    path.write_text(json.dumps({"pid": 3_999_999_999, "profile": "default"}), encoding="utf-8")
    unprobeable = read_turn_lock_holder(root, "default")
    assert unprobeable["pid"] == 3_999_999_999 and "alive" not in unprobeable
    assert holder_fragment(unprobeable) == "held by pid 3999999999"

    # A probe we are not permitted to make is not "dead": EPERM means the process EXISTS and is
    # someone else's (pid 1), so the holder is reported alive and never written off.
    path.write_text(json.dumps({"pid": 1, "profile": "default"}), encoding="utf-8")
    assert read_turn_lock_holder(root, "default")["alive"] is True
    assert holder_fragment(read_turn_lock_holder(root, "default")) == "held by pid 1"


# ── §3.7 the gateway refusal's `data`: reason + holder ───────────────────────


def test_refusal_data_carries_the_holder_beside_the_reason():
    """§3.7: ``data.reason`` is the only field the Desktop forwards — the holder rides beside it.

    Importable without booting ``tui_gateway.server`` (so it runs on this host, where the
    server-driven handler tests are linux-gated).
    """
    from tui_gateway.methods_bot_relay import _refusal_data

    assert _refusal_data(RuntimeError("provider down"), "unknown") == {"reason": "unknown"}
    assert _refusal_data(TurnBusyError("ops", 0.2), "target_busy") == {"reason": "target_busy"}

    busy = TurnBusyError("ops", 0.2, dict(TURN_HOLDER, ppid=None))
    assert _refusal_data(busy, "target_busy") == {
        "reason": "target_busy",
        "holder": {"kind": "turn_lock", "pid": 1234, "profile": "default", "host": "h",
                   "started_at": 1.0, "age_seconds": 252.0, "alive": True}}


def test_the_deliver_handler_binds_the_refusal_data_helper_as_a_default():
    """Handlers are re-created against server.py's globals, so a module-level lookup is invisible
    from the handler body: the helper must be bound as a default parameter (and survive the
    rebind, which copies the function's defaults)."""
    import inspect

    from tui_gateway import method_ctx
    from tui_gateway import methods_bot_relay

    handler = dict(methods_bot_relay._registry._pending)["bot_relay.deliver"]
    assert inspect.signature(handler).parameters["_data"].default is methods_bot_relay._refusal_data

    rebound = method_ctx.rebind(handler, {})
    assert inspect.signature(rebound).parameters["_data"].default is methods_bot_relay._refusal_data


# ── §4 the live-owner receipt on the runner path ────────────────────────────


class _RefusingProc:
    returncode = 1
    stdout = ""
    stderr = ("hermes-refusal-reason: SESSION_NOT_OWNED\n"
              "Session abc is held live by desktop. This delivery is ACCEPTED and retained.\n")


def _run_refused_turn(tmp_path, monkeypatch, capsys, owner_entry):
    dm = tmp_path / "dm.txt"
    dm.write_text("hi", encoding="utf-8")
    monkeypatch.setattr(bot_mode_dm, "_local_delivery_home", lambda argv: tmp_path / "home")
    monkeypatch.setattr("tools.bot_live_delivery.find_canonical_owner",
                        lambda home: None if owner_entry is None else dict(owner_entry))
    monkeypatch.setattr(bot_mode_dm.subprocess, "run", lambda *a, **k: _RefusingProc())
    rc = bot_mode_dm._run_local_turn(["hermes", "-p", "ops", "chat"], str(dm))
    return rc, json.loads(capsys.readouterr().out.strip())


def test_live_owner_receipt_names_the_surface_and_the_session(tmp_path, monkeypatch, capsys):
    """§4/§5.6: the delivery was retained behind a live owner — and the receipt says by whom."""
    entry = {"session_id": "20261013_223229_f78ac5", "surface": "desktop", "pid": 4321,
             "started_at": time.time() - 252, "lease_id": "l"}
    rc, payload = _run_refused_turn(tmp_path, monkeypatch, capsys, entry)

    assert rc == 0, "a live-owner hold is a durable hand-off, never a failure"
    assert payload["result"] == "receipt" and payload["status"] == "queued"
    assert payload["status_detail"] == delivery_queue.STATUS_DETAIL_LIVE_OWNER
    assert payload["busy"] is True and payload["attempts"] == 0
    assert list(payload) == list(delivery_queue.ENVELOPE_FIELDS)
    holder = payload["holder"]
    assert holder["kind"] == "live_owner"
    assert holder["surface"] == "desktop"
    assert holder["session_id"] == "20261013_223229_f78ac5"
    assert holder["pid"] == 4321 and holder["alive"] is True
    assert holder["age_seconds"] >= 252
    assert "desktop" in payload["detail"], payload["detail"]
    assert "(session 20261013_223229_f78ac5)" in payload["detail"]


def test_live_owner_receipt_is_unchanged_when_the_owner_cannot_be_named(tmp_path, monkeypatch,
                                                                      capsys):
    """Best-effort by contract: an unreadable owner stays UNKNOWN and the receipt is not lost."""
    rc, payload = _run_refused_turn(tmp_path, monkeypatch, capsys, None)

    assert rc == 0
    assert payload["status_detail"] == delivery_queue.STATUS_DETAIL_LIVE_OWNER
    assert payload["holder"] is None
    assert payload["detail"] == delivery_queue.DETAIL_HELD_LIVE
