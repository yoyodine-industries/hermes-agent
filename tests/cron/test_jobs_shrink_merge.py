"""Concurrent-safe store writes — the CAS/named-removal contract (#80624 -> t_60afef80).

``no_agent`` watchdog jobs (and any CLI/tool create while the gateway ticker or a
``cron remove`` is live) must not vanish from ``jobs.json`` when a writer persists a
stale/smaller in-memory snapshot.

The original fix MERGED unexpected on-disk ids back into the payload. The guard now
REFUSES the write instead (rule 2 of cron/jobs.py's write guard): a payload may only
drop an id it explicitly named. Both mechanisms end with the concurrent row on disk —
the refusal is louder and, unlike the merge, it also catches the incident shape that
started this (t_14655d0c: a stale whole-file rewrite deleted 33 rows and nothing
noticed for ~10 hours).

These tests were rewritten from merge-succeeds to write-refused; each keeps the original
intent (the concurrent row survives) and asserts the stronger guarantee.
"""

from __future__ import annotations

import errno
import json
import os

import pytest


@pytest.fixture
def hermes_env(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "scripts").mkdir()
    (home / "cron").mkdir()
    (home / "scripts" / "watch.sh").write_text("#!/usr/bin/env bash\necho alert\n")
    monkeypatch.setenv("HERMES_HOME", str(home))

    import importlib
    import hermes_constants
    import cron.jobs

    importlib.reload(hermes_constants)
    importlib.reload(cron.jobs)

    # Never touch a real board from a test: the alert card is the second removal sink.
    cards: list[dict] = []
    monkeypatch.setattr(
        cron.jobs, "_file_removal_card",
        lambda **kw: cards.append(kw) or f"t_fake{len(cards)}",
    )
    return home


def test_stale_empty_save_is_refused_not_applied(hermes_env):
    """Gateway-style stale writer with [] must not wipe a concurrent create."""
    from cron.jobs import UnnamedRemovalRefused, create_job, load_jobs, save_jobs, store_generation

    job = create_job(
        prompt=None,
        schedule="every 2m",
        script="watch.sh",
        no_agent=True,
        deliver="local",
        name="watchdog",
        repeat=0,
    )
    assert [j["id"] for j in load_jobs()] == [job["id"]]
    before = store_generation()

    # Simulate a degraded-lock writer that still holds an empty snapshot from
    # before the create (the filed incident: jobs.json rewritten empty).
    with pytest.raises(UnnamedRemovalRefused) as exc:
        save_jobs([])
    assert job["id"] in str(exc.value)
    assert exc.value.code == "unnamed-removal"

    assert store_generation() == before, "a refused write must not touch the store"
    remaining = load_jobs()
    assert [j["id"] for j in remaining] == [job["id"]]
    assert remaining[0].get("no_agent") is True
    assert remaining[0].get("script") == "watch.sh"


def test_remove_other_job_preserves_concurrent_create(hermes_env):
    """``cron remove`` of job A must not drop job B created mid-flight."""
    from cron.jobs import UnnamedRemovalRefused, create_job, load_jobs, save_jobs

    agent = create_job(
        prompt="hello",
        schedule="every 5m",
        name="agent",
        deliver="local",
    )
    # Stale remove payload: only knew about `agent`, never saw the watchdog.
    save_jobs([], removed_ids={agent["id"]})

    watchdog = create_job(
        prompt=None,
        schedule="every 2m",
        script="watch.sh",
        no_agent=True,
        deliver="local",
        name="watchdog",
        repeat=0,
    )
    # A second stale remove of `agent` (already gone) with an empty payload — the
    # watchdog that landed on disk in between is NOT in the payload and was NOT named:
    # refused, and the watchdog stays.
    with pytest.raises(UnnamedRemovalRefused) as exc:
        save_jobs([], removed_ids={agent["id"]})
    assert exc.value.detail["unnamed_removals"] == [watchdog["id"]]

    ids = {j["id"] for j in load_jobs()}
    assert ids == {watchdog["id"]}


def test_intentional_remove_still_deletes(hermes_env):
    from cron.jobs import create_job, get_job, remove_job

    job = create_job(
        prompt=None,
        schedule="every 2m",
        script="watch.sh",
        no_agent=True,
        deliver="local",
        name="watchdog",
        repeat=0,
    )
    assert remove_job(job["id"]) is True
    assert get_job(job["id"]) is None


def test_replace_flag_is_retired(hermes_env):
    """There is no whole-file replace API: the tombstone refuses and the store is untouched."""
    from cron.jobs import StoreWriteRefused, create_job, load_jobs, save_jobs, store_generation

    create_job(
        prompt=None,
        schedule="every 2m",
        script="watch.sh",
        no_agent=True,
        deliver="local",
        name="watchdog",
        repeat=0,
    )
    before = store_generation()
    with pytest.raises(StoreWriteRefused) as exc:
        save_jobs([], replace=True)
    assert exc.value.detail["refused"] == "replace"
    assert store_generation() == before
    assert len(load_jobs()) == 1


