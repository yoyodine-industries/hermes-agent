"""The three write-boundary doors, wired (design ``t_cb9259fc``).

The class is proven by ``test_consent_surface.py`` (which target is which surface) and the
decision by ``test_consent_surface_guard.py`` (coverage, refusal, receipt, latch). What is
left to prove is the WIRING -- that every door the design names actually asks:

* **FILE** -- ``write_file``/``patch`` (:func:`tools.file_tools_write_guards._check_consent_surface`)
  and the ``file_operations`` primitives behind them (the section 9 census);
* **TERMINAL** -- :func:`tools.approval._consent_floor`, with the other floors, before
  ``--yolo`` / ``approvals.mode=off`` / the allowlist / unattended resolution;
* **EXEC** -- :func:`tools.approval._consent_floor_text`, before the same short-circuits.

Contract under test, from the design:

* D3 -- the clause is YOLO-PROOF: a session set to ``--yolo`` or ``mode=off`` is still
  refused, and (AC2) a covering approval never re-opens a HARD deny -- ``.env`` and the
  active profile's ``config.yaml`` stay unserved with the approval in hand;
* D5 -- INERT without an owning kanban card: a human at a console is the consent authority;
* D6 -- the documented env switch disarms the doors;
* section 6 -- the clause never refuses ordinary work, and a refusal carries the surface, the
  target, the owning card and the token to file, plus exactly ONE deduped receipt.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from hermes_cli import consent_surface_doors as doors
from hermes_cli import consent_surface_guard as g
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from tools import approval, file_operations, file_tools, file_tools_write_guards as fw
from tools.environments.local import LocalEnvironment


# --- fixtures ---------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    g.reset_latch()
    for name in ("HERMES_KANBAN_TASK", "HERMES_SESSION_ID", "KANBAN_APPROVALS_DB",
                 g.DISARM_ENV, "HERMES_KANBAN_DB"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(approval, "_yolo_active", lambda: False)
    yield
    g.reset_latch()


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A throwaway home with the standard ``.hermes`` layout inside it."""
    root = tmp_path / "home"
    (root / ".hermes" / "profiles" / "platform-coder").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(root))
    monkeypatch.setattr(Path, "home", lambda: root)
    monkeypatch.setenv("HERMES_HOME", str(root / ".hermes"))
    monkeypatch.setenv("KANBAN_APPROVALS_DB", str(tmp_path / "approvals.db"))
    kb.init_db()
    return root


@pytest.fixture
def approvals(tmp_path):
    """One covering row per surface this file writes, plus a pending one that must not."""
    con = sqlite3.connect(tmp_path / "approvals.db")
    con.execute(
        "CREATE TABLE approvals (id INTEGER PRIMARY KEY, project TEXT DEFAULT '', "
        "task TEXT DEFAULT '', context TEXT DEFAULT '', status TEXT, decision TEXT, "
        "reason TEXT, idempotency_key TEXT, card_id TEXT, board TEXT, superseded_by TEXT)"
    )
    con.executemany(
        "INSERT INTO approvals (id, status, decision, reason, task, context, idempotency_key) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (20, "approved", "approved", "the platform-coder persona", "", "",
             "platform\x1fhermes-soul:platform-coder"),
            (21, "approved", "approved", "estates env", "", "", "platform\x1fhermes-env"),
            (22, "approved", "approved", "estates config", "", "", "platform\x1fhermes-config"),
            (23, "pending", "", "", "", "", "platform\x1fhermes-plugin"),
        ],
    )
    con.commit()
    con.close()
    return str(tmp_path / "approvals.db")


def _card(title: str, body: str = "") -> str:
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title=title, body=body, assignee="platform-coder")
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
        conn.commit()
        return tid


def _armed(monkeypatch, tid: str, session: str = "sess-1") -> None:
    """Put this process in the place the dispatcher puts a worker."""
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_SESSION_ID", session)


def _events(tid: str, kind: str = g.RECEIPT_KIND) -> list:
    with kbc.connect() as conn:
        return conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND kind = ?", (tid, kind),
        ).fetchall()


def _row(tid: str):
    with kbc.connect() as conn:
        return conn.execute("SELECT status, block_kind FROM tasks WHERE id = ?", (tid,)).fetchone()


