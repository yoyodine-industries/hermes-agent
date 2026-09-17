"""Terminal identity-file write gate: write-target extraction + always-ask integration.

Covers the terminal surface's best-effort identity-file gate: a bot must not be able
to rewrite SOUL.md / *-soul.md via ``>``/``>>``, ``tee``, ``cp``/``mv``/``install`` or
``sed``/``perl``/``ruby -i`` without an always-ask (never auto-approved) consent.
"""

from tools.approval_detection import iter_write_target_paths
from tools.file_tools_write_guards import _protected_instruction_reason


def _paths(command: str) -> list[str]:
    return list(iter_write_target_paths(command))


class TestWriteTargetExtraction:
    """iter_write_target_paths yields the literal paths a command may write."""

    def test_redirect_target(self):
        assert _paths("echo x > ~/.hermes/SOUL.md") == ["~/.hermes/SOUL.md"]

    def test_append_redirect(self):
        assert _paths("printf x >> /tmp/notes-soul.md") == ["/tmp/notes-soul.md"]

    def test_fd_redirect_target(self):
        assert _paths("echo x 2> /tmp/SOUL.md") == ["/tmp/SOUL.md"]

    def test_quoted_redirect_target(self):
        assert _paths('echo x > "~/.hermes/SOUL.md"') == ["~/.hermes/SOUL.md"]

    def test_tee_operands(self):
        assert _paths("echo x | tee -a ~/.hermes/profiles/bot/SOUL.md") == [
            "~/.hermes/profiles/bot/SOUL.md"
        ]

    def test_cp_destination(self):
        assert _paths("cp /tmp/x ~/.hermes/SOUL.md") == ["~/.hermes/SOUL.md"]

    def test_mv_destination(self):
        assert _paths("mv a b SOUL.md") == ["SOUL.md"]

    def test_install_destination(self):
        assert _paths("install -m 600 x soul.md") == ["soul.md"]

    def test_sed_inplace(self):
        assert _paths("sed -i 's/x/y/' SOUL.md") == ["SOUL.md"]

    def test_perl_inplace(self):
        assert _paths("perl -i.bak -pe 's/x/y/' SOUL.md") == ["SOUL.md"]

    def test_fd_dup_is_not_a_write(self):
        assert _paths("echo x 2>&1") == []

    def test_input_redirect_is_not_a_write(self):
        assert _paths("cat < SOUL.md") == []

    def test_no_write_shape(self):
        assert _paths("echo hello") == []

    def test_non_literal_target_fails_open(self):
        # A variable target is extracted but never resolves to an identity match.
        assert _paths("echo x > $F") == ["$F"]


class TestIdentityOnlyMatcher:
    """identity_only=True keeps SOUL.md / *-soul.md but drops project-context files."""

    def test_soul_md_is_identity(self, tmp_path):
        p = tmp_path / "SOUL.md"
        assert _protected_instruction_reason(str(p), identity_only=True) is not None

    def test_extra_pattern_soul(self, tmp_path):
        p = tmp_path / "mybot-soul.md"
        reason = _protected_instruction_reason(str(p), identity_only=True,
                                               enabled=True,
                                               extra_patterns=["*-soul.md"],
                                               allowlist_dirs=[])
        assert reason is not None

    def test_agents_md_is_not_identity(self, tmp_path):
        p = tmp_path / "AGENTS.md"
        assert _protected_instruction_reason(str(p), identity_only=True) is None

    def test_identity_not_exempted_by_allowlist(self, tmp_path):
        trusted = tmp_path / "trusted"
        trusted.mkdir()
        p = trusted / "SOUL.md"
        reason = _protected_instruction_reason(str(p), identity_only=True,
                                               enabled=True,
                                               extra_patterns=[],
                                               allowlist_dirs=[str(trusted)])
        assert reason is not None


class TestTerminalIdentityGateIntegration:
    """A headless terminal write to an identity path is blocked; non-identity is not."""

    def test_identity_write_blocked_headless(self, tmp_path):
        from tools.approval import _check_terminal_identity_write

        target = tmp_path / "SOUL.md"
        result = _check_terminal_identity_write(f"echo hacked > {target}")
        assert result is not None
        assert result.get("approved") is False
        assert "BLOCKED" in result.get("message", "")

    def test_identity_write_blocked_via_tee(self, tmp_path):
        from tools.approval import _check_terminal_identity_write

        target = tmp_path / "SOUL.md"
        result = _check_terminal_identity_write(f"echo hacked | tee {target}")
        assert result is not None
        assert result.get("approved") is False

    def test_non_identity_write_not_gated(self, tmp_path):
        from tools.approval import _check_terminal_identity_write

        target = tmp_path / "notes.txt"
        assert _check_terminal_identity_write(f"echo hi > {target}") is None

    def test_plain_command_not_gated(self):
        from tools.approval import _check_terminal_identity_write

        assert _check_terminal_identity_write("echo hello") is None
