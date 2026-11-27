"""RELEASE AUTHORITY and PURPOSE — `agent/estop.py`, card t_7b226853.

Measured defect (2026-11-25): the fleet hold `SAVECLASS-20261125-0820` was armed by one
process and released 11m23s into a 60m lease by a DIFFERENT one, `cause=handle via=ops-venue`,
with no evidence that the window's purpose (the card it existed for, `t_5800fe41`) was met —
that card was `running` at the release and is `blocked` today.

Two rules, both enforced at the HOLD so every surface inherits them:

* a hold is lifted by its HOLDER, the operator, or an actor the ARM granted — not by whichever
  caller happens to know the handle;
* a hold that names a CARD refuses while that card is non-terminal ("the window's purpose is
  unfinished") unless the operator lifts it or the HOLDER asserts `--purpose-met`.

The purpose and the grant are SIGNED with the hold (a v4 canonical text that leaves every
pre-existing v3 signature verifying), so neither can be added or edited on disk afterwards.
"""

from __future__ import annotations

import argparse
import json

import pytest

from agent import estop

HOLDER = "window:SAVECLASS-20261125-0820"
OPS = "bot:unresolved:66025@D390476A"
CARD = "t_5800fe41"


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    """A sandbox home, an admitted venue, and NO board by default.

    ``_card_status`` is stubbed to "no board knows it" unless a test says otherwise: the
    module must never read (or create) a real board from these tests, and a card nobody can
    find is the fail-closed case a test can opt out of.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(estop, "arm_admission", lambda action="arm": (True, "interactive", None))
    monkeypatch.setattr(estop, "_card_status", lambda card: (None, ""))
    estop._logged_components.clear()
    estop._expired_logged.clear()
    estop._unattributed_logged.clear()
    return tmp_path


def _arm(actor=HOLDER, **kwargs):
    """A lane-scoped hold armed BY ``actor`` (which is therefore its owner too)."""
    params = dict(
        mode="lockdown",
        allow={"profiles": ["platform-stl", "platform-coder"]},
        reason="fleet-window SAVECLASS-20261125-0820",
        ttl="60m",
    )
    params.update(kwargs)
    return estop.arm(actor=actor, **params)


def _set_status(monkeypatch, mapping):
    """Answer `_card_status` from ``mapping`` (card -> (status, board)); unknown -> None."""
    monkeypatch.setattr(estop, "_card_status",
                        lambda card: mapping.get(card, (None, "")))


def _ledger(event):
    return [row for row in estop.read_events() if row.get("event") == event]


# ── D1: WHO may lift ────────────────────────────────────────────────────────


def test_a_third_party_cannot_lift_another_holders_hold(hermes_home):
    """The measured incident: a caller that knows the handle is still not its holder."""
    armed = _arm()
    result = estop.release(handle=armed.handle, actor=OPS)

    assert result.released is False
    assert result.forbidden is True
    assert result.purpose_unfinished is False
    assert f"held by {HOLDER}" in result.message
    assert estop.is_engaged() is True, "the hold must STAND after a refused release"
    refused = _ledger("release_refused")
    assert refused and refused[-1]["handle"] == armed.handle
    assert refused[-1]["authority"] == "refused"


def test_the_holder_lifts_its_own_hold(hermes_home):
    armed = _arm()
    result = estop.release(handle=armed.handle, actor=HOLDER)

    assert result.released is True
    assert result.authority == "holder"
    assert estop.is_engaged() is False
    assert _ledger("release")[-1]["via"] == "holder"


def test_the_operator_lifts_any_hold(hermes_home):
    armed = _arm()
    result = estop.release(handle=armed.handle, actor=estop.ACTOR_OPERATOR)

    assert result.released is True
    assert result.authority == "operator"
    assert _ledger("release")[-1]["via"] == "operator"


def test_an_actor_the_arm_named_may_lift(hermes_home):
    armed = _arm(release_grant=[OPS])
    result = estop.release(handle=armed.handle, actor=OPS)

    assert result.released is True
    assert result.authority == "grant"
    assert _ledger("release")[-1]["via"] == "grant"


def test_the_any_venue_grant_is_opt_in_at_arm_time(hermes_home):
    """The OLD ops path survives only where the ARM asked for it, and it is recorded."""
    granted = _arm(release_grant=[estop.ANY_VENUE], reason="ops window")
    assert estop.release(handle=granted.handle, actor=OPS).released is True

    ungranted = _arm(reason="a different window")
    result = estop.release(handle=ungranted.handle, actor=OPS)
    assert result.forbidden is True, "no grant, no lift — that is the card"


def test_a_legacy_total_hold_is_still_liftable_from_an_admitted_venue(hermes_home):
    """R2's un-strand path: a PRE-RULING total halt under a name nobody owns."""
    body = {"reason": "pre-ruling alarm", "engaged_at": "2026-10-01T00:00:00Z"}
    estop.sentinel_path().write_text(json.dumps(body) + "\n", encoding="utf-8")
    handle = estop.holds()[0]["handle"]

    result = estop.release(handle=handle, actor=OPS)
    assert result.released is True
    assert result.authority == "legacy"


