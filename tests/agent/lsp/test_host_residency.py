"""HOST-wide residency ceiling: ``lsp.max_resident_servers`` bounds the HOST, not one profile.

Review direction on card t_83304682 (2026-10-26): "``max_clients`` is per service instance, but
servers are per-profile processes, so 10 profiles x 3 = 30 servers — it does not bound the thing
that was measured. Make the cap HOST-wide with LRU anyway: `lsp.max_resident_servers`, default 4,
across all profiles."  Measured on the host: 6-7 ``pyright-langserver`` processes, 10.8-13.3 GB RSS,
~2.4 cores.

The census is a directory every profile's service registers in (``agent.lsp.residency``), so these
tests drive several services with *different* profile names over ONE census directory — the shape a
fleet of profile processes has on the host — and assert the count is the host's, not any one
service's.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from agent.lsp import eventlog
from agent.lsp.manager import DEFAULT_MAX_RESIDENT_SERVERS, LSPService, _count_setting
from agent.lsp.residency import RESIDENT_DIR_ENV, HostResidency, default_resident_dir, process_state
from agent.lsp.servers import SERVERS, ServerContext, ServerDef, SpawnSpec
from agent.lsp.workspace import clear_cache

MOCK_SERVER = str(Path(__file__).parent / "_mock_lsp_server.py")


@pytest.fixture(autouse=True)
def _clear_caches():
    clear_cache()
    eventlog.reset_announce_caches()
    yield
    clear_cache()


@pytest.fixture(autouse=True)
def _isolated_census(tmp_path, monkeypatch):
    """A test must never read or evict the operator's real servers: pin the census at ``tmp_path``."""
    monkeypatch.setenv(RESIDENT_DIR_ENV, str(tmp_path / "host-census"))
    return tmp_path / "host-census"


def _make_repo(tmp_path: Path, name: str) -> Path:
    repo = tmp_path / name
    repo.mkdir()
    (repo / ".git").mkdir()
    (repo / "pyproject.toml").write_text("", encoding="utf-8")
    (repo / "x.py").write_text("print('hi')\n", encoding="utf-8")
    return repo


