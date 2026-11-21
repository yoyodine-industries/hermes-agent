"""A notice may only describe a state the card held at ONE instant.

Replay of t_1fc61a62 on the ``financially`` board: a card that crashed, was handed
back for changes on PR #4164, was reworked onto PR #4168, crashed again, and was
then APPROVED and closed. A claimed batch spans every event since the last
successful delivery, so all of those kinds can land in one wake. Composing the
union produced:

    [kanban] Task t_1fc61a62 crashed (worker exited); dispatcher will retry,
    handed off for review; the implementation is done, review requested changes
    (BLOCK); implementation is not approved.
    Result:  Round-1 rework complete ... #4168 ...
    Review feedback: Round 1 on tip 4610dc8d0 (PR ... #4164) ...

— a sentence describing no state the card ever held, pairing a Result from the
new PR with review feedback about the superseded one, on a card that was `done`
and approved eleven minutes after the last crash.
"""

import asyncio

from gateway.config import Platform
from gateway.run import GatewayRunner
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_notify as kbn

# The card's own words, from its run history (2026-11-20).
CHANGES_REASON_4164 = (
    "Round 1 on tip 4610dc8d0 (PR yoyodine-industries/yaan-platform#4164). "
    "Behaviour verifies (DoD fixture green) but the door bypasses the pin."
)
REWORK_SUMMARY_4168 = (
    "Round-1 rework complete and pushed as a NEW branch/PR: "
    "yoyodine-industries/yaan-platform#4168 (head 9f7f05a1, MERGEABLE)."
)
APPROVAL_SUMMARY = (
    "APPROVED round 2 on tip 9f7f05a1 (PR #4168): all five round-1 findings "
    "fixed and proven two-sided with my own probes."
)


class RecordingAdapter:
    def __init__(self):
        self.sent = []
        self.handled = []

    async def send(self, chat_id, text, metadata=None):
        self.sent.append({"chat_id": chat_id, "text": text, "metadata": metadata or {}})

    async def handle_message(self, event):
        self.handled.append(event)
        event._gateway_accepted = True


async def _run_one_notifier_tick(monkeypatch, runner):
    real_sleep = asyncio.sleep

    async def fake_sleep(delay):
        if delay == 5:
            return
        runner._running = False
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    await runner._kanban_notifier_watcher(interval=1)


def _make_runner(adapter):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._kanban_sub_fail_counts = {}
    runner._kanban_dispatcher_lock_handle = object()
    return runner


def _wake_texts(adapter):
    return [getattr(ev, "text", "") or "" for ev in adapter.handled]


def _replay_card(*, complete: bool, through: str = "crash"):
    """Create the card and replay its runs, in their recorded order.

    ``through`` stops the replay at the state the notice is claimed at:
    ``"block"`` (12:27, changes requested), ``"rework"`` (12:47, rework handed
    back) or ``"crash"`` (13:19, crashed again). ``complete`` adds the 13:30
    approval that ended the card.
    """
    conn = kbc.connect()
    try:
        tid = kb.create_task(
            conn, title="kanban status notice synthesises three runs",
            assignee="platform-stl",
            session_id="agent:main:telegram:dm:chat-1",
        )
        kbn.add_notify_sub(
            conn, task_id=tid, platform="telegram", chat_id="chat-1",
            chat_type="dm", delivery_mode="notify+wake",
        )
        # 4888 crashed / 4890 changes_requested / 4894 review_requested / 4897 crashed
        kb._append_event(conn, tid, "crashed", {"pid": 85231, "exit_code": 1})
        kb._append_event(conn, tid, "changes_requested", {
            "reason": CHANGES_REASON_4164, "reviewer": "platform-stl",
            "implementer": "platform-coder", "status": "ready",
        })
        # ... and the row the reviewer's BLOCK left behind: back with the implementer.
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
        conn.commit()
        if through == "block":
            return tid
        # A real review handoff, so the row reads as the event says: ``review``.
        assert kb.request_review(conn, tid, summary=REWORK_SUMMARY_4168) is True
        if through == "rework":
            return tid
        # The crash after the handoff hands the card back for a retry.
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
        conn.commit()
        kb._append_event(conn, tid, "crashed", {"pid": 95921, "exit_code": 1})
        if complete:
            # 4899 completed/approved — the run that ended the card.
            done = kb.complete_task(conn, tid, summary=APPROVAL_SUMMARY)
            assert done is True, f"fixture could not complete the card: {done!r}"
        return tid
    finally:
        conn.close()


