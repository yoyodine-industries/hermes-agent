"""Command-vector coverage for the protected-instruction write gate.

``tests/tools/test_file_write_safety.py`` pins the FILE vector (write_file / patch).
These tests pin the COMMAND vectors — terminal and execute_code — because either one alone
is a bypass: ``echo x > SOUL.md`` and ``Path('SOUL.md').write_text('x')`` never touch
file_tools, so the same gate has to run at that dispatch layer too.

Pinned properties: the shapes a model reaches for are gated, ``force=True`` does not skip
the gate, an ordinary write still runs, and the verdict is the FILE gate's own (no second
matcher to drift).
"""

import json
from contextlib import ExitStack
from unittest.mock import MagicMock, patch

import pytest


def _env_config(cwd):
    return {
        "env_type": "local",
        "timeout": 180,
        "cwd": str(cwd),
        "host_cwd": None,
        "modal_mode": "auto",
        "docker_image": "",
        "singularity_image": "",
        "modal_image": "",
        "daytona_image": "",
    }


def _run_terminal(command, cwd, **kwargs):
    """terminal_tool through the real pre-exec gate chain, on a mocked local env."""
    from tools.terminal_tool import terminal_tool

    config = _env_config(cwd)
    mock_env = MagicMock()
    mock_env.execute.return_value = {"output": "ok", "returncode": 0}
    mock_env.cwd = config["cwd"]
    with ExitStack() as stack:
        stack.enter_context(patch("tools.terminal_tool._get_env_config", return_value=config))
        stack.enter_context(patch("tools.terminal_tool._start_cleanup_thread"))
        stack.enter_context(patch("tools.terminal_tool._active_environments", {"default": mock_env}))
        stack.enter_context(patch("tools.terminal_tool._last_activity", {"default": 0}))
        stack.enter_context(patch("tools.terminal_tool._session_cwd", {}))
        # tirith/DANGEROUS_PATTERNS is not what this file pins: approve it, so a blocked
        # result can only have come from the protected-instruction gate.
        stack.enter_context(patch("tools.terminal_tool._check_all_guards", return_value={"approved": True}))
        result = json.loads(terminal_tool(command=command, **kwargs))
    return result, mock_env


def _run_execute_code(code):
    """execute_code through the real dispatch preamble, with the kernel stubbed out."""
    import tools.code_execution_tool as code_execution_tool

    kernel = MagicMock(return_value="ran-in-kernel")
    with ExitStack() as stack:
        stack.enter_context(patch.object(code_execution_tool, "SANDBOX_AVAILABLE", True))
        stack.enter_context(patch("tools.terminal_scope.enforce_no_refusal", lambda: None))
        stack.enter_context(patch("tools.process_registry._is_supervised_gateway_process", lambda: False))
        stack.enter_context(patch("tools.terminal_tool._get_env_config", return_value=_env_config("/tmp")))
        stack.enter_context(patch("tools.terminal_tool._docker_has_host_access", lambda config: False))
        stack.enter_context(patch("tools.approval.check_execute_code_guard", return_value={"approved": True}))
        stack.enter_context(patch("tools.code_kernel.execute_in_session_kernel", kernel))
        result = code_execution_tool.execute_code(code=code, task_id="t_guard")
    return result, kernel


PROTECTED = "protected agent-instruction"