def _soul(home: Path) -> str:
    return str(home / ".hermes" / "profiles" / "platform-coder" / "SOUL.md")


def _cfg(home: Path) -> str:
    return str(home / ".hermes" / "profiles" / "platform-coder" / "config.yaml")


# --- the door readers (pure) ------------------------------------------------

def test_the_file_door_reads_the_surface_and_its_scope_token(home):
    hit = doors.hit_for_paths([_soul(home)])
    assert hit is not None
    assert (hit.surface, hit.scope_token) == ("hermes-soul", "hermes-soul:platform-coder")


def test_the_first_surface_in_a_path_list_wins(home):
    """A multi-file patch is refused on the surface it names FIRST, not the last."""
    hit = doors.hit_for_paths([str(home / "notes.txt"), _soul(home), _cfg(home)])
    assert hit is not None and hit.scope_token == "hermes-soul:platform-coder"


def test_a_path_outside_the_class_is_not_a_file_door_hit(home):
    assert doors.hit_for_paths([str(home / "projects" / "app" / "config.yaml")]) is None


WRITING_COMMANDS = [
    ("hermes config set approvals.mode off", "hermes-config:approvals.mode"),
    ("echo x > {config}", "hermes-config:root"),
    ("sed -i '' s/a/b/ {soul}", "hermes-soul:platform-coder"),
    ("echo hi >> {plugin}", "hermes-plugin:x"),
    ("cp a {plist}", "launchd-plist:ai.hermes.gateway.plist"),
]


@pytest.mark.parametrize(("template", "token"), WRITING_COMMANDS)
def test_the_terminal_door_reads_a_write_shape(home, template, token):
    command = template.format(
        config=home / ".hermes" / "config.yaml",
        soul=_soul(home),
        plugin=home / ".hermes" / "plugins" / "x" / "router.py",
        plist=home / "Library" / "LaunchAgents" / "ai.hermes.gateway.plist",
    )
    hit = doors.hit_for_command(command)
    assert hit is not None and hit.scope_token == token


READING_COMMANDS = [
    "cat {config}",
    "grep -n approvals {soul}",
    "hermes config get approvals.mode",
    "ls -la {plugins}",
]


@pytest.mark.parametrize("template", READING_COMMANDS)
def test_a_read_is_never_a_terminal_door_hit(home, template):
    command = template.format(
        config=home / ".hermes" / "config.yaml",
        soul=_soul(home),
        plugins=home / ".hermes" / "plugins",
    )
    assert doors.hit_for_command(command) is None


EXEC_WRITES = [
    'open("{soul}", "w").write("x")',
    'open(os.path.expanduser("{soul_tilde}"), "w").write("x")',
    'Path("{soul}").write_text("x")',
    'p = Path("{soul}")\np.write_text("x")',
    'subprocess.run("cp a {plist}", shell=True)',
    'shutil.copy("a", "{plugin}")',
]

EXEC_READS = [
    'open("{config}").read()',
    'data = Path("{soul}").read_text()',
    'open(os.path.expanduser("{soul_tilde}")).read()',
    'subprocess.run("cat {config}", shell=True)',
    'p = Path("~/projects/app/config.yaml")\np.write_text("x")',
]


@pytest.mark.parametrize("template", EXEC_WRITES)
def test_the_exec_door_reads_a_python_write_shape(home, template):
    code = template.format(
        config=home / ".hermes" / "config.yaml",
        soul=_soul(home),
        soul_tilde="~/.hermes/profiles/platform-coder/SOUL.md",
        plugin=home / ".hermes" / "plugins" / "x" / "router.py",
        plist=home / "Library" / "LaunchAgents" / "ai.hermes.gateway.plist",
    )
    assert doors.hit_for_text(code) is not None, code


@pytest.mark.parametrize("template", EXEC_READS)
def test_a_read_is_never_an_exec_door_hit(home, template):
    code = template.format(
        config=home / ".hermes" / "config.yaml",
        soul=_soul(home),
        soul_tilde="~/.hermes/profiles/platform-coder/SOUL.md",
    )
    assert doors.hit_for_text(code) is None, code