@pytest.fixture
def mock_pyright(monkeypatch, tmp_path):
    """Install the mock as ``pyright``; yield (spawn_count, set_multi_root)."""
    idx = next(i for i, s in enumerate(SERVERS) if s.server_id == "pyright")
    original = SERVERS[idx]
    spawns = {"value": 0}

    def _spawn(root: str, ctx: ServerContext) -> SpawnSpec:
        spawns["value"] += 1
        return SpawnSpec(
            command=[sys.executable, MOCK_SERVER],
            workspace_root=root,
            cwd=root,
            env={"MOCK_LSP_SCRIPT": "errors"},
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

    yield spawns, _install
    SERVERS[idx] = original


def _service(census: Path, profile: str, **kwargs) -> LSPService:
    kwargs.setdefault("max_roots", 0)
    kwargs.setdefault("max_clients", 0)
    return LSPService(
        enabled=True, wait_mode="document", wait_timeout=3.0, install_strategy="manual",
        resident_dir=str(census), profile=profile, **kwargs,
    )


def _wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def _write_entry(census: Path, **record) -> Path:
    census.mkdir(parents=True, exist_ok=True)
    path = census / f"{record['owner_pid']}-{record['pid']}-manual.json"
    path.write_text(json.dumps(record), encoding="utf-8")
    return path


# ── the host bound itself ────────────────────────────────────────────────────────────────


def test_five_profiles_one_root_each_keep_exactly_the_host_ceiling(
    tmp_path, mock_pyright, monkeypatch, _isolated_census
):
    """5 profiles x 1 root with a ceiling of 4: exactly 4 resident, and the evicted one is the
    HOST-wide LRU — a profile further down the line loses its server, not merely the newest."""
    _spawns, install = mock_pyright
    install(multi_root=False)
    services, pids = [], []
    try:
        for i in range(5):
            repo = _make_repo(tmp_path, f"profile-{i}-repo")
            svc = _service(_isolated_census, f"profile-{i}", max_resident_servers=4)
            services.append(svc)
            monkeypatch.chdir(str(repo))
            svc.get_diagnostics_sync(str(repo / "x.py"))
            assert len(svc._clients) == 1
            pids.append(next(iter(svc._clients.values())).pid)

        census = HostResidency(str(_isolated_census), profile="probe").census()
        assert len(census) == 4, "the ceiling is the HOST's, not one profile's"
        assert [entry.profile for entry in census] == [
            "profile-1", "profile-2", "profile-3", "profile-4",
        ], "the host-wide LRU (profile-0, first spawned, least recently used) was evicted"
        # Not just unregistered — the process is gone, which is what frees the RSS.
        assert _wait_until(lambda: process_state(pids[0])[0] == "gone"), pids[0]
        assert process_state(pids[4])[0] == "alive", "the newcomer keeps its server"
    finally:
        for svc in services:
            svc.shutdown()


def test_recency_is_host_wide_not_per_profile(tmp_path, mock_pyright, monkeypatch, _isolated_census):
    """A profile that keeps writing stays resident; the idle profile's server is the victim."""
    _spawns, install = mock_pyright
    install(multi_root=False)
    services = []
    try:
        repos = [_make_repo(tmp_path, f"r{i}") for i in range(3)]
        for i, repo in enumerate(repos[:2]):
            svc = _service(_isolated_census, f"profile-{i}", max_resident_servers=2)
            services.append(svc)
            monkeypatch.chdir(str(repo))
            svc.get_diagnostics_sync(str(repo / "x.py"))
        # profile-1 is now the most recently used; profile-2's spawn must evict profile-0.
        svc2 = _service(_isolated_census, "profile-2", max_resident_servers=2)
        services.append(svc2)
        monkeypatch.chdir(str(repos[2]))
        svc2.get_diagnostics_sync(str(repos[2] / "x.py"))

        census = HostResidency(str(_isolated_census)).census()
        assert [entry.profile for entry in census] == ["profile-1", "profile-2"]
    finally:
        for svc in services:
            svc.shutdown()


def test_one_service_over_the_host_ceiling_evicts_its_own_lru(
    tmp_path, mock_pyright, monkeypatch, _isolated_census
):
    """The ceiling holds inside a single profile too: 3 single-root servers, ceiling 2."""
    _spawns, install = mock_pyright
    install(multi_root=False)
    repos = [_make_repo(tmp_path, f"r{i}") for i in range(3)]
    svc = _service(_isolated_census, "solo", max_resident_servers=2)
    try:
        for repo in repos:
            monkeypatch.chdir(str(repo))
            svc.get_diagnostics_sync(str(repo / "x.py"))
        assert len(svc._clients) == 2
        assert ("pyright", str(repos[0])) not in svc._clients
        assert ("pyright", str(repos[2])) in svc._clients
        assert svc.get_status()["resident_count"] == 2
    finally:
        svc.shutdown()


def test_two_profiles_each_at_max_clients_three_still_hold_the_host_ceiling(
    tmp_path, mock_pyright, monkeypatch, _isolated_census
):
    """The LIVE shape: ``max_clients: 3`` per profile under a ceiling of 4, two profiles at once.

    This is the case that made the per-profile cap useless (6-7 ``pyright-langserver`` processes,
    10.8-13.3 GB RSS, measured on the host): 3 clients is legal for each profile on its own, so no
    service ever evicts itself, and only the HOST census can bind 2 x 3 = 6 spawns down to 4.  The
    two victims are the host-wide LRU — both of them the first profile's — and their processes are
    gone, not merely unregistered, which is what actually frees the RSS.
    """
    _spawns, install = mock_pyright
    install(multi_root=False)
    services = []
    try:
        repos = {
            "profile-a": [_make_repo(tmp_path, f"a{i}") for i in range(3)],
            "profile-b": [_make_repo(tmp_path, f"b{i}") for i in range(3)],
        }
        pids: dict[str, int] = {}
        for profile, profile_repos in repos.items():
            svc = _service(_isolated_census, profile, max_clients=3, max_resident_servers=4)
            services.append(svc)
            for repo in profile_repos:
                monkeypatch.chdir(str(repo))
                svc.get_diagnostics_sync(str(repo / "x.py"))
                client = svc._clients[("pyright", str(repo))]
                assert client.pid is not None
                pids[str(repo)] = int(client.pid)
            assert len(svc._clients) == 3, "3 clients is AT the per-profile cap, so it never fires"

        assert _spawns["value"] == 6
        census = HostResidency(str(_isolated_census)).census()
        assert len(census) == 4, "2 profiles x 3 clients is 6 spawns; the HOST's 4 is the bound"
        assert [entry.profile for entry in census] == [
            "profile-a", "profile-b", "profile-b", "profile-b",
        ], "the two victims are the host-wide LRU, and both belong to the first profile"
        assert {entry.workspace_root for entry in census} == {
            str(repos["profile-a"][2]), str(repos["profile-b"][0]),
            str(repos["profile-b"][1]), str(repos["profile-b"][2]),
        }
        for evicted in repos["profile-a"][:2]:
            assert _wait_until(
                lambda path=str(evicted): process_state(pids[path])[0] == "gone"
            ), evicted
        for kept in (repos["profile-a"][2], *repos["profile-b"]):
            assert process_state(pids[str(kept)])[0] == "alive", kept
    finally:
        for svc in services:
            svc.shutdown()


def test_a_profile_at_its_own_client_cap_evicts_locally_not_a_foreign_server(
    tmp_path, mock_pyright, monkeypatch, _isolated_census
):
    """Composition: when both caps can bind one spawn, the profile's own LRU goes first.

    A profile at ``max_clients`` 2 pays for its own 3rd root: its local cap fires and shuts down
    its own least-recently-used client, which leaves the host AT the ceiling, so the host bound
    finds nothing to reach for.  The other profile's servers stay alive — evicting a stranger to
    spare one's own stale client would be exactly the unfairness the host-wide LRU prevents.
    """
    _spawns, install = mock_pyright
    install(multi_root=False)
    services = []
    try:
        repos = {
            "profile-a": [_make_repo(tmp_path, f"ca{i}") for i in range(2)],
            "profile-b": [_make_repo(tmp_path, f"cb{i}") for i in range(3)],
        }
        pids: dict[str, int] = {}
        svc_a = _service(_isolated_census, "profile-a", max_clients=2, max_resident_servers=4)
        svc_b = _service(_isolated_census, "profile-b", max_clients=2, max_resident_servers=4)
        services.extend([svc_a, svc_b])
        for svc, profile in ((svc_a, "profile-a"), (svc_b, "profile-b")):
            for repo in repos[profile]:
                monkeypatch.chdir(str(repo))
                svc.get_diagnostics_sync(str(repo / "x.py"))
                client = svc._clients[("pyright", str(repo))]
                assert client.pid is not None
                pids[str(repo)] = int(client.pid)

        assert len(svc_b._clients) == 2, "the local cap evicted its own LRU, not a foreign server"
        census = HostResidency(str(_isolated_census)).census()
        assert [entry.profile for entry in census] == [
            "profile-a", "profile-a", "profile-b", "profile-b",
        ]
        assert {entry.workspace_root for entry in census} == {
            str(repos["profile-a"][0]), str(repos["profile-a"][1]),
            str(repos["profile-b"][1]), str(repos["profile-b"][2]),
        }
        assert _wait_until(
            lambda: process_state(pids[str(repos["profile-b"][0])])[0] == "gone"
        ), "the profile's own local victim is shut down"
        for kept in (*repos["profile-a"], repos["profile-b"][1], repos["profile-b"][2]):
            assert process_state(pids[str(kept)])[0] == "alive", kept
    finally:
        for svc in services:
            svc.shutdown()


def test_zero_disables_the_host_ceiling(tmp_path, mock_pyright, monkeypatch, _isolated_census):
    _spawns, install = mock_pyright
    install(multi_root=False)
    services = []
    try:
        for i in range(5):
            repo = _make_repo(tmp_path, f"off-{i}")
            svc = _service(_isolated_census, f"profile-{i}", max_resident_servers=0)
            services.append(svc)
            assert svc._residency is None
            monkeypatch.chdir(str(repo))
            svc.get_diagnostics_sync(str(repo / "x.py"))
        assert HostResidency(str(_isolated_census)).census() == [], "0 registers nothing"
        assert len(services) == 5 and all(len(s._clients) == 1 for s in services)
    finally:
        for svc in services:
            svc.shutdown()


# ── the published bound ──────────────────────────────────────────────────────────────────


def test_default_is_four_and_status_publishes_the_host_census(
    tmp_path, mock_pyright, monkeypatch, _isolated_census
):
    """``hermes lsp status --json`` must publish the ceiling AND every profile's servers: the
    status process owns no servers, so its own ``clients`` list is always empty."""
    assert DEFAULT_MAX_RESIDENT_SERVERS == 4
    _spawns, install = mock_pyright
    install(multi_root=False)
    repos = [_make_repo(tmp_path, f"pub-{i}") for i in range(2)]
    services = []
    try:
        for i, repo in enumerate(repos):
            svc = _service(_isolated_census, f"profile-{i}")
            services.append(svc)
            monkeypatch.chdir(str(repo))
            svc.get_diagnostics_sync(str(repo / "x.py"))

        # A different process (here: the read-only reader the CLI builds) sees the host's servers.
        status = LSPService(
            enabled=False, wait_mode="document", wait_timeout=1.0, install_strategy="manual",
            resident_dir=str(_isolated_census), profile="status-probe",
        ).get_status()
        assert status["clients"] == []
        assert status["max_resident_servers"] == 4
        assert status["resident_count"] == 2
        assert [row["profile"] for row in status["resident_servers"]] == ["profile-0", "profile-1"]
        assert all(row["pid"] > 0 and row["workspace_root"] for row in status["resident_servers"])
        json.dumps(status)  # the CLI writes it verbatim

        own = services[0].get_status()
        assert own["resident_count"] == 2, "a profile's status reports the HOST's set, not its own"
    finally:
        for svc in services:
            svc.shutdown()


def test_ceiling_comes_from_config(tmp_path, monkeypatch, _isolated_census):
    """``lsp.max_resident_servers`` drives the ctor; a missing or junk key keeps the bound on."""
    def _from(lsp_cfg: dict) -> LSPService:
        with patch("hermes_cli.config.load_config_readonly", return_value={"lsp": lsp_cfg}):
            svc = LSPService.create_from_config()
        assert svc is not None
        return svc

    assert _from({"wait_timeout": 1.0}).get_status()["max_resident_servers"] == 4
    assert _from({"max_resident_servers": 6}).get_status()["max_resident_servers"] == 6
    assert _from({"max_resident_servers": "2"}).get_status()["max_resident_servers"] == 2
    assert _from({"max_resident_servers": 0}).get_status()["max_resident_servers"] == 0
    assert _from({"max_resident_servers": "junk"}).get_status()["max_resident_servers"] == 4
    assert _count_setting(None, DEFAULT_MAX_RESIDENT_SERVERS) == DEFAULT_MAX_RESIDENT_SERVERS


def test_default_census_dir_is_host_wide(tmp_path, monkeypatch):
    """The default directory is the HOST's root, not ``HERMES_HOME``: every profile must land in it."""
    monkeypatch.delenv(RESIDENT_DIR_ENV, raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profiles" / "platform-coder"))
    directory = Path(default_resident_dir())
    assert directory.parts[-2:] == ("run", "lsp-resident")
    assert "profiles" not in directory.parts, "a per-profile home would give every profile its own bound"


# ── stale entries: the leak this card measured ──────────────────────────────────────────


def test_census_prunes_a_dead_server_pid(_isolated_census):
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    _write_entry(_isolated_census, server_id="pyright", workspace_root="/gone", profile="p",
                 pid=dead.pid, owner_pid=os.getpid(), create_time=0.0, owner_create_time=0.0,
                 started=time.time())
    assert HostResidency(str(_isolated_census)).census() == []
    assert list(_isolated_census.iterdir()) == [], "the stale entry is removed, not just ignored"


def test_census_reaps_a_server_whose_owner_died(_isolated_census):
    """A SIGKILLed gateway leaves ``start_new_session`` servers alive, holding a host slot forever."""
    owner = subprocess.Popen([sys.executable, "-c", "pass"])
    owner.wait()
    orphan = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        _write_entry(_isolated_census, server_id="pyright", workspace_root="/orphan", profile="dead",
                     pid=orphan.pid, owner_pid=owner.pid,
                     create_time=process_state(orphan.pid)[1], owner_create_time=0.0,
                     started=time.time())
        assert HostResidency(str(_isolated_census)).census() == []
        assert _wait_until(lambda: orphan.poll() is not None), "the orphaned server is terminated"
    finally:
        if orphan.poll() is None:
            orphan.kill()
            orphan.wait()


def test_an_entry_for_a_recycled_pid_is_pruned_not_counted(_isolated_census):
    """A recorded ``create_time`` that no longer matches the pid means the pid was recycled."""
    live = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        _write_entry(_isolated_census, server_id="pyright", workspace_root="/recycled", profile="p",
                     pid=live.pid, owner_pid=os.getpid(),
                     create_time=process_state(live.pid)[1] - 600.0, owner_create_time=0.0,
                     started=time.time())
        assert HostResidency(str(_isolated_census)).census() == []
        assert live.poll() is None, "a recycled pid is never signalled"
    finally:
        live.kill()
        live.wait()


def test_victims_never_include_the_newcomer(_isolated_census):
    """Enforcement must not evict the client whose spawn triggered it, even at the LRU end."""
    live = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        created = process_state(live.pid)[1]
        for token in ("old", "mid"):
            record = {"server_id": "pyright", "workspace_root": f"/{token}", "profile": "p",
                      "pid": live.pid, "owner_pid": os.getpid(), "create_time": created,
                      "owner_create_time": 0.0, "started": time.time()}
            _write_entry(_isolated_census, **record)
            os.rename(
                _isolated_census / f"{os.getpid()}-{live.pid}-manual.json",
                _isolated_census / f"{token}.json",
            )
        time.sleep(0.05)
        residency = HostResidency(str(_isolated_census))
        assert [entry.token for entry in residency.victims(1)] == ["old"]
        skipped = residency.victims(1, keep="old")
        assert [entry.token for entry in skipped] == ["mid"], "the kept entry is never a victim"
        assert residency.victims(2, keep="old") == [], "nothing to do when within the ceiling"
    finally:
        live.kill()
        live.wait()
