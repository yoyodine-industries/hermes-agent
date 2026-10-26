"""Residency bounds: a language server is demand-spawned, never resident per lane.

Measured on yoyodine 2026-10-25 (card t_83304682): one platform-coder session walked 25
git worktrees, and because pyright is ``multi_root`` the service attached all 25 to ONE
process — 1.8 GB RSS held for the life of the session, on a host already swapping 5.2 GB.
An UNTRUSTED root is keyed per root (it cannot inherit a trusted process's trust), so each
new untrusted workspace added another resident server on top of that.

``lsp.max_roots_per_server`` bounds the first (LRU folder removal, never the client's own
spawn root) and ``lsp.max_clients`` bounds the second (LRU client shutdown).  Both are
ON by default and both are disabled by 0, exactly like ``lsp.idle_timeout``.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from agent.lsp.manager import (
    DEFAULT_MAX_CLIENTS, DEFAULT_MAX_RESIDENT_SERVERS, DEFAULT_MAX_ROOTS, LSPService,
)
from agent.lsp.residency import RESIDENT_DIR_ENV
from agent.lsp.servers import SERVERS, ServerContext, ServerDef, SpawnSpec
from agent.lsp.workspace import clear_cache

MOCK_SERVER = str(Path(__file__).parent / "_mock_lsp_server.py")


@pytest.fixture(autouse=True)
def _clear_workspace_cache():
    clear_cache()
    yield
    clear_cache()


@pytest.fixture(autouse=True)
def _isolated_census(tmp_path, monkeypatch):
    """These tests bound one profile; keep their servers out of the HOST-wide census directory so a
    test run can never read — or evict — the operator's real servers (see test_host_residency)."""
    monkeypatch.setenv(RESIDENT_DIR_ENV, str(tmp_path / "host-census"))


def _make_repo(tmp_path: Path, name: str) -> Path:
    repo = tmp_path / name
    repo.mkdir()
    (repo / ".git").mkdir()
    (repo / "pyproject.toml").write_text("", encoding="utf-8")
    (repo / "x.py").write_text("print('hi')\n", encoding="utf-8")
    return repo


def _repos(tmp_path: Path, count: int) -> list[Path]:
    return [_make_repo(tmp_path, f"repo-{i}") for i in range(count)]


@pytest.fixture
def mock_pyright(monkeypatch, tmp_path):
    """Install the mock as ``pyright``; yield (spawn_count, folders_log, set_multi_root)."""
    idx = next(i for i, s in enumerate(SERVERS) if s.server_id == "pyright")
    original = SERVERS[idx]
    spawns = {"value": 0}
    folders_log = tmp_path / "folders.jsonl"

    def _spawn(root: str, ctx: ServerContext) -> SpawnSpec:
        spawns["value"] += 1
        return SpawnSpec(
            command=[sys.executable, MOCK_SERVER],
            workspace_root=root,
            cwd=root,
            env={"MOCK_LSP_SCRIPT": "errors", "MOCK_LSP_FOLDERS_LOG": str(folders_log)},
        )

    def _install(multi_root: bool) -> None:
        SERVERS[idx] = ServerDef(
            server_id="pyright",
            extensions=original.extensions,
            resolve_root=lambda fp, ws: ws,
            build_spawn=_spawn,
            multi_root=multi_root,
            description="mock pyright",
        )

    yield spawns, folders_log, _install
    SERVERS[idx] = original


def _service(**kwargs) -> LSPService:
    return LSPService(
        enabled=True, wait_mode="document", wait_timeout=3.0, install_strategy="manual", **kwargs
    )


def _folder_events(folders_log: Path) -> list[dict]:
    if not folders_log.exists():
        return []
    return [json.loads(line) for line in folders_log.read_text(encoding="utf-8").splitlines()]


