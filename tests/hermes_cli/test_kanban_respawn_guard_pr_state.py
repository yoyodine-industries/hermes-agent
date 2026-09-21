"""Respawn guard: a PR-URL comment holds a card only while that PR is OPEN.

The guard used to read *any* GitHub PR URL in a comment younger than 24h as
"work already proposed" and park the card. But a URL proves a worker once
OPENED a PR, not that the PR is still pending: a MERGED or CLOSED PR is
finished work, and holding the card on it kept a lane's card off the board for
the whole window (a merged PR hid a lane's only card for a day).

The guard's one network hop — ``gh pr view <url> --json state`` — is pinned by
the shared ``gh_pr_state`` fixture (tests/hermes_cli/conftest.py). The real
guard, the real argv and the real JSON parse all still run: these tests assert
the production verdict, its cache, and its failure mode.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture
def spawnable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Synthetic assignees map to real profiles, and the tick may spawn."""
    import hermes_cli.config as cfgmod
    import hermes_cli.profiles as profmod

    monkeypatch.setattr(profmod, "profile_exists", lambda name: True)
    monkeypatch.setattr(
        cfgmod, "load_config", lambda *a, **k: {"kanban": {"review_dispatch": True}}
    )


MERGED_URL = "https://github.com/yoyodine-industries/financially/pull/25"
OPEN_URL = "https://github.com/yoyodine-industries/yaan-hermes-skills/pull/106"
GONE_URL = "https://github.com/yoyodine-industries/financially/pull/16"


def test_guard_defers_a_card_only_while_its_pr_is_open(
    kanban_home: Path, spawnable, gh_pr_state
) -> None:
    """MERGED releases the card, OPEN still defers it — and the tick agrees.

    Re-spawning under an OPEN PR risks a duplicate PR, which is what the guard
    is for. Re-spawning under a MERGED PR risks nothing: that work is done.
    """
    gh_pr_state.answer(MERGED_URL, "MERGED")
    gh_pr_state.answer(OPEN_URL, "OPEN")

    with kbc.connect() as conn:
        merged_id = kb.create_task(conn, title="merged PR", assignee="worker")
        kb.add_comment(
            conn, merged_id, author="worker", body=f"DONE — landed as {MERGED_URL}"
        )
        open_id = kb.create_task(conn, title="open PR", assignee="worker")
        kb.add_comment(conn, open_id, author="worker", body=f"DELIVERED — {OPEN_URL}")

        assert kbd.check_respawn_guard(conn, merged_id) is None
        assert kbd.check_respawn_guard(conn, open_id) == "active_pr"

        res = kbd.dispatch_once(conn, dry_run=True)
        spawned = [row[0] for row in res.spawned]
        assert merged_id in spawned
        assert open_id not in spawned
        assert dict(res.respawn_guarded).get(open_id) == "active_pr"


def test_a_pr_the_forge_cannot_resolve_is_not_an_open_pr(
    kanban_home: Path, gh_pr_state
) -> None:
    """The forge answering "no such PR" is an ANSWER: the card is not held.

    This is the shape a purge/recreate leaves behind — the comment still quotes
    a URL that no longer exists. gh exits non-zero with the GraphQL 404 on
    stderr, and the guard must read that as "not open", not as a failure.
    """
    with kbc.connect() as conn:
        gone_id = kb.create_task(conn, title="purged PR", assignee="worker")
        kb.add_comment(conn, gone_id, author="worker", body=f"PR: {GONE_URL}")

        assert kbd.check_respawn_guard(conn, gone_id) is None


def test_pr_state_is_looked_up_once_per_url_and_re_asked_after_the_ttl(
    kanban_home: Path, gh_pr_state
) -> None:
    """One ``gh pr view`` per URL per TTL — not one per card per tick.

    The tick calls the guard for every ready row, and a fan-out of cards can
    quote the same PR: without the cache that is a network call apiece. The
    entry expires, so a PR that merges while the card waits is still noticed.
    """
    gh_pr_state.answer(OPEN_URL, "OPEN")
    url = OPEN_URL

    with kbc.connect() as conn:
        first = kb.create_task(conn, title="quotes the PR", assignee="worker")
        second = kb.create_task(conn, title="quotes it again", assignee="other")
        for tid in (first, second):
            kb.add_comment(conn, tid, author="worker", body=f"PR: {url}")

        assert kbd.check_respawn_guard(conn, first) == "active_pr"
        assert kbd.check_respawn_guard(conn, second) == "active_pr"
        assert gh_pr_state.calls == [["gh", "pr", "view", url, "--json", "state"]]

        # Expire the entry and flip the answer: the guard must ask again and see
        # the new state, which is how a PR that merges under a held card gets
        # the card moving without waiting out the 24h window.
        kbd._respawn_guard_pr_states[url] = (0.0, "OPEN")
        gh_pr_state.answer(url, "MERGED")
        assert kbd.check_respawn_guard(conn, second) is None
        assert len(gh_pr_state.calls) == 2


def test_unreadable_pr_state_holds_the_card_and_logs_it_once(
    kanban_home: Path, gh_pr_state, caplog: pytest.LogCaptureFixture
) -> None:
    """FAILURE MODE: CLOSED — an unknowable state re-parks on the text-only rule.

    Holding costs a delayed turn; failing open costs a duplicate PR. The
    fallback is therefore deliberate, and deliberately loud: a lane parked on a
    state nobody could read must say so (once per card, not once per tick).
    """
    gh_pr_state.go_offline()

    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="gh is down", assignee="worker")
        kb.add_comment(conn, tid, author="worker", body=f"PR: {OPEN_URL}")

        with caplog.at_level(logging.WARNING, logger="hermes_cli.kanban_db"):
            assert kbd.check_respawn_guard(conn, tid) == "active_pr"
            assert kbd.check_respawn_guard(conn, tid) == "active_pr"

    holds = [r for r in caplog.records if "could not be read" in r.getMessage()]
    assert len(holds) == 1
    assert tid in holds[0].getMessage()
    assert OPEN_URL in holds[0].getMessage()
