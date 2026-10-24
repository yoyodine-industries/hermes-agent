"""The consent-surface WRITE guard: coverage, refusal, receipt, latch (design t_cb9259fc).

Contract under test (D2-D6 of the design of record):

* a write to a consent-gated surface needs an APPROVED row whose scope COVERS the
  hit's scope token -- broader covers narrower, never the other way round, and a
  legacy row with no declared scope is matched literally;
* the refusal is a yolo-proof deny PLUS one deduped receipt on the owning card and a
  card parked ``blocked``/``needs_input``;
* the per-session latch refuses the same surface on every later vector;
* the gate is INERT without an owning card, refuses surfaces (never ordinary work)
  when the store cannot be read, is disarmed only by the documented env switch, and
  NEVER raises into a tool call.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import pytest

from hermes_cli import consent_surface_guard as g
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@dataclass(frozen=True)
class Hit:
    """Stand-in for ``hermes_cli.consent_surface.SurfaceHit`` (pinned field names)."""

    surface: str
    scope_token: str
    path: Optional[str] = None
    reason: str = ""


CONFIG = Hit("hermes-config", "hermes-config:platform-stl", "/home/u/.hermes/profiles/platform-stl/config.yaml")
SOUL = Hit("hermes-soul", "hermes-soul:platform-stl", "/home/u/.hermes/profiles/platform-stl/SOUL.md")


@pytest.fixture(autouse=True)
def _clean_latch():
    g.reset_latch()
    yield
    g.reset_latch()


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv(g.DISARM_ENV, raising=False)
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    kb.init_db()
    return home


def _approvals(path: Path, rows) -> str:
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE approvals (id INTEGER PRIMARY KEY, project TEXT DEFAULT '', "
        "task TEXT DEFAULT '', context TEXT DEFAULT '', status TEXT, decision TEXT, "
        "reason TEXT, idempotency_key TEXT, card_id TEXT, board TEXT, superseded_by TEXT)"
    )
    con.executemany(
        "INSERT INTO approvals (id, status, decision, reason, task, context, idempotency_key) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)", rows,
    )
    con.commit()
    con.close()
    return str(path)


@pytest.fixture
def approvals(tmp_path):
    """One covering, one narrower, one pending, one legacy (no declared scope) row."""
    return _approvals(tmp_path / "approvals.db", [
        (7, "approved", "approved", "the estates config surface", "", "", "platform\x1fhermes-config"),
        (8, "approved", "approved", "one profile only", "", "", "platform\x1fhermes-config:platform-stl"),
        (9, "pending", "", "", "", "", "platform\x1fhermes-soul"),
        (10, "approved", "approved", "hermes-soul:platform-stl rewrite", "", "", None),
        (11, "approved", "approved", "unrelated cleanup", "", "", "platform\x1fdb-registry-audit"),
    ])


def _card(title: str, body: str) -> str:
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title=title, body=body, assignee="platform-coder")
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
        conn.commit()
        return tid


def _ctx(tid: str, approvals_db: str, session: str = "sess-1") -> g.GuardContext:
    return g.GuardContext(task_id=tid, session_id=session, approvals_db=Path(approvals_db))


# --- coverage (D2) -----------------------------------------------------------

def test_broader_scope_covers_the_profile_token(home, approvals):
    tid = _card("Rotate the bloom filters", "refs: APR-0007")
    assert g.surface_decision(CONFIG, _ctx(tid, approvals)) == ("allow", "")


def test_a_sibling_profile_scope_does_not_cover(home, approvals):
    """A profile-narrow row (APR-0008 is ``platform-stl``) never covers a sibling."""
    tid = _card("Rotate the bloom filters", "refs: APR-0008")
    hit = Hit("hermes-config", "hermes-config:research-stl", "/home/u/.hermes/profiles/research-stl/config.yaml")
    decision, message = g.surface_decision(hit, _ctx(tid, approvals))
    assert decision == "deny"
    assert "hermes-config:research-stl" in message and tid in message


def test_exact_scope_covers_itself(home, approvals):
    tid = _card("Rotate the bloom filters", "refs: APR-0008")
    assert g.surface_decision(CONFIG, _ctx(tid, approvals)) == ("allow", "")


def test_a_narrow_row_does_not_cover_the_broader_token(home, approvals):
    """Coverage is one-directional: ``hermes-config:x`` never covers ``hermes-config``."""
    tid = _card("Rotate the bloom filters", "refs: APR-0008")
    hit = Hit("hermes-config", "hermes-config", "/home/u/.hermes/config.yaml")
    assert g.surface_decision(hit, _ctx(tid, approvals))[0] == "deny"


def test_unrelated_scope_does_not_cover(home, approvals):
    tid = _card("Rotate the bloom filters", "refs: APR-0011")
    decision, message = g.surface_decision(CONFIG, _ctx(tid, approvals))
    assert decision == "deny"
    assert "does not cover" in message


def test_pending_row_is_not_consent(home, approvals):
    tid = _card("Rotate the bloom filters", "refs: APR-0009")
    decision, message = g.surface_decision(SOUL, _ctx(tid, approvals))
    assert decision == "deny"
    assert "APR-0009 -> pending" in message


def test_no_reference_at_all_is_refused(home, approvals):
    tid = _card("Rotate the bloom filters", "no reference here")
    decision, message = g.surface_decision(CONFIG, _ctx(tid, approvals))
    assert decision == "deny"
    assert "references on the card: none" in message


# --- the legacy row (no declared scope) path ---------------------------------

def test_legacy_row_matches_the_token_literally(home, approvals):
    tid = _card("Rotate the bloom filters", "refs: APR-0010")
    hit = Hit("hermes-soul", "hermes-soul:platform-stl", "/home/u/.hermes/profiles/platform-stl/SOUL.md")
    assert g.surface_decision(hit, _ctx(tid, approvals)) == ("allow", "")


def test_legacy_row_with_unrelated_prose_does_not_cover(home, tmp_path):
    legacy = _approvals(tmp_path / "legacy.db", [
        (12, "approved", "approved", "unrelated cleanup", "", "", None),
    ])
    tid = _card("Rotate the bloom filters", "refs: APR-0012")
    assert g.surface_decision(CONFIG, _ctx(tid, legacy))[0] == "deny"


def test_legacy_row_matches_the_surface_name(home, approvals):
    """The documented fallback accepts the surface name too, not only the full token."""
    legacy = _approvals(Path(approvals).parent / "surface-name.db", [
        (13, "approved", "approved", "covered by the hermes-config work order", "", "", None),
    ])
    tid = _card("Rotate the bloom filters", "refs: APR-0013")
    assert g.surface_decision(CONFIG, _ctx(tid, legacy)) == ("allow", "")


# --- coverage is delegated to the ops repo's own rule -------------------------

def test_key_covers_agrees_with_approvals_core():
    """No drift: the guard's coverage agrees with ``approvals_core._key_covers``."""
    core = g._approvals_core()
    if core is None:
        pytest.skip("approvals_core is not reachable on this host")
    pairs = [
        ("hermes-config", "hermes-config"),
        ("hermes-config", "hermes-config:platform-stl"),
        ("hermes-config:platform-stl", "hermes-config"),
        ("hermes-config:platform-stl", "hermes-config:platform-stl"),
        ("hermes-config", "hermes-env"),
        ("launchd-plist", "launchd-plist:ai.hermes.gateway.plist"),
    ]
    for covering, token in pairs:
        assert g._key_covers(covering, token) == core._key_covers(covering, token), (covering, token)


