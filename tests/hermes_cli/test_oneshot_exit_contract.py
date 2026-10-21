"""``hermes -z`` exit-code contract: the outcome is judged from the turn result, not from
whether any text was printed (#111770 — an incomplete or failed run that left an explanation on
stdout used to exit 0, so scripts treated a half-done job as success)."""

from unittest import mock

from hermes_cli import oneshot


def _run(monkeypatch, tmp_path, response, result):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    usage = tmp_path / "usage.json"
    with mock.patch.object(oneshot, "_run_agent", return_value=(response, dict(result))):
        code = oneshot.run_oneshot("q", usage_file=str(usage))
    return code, usage


def test_incomplete_run_with_text_exits_nonzero_and_reports_why(monkeypatch, tmp_path):
    import json

    partial = {"final_response": "Got as far as step 2.", "completed": False, "partial": True,
               "turn_exit_reason": "iteration_limit"}
    code, usage = _run(monkeypatch, tmp_path, "Got as far as step 2.", partial)
    assert code == 2
    report = json.loads(usage.read_text(encoding="utf-8"))
    assert report["completed"] is False and report["partial"] is True
    assert report["turn_exit_reason"] == "iteration_limit"

    interrupted = {"final_response": "Stopping.", "completed": False, "interrupted": True}
    code, usage = _run(monkeypatch, tmp_path, "Stopping.", interrupted)
    assert code == 130
    assert json.loads(usage.read_text(encoding="utf-8"))["interrupted"] is True

    failed = {"final_response": "Provider returned 401.", "completed": False, "failed": True}
    code, _ = _run(monkeypatch, tmp_path, "Provider returned 401.", failed)
    assert code == 2


def test_completed_run_still_exits_zero(monkeypatch, tmp_path):
    code, _ = _run(monkeypatch, tmp_path, "Paris.", {"final_response": "Paris.", "completed": True, "failed": False})
    assert code == 0


def test_advisory_empty_response_exits_nonzero_headless(monkeypatch, tmp_path):
    """The loop keeps ``empty_response_exhausted`` advisory so an interactive session is not
    told "failed" — but ``-z`` has no human to notice, so a pipeline must see exit 2 rather
    than accept a run whose answer is a notice."""
    import json

    advisory = {
        "final_response": "(empty)", "completed": True, "failed": False,
        "turn_exit_reason": "empty_response_exhausted", "failure_reason": "empty_response",
    }
    code, usage = _run(monkeypatch, tmp_path, "(empty)", advisory)
    assert code == 2
    report = json.loads(usage.read_text(encoding="utf-8"))
    assert report["failed"] is True
    assert report["failure"] == "empty_response"


def test_usage_report_names_the_failure_class_of_a_failed_turn(monkeypatch, tmp_path):
    """A failed turn's report says WHY, not just ``failed: true``."""
    import json

    failed = {
        "final_response": "no answer was produced", "completed": False, "failed": True,
        "failure_reason": "empty_response",
    }
    code, usage = _run(monkeypatch, tmp_path, "no answer was produced", failed)
    assert code == 2
    report = json.loads(usage.read_text(encoding="utf-8"))
    assert report["failure"] == "empty_response"


def test_completed_run_reports_no_failure_class(monkeypatch, tmp_path):
    import json

    code, usage = _run(
        monkeypatch, tmp_path, "Paris.",
        {"final_response": "Paris.", "completed": True, "failed": False, "failure_reason": None},
    )
    assert code == 0
    report = json.loads(usage.read_text(encoding="utf-8"))
    assert report["failed"] is False
    assert "failure" not in report