def test_sibling_write_inside_section_is_refused(hermes_env):
    """A write that lands on disk after the section's load changes the generation, so the
    save must refuse instead of trusting its stale snapshot."""
    from cron import jobs
    from cron.jobs import StoreGenerationMismatch, create_job

    job = create_job(
        prompt=None,
        schedule="every 2m",
        script="watch.sh",
        no_agent=True,
        deliver="local",
        name="watchdog",
        repeat=0,
    )

    with jobs._jobs_lock():
        current = jobs.load_jobs()
        (jobs._current_cron_store().jobs_file).write_text(
            json.dumps({"jobs": [dict(job), {"id": "bbbbbbbbbbbb", "name": "b"}]}),
            encoding="utf-8",
        )
        with pytest.raises(StoreGenerationMismatch) as exc:
            jobs._save_jobs_unlocked(current)
        assert "bbbbbbbbbbbb" in exc.value.detail["ids_added_since_read"]
    ids = {j["id"] for j in jobs.load_jobs()}
    assert ids == {job["id"], "bbbbbbbbbbbb"}


def test_unnamed_removal_does_not_mutate_caller_list(hermes_env):
    """A refused save must not grow or otherwise edit the caller's payload object."""
    from cron.jobs import UnnamedRemovalRefused, create_job, load_jobs, save_jobs

    create_job(
        prompt=None,
        schedule="every 2m",
        script="watch.sh",
        no_agent=True,
        deliver="local",
        name="watchdog",
        repeat=0,
    )
    my_payload = []  # stale snapshot from before the create
    with pytest.raises(UnnamedRemovalRefused):
        save_jobs(my_payload)
    assert my_payload == [], "caller's list was mutated in place"
    assert len(load_jobs()) == 1


def test_save_over_corrupt_store_fails_closed(hermes_env):
    """A write over an unreadable jobs.json must refuse (the jobs in it are unknown, so
    overwriting could drop them) and leave the bytes intact.

    There is no ``replace=True`` escape hatch any more: recovery of an unparseable store is
    an out-of-band file act, not an in-process write (t_60afef80 rule 2)."""
    from cron import jobs
    from cron.jobs import StoreWriteRefused, save_jobs

    jobs.ensure_dirs()
    jobs_file = jobs._current_cron_store().jobs_file
    corrupt = b'{"jobs": [{"id": "ccc'
    jobs_file.write_bytes(corrupt)
    with pytest.raises(StoreWriteRefused, match="unparseable"):
        save_jobs([{"id": "aaaaaaaaaaaa", "name": "a"}])
    assert jobs_file.read_bytes() == corrupt
    with pytest.raises(StoreWriteRefused):
        save_jobs([{"id": "aaaaaaaaaaaa", "name": "a"}], replace=True)
    assert jobs_file.read_bytes() == corrupt


def test_nested_create_survives_outer_stale_save(hermes_env):
    """A save inside a critical section invalidates the section's generation, so an outer
    caller's later save with a pre-create payload must refuse — the nested create survives
    because nothing writes.

    Which rule catches it depends on the section: the nested save invalidates the recorded
    generation (fail-safe), so the outer save presents none and rule 2 refuses the unnamed
    drop. Either way the write is refused and the store is untouched."""
    from cron import jobs
    from cron.jobs import StoreWriteRefused, create_job, load_jobs, save_jobs

    seed = {"id": "aaaaaaaaaaaa", "name": "a"}
    save_jobs([seed])

    with jobs._jobs_lock():
        stale = jobs.load_jobs()  # records the generation
        created = create_job(
            prompt="x", schedule="every 5m", name="nested", deliver="local"
        )
        with pytest.raises(StoreWriteRefused) as exc:
            jobs._save_jobs_unlocked(stale)  # outer save with pre-create payload
        assert exc.value.code in {"generation-mismatch", "unnamed-removal"}
        assert created["id"] in str(exc.value.detail)

    ids = {j["id"] for j in load_jobs()}
    assert created["id"] in ids, "nested create was clobbered by outer stale save"
    assert seed["id"] in ids


def test_symlinked_store_saves_by_rename_not_in_place_copy(hermes_env, tmp_path, monkeypatch):
    """jobs.json symlinked into another dir (treated as another filesystem) must still be
    published by an atomic rename — never the EXDEV in-place copy fallback, which tears the
    store on a crash mid-copy."""
    from cron.jobs import load_jobs, save_jobs

    real_dir = tmp_path / "elsewhere"
    real_dir.mkdir()
    (real_dir / "jobs.json").write_text('{"jobs": []}')
    link = hermes_env / "cron" / "jobs.json"
    link.symlink_to(real_dir / "jobs.json")

    real_replace = os.replace

    def replace_same_dir_only(src, dst):
        if os.path.dirname(os.path.realpath(src)) != os.path.dirname(os.path.realpath(dst)):
            raise OSError(errno.EXDEV, os.strerror(errno.EXDEV))
        return real_replace(src, dst)

    def no_copy(*a, **k):
        raise AssertionError("in-place copy fallback used")

    monkeypatch.setattr("utils.files.os.replace", replace_same_dir_only)
    monkeypatch.setattr("utils.files._copy_fallback", no_copy)

    # Empty store -> one addition: no removal, so the guarded path writes happily.
    save_jobs([{"id": "a", "prompt": "x"}])

    assert link.is_symlink()
    assert [j["id"] for j in load_jobs()] == ["a"]