def test_done_card_is_reported_done_and_never_carries_a_superseded_crash(tmp_path, monkeypatch):
    """The replay of t_1fc61a62 must read as the approval, not as the union."""
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "replay-done.db"))
    kb.init_db()
    tid = _replay_card(complete=True)

    adapter = RecordingAdapter()
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter)))

    wakes = _wake_texts(adapter)
    assert len(wakes) == 1, f"a terminal card is ONE notice, got {wakes}"
    wake = wakes[0]

    # Terminal wins and is said first: this is a completion notice, naming the run
    # that ended the card.
    assert "done" in wake
    assert APPROVAL_SUMMARY in wake
    assert "#4168" in wake
    # ... and it carries none of the states the batch also held.
    assert "crashed" not in wake, wake
    assert "changes requested" not in wake, wake
    assert "not approved" not in wake, wake
    assert "Review feedback" not in wake, wake
    assert "#4164" not in wake, wake
    assert tid in wake


def test_batch_spanning_runs_describes_only_the_newest_state(tmp_path, monkeypatch):
    """With the card still live, the notice reports the newest transition alone.

    At 13:19 the card was crashed; the 12:47 handoff and the 12:27 BLOCK were
    history. Unioning them is what produced a sentence no instant ever held.
    """
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "replay-live.db"))
    kb.init_db()
    _replay_card(complete=False)

    adapter = RecordingAdapter()
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter)))

    wakes = _wake_texts(adapter)
    assert wakes, "the crash still wakes the origin"
    crash_wake = next((w for w in wakes if "crashed" in w), None)
    assert crash_wake is not None, wakes
    assert "review requested changes" not in crash_wake, crash_wake
    assert "not approved" not in crash_wake, crash_wake
    assert "handed off for review" not in crash_wake, crash_wake


def test_result_is_never_paired_with_review_feedback_from_another_run(tmp_path, monkeypatch):
    """Contract 3: the #4168 Result must not ride beside #4164's feedback.

    The BLOCK's own ping still carries its reason — that describes the instant it
    happened — but the synthetic wake must not compose the two.
    """
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "replay-pairing.db"))
    kb.init_db()
    _replay_card(complete=False, through="rework")

    adapter = RecordingAdapter()
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter)))

    wakes = _wake_texts(adapter)
    assert wakes
    for wake in wakes:
        assert not ("Result:" in wake and "Review feedback:" in wake), wake
        assert "#4164" not in wake, wake
    handoff_wake = next((w for w in wakes if "#4168" in w), None)
    assert handoff_wake is not None, wakes
    assert "Review feedback" not in handoff_wake, handoff_wake
    # The BLOCK's own ping is unchanged: it describes its own instant.
    assert any("#4164" in sent["text"] for sent in adapter.sent), adapter.sent


def test_the_terminal_notice_does_not_refire(tmp_path, monkeypatch):
    """Dedupe rides the delivered transition, not a field that keeps changing."""
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "replay-dedupe.db"))
    kb.init_db()
    _replay_card(complete=True)

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))
    first_wakes, first_pings = len(adapter.handled), len(adapter.sent)
    assert first_wakes == 1 and first_pings > 0

    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter)))
    assert len(adapter.handled) == first_wakes, "the completion notice must not re-fire"
    assert len(adapter.sent) == first_pings, "delivered pings must not re-send"


def _ready_card_with_events(tmp_path, monkeypatch, kinds):
    """A LIVE card (ready, no failures) whose batch still holds older transitions."""
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / f"replay-{'-'.join(kinds)}.db"))
    kb.init_db()
    conn = kbc.connect()
    try:
        tid = kb.create_task(
            conn, title="notice must quote the row, not a superseded run",
            assignee="platform-coder",
            session_id="agent:main:telegram:dm:chat-1",
        )
        kbn.add_notify_sub(
            conn, task_id=tid, platform="telegram", chat_id="chat-1",
            chat_type="dm", delivery_mode="notify+wake",
        )
        for kind in kinds:
            if kind == "crashed":
                kb._append_event(conn, tid, "crashed", {"pid": 95921, "exit_code": 1})
            elif kind == "gave_up":
                kb._append_event(conn, tid, "gave_up", {
                    "reason": "retries exhausted", "consecutive_failures": 3,
                })
        return tid
    finally:
        conn.close()


