"""The store write guard — each test is a reproduction of the t_14655d0c incident.

On the live default store a stale-snapshot whole-file rewrite deleted 33 rows and
NOTHING noticed for ~10 hours. The defect was not the bad write: it was that a write
could remove a third of the store and the system had no opinion. These tests pin the
four rules of ``cron/jobs.py``'s write guard against that shape:

1. CAS on a store generation - a snapshot-derived write dies at the door.
2. Removals must be named - an unnamed drop is refused, no force flag.
3. Post-write proof from the store - a write that cannot prove itself did not happen.
4. Removal is an event - audit row + alert card, on every removal, from one row up.
"""

from __future__ import annotations

import json

import pytest


def _row(i: int) -> dict:
    return {
        "id": f"job{i:08d}",
        "name": f"job{i}",
        "prompt": f"prompt {i}",
        "schedule": "every 5m",
        "deliver": "local",
        "enabled": True,
        "created_at": "2026-11-01T00:00:00-05:00",
        "next_run": "2026-11-23T05:00:00-05:00",
    }


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "cron").mkdir()
    (home / "scripts").mkdir()
    (home / "scripts" / "watch.sh").write_text("#!/usr/bin/env bash\necho hi\n")
    monkeypatch.setenv("HERMES_HOME", str(home))

    import importlib
    import hermes_constants
    import cron.jobs

    importlib.reload(hermes_constants)
    importlib.reload(cron.jobs)

    state = {"cards": [], "proof_cards": []}
    monkeypatch.setattr(
        cron.jobs, "_file_removal_card",
        lambda **kw: state["cards"].append(kw) or f"t_card{len(state['cards'])}",
    )
    monkeypatch.setattr(
        cron.jobs, "_file_proof_card",
        lambda **kw: state["proof_cards"].append(kw) or f"t_proof{len(state['proof_cards'])}",
    )
    return cron.jobs, home, state


def _seed(jobs_mod, rows) -> None:
    """Write the store as it was at some earlier moment (file level, like a snapshot)."""
    jobs_mod.ensure_dirs()
    jobs_mod._current_cron_store().jobs_file.write_text(
        json.dumps({"jobs": list(rows)}, indent=2), encoding="utf-8"
    )


def _ids(rows) -> set[str]:
    return {r["id"] for r in rows}


def _audit(home) -> list[dict]:
    path = home / "cron" / "store-removals.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# --- rule 1: compare-and-swap on the generation -------------------------------------------


def test_write_built_from_a_ten_hour_old_snapshot_is_refused(env):
    """THE incident: a writer holding a 13-row snapshot from 10h ago writes it back over a
    44-row store. Refused, store byte-identical."""
    jobs, home, state = env

    _seed(jobs, [_row(i) for i in range(13)])
    snapshot_generation = jobs.store_generation()   # what the stale writer read
    stale_payload = jobs.load_jobs()                # the 13 rows it is still holding

    # 10 hours of other writers: 31 rows created since that read.
    for i in range(13, 44):
        with jobs._jobs_lock():
            current = jobs.load_jobs()
            jobs.save_jobs(current + [_row(i)])
    assert len(jobs.load_jobs()) == 44
    before = jobs._current_cron_store().jobs_file.read_bytes()

    with pytest.raises(jobs.StoreGenerationMismatch) as exc:
        jobs.save_jobs(stale_payload, expected_generation=snapshot_generation)
    assert exc.value.code == "generation-mismatch"
    assert exc.value.detail["presented"]["row_count"] == 13
    assert exc.value.detail["live"]["row_count"] == 44
    assert len(exc.value.detail["ids_added_since_read"]) == 31
    assert exc.value.detail["ids_gone_since_read"] == []

    assert jobs._current_cron_store().jobs_file.read_bytes() == before
    assert len(jobs.load_jobs()) == 44

    # Belt and braces: the same stale payload with NO generation cannot sneak through either -
    # rule 2 refuses the 31 unnamed removals.
    with pytest.raises(jobs.UnnamedRemovalRefused) as exc2:
        jobs.save_jobs(stale_payload)
    assert len(exc2.value.detail["unnamed_removals"]) == 31
    assert jobs._current_cron_store().jobs_file.read_bytes() == before
    assert state["cards"] == [], "a refused write must not announce a removal"


