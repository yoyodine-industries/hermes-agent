"""The set-seam contract verification (card t_b58332a4).

A card born with an ``OWNER/REPO`` (or exact-PR-URL) completion contract on a
repository that requires no status checks can never satisfy the acceptance gate:
the receipt stays ``classification=missing`` / ``required=[]`` and the card parks
``blocked``/``capability`` with its work already done.  These tests pin the fix at
the one seam every filing surface reaches (``kanban_db.create_task``):

* a repository WITH required checks keeps the declared contract;
* a repository WITHOUT falls back to ``local-only`` and records the measured
  reason on a ``contract_changed`` event;
* a repository whose CHECKS APIs are plan-gated is the card's "cannot determine"
  case: also a fallback, with that said in the reason;
* a repository that cannot be read AT ALL is left alone -- an access gap the
  acceptance gate already surfaces as a fixable ``auth`` -- so the CI gate is
  never silently stripped;
* ``landed`` -- the deploy-proof class, a card that carries a DEPLOYED unit -- is
  never routed through the resolver, so its deploy-proof gate is never touched.

No network: the provider transport (``kanban_pr_acceptance._api``) is faked, so
the real resolver and the real SQLite board are exercised.
"""
from __future__ import annotations

import importlib
import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_connect import connect


@pytest.fixture
def board(tmp_path, monkeypatch):
    """An isolated board: no live ``HERMES_KANBAN_DB`` pin, no delegated-child fence."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "home"))
    for name in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_DELEGATED_CHILD_CONTEXT"):
        monkeypatch.delenv(name, raising=False)
    kb.init_db()


# Every repository the fake provider knows, and the measured state it answers.
_REPOS = {
    # has one required check -> the declaration stands
    "acme/withchecks": {"checks": True},
    # readable, 0 rulesets, unprotected branch -> the yaan-platform shape
    "acme/nochecks": {"checks": False},
    # readable, but every checks API is gated (the card's "cannot determine")
    "acme/gated": {"checks": False, "metadata": "ok", "gated": True},
    # the repository itself is invisible to the login -> unverifiable
    "acme/invisible": {"unreadable": True},
    # THE REGRESSION CASE (card t_b58332a4): the REAL repo the stranded cards
    # declared -- readable, 0 rulesets, unprotected -> no required checks. Observed
    # live on ops cards t_b516e265 / t_0396c536 / t_dba735ff, each born on
    # yoyodine-industries/yaan-platform and parked blocked with its work done.
    "yoyodine-industries/yaan-platform": {"checks": False},
}


def _install_provider(monkeypatch, repos=None):
    """Fake the provider read and record every endpoint consulted."""
    accept = importlib.import_module("hermes_cli.kanban_pr_acceptance")
    known = dict(_REPOS if repos is None else repos)
    calls: list[str] = []

    def fake_api(endpoint, *, query=None, paginate=False, profile_home=None):
        calls.append(endpoint)
        for repo, spec in known.items():
            if spec.get("unreadable"):
                if endpoint.startswith(f"repos/{repo}"):
                    raise accept._GateAuthError(f"HTTP 403 on repos/{repo}")
                continue
            if endpoint == f"repos/{repo}":
                return {"default_branch": "main"}
            if endpoint.startswith(f"repos/{repo}/rules/branches/"):
                if spec.get("gated"):
                    raise accept._GateAuthError("HTTP 403 on the rules API")
                return [[]]
            if endpoint.startswith(f"repos/{repo}/branches/") and endpoint.endswith("/protection"):
                if spec.get("gated"):
                    raise accept._GateAuthError("HTTP 403 on the branch protection API")
                if spec.get("checks"):
                    return {"required_status_checks": {"contexts": ["ci"], "checks": []}}
                raise accept._GateAuthError("HTTP 404 on the branch protection API")
        raise AssertionError(f"unexpected endpoint {endpoint}")

    monkeypatch.setattr(accept, "_api", fake_api)
    assert accept._api is fake_api
    return calls


def _events(conn, tid: str, kind: str) -> list[dict]:
    return [json.loads(row[0]) for row in conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = ?", (tid, kind))]


# --- branch A: the repository requires checks -> the declaration stands ---------

def test_repo_with_required_checks_keeps_the_pr_contract(board, monkeypatch):
    calls = _install_provider(monkeypatch)
    with connect() as conn:
        tid = kb.create_task(conn, title="publish", completion_contract="acme/withchecks")
        assert kb.get_task(conn, tid).completion_contract == "acme/withchecks"
        assert _events(conn, tid, "contract_changed") == []
    # the provider WAS read (proves the seam runs), not inferred from the card
    assert any(call.endswith("protection") for call in calls)


# --- branch B: the repository requires none -> local-only + measured reason -----

def test_repo_without_required_checks_falls_back_to_local_only(board, monkeypatch):
    _install_provider(monkeypatch)
    with connect() as conn:
        tid = kb.create_task(conn, title="publish", completion_contract="acme/nochecks")
        assert kb.get_task(conn, tid).completion_contract == "local-only"
        events = _events(conn, tid, "contract_changed")
    assert len(events) == 1
    payload = events[0]
    assert payload["old"] == "acme/nochecks"
    assert payload["new"] == "local-only"
    assert "required_status_checks=null" in payload["reason"]
    assert "acme/nochecks" in payload["reason"]


def test_gated_checks_api_is_treated_as_no_checks_and_says_so(board, monkeypatch):
    _install_provider(monkeypatch)
    with connect() as conn:
        tid = kb.create_task(conn, title="publish", completion_contract="acme/gated")
        assert kb.get_task(conn, tid).completion_contract == "local-only"
        reason = _events(conn, tid, "contract_changed")[0]["reason"]
    assert "could not determine" in reason
    assert "HTTP 403" in reason


def test_unreadable_repo_is_left_alone(board, monkeypatch):
    """An access gap is the acceptance gate's to surface (``auth``), not ours to strip."""
    _install_provider(monkeypatch)
    with connect() as conn:
        tid = kb.create_task(conn, title="publish", completion_contract="acme/invisible")
        assert kb.get_task(conn, tid).completion_contract == "acme/invisible"
        assert _events(conn, tid, "contract_changed") == []