def test_a_surface_built_at_runtime_is_the_named_residual(home):
    """Section 9: a path assembled at runtime evades the EXEC literal scan, not the
    TERMINAL door -- the script's own ``terminal()`` call is a command, not text."""
    code = ('parts = ["{home}", ".hermes", "profiles", "platform-coder", "SOUL.md"]\n'
            'Path("/".join(parts)).write_text("x")').format(home=home)
    assert doors.hit_for_text(code) is None
    assert doors.hit_for_command('echo x > "{soul}"'.format(soul=_soul(home))) is not None


# --- D5: inert without an owning card ---------------------------------------

def test_all_three_doors_are_inert_without_an_owning_card(home):
    refusal = 'open("{soul}", "w")'.format(soul=_soul(home))
    assert not g.consent_gate_active()
    assert doors.file_denial([_soul(home)]) is None
    assert doors.command_denial("echo x > " + _soul(home)) is None
    assert doors.text_denial(refusal) is None


def test_the_env_switch_disarms_the_doors(home, monkeypatch):
    """D6: the documented emergency switch, and only it."""
    tid = _card("Rotate the bloom filters")
    _armed(monkeypatch, tid)
    assert doors.file_denial([_soul(home)]) is not None
    monkeypatch.setenv(g.DISARM_ENV, "off")
    assert doors.file_denial([_soul(home)]) is None


# --- the FILE door ----------------------------------------------------------

def test_an_unlicensed_surface_write_is_refused_with_the_token_to_file(home, approvals, monkeypatch):
    tid = _card("Rotate the bloom filters")
    _armed(monkeypatch, tid)
    message = doors.file_denial([_soul(home)])
    assert message is not None
    assert "BLOCKED (consent surface)" in message
    assert "hermes-soul:platform-coder" in message and tid in message
    assert _soul(home) in message


def test_a_covering_approval_lets_the_write_through(home, approvals, monkeypatch):
    tid = _card("Rewrite the platform-coder persona", "refs: APR-0020")
    _armed(monkeypatch, tid)
    assert doors.file_denial([_soul(home)]) is None


def test_a_pending_approval_does_not_license_the_write(home, approvals, monkeypatch):
    tid = _card("Install a plugin", "refs: APR-0023")
    _armed(monkeypatch, tid)
    assert doors.file_denial([home / ".hermes" / "plugins" / "x" / "router.py"]) is not None


def test_ordinary_work_is_never_refused_when_armed(home, approvals, monkeypatch):
    tid = _card("Write some notes")
    _armed(monkeypatch, tid)
    assert doors.file_denial([str(home / "notes.txt")]) is None


def test_a_refusal_parks_the_card_and_records_one_deduped_receipt(home, approvals, monkeypatch):
    tid = _card("Rotate the bloom filters")
    _armed(monkeypatch, tid)
    assert doors.file_denial([_soul(home)]) is not None
    assert doors.file_denial([_soul(home)]) is not None  # same call again: no second receipt
    row = _row(tid)
    assert row["status"] == "blocked" and row["block_kind"] == "needs_input"
    assert len(_events(tid)) == 1


def test_the_latch_refuses_the_same_surface_on_the_next_vector(home, approvals, monkeypatch):
    """D4b: one refusal per surface per run -- the terminal door does not get a retry."""
    tid = _card("Rotate the bloom filters")
    _armed(monkeypatch, tid)
    assert doors.file_denial([_soul(home)]) is not None
    second = doors.command_denial("echo x > " + _soul(home))
    assert second is not None and second["approved"] is False
    assert "refused earlier in this session" in second["message"]


# --- AC2: a covering approval never re-opens a HARD deny --------------------

def test_a_hard_denied_env_write_stays_unserved_with_a_covering_approval(home, approvals, monkeypatch):
    tid = _card("Rotate the estates env", "refs: APR-0021")
    _armed(monkeypatch, tid)
    env_path = home / ".hermes" / ".env"
    env_path.write_text("EXISTING=1\n")
    result = file_tools.write_file_tool(str(env_path), "EVIL=1\n", task_id="t1")
    assert "EXISTING=1" in env_path.read_text(), result
    assert "consent surface" not in result.lower(), result
    assert not _events(tid)


