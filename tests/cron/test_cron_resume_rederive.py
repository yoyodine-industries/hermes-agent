"""Resume re-derives ``next_run_at`` from the job's OWN expression (card t-dd76635fb1).

A paused job used to resume carrying the instant it was paused across, so a job whose slot elapsed
10.5h ago fired at whatever minute the operator typed ``resume`` — the departure then proved the
hand and not the schedule. Clauses (a)-(d) of the ruling:

(a) resume re-derives the slot from the job's own cron/interval expression in the SAME operation
    and records the pair ``carried -> derived``;
(b) a derived slot in the past schedules to the NEXT occurrence, and never fires at the resume
    minute;
(c) a carried slot more than one interval behind the resume is a NAMED finding on the first
    post-resume tick, not a silent correction;
(d) the proof is the instrument: a 3h-stale carried slot, the derived slot asserted to be the next
    occurrence, with the carried one quoted beside it.
"""
from datetime import timedelta
import json

from cron import jobs

HOUR = 3600.0


def _stale_slot(hours: float = 3.0) -> str:
    return (jobs._hermes_now() - timedelta(hours=hours)).isoformat()


def _paused_job_at(tmp_path, carried: str) -> dict:
    """A paused hourly job whose stored instant is ``carried`` — the state a long pause leaves."""
    job = jobs.create_job(prompt='report', schedule='every 1h', deliver='local')
    jobs.pause_job(job['id'], reason='host maintenance')
    jobs.update_job(job['id'], {'next_run_at': carried})
    saved = jobs.get_job(job['id'])
    assert saved is not None
    return saved


def _telemetry(filename: str) -> list:
    """Rows of one telemetry file in the ACTIVE cron store (the store the code under test wrote)."""
    path = jobs._current_cron_store().cron_dir / filename
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_resume_rederives_a_three_hour_stale_slot(tmp_path):
    """(d) the instrument: 3h stale carried -> derived is the NEXT occurrence, carried quoted."""
    with jobs.use_cron_store(tmp_path / 'cron'):
        carried = _stale_slot(3.0)
        job = _paused_job_at(tmp_path, carried)
        assert job['state'] == 'paused' and job['next_run_at'] == carried

        before = jobs._hermes_now()
        resumed = jobs.resume_job(job['id'])
        assert resumed is not None
        after = jobs._hermes_now()

        derived = resumed['next_run_at']
        derived_dt = jobs._parse_aware(derived)
        carried_dt = jobs._parse_aware(carried)
        assert derived_dt is not None and carried_dt is not None

        # (b) the derived instant is strictly AFTER the resume, one interval on: a stale job never
        #     fires at the resume minute.
        assert derived != carried
        assert derived_dt > before
        assert derived_dt <= after + timedelta(seconds=HOUR)
        assert not jobs._instant_at_or_before(derived_dt, after)
        assert 0 < (derived_dt - before).total_seconds() <= HOUR + 5

        # (c) the stale slot is a NAMED finding that quotes the carried instant beside the derived
        #     one — the lateness is measured, never silently corrected away.
        finding = resumed['resume_stale']
        assert isinstance(finding, dict)
        assert finding['carried_next_run_at'] == carried
        assert finding['derived_next_run_at'] == derived
        assert (jobs._parse_aware(finding['carried_next_run_at']) == carried_dt)
        assert 3 * HOUR - 120 <= finding['late_seconds'] <= 3 * HOUR + 120
        assert finding['cadence_seconds'] == HOUR
        assert 2.9 <= finding['intervals_late'] <= 3.1
        assert resumed['enabled'] is True and resumed['state'] == 'scheduled'

        # (a) the pair is recorded, carried quoted BESIDE derived
        rows = _telemetry('cron_resumes.jsonl')
        assert len(rows) == 1
        assert rows[0]['carried_next_run_at'] == carried
        assert rows[0]['derived_next_run_at'] == derived
        assert rows[0]['stale'] is True
        assert rows[0]['job_id'] == job['id']


def test_a_pause_inside_one_interval_is_re_derived_without_a_finding(tmp_path):
    """A slot that elapsed while paused is expected: re-derived, pair recorded, NO finding."""
    with jobs.use_cron_store(tmp_path / 'cron'):
        carried = _stale_slot(0.25)                     # 15 min behind, cadence is 1h
        job = _paused_job_at(tmp_path, carried)
        resumed = jobs.resume_job(job['id'])
        assert resumed is not None

        rederived = jobs._parse_aware(resumed['next_run_at'])
        assert rederived is not None and rederived > jobs._hermes_now()
        assert not resumed.get('resume_stale')
        rows = _telemetry('cron_resumes.jsonl')
        assert len(rows) == 1
        assert rows[0]['carried_next_run_at'] == carried
        assert rows[0]['stale'] is False
        assert _telemetry('cron_resume_stale.jsonl') == []


