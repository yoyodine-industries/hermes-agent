"""The dispatcher boot RECORDS the tree it dispatches from (card t_70e91ef2).

Without this half the fence has nothing to compare against: the record is written here,
at the only moment the process knows it owns dispatch.
"""

import json
from unittest.mock import patch

from gateway import kanban_watchers as kw
from gateway.run import GatewayRunner
from hermes_cli import tree_identity as ti


def _runner():
    return GatewayRunner.__new__(GatewayRunner)


def _home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_DISPATCH_IN_GATEWAY", raising=False)
    return home


def _record(home):
    return home / "kanban" / ti.RECORD_FILENAME


def test_dispatcher_boot_records_the_tree_it_dispatches_from(tmp_path, monkeypatch):
    home = _home(tmp_path, monkeypatch)

    with patch("hermes_cli.config.load_config",
               return_value={"kanban": {"dispatch_in_gateway": True}}):
        boot = _runner()._kanban_dispatcher_boot()

    assert boot is not None, "the dispatcher should have booted"
    data = json.loads(_record(home).read_text(encoding="utf-8"))
    assert data["tree"] == str(ti.current_tree())
    assert data["pid"] > 0
    assert data["version"] == ti.RECORD_VERSION
    # The armed record is what the fence reads back.
    assert ti.dispatching_tree_refusal("complete") is None


def test_dispatcher_boot_is_a_noop_when_dispatch_is_disabled(tmp_path, monkeypatch):
    home = _home(tmp_path, monkeypatch)

    with patch("hermes_cli.config.load_config",
               return_value={"kanban": {"dispatch_in_gateway": False}}):
        boot = _runner()._kanban_dispatcher_boot()

    assert boot is None
    assert not _record(home).exists()


def test_a_contended_lock_never_overwrites_the_real_dispatchers_record(tmp_path, monkeypatch):
    """A gateway that will NOT dispatch must not claim the board to its own tree."""
    home = _home(tmp_path, monkeypatch)
    rec = _record(home)
    rec.parent.mkdir(parents=True, exist_ok=True)
    rec.write_text(json.dumps({"version": 1, "tree": "/opt/other-dispatcher",
                               "pid": 4242}), encoding="utf-8")

    handle, state = kw._acquire_singleton_lock(home / "kanban" / ".dispatcher.lock")
    try:
        assert state == "held", state  # this test process IS the other dispatcher
        with patch("hermes_cli.config.load_config",
                   return_value={"kanban": {"dispatch_in_gateway": True}}):
            boot = _runner()._kanban_dispatcher_boot()
        assert boot is None, "a contended gateway must not dispatch"
        assert json.loads(rec.read_text(encoding="utf-8"))["tree"] == "/opt/other-dispatcher"
    finally:
        if handle is not None:
            kw._release_singleton_lock(handle)