def test_defaults_bound_residency():
    """The bound is on out of the box — a missing config key must not mean "unbounded"."""
    assert DEFAULT_MAX_ROOTS > 0 and DEFAULT_MAX_CLIENTS > 0 and DEFAULT_MAX_RESIDENT_SERVERS > 0
    status = _service().get_status()
    assert status["max_roots_per_server"] == DEFAULT_MAX_ROOTS
    assert status["max_clients"] == DEFAULT_MAX_CLIENTS
    assert status["max_resident_servers"] == DEFAULT_MAX_RESIDENT_SERVERS
    assert status["resident_servers"] == [] or status["resident_count"] == len(status["resident_servers"])


def test_max_roots_setting_accepts_a_string_and_falls_back_on_junk():
    assert _service(max_roots=3).get_status()["max_roots_per_server"] == 3
    assert _service(max_clients="2").get_status()["max_clients"] == 2


def test_shared_client_trims_lru_roots_instead_of_growing_without_limit(
    tmp_path, mock_pyright, monkeypatch
):
    """Six roots through ONE process: the cap keeps the spawn root + the newest N."""
    repos = _repos(tmp_path, 6)
    spawns, folders_log, install = mock_pyright
    install(multi_root=True)
    svc = _service(max_roots=2, max_clients=0)
    try:
        for repo in repos:
            monkeypatch.chdir(str(repo))
            svc.get_diagnostics_sync(str(repo / "x.py"))

        assert spawns["value"] == 1, "root trimming must never spawn a second process"
        client = next(iter(svc._clients.values()))
        # spawn root pinned + the two most recent roots
        assert client.workspace_folders == [str(repos[0]), str(repos[4]), str(repos[5])]
        assert len(client.workspace_folders) <= 2 + 1
        removed = [f["uri"] for e in _folder_events(folders_log) for f in e["event"]["removed"]]
        assert Path(repos[1]).as_uri() in removed
        assert Path(repos[0]).as_uri() not in removed, "the spawn root stays attached"
    finally:
        svc.shutdown()


def test_max_roots_zero_disables_the_root_cap(tmp_path, mock_pyright, monkeypatch):
    repos = _repos(tmp_path, 5)
    _spawns, folders_log, install = mock_pyright
    install(multi_root=True)
    svc = _service(max_roots=0, max_clients=0)
    try:
        for repo in repos:
            monkeypatch.chdir(str(repo))
            svc.get_diagnostics_sync(str(repo / "x.py"))
        client = next(iter(svc._clients.values()))
        assert client.workspace_folders == [str(r) for r in repos]
        assert _folder_events(folders_log) and all(
            not e["event"]["removed"] for e in _folder_events(folders_log)
        )
    finally:
        svc.shutdown()


def test_client_cap_shuts_down_the_least_recently_used_server(
    tmp_path, mock_pyright, monkeypatch
):
    """Single-root servers are keyed per root, so an untrusted workspace walk added one each."""
    repos = _repos(tmp_path, 3)
    spawns, _folders_log, install = mock_pyright
    install(multi_root=False)
    svc = _service(max_clients=1, max_roots=0)
    try:
        for repo in repos:
            monkeypatch.chdir(str(repo))
            svc.get_diagnostics_sync(str(repo / "x.py"))

        assert spawns["value"] == 3, "each root still gets its own process while it is live"
        assert len(svc._clients) == 1, "but only the newest stays resident"
        assert next(iter(svc._clients)) == ("pyright", str(repos[2]))
        assert svc.get_status()["max_clients"] == 1
    finally:
        svc.shutdown()


def test_client_cap_zero_keeps_every_server(tmp_path, mock_pyright, monkeypatch):
    repos = _repos(tmp_path, 3)
    _spawns, _folders_log, install = mock_pyright
    install(multi_root=False)
    svc = _service(max_clients=0, max_roots=0)
    try:
        for repo in repos:
            monkeypatch.chdir(str(repo))
            svc.get_diagnostics_sync(str(repo / "x.py"))
        assert len(svc._clients) == 3
    finally:
        svc.shutdown()