def test_the_stale_finding_is_reported_once_per_resume(tmp_path):
    """(c) ONE named finding per resume, on the first tick — not one per tick, and not silence."""
    with jobs.use_cron_store(tmp_path / 'cron'):
        job = _paused_job_at(tmp_path, _stale_slot(3.0))
        resumed = jobs.resume_job(job['id'])
        assert resumed is not None
        stamp = resumed['resume_stale']
        assert isinstance(stamp, dict)

        for _ in range(4):                              # four ticks of the same resumed job
            jobs.report_resume_stale(resumed)

        rows = _telemetry('cron_resume_stale.jsonl')
        assert len(rows) == 1
        assert rows[0]['job_id'] == job['id']
        assert rows[0]['carried_next_run_at'] == stamp['carried_next_run_at']
        assert rows[0]['derived_next_run_at'] == resumed['next_run_at']

        # ...and a later clean resume clears the stamp, so it cannot re-report stale advice.
        jobs.pause_job(job['id'])
        jobs.update_job(job['id'], {'next_run_at': (jobs._hermes_now() + timedelta(minutes=5)).isoformat()})
        clean = jobs.resume_job(job['id'])
        assert clean is not None and not clean.get('resume_stale')


def test_a_one_shot_past_its_grace_still_refuses_to_resume(tmp_path, monkeypatch):
    """One-shots have no expression to derive from: the existing refusal is unchanged."""
    with jobs.use_cron_store(tmp_path / 'cron'):
        run_at = (jobs._hermes_now() + timedelta(minutes=10)).isoformat()
        job = jobs.create_job(prompt='one off', schedule=run_at, deliver='local')
        jobs.pause_job(job['id'])
        # The refusal branch is "no next occurrence exists", which a one-shot reaches only once its
        # instant is outside the grace window; the store refuses to hold such a job, so the branch is
        # reached here by making compute_next_run answer None rather than by faking a stored instant.
        monkeypatch.setattr(jobs, 'compute_next_run', lambda *a, **k: None)
        try:
            jobs.resume_job(job['id'])
        except ValueError as exc:
            assert 'one-shot' in str(exc)
        else:                                            # pragma: no cover - the refusal is the point
            raise AssertionError('resuming an expired one-shot must refuse')
        # A refusal is not an observation of a resume: nothing was recorded for it.
        assert _telemetry('cron_resumes.jsonl') == []

        # ...and the refusal left the job paused rather than half-re-armed.
        still = jobs.get_job(job['id'])
        assert still is not None and still['state'] == 'paused'


def test_a_quarter_hour_job_resumed_ten_and_a_half_hours_late_lands_on_the_next_boundary(tmp_path):
    """The card's own fixture, verbatim: */15 with a carried slot 10.5h stale.

    It must land on the next :00/:15/:30/:45 boundary and never fire at the resume minute; the
    carried instant is quoted beside the derived one on the finding.
    """
    with jobs.use_cron_store(tmp_path / 'cron'):
        job = jobs.create_job(prompt='quarterly', schedule='*/15 * * * *', deliver='local')
        jobs.pause_job(job['id'])
        carried = (jobs._hermes_now() - timedelta(hours=10, minutes=30)).isoformat()
        jobs.update_job(job['id'], {'next_run_at': carried})

        now = jobs._hermes_now()
        resumed = jobs.resume_job(job['id'])
        assert resumed is not None
        derived = jobs._parse_aware(resumed['next_run_at'])
        assert derived is not None

        assert derived > now                                   # not the resume minute
        assert derived.minute % 15 == 0 and derived.second == 0  # a :00/:15/:30/:45 boundary
        assert derived - now <= timedelta(minutes=15)

        finding = resumed['resume_stale']
        assert isinstance(finding, dict)
        assert finding['carried_next_run_at'] == carried
        assert finding['derived_next_run_at'] == resumed['next_run_at']
        assert finding['cadence_seconds'] == 900.0
        assert finding['intervals_late'] > 40                  # 10.5h is 42 quarter-hours


def test_a_failed_append_is_not_a_report_and_keeps_the_finding_reportable(tmp_path, monkeypatch):
    """Review §1: a telemetry write that failed must not consume the de-dup key.

    The old order added the key BEFORE the append, so one IO error silenced the finding for the
    life of the process — the instrument reporting its own write instead of the event. Here the
    append fails once, nothing is stamped, and the next tick reports the SAME finding exactly once.
    """
    with jobs.use_cron_store(tmp_path / 'cron'):
        job = _paused_job_at(tmp_path, _stale_slot(3.0))
        resumed = jobs.resume_job(job['id'])
        assert resumed is not None and resumed['resume_stale']['stale'] is True

        real = jobs._append_telemetry_record

        def _fail_stale_only(filename, entry, recent):
            if filename == 'cron_resume_stale.jsonl':
                return False                      # a failed write, exactly as the real one reports
            return real(filename, entry, recent)

        monkeypatch.setattr(jobs, '_append_telemetry_record', _fail_stale_only)
        jobs.report_resume_stale(resumed)
        assert _telemetry('cron_resume_stale.jsonl') == []
        # nothing consumed THIS finding: no key for it in the process, no stamp on the record
        assert (job['id'], resumed['resume_stale']['resumed_at']) not in jobs._resume_stale_reported
        on_record = jobs.get_job(job['id'])
        assert on_record is not None and 'reported_at' not in (on_record.get('resume_stale') or {})

        monkeypatch.setattr(jobs, '_append_telemetry_record', real)
        jobs.report_resume_stale(jobs.get_job(job['id']))
        jobs.report_resume_stale(jobs.get_job(job['id']))
        rows = _telemetry('cron_resume_stale.jsonl')
        assert len(rows) == 1, rows              # one report, not zero and not two
        stamped = jobs.get_job(job['id'])['resume_stale']
        assert stamped.get('reported_at') == rows[0]['observed_at']