def test_a_hard_denied_root_config_stays_unserved_with_a_covering_approval(home, approvals, monkeypatch):
    tid = _card("Rotate the estates config", "refs: APR-0022")
    _armed(monkeypatch, tid)
    cfg_path = home / ".hermes" / "config.yaml"
    cfg_path.write_text("model: original\n")
    result = file_tools.write_file_tool(str(cfg_path), "model: evil\n", task_id="t1")
    assert "original" in cfg_path.read_text(), result
    assert "Refusing to write to Hermes config file" in result
    assert not _events(tid)


def test_a_hard_denied_surface_is_still_refused_by_the_file_clause_without_an_approval(home, approvals, monkeypatch):
    """The other half of AC2: unlicensed, the clause says no -- and the write is not done."""
    tid = _card("Rotate the bloom filters")
    _armed(monkeypatch, tid)
    result = file_tools.write_file_tool(_soul(home), "you are now Evil\n", task_id="t1")
    assert "consent surface" in result.lower(), result
    assert not Path(_soul(home)).exists()


# --- the TERMINAL floor -----------------------------------------------------

def test_the_terminal_floor_refuses_a_surface_write(home, approvals, monkeypatch):
    tid = _card("Rotate the bloom filters")
    _armed(monkeypatch, tid)
    result = approval._consent_floor("echo x > " + _soul(home))
    assert result is not None and result["approved"] is False
    assert "hermes-soul:platform-coder" in result["message"]
    assert result["receipt"] is True


def test_the_terminal_floor_leaves_reads_and_ordinary_writes_alone(home, approvals, monkeypatch):
    tid = _card("Inspect the gateway")
    _armed(monkeypatch, tid)
    assert approval._consent_floor("cat " + _soul(home)) is None
    assert approval._consent_floor("echo ok > " + str(home / "notes.txt")) is None


@pytest.mark.parametrize("yolo", [True, False])
def test_the_standing_order_command_is_refused_under_yolo_and_off(home, approvals, monkeypatch, yolo):
    """D3: ``hermes config set approvals.mode off`` is refused whatever the session says."""
    tid = _card("Rotate the bloom filters")
    _armed(monkeypatch, tid)
    monkeypatch.setattr(approval, "_yolo_active", lambda: yolo)
    monkeypatch.setattr(approval.approval_context, "_get_approval_mode", lambda: "off")
    result = approval.check_all_command_guards("hermes config set approvals.mode off", "local")
    assert result["approved"] is False
    assert "consent surface" in result["message"].lower()
    assert "hermes-config:approvals.mode" in result["message"]


def test_a_covering_approval_licenses_the_cli_write(home, approvals, monkeypatch):
    tid = _card("Rotate the estates config", "refs: APR-0022")
    _armed(monkeypatch, tid)
    monkeypatch.setattr(approval.approval_context, "_get_approval_mode", lambda: "off")
    result = approval.check_all_command_guards("hermes config set approvals.mode off", "local")
    assert result["approved"] is True, result


def test_the_terminal_floor_does_not_refuse_ordinary_commands_under_yolo(home, approvals, monkeypatch):
    tid = _card("Look around")
    _armed(monkeypatch, tid)
    monkeypatch.setattr(approval, "_yolo_active", lambda: True)
    assert approval.check_all_command_guards("ls -la /tmp", "local")["approved"] is True


# --- the EXEC floor ---------------------------------------------------------

def test_the_exec_floor_refuses_a_script_that_writes_a_surface(home, approvals, monkeypatch):
    tid = _card("Rotate the bloom filters")
    _armed(monkeypatch, tid)
    code = 'open("{soul}", "w").write("x")'.format(soul=_soul(home))
    result = approval.check_execute_code_guard(code, "local")
    assert result["approved"] is False
    assert "hermes-soul:platform-coder" in result["message"]


def test_the_exec_floor_is_yolo_proof(home, approvals, monkeypatch):
    tid = _card("Rotate the bloom filters")
    _armed(monkeypatch, tid)
    monkeypatch.setattr(approval, "_yolo_active", lambda: True)
    monkeypatch.setattr(approval.approval_context, "_get_approval_mode", lambda: "off")
    code = 'Path("{soul}").write_text("x")'.format(soul=_soul(home))
    assert approval.check_execute_code_guard(code, "local")["approved"] is False


