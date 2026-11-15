"""The update's end state must be the tree its install stamp describes (t_391ae86c).

The 2026-11-06 incident: ``hermes update`` parked on ``overrides/live`` (289 commits not in
``origin/main``), switched the running checkout to ``main``, and returned success while
``install-stamp.json`` still named ``overrides/live@6444bc1acf``. Nothing refused the end
state; the *next* launch discovered it and could not repair it (six attempts, then silence).
The guard belongs where the update hands the tree back.
"""

import json
import subprocess
from types import SimpleNamespace

import pytest


def _git(root, *args):
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _repo(tmp_path):
    root = tmp_path / "checkout"
    root.mkdir()
    _git(root, "init", "-q", "-b", "overrides/live")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "test")
    (root / "app.py").write_text("VALUE = 1\n")
    _git(root, "add", ".")
    _git(root, "-c", "commit.gpgsign=false", "commit", "-q", "-m", "one")
    return root


def _stamp(root, commit, branch):
    (root / "install-stamp.json").write_text(
        json.dumps(
            {
                "schemaVersion": 2,
                "commit": commit,
                "branch": branch,
                "dirty": False,
                "source": "git",
                "updateMechanism": "self",
            }
        )
        + "\n"
    )


def test_a_checkout_on_its_stamp_is_not_a_divergence(tmp_path):
    from hermes_cli.update_end_state import checkout_off_install_stamp

    root = _repo(tmp_path)
    _stamp(root, _git(root, "rev-parse", "HEAD"), "overrides/live")

    assert checkout_off_install_stamp(root) is None


def test_an_update_that_switches_the_branch_off_the_stamp_is_named(tmp_path):
    from hermes_cli.update_end_state import checkout_off_install_stamp

    root = _repo(tmp_path)
    parked = _git(root, "rev-parse", "HEAD")
    _git(root, "checkout", "-q", "-b", "main")
    (root / "app.py").write_text("VALUE = 2\n")
    _git(root, "-c", "commit.gpgsign=false", "commit", "-q", "-am", "two")
    landed = _git(root, "rev-parse", "HEAD")
    _stamp(root, parked, "overrides/live")

    line = checkout_off_install_stamp(root)

    assert line is not None
    assert "overrides/live@" + parked[:10] in line
    assert "main@" + landed[:10] in line
    assert line.startswith("✗ ")


def test_a_tree_with_no_stamp_claims_nothing(tmp_path):
    """A dev checkout has no stamp; the guard names no divergence it cannot prove."""
    from hermes_cli.update_end_state import checkout_off_install_stamp

    assert checkout_off_install_stamp(_repo(tmp_path)) is None


@pytest.fixture
def _quiet_update(monkeypatch):
    """``hermes update`` whose implementation succeeds, over a checkout *root*."""
    from hermes_cli import main, update_cmd, update_lock

    def arm(root):
        monkeypatch.setattr(main, "PROJECT_ROOT", root)
        monkeypatch.setattr(
            "hermes_cli.update_owning_install.retarget_to_owning_install", lambda p: None
        )
        monkeypatch.setattr(main, "_update_preflight_handled", lambda args: False)
        monkeypatch.setattr(main, "_install_hangup_protection", lambda **kw: None)
        monkeypatch.setattr(main, "_finalize_update_output", lambda state: None)
        finalized = []
        monkeypatch.setattr(
            main,
            "_finalize_update_receipt",
            lambda code, reason: finalized.append((code, reason)),
        )
        monkeypatch.setattr(update_cmd, "_cmd_update_impl", lambda args, gateway_mode: None)

        class _Lock:
            holder = None

            def __init__(self, **_kw):
                pass

            def acquire(self):
                return True

            def release(self):
                pass

        monkeypatch.setattr(update_lock, "UpdateLock", _Lock)
        return finalized

    return arm


def test_hermes_update_exits_non_zero_when_it_hands_back_a_tree_off_its_stamp(
    tmp_path, capsys, _quiet_update
):
    from hermes_cli import main

    root = _repo(tmp_path)
    parked = _git(root, "rev-parse", "HEAD")
    _git(root, "checkout", "-q", "-b", "main")
    (root / "app.py").write_text("VALUE = 2\n")
    _git(root, "-c", "commit.gpgsign=false", "commit", "-q", "-am", "two")
    _stamp(root, parked, "overrides/live")
    finalized = _quiet_update(root)

    with pytest.raises(SystemExit) as exit_info:
        main.cmd_update(SimpleNamespace(gateway=False))

    assert exit_info.value.code == 1
    out = capsys.readouterr().out
    assert "✗ Update finished with the checkout OFF its install stamp" in out
    assert finalized == [(1, "install_stamp_mismatch")]


def test_hermes_update_succeeds_when_the_tree_is_on_its_stamp(
    tmp_path, capsys, _quiet_update
):
    from hermes_cli import main

    root = _repo(tmp_path)
    _stamp(root, _git(root, "rev-parse", "HEAD"), "overrides/live")
    finalized = _quiet_update(root)

    main.cmd_update(SimpleNamespace(gateway=False))

    assert finalized == [(0, "completed at command boundary")]