# ── D2: WHAT the hold exists for ────────────────────────────────────────────


def test_a_card_named_in_the_reason_becomes_the_purpose(hermes_home):
    armed = _arm(reason=f"fleet-window SAVECLASS-20261125-0820 for {CARD}")
    hold = estop.read_state().holds[0]
    assert hold["purpose"] == {"cards": [CARD]}
    assert hold["verified"] is True
    assert estop.read_events()[-1]["purpose_cards"] == [CARD]
    assert armed.purpose == (CARD,)


def test_a_running_card_refuses_the_holders_release(hermes_home, monkeypatch):
    _set_status(monkeypatch, {CARD: ("running", "platform")})
    armed = _arm(purpose=[CARD])

    result = estop.release(handle=armed.handle, actor=HOLDER)

    assert result.released is False
    assert result.purpose_unfinished is True
    assert result.forbidden is False
    assert "purpose is UNFINISHED" in result.message
    assert f"{CARD} is running" in result.message
    assert estop.is_engaged() is True
    refused = _ledger("release_refused")[-1]
    assert refused["reason"] == "purpose-unfinished"
    assert refused["statuses"] == {CARD: "running"}


def test_a_blocked_card_also_holds_the_window(hermes_home, monkeypatch):
    """`blocked` is NOT terminal: the SEV the window protects was blocked when it mattered."""
    _set_status(monkeypatch, {CARD: ("blocked", "platform")})
    armed = _arm(purpose=[CARD])
    assert estop.release(handle=armed.handle, actor=HOLDER).purpose_unfinished is True
    assert estop.is_engaged() is True


def test_the_same_release_succeeds_once_the_card_is_done(hermes_home, monkeypatch):
    """The card's own acceptance test: arm naming a running card, third-party release
    refused, complete the card, the SAME release succeeds."""
    _set_status(monkeypatch, {CARD: ("running", "platform")})
    armed = _arm(purpose=[CARD], release_grant=[OPS])
    refused = estop.release(handle=armed.handle, actor=OPS)
    assert refused.purpose_unfinished is True
    assert estop.is_engaged() is True

    _set_status(monkeypatch, {CARD: ("done", "platform")})
    result = estop.release(handle=armed.handle, actor=OPS)
    assert result.released is True
    assert result.authority == "grant"
    assert estop.is_engaged() is False


def test_an_unreadable_card_refuses_the_release(hermes_home):
    """A board we cannot read is NOT a licence to resume: fail CLOSED, TTL is the backstop."""
    armed = _arm(purpose=[CARD])  # _card_status stubbed to (None, "")
    result = estop.release(handle=armed.handle, actor=HOLDER)

    assert result.purpose_unfinished is True
    assert "UNREADABLE" in result.message
    assert estop.is_engaged() is True