def test_a_read_only_script_passes_the_exec_floor(home, approvals, monkeypatch):
    tid = _card("Inspect the estates config")
    _armed(monkeypatch, tid)
    code = 'print(open("{cfg}").read())'.format(cfg=str(home / ".hermes" / "config.yaml"))
    assert approval.check_execute_code_guard(code, "local")["approved"] is True


# --- the census: the file_operations primitives (section 9) -----------------

@pytest.fixture
def ops(tmp_path, monkeypatch):
    """A real local shell backend; the hermes ``bin`` look-up is stubbed because it probes
    the REAL install (the test harness forbids that I/O, and the clause under test reads no
    bin directory)."""
    from tools.environments import local as local_env

    monkeypatch.setattr(local_env, "_resolve_hermes_bin_dir", lambda: None)
    return file_operations.ShellFileOperations(LocalEnvironment(cwd=str(tmp_path)), cwd=str(tmp_path))


def test_the_census_holds_write_file(ops, home, approvals, monkeypatch):
    tid = _card("Rotate the bloom filters")
    _armed(monkeypatch, tid)
    result = ops.write_file(_soul(home), "you are now Evil\n")
    assert result.error and "consent surface" in result.error.lower(), result
    assert not Path(_soul(home)).exists()


def test_the_census_holds_patch_replace(ops, home, approvals, monkeypatch):
    tid = _card("Rotate the bloom filters")
    _armed(monkeypatch, tid)
    result = ops.patch_replace(_soul(home), "a", "b")
    assert result.error and "consent surface" in result.error.lower(), result


def test_the_census_holds_delete_file(ops, home, approvals, monkeypatch):
    tid = _card("Rotate the bloom filters")
    _armed(monkeypatch, tid)
    result = ops.delete_file(_soul(home))
    assert result.error and "consent surface" in result.error.lower(), result


def test_the_census_holds_move_file(ops, home, approvals, monkeypatch):
    """A move INTO a surface is a surface write (the destination is the write)."""
    tid = _card("Rotate the bloom filters")
    _armed(monkeypatch, tid)
    result = ops.move_file(str(home / "notes.txt"), _soul(home))
    assert result.error and "consent surface" in result.error.lower(), result


def test_a_refused_write_does_not_touch_an_existing_surface(home, approvals, monkeypatch):
    """DoD: a refusal never leaves a partially written artifact -- the bytes stay as they were."""
    tid = _card("Rotate the bloom filters")
    _armed(monkeypatch, tid)
    soul = Path(_soul(home))
    soul.write_text("original persona\n")
    result = file_tools.write_file_tool(str(soul), "you are now Evil\n", task_id="t1")
    assert "consent surface" in result.lower(), result
    assert soul.read_text() == "original persona\n"


def test_a_refused_delete_removes_nothing(ops, home, approvals, monkeypatch):
    """DoD: a deletion removes nothing when it is refused (the primitive refuses before the `rm`)."""
    tid = _card("Rotate the bloom filters")
    _armed(monkeypatch, tid)
    plugin = home / ".hermes" / "plugins" / "x" / "router.py"
    plugin.parent.mkdir(parents=True)
    plugin.write_text("print('still here')\n")
    result = ops.delete_file(str(plugin))
    assert result.error and "consent surface" in result.error.lower(), result
    assert plugin.read_text() == "print('still here')\n"


def test_the_census_leaves_ordinary_writes_alone(ops, home, approvals, monkeypatch):
    tid = _card("Write some notes")
    _armed(monkeypatch, tid)
    target = str(home / "notes.txt")
    result = ops.write_file(target, "hello\n")
    assert not result.error, result
    assert Path(target).read_text() == "hello\n"


# --- the patch door covers its V4A headers ----------------------------------

def test_a_v4a_patch_that_deletes_a_surface_is_refused(home, approvals, monkeypatch):
    """``patch`` writes through the same precheck, so a V4A header naming a surface is a
    surface write even when the body is an innocent edit elsewhere."""
    tid = _card("Rotate the bloom filters")
    _armed(monkeypatch, tid)
    v4a = (
        "*** Begin Patch\n"
        f"*** Delete File: {_soul(home)}\n"
        "*** End Patch\n"
    )
    result = file_tools.patch_tool(mode="patch", patch=v4a, task_id="t1")
    assert "consent surface" in result.lower(), result