# --- the refusal (D4) --------------------------------------------------------

def test_refusal_names_surface_target_token_and_refs(home, approvals):
    tid = _card("Rotate the bloom filters", "refs: APR-0011, APR-0009")
    decision, message = g.surface_decision(CONFIG, _ctx(tid, approvals))
    assert decision == "deny"
    for expected in ("hermes-config", CONFIG.path, tid, "hermes-config:platform-stl",
                     "APR-0011 -> approved", "APR-0009 -> pending", "FLOOR"):
        assert expected in message


def test_receipt_is_emitted_once_and_parks_the_card(home, approvals):
    tid = _card("Rotate the bloom filters", "no reference")
    ctx = _ctx(tid, approvals)
    first = g.refuse_surface_write(CONFIG, ctx)
    assert first["approved"] is False and first["consent_surface"] is True
    assert first["receipt"] is True
    second = g.refuse_surface_write(CONFIG, ctx)   # a retry on another vector
    assert second["approved"] is False

    with kbc.connect() as conn:
        row = conn.execute("SELECT status, block_kind FROM tasks WHERE id = ?", (tid,)).fetchone()
        events = conn.execute(
            "SELECT count(*) AS n FROM task_events WHERE task_id = ? AND kind = 'consent_refused'",
            (tid,)).fetchone()["n"]
        comments = conn.execute(
            "SELECT count(*) AS n FROM task_comments WHERE task_id = ?", (tid,)).fetchone()["n"]
    assert row["status"] == "blocked" and row["block_kind"] == "needs_input"
    assert events == 1 and comments == 1


def test_receipt_payload_names_the_cause_and_surface(home, approvals):
    import json

    tid = _card("Rotate the bloom filters", "no reference")
    assert g.emit_surface_refusal_receipt(CONFIG, _ctx(tid, approvals)) is True
    with kbc.connect() as conn:
        payload = conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'consent_refused'",
            (tid,)).fetchone()["payload"]
    decoded = json.loads(payload)
    assert decoded["cause"] == g.CAUSE
    assert decoded["surface"] == "hermes-config"
    assert decoded["scope_token"] == "hermes-config:platform-stl"