def test_the_operator_may_end_an_unfinished_window(hermes_home, monkeypatch):
    _set_status(monkeypatch, {CARD: ("running", "platform")})
    armed = _arm(purpose=[CARD])
    result = estop.release(handle=armed.handle, actor=estop.ACTOR_OPERATOR)

    assert result.released is True
    assert result.authority == "operator"


def test_the_holder_may_assert_its_purpose_met_but_a_grantee_may_not(hermes_home, monkeypatch):
    _set_status(monkeypatch, {CARD: ("running", "platform")})
    granted = _arm(purpose=[CARD], release_grant=[OPS])

    # A grantee is allowed to LIFT, not to declare the holder's purpose finished.
    assert estop.release(handle=granted.handle, actor=OPS, purpose_met=True).purpose_unfinished is True
    assert estop.is_engaged() is True

    result = estop.release(handle=granted.handle, actor=HOLDER, purpose_met=True)
    assert result.released is True
    assert _ledger("release")[-1]["purpose_met"] is True


# ── provenance: v4 is additive, never a re-cut ──────────────────────────────


def test_a_hold_without_a_purpose_keeps_the_v3_signature(hermes_home):
    """Every hold already on disk was signed over the v3 text: that text is unchanged."""
    armed = _arm(reason="plain window")
    payload = json.loads(estop.sentinel_path().read_text(encoding="utf-8"))
    entry = payload["holds"][0]

    assert "purpose" not in entry and "release_grant" not in entry
    assert entry["sig"].startswith(estop.SIG_PREFIX)
    assert estop.read_state().holds[0]["verified"] is True
    assert estop.read_state().holds[0]["handle"] == armed.handle


def test_a_purpose_is_signed_not_decorative(hermes_home, monkeypatch):
    """Editing the purpose on disk invalidates the entry — it cannot be narrowed after the arm."""
    _set_status(monkeypatch, {CARD: ("running", "platform")})
    armed = _arm(purpose=[CARD])
    path = estop.sentinel_path()
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["holds"][0]["purpose"] = {"cards": []}
    path.write_text(json.dumps(payload), encoding="utf-8")

    hold = estop.read_state().holds[0]
    assert hold["verified"] is False
    assert hold["mode"] == "estop", "a tampered hold is demoted to a TOTAL halt (D5)"
    # A demoted entry is re-keyed under its CONTENT handle, so the old handle is stale...
    assert estop.release(handle=armed.handle, actor=OPS).stale is True
    # ...and the new handle is not the third party's to lift either.
    assert estop.release(handle=hold["handle"], actor=OPS).forbidden is True
    assert estop.is_engaged() is True


def test_an_edited_grant_does_not_survive_verification(hermes_home):
    armed = _arm(reason="plain window")
    path = estop.sentinel_path()
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["holds"][0]["release_grant"] = {"actors": [OPS]}
    path.write_text(json.dumps(payload), encoding="utf-8")

    assert estop.read_state().holds[0]["verified"] is False
    hold = estop.read_state().holds[0]
    assert estop.release(handle=armed.handle, actor=OPS).stale is True
    assert estop.release(handle=hold["handle"], actor=OPS).forbidden is True
    assert estop.is_engaged() is True


# ── the CLI reports the two refusals apart ──────────────────────────────────


def _pause_args(**kwargs):
    base = dict(lockdown=True, allow_profile=["platform-stl"], allow_user=None, reason=None,
                ttl="60m", order=None, release_by=None, release_by_any_venue=False,
                purpose_card=None, history=False, history_limit=20, json=False)
    base.update(kwargs)
    return argparse.Namespace(**base)


def test_cli_pause_records_the_purpose_and_the_grant(hermes_home, monkeypatch, capsys):
    from hermes_cli.subcommands import pause as pause_mod

    monkeypatch.setattr(pause_mod, "_profile_is_real", lambda name: True)
    monkeypatch.setenv("HERMES_ESTOP_ACTOR", HOLDER)
    rc = pause_mod.cmd_pause(_pause_args(reason="window", purpose_card=[CARD],
                                         release_by=[OPS]))
    assert rc == 0
    hold = estop.read_state().holds[0]
    assert hold["purpose"] == {"cards": [CARD]}
    assert hold["release_grant"] == {"actors": [OPS]}
    out = capsys.readouterr().out
    assert CARD in out and OPS in out


