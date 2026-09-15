"""Imported turns retain their receipt and cannot bypass the local FIFO."""
import threading
from types import SimpleNamespace

from tui_gateway.method_ctx import rebind
from tui_gateway import session_notifications, session_auto_continue
from tui_gateway.turn_marker import record_turn_start, read_turn_marker


def test_refused_input_commits_failed_mailbox_receipt(tmp_path):
    import contextlib
    import contextvars
    import logging
    import time
    from tui_gateway import prompt_turn
    from tools import bot_live_delivery as mailbox

    owner = dict(profile_home=str(tmp_path.resolve()), session_id="chat",
                 lease_id="lease", live_session_id="live")
    queued = mailbox.deliver_to_live_owner(tmp_path, owner, "refused input")
    mailbox.claim_pending_delivery(tmp_path, owner)
    agent = SimpleNamespace(session_id="chat")
    session = dict(agent=agent, session_key="chat", history_lock=threading.RLock(), running=True)
    retired = []
    noop = lambda *args, **kwargs: None
    submit = rebind(prompt_turn._run_prompt_submit, {
        "threading": threading, "time": time, "logger": logging.getLogger(__name__),
        "_sessions_lock": threading.RLock(), "_sessions": {},
        "_admit_prompt_turn": lambda *args: ([], agent),
        "_emit": noop, "bind_transport": noop, "reset_transport": noop,
        "_current_runtime_session_record": contextvars.ContextVar("refused_turn"),
        "_TurnRun": prompt_turn._TurnRun,
        "_record_turn_marker": lambda *args, **kwargs: "marker",
        "_prepare_turn_input": lambda *args: None,
        "_finish_turn": noop, "_clear_inflight_turn": noop,
        "_retire_turn_marker": lambda *args: retired.append(args),
        "_emit_settled_session_info": noop,
        "_routing_provenance_db": lambda _session: contextlib.nullcontext(None),
        "_reopen_routed_session_row": noop,
    })
    def terminal(outcome):
        mailbox.complete_delivery(tmp_path, queued["id"], status=outcome["status"],
                                  error=outcome.get("error", ""))
    assert submit(None, "live", session, "refused input", terminal_callback=terminal)
    session["_run_thread"].join(timeout=5)
    assert not session["_run_thread"].is_alive()
    assert mailbox.read_delivery_result(tmp_path, queued["id"])["status"] == "failed"
    assert retired and session["running"] is False


def _dead_owner_lease(registry_home, session_id, live_session_id):
    """A lease left behind by a process that exited — the shape of a stranded record's owner."""
    import json
    import subprocess
    import sys
    from pathlib import Path

    script = (
        "import json,sys;"
        "from hermes_cli.active_sessions import try_acquire_active_session as acquire;"
        "lease,refusal=acquire(session_id=sys.argv[1],surface='desktop',config={},registry_home=sys.argv[2],"
        "metadata={'live_session_id':sys.argv[3],'bot_live_delivery_consumer':True});"
        "print(json.dumps({'lease_id':lease.lease_id if lease else None,"
        "'refusal':(str(refusal) if refusal else None)}))"
    )
    child = subprocess.run(
        [sys.executable, "-c", script, session_id, str(registry_home), live_session_id],
        cwd=Path(__file__).resolve().parents[2], capture_output=True, text=True, timeout=60,
    )
    assert child.returncode == 0, child.stderr
    payload = json.loads(child.stdout.strip().splitlines()[-1])
    assert payload["refusal"] is None, payload["refusal"]
    assert payload["lease_id"]
    return payload["lease_id"]