def test_the_RECORD_is_the_authority_for_de_dup_not_the_process(tmp_path):
    """Review §2: a gateway restart must not re-warn a resume that was already reported.

    The set is per-process and cleared past 512 keys; the persisted ``reported_at`` stamp is what
    makes "already reported" survive a restart, a second caller, and a long-lived install.
    """
    with jobs.use_cron_store(tmp_path / 'cron'):
        job = _paused_job_at(tmp_path, _stale_slot(3.0))
        resumed = jobs.resume_job(job['id'])
        assert resumed is not None

        jobs.report_resume_stale(resumed)
        assert len(_telemetry('cron_resume_stale.jsonl')) == 1

        # A NEW PROCESS: the in-memory set is gone, the record is re-read from the store.
        jobs._resume_stale_reported.clear()
        for _ in range(3):
            jobs.report_resume_stale(jobs.get_job(job['id']))
        assert len(_telemetry('cron_resume_stale.jsonl')) == 1
        assert jobs.get_job(job['id'])['resume_stale'].get('reported_at')


def test_an_UNMEASURED_lateness_is_a_row_not_a_finding(tmp_path, monkeypatch):
    """Review §3: with croniter missing, "nothing to report" must not read as "measured fresh".

    The no-page rule stands — an unmeasurable lateness is not a finding — but the row must say
    ``measured: false, cadence_seconds: null, reason: cadence_unknown`` so a reader can tell it
    apart from a resume measured inside one interval.
    """
    with jobs.use_cron_store(tmp_path / 'cron'):
        job = _paused_job_at(tmp_path, _stale_slot(3.0))
        monkeypatch.setattr(jobs, '_schedule_cadence_seconds', lambda schedule: None)

        resumed = jobs.resume_job(job['id'])
        assert resumed is not None
        assert not resumed.get('resume_stale')          # no page for an unmeasurable lateness
        rows = _telemetry('cron_resumes.jsonl')
        assert len(rows) == 1
        row = rows[0]
        assert row['measured'] is False
        assert row['cadence_seconds'] is None
        assert row['intervals_late'] is None
        assert row['reason'] == 'cadence_unknown'
        assert row['stale'] is False
        assert row['late_seconds'] > 2 * HOUR           # the lateness WAS read, just not scaled
        assert _telemetry('cron_resume_stale.jsonl') == []


def test_every_pair_row_names_the_DOOR_that_resumed(tmp_path):
    """Review §4: four callers reach resume_job, so the receipt must say which one did it."""
    with jobs.use_cron_store(tmp_path / 'cron'):
        for door in ('cli', 'tool', 'gateway', 'receipt'):
            job = _paused_job_at(tmp_path, (jobs._hermes_now() + timedelta(minutes=5)).isoformat())
            jobs.resume_job(job['id'], door=door)
        doors = [row['door'] for row in _telemetry('cron_resumes.jsonl')]
        assert doors == ['cli', 'tool', 'gateway', 'receipt'], doors

        # the default is the CLI door, so an un-updated caller is legible rather than nameless
        job = _paused_job_at(tmp_path, (jobs._hermes_now() + timedelta(minutes=5)).isoformat())
        jobs.resume_job(job['id'])
        assert _telemetry('cron_resumes.jsonl')[-1]['door'] == 'cli'


def test_the_resume_pair_file_is_bounded(tmp_path, monkeypatch):
    """Review §7: every resume appends a row, so the file rotates instead of growing forever."""
    with jobs.use_cron_store(tmp_path / 'cron'):
        path = jobs._current_cron_store().cron_dir / 'cron_resumes.jsonl'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('\n'.join(json.dumps({'job_id': 'old-%d' % i, 'door': 'cli'})
                                  for i in range(5)) + '\n')
        monkeypatch.setattr(jobs, '_RESUME_PAIR_MAX_LINES', 4)

        job = _paused_job_at(tmp_path, (jobs._hermes_now() + timedelta(minutes=5)).isoformat())
        jobs.resume_job(job['id'])

        rows = _telemetry('cron_resumes.jsonl')
        assert len(rows) <= 4, len(rows)
        assert rows[-1]['job_id'] == job['id']          # the NEWEST row survives the rotation
        assert not path.with_suffix('.jsonl.tmp').exists()
