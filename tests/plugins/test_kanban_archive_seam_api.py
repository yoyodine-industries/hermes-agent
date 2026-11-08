"""The dashboard's two archive doors obey the archive seam (C1: no anonymous archive).

Both entry points the ruling names — ``PATCH /tasks/{id}`` with ``status:
"archived"`` and ``POST /tasks/bulk`` with ``archive: true`` — must refuse a
reason-less archive with the machine-readable ``reason_missing`` code and must
record the receipt when a reason IS supplied.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc

PREFIX = "/api/plugins/kanban"


def _load_plugin_router():
    repo_root = Path(__file__).resolve().parents[2]
    plugin_file = repo_root / "plugins" / "kanban" / "dashboard" / "plugin_api.py"
    spec = importlib.util.spec_from_file_location(
        "hermes_dashboard_plugin_kanban_seam_test", plugin_file)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod.router


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for var in ("HERMES_KANBAN_DB", "HERMES_KANBAN_HOME", "HERMES_KANBAN_BOARD",
                "HERMES_KANBAN_WORKSPACES_ROOT"):
        monkeypatch.delenv(var, raising=False)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    return home


@pytest.fixture
def client(kanban_home):
    app = FastAPI()
    app.include_router(_load_plugin_router(), prefix=PREFIX)
    return TestClient(app)


def _task(conn, **kw):
    return kb.create_task(conn, title=kw.pop("title", "x"), **kw)


def _receipt(conn, task_id):
    row = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'archived' "
        "ORDER BY id DESC LIMIT 1", (task_id,),
    ).fetchone()
    assert row is not None
    return json.loads(row["payload"])


def test_patch_archive_without_a_reason_is_a_400_naming_the_code(client, kanban_home):
    with kbc.connect() as conn:
        t = _task(conn)
    r = client.patch(f"{PREFIX}/tasks/{t}", json={"status": "archived"})
    assert r.status_code == 400
    assert "reason_missing" in r.json()["detail"]
    with kbc.connect() as conn:
        assert kb.get_task(conn, t).status != "archived"


def test_patch_archive_with_a_reason_records_the_receipt(client, kanban_home):
    with kbc.connect() as conn:
        t = _task(conn)
    r = client.patch(f"{PREFIX}/tasks/{t}",
                     json={"status": "archived", "reason": "superseded"})
    assert r.status_code == 200
    with kbc.connect() as conn:
        assert kb.get_task(conn, t).status == "archived"
        receipt = _receipt(conn, t)
        assert receipt["reason"] == "superseded"
        assert receipt["actor"] == "dashboard"


def test_patch_rehome_from_the_dashboard_writes_the_key(client, kanban_home):
    with kbc.connect() as conn:
        src, dest = _task(conn, title="src"), _task(conn, title="dest")
    r = client.patch(f"{PREFIX}/tasks/{src}",
                     json={"status": "archived", "reason": "rehomed",
                           "destination": f"default/{dest}"})
    assert r.status_code == 200
    with kbc.connect() as conn:
        assert conn.execute("SELECT idempotency_key FROM tasks WHERE id = ?",
                            (dest,)).fetchone()[0] == f"rehome-default-{src}"


def test_patch_rehome_without_a_destination_is_refused(client, kanban_home):
    with kbc.connect() as conn:
        t = _task(conn)
    r = client.patch(f"{PREFIX}/tasks/{t}",
                     json={"status": "archived", "reason": "rehomed"})
    assert r.status_code == 400
    assert "destination_missing" in r.json()["detail"]


def test_bulk_archive_without_a_reason_refuses_every_card(client, kanban_home):
    with kbc.connect() as conn:
        ids = [_task(conn, title=f"x{i}") for i in range(3)]
    r = client.post(f"{PREFIX}/tasks/bulk", json={"ids": ids, "archive": True})
    assert r.status_code == 200
    results = r.json()["results"]
    assert [e["ok"] for e in results] == [False, False, False]
    assert all("reason_missing" in e["error"] for e in results)
    with kbc.connect() as conn:
        for tid in ids:
            assert kb.get_task(conn, tid).status != "archived"


def test_bulk_archive_with_a_reason_archives_with_receipts(client, kanban_home):
    with kbc.connect() as conn:
        ids = [_task(conn, title=f"x{i}") for i in range(2)]
    r = client.post(f"{PREFIX}/tasks/bulk",
                    json={"ids": ids, "archive": True, "reason": "never-started"})
    assert r.status_code == 200
    assert all(e["ok"] for e in r.json()["results"])
    with kbc.connect() as conn:
        for tid in ids:
            assert kb.get_task(conn, tid).status == "archived"
            assert _receipt(conn, tid)["reason"] == "never-started"