def test_an_allow_writes_nothing(home, approvals):
    tid = _card("Rotate the bloom filters", "refs: APR-0007")
    assert g.refuse_surface_write(CONFIG, _ctx(tid, approvals)) == {"approved": True, "message": None}
    with kbc.connect() as conn:
        assert conn.execute("SELECT status FROM tasks WHERE id = ?", (tid,)).fetchone()["status"] == "ready"


# --- the per-session latch (D4c) ---------------------------------------------

def test_latch_refuses_the_same_surface_on_every_later_vector(home, approvals):
    tid = _card("Rotate the bloom filters", "no reference")
    ctx = _ctx(tid, approvals)
    assert g.surface_decision(CONFIG, ctx)[0] == "deny"
    # The card is amended to cite a covering row; the latch still refuses the surface.
    with kbc.connect() as conn:
        conn.execute("UPDATE tasks SET body = 'refs: APR-0007' WHERE id = ?", (tid,))
        conn.commit()
    decision, message = g.surface_decision(CONFIG, ctx)
    assert decision == "deny"
    assert "refused earlier in this session for: no_ref" in message
    # A different surface on the same run is evaluated afresh.
    assert g.surface_decision(SOUL, ctx)[0] == "deny"   # APR-0009 is only pending
    # A different session is a different latch.
    assert g.surface_decision(CONFIG, _ctx(tid, approvals, session="sess-2")) == ("allow", "")


# --- inertness and fail-closed (D5) ------------------------------------------

def test_inert_without_an_owning_card(home, approvals, monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    assert g.consent_gate_active() is False
    assert g.surface_decision(CONFIG, {"approvals_db": approvals}) == ("allow", "")


def test_inert_when_the_named_card_does_not_resolve(home, approvals, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_ffffffff")
    assert g.consent_gate_active() is False
    assert g.surface_decision(CONFIG, {"approvals_db": approvals}) == ("allow", "")


def test_an_unreadable_approvals_store_refuses_the_surface(home, tmp_path):
    tid = _card("Rotate the bloom filters", "refs: APR-0007")
    missing = tmp_path / "no-such-approvals.db"
    decision, message = g.surface_decision(CONFIG, _ctx(tid, str(missing)))
    assert decision == "deny"
    assert "approvals_store_unreadable" in message


def test_an_unreadable_board_store_fails_closed_for_surfaces(home, approvals, tmp_path):
    junk = tmp_path / "not-a-db.db"
    junk.write_text("not sqlite at all")
    ctx = g.GuardContext(task_id="t_12345678", approvals_db=Path(approvals), board_db=junk)
    assert g.consent_gate_active(ctx) is True
    assert g.surface_decision(CONFIG, ctx)[0] == "deny"


def test_a_guard_bug_never_escapes_into_the_tool_path(home, approvals, monkeypatch):
    tid = _card("Rotate the bloom filters", "no reference")
    monkeypatch.setattr(g, "_assess", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    decision, message = g.surface_decision(CONFIG, _ctx(tid, approvals))
    assert decision == "deny" and "fails closed" in message


# --- import hygiene ----------------------------------------------------------

def test_module_import_is_side_effect_free(tmp_path):
    """Importing the guard must not open a store, create files, or pull the heavy path."""
    import json
    import subprocess
    import sys

    repo = Path(__file__).resolve().parents[2]
    fresh = tmp_path / "fresh-home"
    fresh.mkdir()
    code = (
        "import json, sys\n"
        "import hermes_cli.consent_surface_guard as g\n"
        "print(json.dumps({'kanban_db': 'hermes_cli.kanban_db' in sys.modules,\n"
        "                  'gate': 'hermes_cli.kanban_consent_gate' in sys.modules}))\n"
    )
    env = dict(os.environ, PYTHONPATH=str(repo), HERMES_HOME=str(fresh))
    env.pop("HERMES_KANBAN_DB", None)
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                          env=env, timeout=180)
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout) == {"kanban_db": False, "gate": False}
    assert list(fresh.iterdir()) == []


# --- the disarm (D6) ---------------------------------------------------------

def test_disarm_env_short_circuits_the_gate(home, approvals, monkeypatch, caplog):
    tid = _card("Rotate the bloom filters", "no reference")
    monkeypatch.setenv(g.DISARM_ENV, "off")
    caplog.set_level("WARNING", logger=g.__name__)
    assert g.consent_gate_active(_ctx(tid, approvals)) is False
    assert g.surface_decision(CONFIG, _ctx(tid, approvals)) == ("allow", "")
    assert any("DISARMED" in r.message for r in caplog.records)


def test_the_design_documents_old_field_names_also_work(home, approvals):
    """``token``/``detail`` (design §2 spellings) stay accepted, not just the pinned ones."""
    class DesignHit:
        surface = "hermes-config"
        token = "hermes-config:platform-stl"
        detail = "/home/u/.hermes/config.yaml"

    tid = _card("Rotate the bloom filters", "refs: APR-0007")
    assert g.surface_decision(DesignHit(), _ctx(tid, approvals)) == ("allow", "")