# --- rule 2: removals must be named -------------------------------------------------------


def test_omitting_five_rows_without_naming_them_is_refused(env):
    jobs, home, state = env
    _seed(jobs, [_row(i) for i in range(6)])
    before = jobs._current_cron_store().jobs_file.read_bytes()
    doomed = [_row(i)["id"] for i in range(1, 6)]

    payload = [r for r in jobs.load_jobs() if r["id"] not in doomed]
    with pytest.raises(jobs.UnnamedRemovalRefused) as exc:
        jobs.save_jobs(payload)
    assert sorted(exc.value.detail["unnamed_removals"]) == sorted(doomed)
    assert jobs._current_cron_store().jobs_file.read_bytes() == before
    assert len(jobs.load_jobs()) == 6
    assert state["cards"] == [] and _audit(home) == []


def test_naming_the_five_rows_succeeds_and_files_audit_and_card(env):
    jobs, home, state = env
    _seed(jobs, [_row(i) for i in range(6)])
    doomed = sorted(_row(i)["id"] for i in range(1, 6))

    with jobs._jobs_lock():
        payload = [r for r in jobs.load_jobs() if r["id"] not in doomed]
        jobs.save_jobs(payload, explicit_removals=doomed, reason="test: retire five")

    assert _ids(jobs.load_jobs()) == {_row(0)["id"]}

    audit = _audit(home)
    removal = [r for r in audit if r["event"] == "removal"]
    assert len(removal) == 1
    row = removal[0]
    assert row["ids_removed"] == doomed
    assert row["row_count_before"] == 6 and row["row_count_after"] == 1
    assert row["generation_before"]["row_count"] == 6
    assert row["generation_after"]["row_count"] == 1
    assert row["reason"] == "test: retire five"
    assert row["caller"]

    assert len(state["cards"]) == 1
    card = state["cards"][0]
    assert card["removed"] == doomed
    assert card["event"] == "removal"
    # the audit row records the alert it produced
    assert [r for r in audit if r["event"] == "removal-alert"][0]["card_id"] == "t_card1"


def test_one_named_removal_alone_is_an_event(env):
    """The threshold is intent, not size: removing a single row alerts."""
    jobs, home, state = env
    _seed(jobs, [_row(0), _row(1)])
    with jobs._jobs_lock():
        payload = [r for r in jobs.load_jobs() if r["id"] == _row(0)["id"]]
        jobs.save_jobs(payload, explicit_removals=[_row(1)["id"]], reason="operate: retire one")
    assert [r for r in _audit(home) if r["event"] == "removal"][0]["ids_removed"] == [_row(1)["id"]]
    assert len(state["cards"]) == 1


def test_unannounceable_removal_is_undone(env, monkeypatch):
    """Rule 4: if the audit row cannot be written, the pre-image is restored - a silent
    removal is impossible by construction."""
    jobs, home, state = env
    _seed(jobs, [_row(0), _row(1)])
    before = jobs._current_cron_store().jobs_file.read_bytes()

    def boom(*a, **k):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(jobs, "_append_removal_audit", boom)
    with jobs._jobs_lock():
        payload = [r for r in jobs.load_jobs() if r["id"] == _row(0)["id"]]
        with pytest.raises(jobs.StoreAlertFailed):
            jobs.save_jobs(payload, explicit_removals=[_row(1)["id"]], reason="nope")
    assert jobs._current_cron_store().jobs_file.read_bytes() == before
    assert len(jobs.load_jobs()) == 2


# --- rule 3: post-write proof -------------------------------------------------------------