def test_gave_up_from_before_an_unblock_never_speaks_for_a_live_card(tmp_path, monkeypatch):
    """The t_f001ec48 receipt: cf 0, no run since the reset, yet "gave up" was claimed.

    The batch holds the pre-reset terminal words; the row holds the counters that
    contradict them. The notice must quote the row's state, never the stale event.
    """
    _ready_card_with_events(tmp_path, monkeypatch, ["gave_up", "crashed"])

    adapter = RecordingAdapter()
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter)))

    wakes = _wake_texts(adapter)
    assert wakes, "the crash still wakes the origin"
    joined = "\n".join(wakes)
    assert "gave up" not in joined, joined
    assert "retries exhausted" not in joined, joined
    # The newest corroborated transition is the crash, and it may say so.
    assert any("crashed" in w for w in wakes), wakes


def test_a_wholly_superseded_batch_speaks_the_snapshot(tmp_path, monkeypatch):
    """No event in the batch survives the row: say what the card IS (ready)."""
    _ready_card_with_events(tmp_path, monkeypatch, ["gave_up"])

    adapter = RecordingAdapter()
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter)))

    wakes = _wake_texts(adapter)
    assert wakes, "a superseded batch must not go silent"
    joined = "\n".join(wakes)
    assert "gave up" not in joined and "retries exhausted" not in joined, joined
    assert "ready" in joined, joined


def test_a_done_card_names_its_approval_even_outside_the_batch(tmp_path, monkeypatch):
    """The delayed-notice case: the completion was already delivered, a later crash
    record is claimed on its own. The row says done, so the notice says done and
    names the approval the row's ledger holds — it does not read the crash as state.
    """
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "replay-late.db"))
    kb.init_db()
    conn = kbc.connect()
    try:
        tid = kb.create_task(
            conn, title="late crash record after an approval", assignee="platform-stl",
            session_id="agent:main:telegram:dm:chat-1",
        )
        kbn.add_notify_sub(
            conn, task_id=tid, platform="telegram", chat_id="chat-1",
            chat_type="dm", delivery_mode="notify+wake",
        )
        assert kb.complete_task(conn, tid, summary=APPROVAL_SUMMARY) is True
    finally:
        conn.close()

    first = RecordingAdapter()
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(first)))
    assert any(APPROVAL_SUMMARY in w for w in _wake_texts(first)), _wake_texts(first)

    # A crash record lands afterwards and is claimed in a batch of its own.
    conn = kbc.connect()
    try:
        kb._append_event(conn, tid, "crashed", {"pid": 95921, "exit_code": 1})
    finally:
        conn.close()
    late = RecordingAdapter()
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(late)))

    wakes = _wake_texts(late)
    assert wakes, "the late event must not go silent"
    for text in wakes:
        assert "done" in text, wakes
        assert "crashed" not in text, wakes


def test_an_archived_card_still_reports_how_it_ended(tmp_path, monkeypatch):
    """Archive is the last tick before the sub is dropped: the completion must land."""
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "replay-archived.db"))
    kb.init_db()
    conn = kbc.connect()
    try:
        tid = kb.create_task(
            conn, title="archived after approval", assignee="platform-stl",
            session_id="agent:main:telegram:dm:chat-1",
        )
        kbn.add_notify_sub(
            conn, task_id=tid, platform="telegram", chat_id="chat-1",
            chat_type="dm", delivery_mode="notify+wake",
        )
        kb._append_event(conn, tid, "crashed", {"pid": 95921, "exit_code": 1})
        assert kb.complete_task(conn, tid, summary=APPROVAL_SUMMARY) is True
        assert kb.archive_task(conn, tid, reason="generated-moot", actor="fixture") is True
    finally:
        conn.close()

    adapter = RecordingAdapter()
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter)))

    wakes = _wake_texts(adapter)
    assert len(wakes) == 1, f"one completion notice, got {wakes}"
    assert APPROVAL_SUMMARY in wakes[0], wakes
    assert "crashed" not in wakes[0], wakes