class TestTerminalVector:
    def test_write_shapes_are_gated(self, tmp_path):
        """Every shape that reaches a protected file through the shell is refused."""
        target = tmp_path / "SOUL.md"
        shapes = (
            f"echo x > {target}",
            f"echo x >> {target}",
            f"cat > {target} <<'EOF'\nhi\nEOF",
            f"python3 -c \"open('{target}','w').write('x')\"",
            f"sh -c \"echo x > {target}\"",
            f"tee {target}",
            f"cp /tmp/anything {target}",
            f"sed -i 's/a/b/' {target}",
        )
        for command in shapes:
            result, env = _run_terminal(command, tmp_path)
            assert result["status"] == "blocked", command
            assert PROTECTED in result["error"], command
            assert "SOUL.md" in result["error"], command
            env.execute.assert_not_called()

    def test_hard_deny_paths_are_refused(self, tmp_path):
        """The file tools' hard deny (Hermes config.yaml, system paths) is not shell-bypassable."""
        import tools.file_tools_write_guards as write_guards

        cases = [("echo x > /etc/hosts", "sensitive system path")]
        hermes_config = write_guards._get_hermes_config_resolved()
        if hermes_config:
            cases.append((f"echo x > {hermes_config}", "Hermes config file"))
        for command, needle in cases:
            result, env = _run_terminal(command, tmp_path)
            assert result["status"] == "blocked", command
            assert needle in result["error"], command
            env.execute.assert_not_called()

    def test_force_cannot_bypass(self, tmp_path):
        """--yolo/force skips the approval guards; it must not skip this gate."""
        result, env = _run_terminal(f"echo x > {tmp_path}/SOUL.md", tmp_path, force=True)
        assert result["status"] == "blocked"
        assert PROTECTED in result["error"]
        env.execute.assert_not_called()

    def test_ordinary_write_runs(self, tmp_path):
        result, env = _run_terminal(f"echo x > {tmp_path}/notes.md", tmp_path)
        assert result.get("status") != "blocked"
        env.execute.assert_called_once()

    def test_read_of_a_protected_file_still_runs(self, tmp_path):
        result, env = _run_terminal(f"cat {tmp_path}/AGENTS.md", tmp_path)
        assert result.get("status") != "blocked"
        env.execute.assert_called_once()


class TestExecuteCodeVector:
    def test_write_shapes_are_gated(self):
        """Python writes (direct, path-object, subprocess, RPC) are refused."""
        shapes = (
            "open('/tmp/guard-target/SOUL.md','w').write('x')",
            "Path('/tmp/guard-target/SOUL.md').write_text('x')",
            "shutil.copy('/tmp/a', '/tmp/guard-target/AGENTS.md')",
            "subprocess.run(['tee', '/tmp/guard-target/SOUL.md'])",
            "os.system('echo x > /tmp/guard-target/SOUL.md')",
            "write_file('/tmp/guard-target/SOUL.md', 'x')",
        )
        for code in shapes:
            result, kernel = _run_execute_code(code)
            assert PROTECTED in result, code
            kernel.assert_not_called()

    def test_ordinary_write_runs(self):
        result, kernel = _run_execute_code("Path('/tmp/guard-target/notes.md').write_text('x')")
        assert PROTECTED not in result
        kernel.assert_called_once()

    def test_hard_deny_path_is_refused(self):
        """A cell cannot write the Hermes config.yaml or a system path either."""
        result, kernel = _run_execute_code("open('/etc/hosts', 'w').write('x')")
        assert "sensitive system path" in result
        kernel.assert_not_called()


def test_both_vectors_take_the_file_gate_verdict(monkeypatch):
    """Drift guard: the command vectors call the file gate's check, not a copy of it."""
    import tools.file_tools_write_guards as write_guards

    seen = []

    def fake_check(paths, task_id="default"):
        seen.append((tuple(paths), task_id))
        return "REFUSED-BY-FILE-GATE"

    monkeypatch.setattr(write_guards, "_check_protected_instruction_write", fake_check)
    terminal_result, _env = _run_terminal("echo x > /tmp/guard-target/SOUL.md", "/tmp")
    code_result, _kernel = _run_execute_code("Path('/tmp/guard-target/SOUL.md').write_text('x')")

    assert "REFUSED-BY-FILE-GATE" in (terminal_result.get("error") or "")
    assert "REFUSED-BY-FILE-GATE" in (code_result or "")
    assert seen == [
        (("/tmp/guard-target/SOUL.md",), "default"),
        (("/tmp/guard-target/SOUL.md",), "t_guard"),
    ]