def test_post_write_disagreement_leaves_store_untouched_and_files_a_card(env, monkeypatch):
    jobs, home, state = env
    _seed(jobs, [_row(0), _row(1)])
    before = jobs._current_cron_store().jobs_file.read_bytes()

    real = jobs._intent_violations
    calls = {"n": 0}

    def disagree_after_the_write(pre_ids, post_ids, payload_ids, named):
        calls["n"] += 1
        if calls["n"] == 1:            # staged-payload proof: honest
            return real(pre_ids, post_ids, payload_ids, named)
        return {"missing_additions": ["ghost-row"]}   # the store re-read disagrees

    monkeypatch.setattr(jobs, "_intent_violations", disagree_after_the_write)

    with jobs._jobs_lock():
        payload = jobs.load_jobs()
        payload[0]["schedule"] = "every 9m"
        payload.append(_row(99))
        with pytest.raises(jobs.StoreWriteProofFailed) as exc:
            jobs.save_jobs(payload)
    assert exc.value.detail["where"] == "store re-read"
    assert exc.value.detail["violations"] == {"missing_additions": ["ghost-row"]}
    assert jobs._current_cron_store().jobs_file.read_bytes() == before
    assert len(state["proof_cards"]) == 1
    assert state["proof_cards"][0]["violations"] == {"missing_additions": ["ghost-row"]}
    assert _audit(home) == [], "a write that proved nothing must not record a removal"


def test_naming_a_row_that_stays_in_the_payload_is_refused(env):
    """Rule 3's 'named removals are absent' holds by construction: a payload that both names
    and keeps an id is a contradiction, refused before the store is touched."""
    jobs, home, state = env
    _seed(jobs, [_row(0), _row(1)])
    before = jobs._current_cron_store().jobs_file.read_bytes()
    with jobs._jobs_lock():
        payload = jobs.load_jobs()
        with pytest.raises(jobs.StoreWriteRefused) as exc:
            jobs.save_jobs(payload, explicit_removals=[_row(0)["id"]])
    assert exc.value.detail["named_but_present"] == [_row(0)["id"]]
    assert jobs._current_cron_store().jobs_file.read_bytes() == before


# --- and normal life still works ----------------------------------------------------------


def test_single_row_schedule_edit_works_and_files_nothing(env):
    jobs, home, state = env
    created = jobs.create_job(prompt="hello", schedule="every 5m", name="one", deliver="local")
    updated = jobs.update_job(created["id"], {"schedule": "every 15m"})
    assert updated["schedule"]["display"] == "every 15m"
    assert jobs.get_job(created["id"])["schedule"]["display"] == "every 15m"
    assert len(jobs.load_jobs()) == 1
    assert _audit(home) == []
    assert state["cards"] == [] and state["proof_cards"] == []


# --- the generation primitive -------------------------------------------------------------


def test_generation_describes_the_existing_file(env):
    jobs, home, state = env
    jobs_file = jobs._current_cron_store().jobs_file

    absent = jobs.store_generation()
    assert absent.present is False and absent.row_count == 0

    _seed(jobs, [_row(0), _row(1)])
    gen = jobs.store_generation()
    assert gen.present is True and gen.parsed is True
    assert gen.row_count == 2
    assert set(gen.ids) == {_row(0)["id"], _row(1)["id"]}

    # Id order is not a generation change; the byte digest is what pins "no write happened".
    jobs_file.write_text(json.dumps({"jobs": [_row(1), _row(0)]}, indent=2), encoding="utf-8")
    reordered = jobs.store_generation()
    assert reordered.ids_sha256 == gen.ids_sha256
    assert reordered.row_count == gen.row_count
    assert reordered.bytes_sha256 != gen.bytes_sha256
    assert reordered != gen

    jobs_file.write_bytes(b'{"jobs": [{"id": "broken')
    corrupt = jobs.store_generation()
    assert corrupt.present is True and corrupt.parsed is False