def test_the_real_poll_path_drains_a_record_left_by_a_dead_owner(tmp_path):
    from hermes_state import SessionDB
    from hermes_cli.active_sessions import try_acquire_active_session
    from tools import bot_live_delivery as mailbox

    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session(session_id="chat", source="cli")
    db.set_session_title("chat", "Bot Chat")
    try:
        dead_lease = _dead_owner_lease(tmp_path, "chat", "stale-live")
        stranded = mailbox.deliver_to_live_owner(
            tmp_path, {"profile_home": str(tmp_path.resolve()), "session_id": "chat",
                       "lease_id": dead_lease, "live_session_id": "stale-live"},
            "bot dm that never arrived")
        assert stranded["status"] == "queued"
        lease, refusal = try_acquire_active_session(
            session_id="chat", surface="tui", config={}, registry_home=tmp_path,
            metadata={"live_session_id": "live-now", "bot_live_delivery_consumer": True})
        assert refusal is None and lease is not None
        submitted = []

        def submit(delivery_id, sid, session, text, **kwargs):
            submitted.append((delivery_id, text))
            kwargs["terminal_callback"]({"status": "settled", "text": "reply"})
            return True

        poll = rebind(session_notifications._poll_bot_live_delivery_once, {
            "_session_home": lambda session: tmp_path,
            "_run_prompt_submit": submit,
            "_notif_release_turn": lambda session: session.update(running=False),
        })
        session = {"history_lock": threading.RLock(), "agent": object(), "session_key": "chat",
                   "active_session_lease": SimpleNamespace(lease_id=lease.lease_id, released=False)}
        assert poll("live-now", session) is True
        assert submitted == [(f"__bot_dm__{stranded['id']}", "bot dm that never arrived")]
        receipt = mailbox.read_delivery_result(tmp_path, stranded["id"])
        assert receipt is not None and receipt["status"] == "settled"
    finally:
        db.close()


def test_imported_crash_marker_never_autocontinues(tmp_path):
    record_turn_start(tmp_path, "chat", "imported", auto_continue=False)
    marker = read_turn_marker(tmp_path, "chat")
    assert marker["auto_continue"] is False
    schedule = rebind(session_auto_continue._maybe_schedule_auto_continue, {
        "_session_home": lambda session: tmp_path,
        "read_turn_marker": read_turn_marker,
    })
    assert schedule("live", {}, "chat") is None


def test_local_work_blocks_mailbox_claim_without_consuming_envelope(monkeypatch, tmp_path):
    import tools.bot_live_delivery as mailbox
    owner = {"lease_id": "lease", "live_session_id": "live", "session_id": "chat"}
    author = {"id": "bot:coder", "name": "coder", "is_bot": True}
    pending = [{"id": "receipt", "message": "imported", "author": author}]
    monkeypatch.setattr(mailbox, "find_canonical_live_owner", lambda home: owner)
    monkeypatch.setattr(mailbox, "claim_pending_delivery", lambda home, pinned: pending.pop(0))
    receipts = []
    monkeypatch.setattr(mailbox, "complete_delivery", lambda *args, **kwargs: receipts.append((args, kwargs)))
    submitted = []
    def submit(rid, sid, session, text, **kwargs):
        submitted.append((text, kwargs.get("turn_author")))
        kwargs["terminal_callback"]({"status": "settled", "text": "reply"})
        return True
    poll = rebind(session_notifications._poll_bot_live_delivery_once, {
        "_session_home": lambda session: tmp_path,
        "_run_prompt_submit": submit,
        "_notif_release_turn": lambda session: session.update(running=False),
    })
    session = {"history_lock": threading.RLock(), "agent": object(), "session_key": "chat",
               "active_session_lease": SimpleNamespace(lease_id="lease", released=False)}
    for blocker in ("running", "queued_prompt", "queued_prompts", "_auto_continue_scheduled"):
        session[blocker] = True
        assert poll("live", session) is False
        assert pending and not submitted
        session.pop(blocker)
    assert poll("other-live", session) is False
    assert pending
    assert poll("live", session) is True
    assert submitted == [("imported", author)] and not pending
    assert receipts[0][0][1] == "receipt"
    assert receipts[0][1]["reply"] == "reply"