def test_ruleset_required_checks_keep_the_contract(board, monkeypatch):
    """A required_status_checks RULESET alone keeps the declaration, with classic
    branch protection absent (404 means 'unprotected', not 'unknown')."""
    accept = importlib.import_module("hermes_cli.kanban_pr_acceptance")

    def fake_api(endpoint, *, query=None, paginate=False, profile_home=None):
        if endpoint == "repos/acme/rules":
            return {"default_branch": "main"}
        if endpoint.startswith("repos/acme/rules/rules/branches/"):
            return [[{"type": "required_status_checks",
                      "parameters": {"required_status_checks": [{"context": "ci"}]}}]]
        if endpoint.endswith("/protection"):
            raise accept._GateAuthError("HTTP 404 on the branch protection API")
        raise AssertionError(endpoint)

    monkeypatch.setattr(accept, "_api", fake_api)
    with connect() as conn:
        tid = kb.create_task(conn, title="publish", completion_contract="acme/rules")
        assert kb.get_task(conn, tid).completion_contract == "acme/rules"
        assert _events(conn, tid, "contract_changed") == []


# --- the deployed-unit class is never routed through here -----------------------

def test_landed_contract_is_never_routed_through_the_resolver(board, monkeypatch):
    """A card carrying a DEPLOYED unit declares ``landed``; its gate is untouched."""
    calls = _install_provider(monkeypatch)
    with connect() as conn:
        tid = kb.create_task(conn, title="ship", completion_contract="landed")
        assert kb.get_task(conn, tid).completion_contract == "landed"
        assert _events(conn, tid, "contract_changed") == []
    assert calls == []  # never even read the provider


def test_pr_url_contract_is_resolved_too(board, monkeypatch):
    _install_provider(monkeypatch)
    url = "https://github.com/acme/nochecks/pull/12"
    with connect() as conn:
        tid = kb.create_task(conn, title="publish", completion_contract=url)
        assert kb.get_task(conn, tid).completion_contract == "local-only"
        assert _events(conn, tid, "contract_changed")[0]["old"] == url


# --- the measured regression case (ops cards t_b516e265 / t_0396c536 / t_dba735ff) --

def test_regression_case_the_three_yaan_platform_cards_fall_back(board, monkeypatch):
    """The three measured instances: ops cards t_b516e265 / t_0396c536 / t_dba735ff.

    Each was born on ``yoyodine-industries/yaan-platform`` (readable, 0 rulesets,
    unprotected branch) and could never clear its OWNER/REPO contract, so it parked
    blocked with its work already done. The set-seam fallback is what stops the next
    card repeating it.
    """
    _install_provider(monkeypatch)
    with connect() as conn:
        tid = kb.create_task(conn, title="publish",
                             completion_contract="yoyodine-industries/yaan-platform")
        assert kb.get_task(conn, tid).completion_contract == "local-only"
        payload = _events(conn, tid, "contract_changed")[0]
    assert payload["old"] == "yoyodine-industries/yaan-platform"
    assert "required_status_checks=null" in payload["reason"]


def test_missing_resolver_module_keeps_the_declared_contract(board, monkeypatch, caplog):
    """Review item B: a missing/stale resolver module must never fail a filing.

    The declared contract stands, with a warning, rather than every OWNER/REPO card
    raising ``ModuleNotFoundError`` -- which would be worse than the park this seam
    prevents.
    """
    import builtins
    real_import = builtins.__import__

    def flaky(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "hermes_cli" and fromlist and "kanban_contract_resolve" in fromlist:
            raise ImportError("simulated missing resolver module")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", flaky)
    with caplog.at_level("WARNING"):
        with connect() as conn:
            tid = kb.create_task(conn, title="publish", completion_contract="acme/nochecks")
            assert kb.get_task(conn, tid).completion_contract == "acme/nochecks"
            assert _events(conn, tid, "contract_changed") == []
    assert any("contract resolution unavailable" in r.getMessage() for r in caplog.records)


# --- the resolver in isolation --------------------------------------------------

def test_resolver_is_inert_for_non_repo_contracts(monkeypatch):
    from hermes_cli import kanban_contract_resolve as resolve

    calls = _install_provider(monkeypatch)
    for contract in ("local-only", "landed"):
        out = resolve.resolve_contract_at_set(contract)
        assert out.changed is False and out.contract == contract
    assert calls == []


def test_repository_of_parses_repo_and_pr_url():
    from hermes_cli import kanban_contract_resolve as resolve

    assert resolve.repository_of("acme/repo") == "acme/repo"
    assert resolve.repository_of("https://github.com/acme/repo/pull/7") == "acme/repo"
    assert resolve.repository_of("local-only") is None
    assert resolve.repository_of(None) is None