def test_cli_resume_refuses_a_third_party_with_rc6(hermes_home, monkeypatch, capsys):
    from hermes_cli.subcommands import pause as pause_mod
    from hermes_cli.subcommands.pause import cmd_resume

    armed = _arm()
    monkeypatch.setenv("HERMES_ESTOP_ACTOR", OPS)
    rc = cmd_resume(argparse.Namespace(handle=armed.handle, owner=None, all=False, json=False))
    assert rc == 6
    assert "not" in capsys.readouterr().out
    assert estop.is_engaged() is True


def test_cli_resume_reports_the_unfinished_purpose_with_rc7(hermes_home, monkeypatch, capsys):
    from hermes_cli.subcommands.pause import cmd_resume

    _set_status(monkeypatch, {CARD: ("running", "platform")})
    armed = _arm(purpose=[CARD])
    monkeypatch.setenv("HERMES_ESTOP_ACTOR", HOLDER)
    rc = cmd_resume(argparse.Namespace(handle=armed.handle, owner=None, all=False, json=False))
    assert rc == 7
    assert "UNFINISHED" in capsys.readouterr().out
    assert estop.is_engaged() is True


def test_cli_resume_purpose_met_lets_the_holder_out(hermes_home, monkeypatch):
    from hermes_cli.subcommands.pause import cmd_resume

    _set_status(monkeypatch, {CARD: ("running", "platform")})
    armed = _arm(purpose=[CARD])
    monkeypatch.setenv("HERMES_ESTOP_ACTOR", HOLDER)
    rc = cmd_resume(argparse.Namespace(handle=armed.handle, owner=None, all=False,
                                       json=False, purpose_met=True))
    assert rc == 0
    assert estop.is_engaged() is False


# ── the two READ surfaces must show WHY a hold will not lift ────────────────


def test_the_status_line_names_the_purpose(hermes_home, monkeypatch):
    from hermes_cli.status import _estop_status_line

    _arm(purpose=[CARD], reason="SEV landing")
    line = _estop_status_line()
    assert f"for {CARD}" in line
    assert "SEV landing" in line


def test_the_history_row_names_the_purpose_and_the_grant(hermes_home, capsys):
    from hermes_cli.subcommands.pause import _print_history

    _arm(purpose=[CARD], release_grant=[OPS])
    assert _print_history(10, False) == 0
    out = capsys.readouterr().out
    assert f"for={CARD}" in out and f"may_lift={OPS}" in out


def test_an_unresolved_pid_owner_is_ownerless_for_release(hermes_home):
    """The incident's own arm (`bot:unresolved:<pid>@<boot>`) cannot be re-adopted by any later
    process, so scoping D1 to it would strand the fleet until TTL. It is ownerless; D2 is the
    guard that then matters — which is why a runner must name a card, and, to be protected,
    a stable holder."""
    armed = _arm(actor="bot:unresolved:51899@D390476A")
    result = estop.release(handle=armed.handle, actor=OPS)

    assert result.released is True
    assert result.authority == "unstable-owner"
    assert _ledger("release")[-1]["via"] == "ops-venue"


def test_an_unresolved_pid_hold_still_honours_its_purpose(hermes_home, monkeypatch):
    """Ownerless for D1 is not ownerless for the WORK: the window cannot be voided mid-landing."""
    _set_status(monkeypatch, {CARD: ("running", "platform")})
    armed = _arm(actor="bot:unresolved:51899@D390476A", purpose=[CARD])

    result = estop.release(handle=armed.handle, actor=OPS)
    assert result.purpose_unfinished is True
    assert result.released is False
    assert estop.is_engaged() is True
