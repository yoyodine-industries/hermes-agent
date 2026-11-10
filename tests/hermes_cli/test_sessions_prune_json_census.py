"""``hermes sessions prune --dry-run --json`` is the census carriers read (card t_0ee10a96).

The dashboard's ``hermes_sessions`` class shells out to this command instead of re-typing the
prune predicate in its own SQL; a mirror drifted once and planned 260 sessions the runtime
selected 0 of. So the command must answer with the runtime's own selection, in JSON, on
stdout alone — and must never fall back to the human preview.
"""

import json
import sys

import pytest

from hermes_cli.session_filters import build_prune_filters


def _run(monkeypatch, argv_tail, census=None, seen=None):
    import hermes_cli.main as main_mod
    import hermes_state

    seen = seen if seen is not None else {}
    census = census if census is not None else {
        "cutoff": 1_700_000_000.0,
        "selected_sessions": 3,
        "selected_messages": 42,
        "selected_ids": ["a", "b", "c"],
        "past_window_sessions": 9,
        "past_window_messages": 900,
    }

    class FakeDB:
        def retention_window_census(self, **kwargs):
            seen["census_kwargs"] = kwargs
            return dict(census)

        def list_prune_candidates(self, **kwargs):  # must NOT be reached
            seen["candidates_called"] = True
            return []

        def count_open_prune_matches(self, **kwargs):
            seen["candidates_called"] = True
            return 0

        def count_prune_matches(self, **kwargs):
            return 0

        def close(self):
            pass

    monkeypatch.setattr(hermes_state, "SessionDB", lambda *args, **kwargs: FakeDB())
    monkeypatch.setattr(sys, "argv", ["hermes", "sessions", "prune", *argv_tail])
    monkeypatch.setattr("builtins.input", lambda _prompt="": "y")
    main_mod.main()
    return seen


def test_prune_dry_run_json_emits_the_runtime_census_only(monkeypatch, capsys):
    seen = _run(monkeypatch, ["--dry-run", "--json", "--older-than", "7d"])

    out = capsys.readouterr().out
    payload = json.loads(out)          # stdout is JSON and NOTHING else
    assert payload["selected_sessions"] == 3
    assert payload["selected_messages"] == 42
    assert payload["selected_ids"] == ["a", "b", "c"]
    assert payload["past_window_sessions"] == 9
    assert payload["past_window_messages"] == 900
    assert payload["action"] == "prune"
    assert "session(s) match" not in out
    assert not seen.get("candidates_called"), "the census path must not re-derive the selection"


def test_prune_json_asks_with_the_prune_filters_not_archive_ones(monkeypatch, capsys):
    """The census must be asked with the SAME filters the apply leg would use."""
    seen = _run(monkeypatch, ["--dry-run", "--json", "--older-than", "7d"])
    kwargs = seen["census_kwargs"]
    assert kwargs["archived"] is False        # "not archived" == not HUMAN-archived
    assert kwargs["include_auto_archived"] is True   # runtime bookkeeping is eligible
    assert kwargs["include_pinned"] is False
    assert kwargs["lineage_tips_only"] is False      # whole lineages, like the prune
    assert kwargs["whole_lineages"] is True
    assert kwargs["last_active_before"] is not None


def test_prune_json_without_dry_run_refuses_and_deletes_nothing(monkeypatch, capsys):
    # --force walks past the live-holder guard (which otherwise refuses first and is not what
    # this test is about), so the flag combination itself is what answers.
    with pytest.raises(SystemExit) as exc:
        _run(monkeypatch, ["--json", "--yes", "--force", "--older-than", "7d"])
    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert "--dry-run" in (captured.err + captured.out), f"out={captured.out!r} err={captured.err!r}"


def test_the_window_the_census_reports_is_the_one_the_prune_uses():
    """One definition of the window: the census' age arm is the prune's own cutoff."""
    import time

    class Args:
        older_than = "7d"

    filters = build_prune_filters(Args())
    cutoff = filters["last_active_before"]
    assert abs((time.time() - 7 * 86400) - cutoff) < 120
