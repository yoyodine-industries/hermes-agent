"""Role-based routing of decomposed children — card t_87e4e2e2 (ruling t_d23cd91f §5).

The defect, measured on the live instance: card ``t_bf0f2c47`` (deliverable = yoyoflow
engine code) was fanned out on 2026-10-15 by ``auto-decomposer`` into five children, ALL
FIVE on the ops seat, three of them pure engine code. ``_clean_children`` normalises an
absent/unknown LLM assignee to ``kanban.default_assignee`` and ``_load_routing`` resolves
that with ``fallback=root_assignee`` — so routing degenerates to the TIER the card arrived
on, and a code child on the ops seat is a guaranteed hand-back.

These tests drive the REAL entry point (``kanban_decompose.decompose_task``) with the aux
LLM stubbed, exactly as the instance was driven. The five children's titles and bodies are
that instance's (migrations board, card ``t_bf0f2c47``), trimmed to the text that decides
the routing; the five source ids are t_1ad667d4, t_5c68719d, t_105b6356, t_32d868b7 and
t_c15ac400.

Every profile name a case asserts on is SUPPLIED BY THE FIXTURE — the ops seat by the
config, the owner by a repo's OWNERS file, the coder derived from that owner — never typed
into the guard and never assumed of the fleet.
"""

from __future__ import annotations

import json as jsonlib
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_boards as kbb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_decompose as decomp
from hermes_cli import profiles as profiles_mod


# --------------------------------------------------------------------------- #
# the instance, verbatim in its decisive parts
# --------------------------------------------------------------------------- #

INSTANCE_PARENT_TITLE = "[yoyoflow engine] bound node_outputs at the record: 1 MiB per row"

INSTANCE_PARENT_BODY = """DESIGN OF RECORD: t_53294612 (platform-stl). This card implements decision 1-6 below.

## DECISION (platform-stl) - a per-row size bound at the write path

1. Default cap = 1,048,576 bytes (1 MiB) on the serialized value of any single node_outputs row.
2. Over-cap rows persist as an ELISION ENVELOPE.
4. No silent elision: emit a run event `node_output_elided` and one stdout line.
5. Consumer safety: `yoyoflow/web.py` / `yoyoflow/monitor.py` rendering must render an envelope
   as explicit `elided (N bytes)`.

## DONE = tested + deployed + running

Unit tests in the repo's `tests/` (`test_state.py` has the node_outputs roundtrip precedent,
`test_monitor.py` the render precedent).

**Route:** branch off `main` of `git@github.com:yoyodine-industries/yoyoflow.git` (clone on host:
`/Users/hermes_user/yoyoflow-49`), open a PR for review; platform-stl accepts. Deploy rides the
enrolled train car (dest `/opt/hermes_prod/yoyoflow`).
"""

# (title, body) — the instance's five children, trimmed.
INSTANCE_CHILDREN_RAW: list[tuple[str, str]] = [
    (
        "Implement 1 MiB node_outputs cap + elision envelope in StateStore",
        "Goal: bound every row written to `node_outputs` at a per-row byte cap, at the write path "
        "(do not touch db-retention-purge.py, db-maintenance.sh, the retention window, or the DB "
        "schema).\n\nWhere: `yoyoflow/state.py`, `StateStore.set_node_outputs` is the SINGLE writer "
        "to `node_outputs`. All bounding logic lives here.\n\n"
        "- Existing `tests/test_state.py` node_outputs roundtrip tests still pass.",
    ),
    (
        "Support optional per-output retain_bytes declaration (validated, <= 16 MiB)",
        "Goal: let a DAG raise the per-row cap for a declared output. A declaration can RAISE the "
        "bound for a named receipt; it can never remove it.\n\nWhere: the DAG/output declaration "
        "parsing and validation path in the yoyoflow engine. Task 1's `yoyoflow/state.py` provides "
        "`DEFAULT_NODE_OUTPUT_CAP_BYTES` and `MAX_NODE_OUTPUT_CAP_BYTES`.",
    ),
    (
        "Emit node_output_elided run event + one stdout line per elided output",
        "Goal: no silent elision. Every elided `node_outputs` row must be visible in the run's own "
        "record and on the run's stdout.\n\nWhere: the node-output write call site in the yoyoflow "
        "engine - the same place that calls `StateStore.set_node_outputs` and has `run_id`, "
        "`node_id` and `output_name` in scope.\n\n1. Append a run event named "
        "`node_output_elided` to the run's event stream.",
    ),
    (
        "Unit tests + live-DB verification for the cap, envelope and retain_bytes",
        "Goal: prove the change tested, deployed and running against the real store. "
        "`tests/test_state.py` has the node_outputs roundtrip precedent; `tests/test_monitor.py` "
        "has the render precedent - follow both.\n\nUnit tests (repo `tests/`):\n a) a value "
        "serializing to exactly the 1 MiB cap is stored verbatim (boundary inclusive);",
    ),
    (
        "Render elision envelopes in web.py/monitor.py and verify fleet_core path",
        "Goal: consumer safety. Any renderer of a node output must display an elision envelope as "
        "an explicit `elided (N bytes)` - never crash, never blank - and the `fleet_core.py` reader "
        "must be PROVEN out of the envelope path, not assumed.\n\nWhere: `yoyoflow/web.py` and "
        "`yoyoflow/monitor.py` node-output rendering paths.",
    ),
]

CODE_CHILD_TITLES = [title for title, _ in INSTANCE_CHILDREN_RAW[:4]]
OPS_CHILD_TITLE = INSTANCE_CHILDREN_RAW[4][0]


def _instance_children(assignee: object = None) -> list[dict]:
    """The instance's fan-out as the LLM returned it: no usable assignee for any child."""
    return [
        {"title": title, "body": body, "assignee": assignee, "parents": []}
        for title, body in INSTANCE_CHILDREN_RAW
    ]


# --------------------------------------------------------------------------- #
# the harness: everything the guard may learn from, declared by the FIXTURE
# --------------------------------------------------------------------------- #


@dataclass
class Fleet:
    """One config, one roster, one repo — the only sources the guard is allowed to read."""

    ops_seat: str          # supplied by the fixture's ``kanban.default_assignee``
    owner_lane: str        # supplied by the fixture repo's OWNERS file
    coder: str             # derived from the owner
    profiles: list[str] = field(default_factory=list)
    root: Path | None = None
    repo_slug: str = ""


def _patch_roster(monkeypatch, names: list[str]) -> None:
    fake = [
        SimpleNamespace(
            name=n, is_default=(i == 0), description=f"desc for {n}",
            description_auto=False, model="m", provider="p", skill_count=1,
        )
        for i, n in enumerate(names)
    ]
    monkeypatch.setattr(profiles_mod, "list_profiles", lambda: fake)
    monkeypatch.setattr(profiles_mod, "profile_exists", lambda name: name in names)
    monkeypatch.setattr(profiles_mod, "get_active_profile_name", lambda: names[0])


def build_fleet(
    tmp_path: Path,
    monkeypatch,
    *,
    ops_seat: str = "tier-seat",
    owner_lane: str = "widget-stl",
    repo_leaf: str = "yoyoflow",
    extra_coders: tuple[str, ...] = (),
    checkout: bool = True,
) -> Fleet:
    """A hermetic fleet: config + roster + a control plane + a repo that names its owner."""
    home = tmp_path / ".hermes"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()

    control = tmp_path / "control-plane"
    control.mkdir(exist_ok=True)
    monkeypatch.setenv("YAAN_CONTROL_PLANE", str(control))
    (control / "upstream-foreign-owners.conf").write_text(
        "# registry rows: <origin-slug>  <owner-lane>\n", encoding="utf-8",
    )

    coder = f"{owner_lane.rsplit('-', 1)[0]}-coder"
    repo_slug = f"yoyodine-industries/{repo_leaf}"
    if checkout:
        tree = tmp_path / "repos" / repo_leaf
        tree.mkdir(parents=True, exist_ok=True)
        (tree / "OWNERS").write_text(
            "# Owner of this repo.\n# comment line, never the answer.\n" f"{owner_lane}\n",
            encoding="utf-8",
        )
        (control / "projects.conf").write_text(
            "# project  canonical-checkout\n" f"{repo_leaf}  {tree}\n", encoding="utf-8",
        )

    profiles = [ops_seat, owner_lane, coder] + [f"{stem}-coder" for stem in extra_coders]
    _patch_roster(monkeypatch, profiles)

    from hermes_cli import config as config_mod

    cfg = {"kanban": {"orchestrator_profile": ops_seat, "default_assignee": ops_seat}}
    monkeypatch.setattr(config_mod, "load_config_readonly", lambda: cfg)

    return Fleet(
        ops_seat=ops_seat, owner_lane=owner_lane, coder=coder,
        profiles=profiles, root=tmp_path, repo_slug=repo_slug,
    )


def _aux_response(content: str):
    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = content
    return resp


def _decompose(
    fleet: Fleet, *, children: list[dict], parent_title: str, parent_body: str,
    parent_assignee: str | None = None,
) -> tuple[str, decomp.DecomposeOutcome]:
    """Create a triage root and drive the real decompose path with the aux client stubbed."""
    with kbc.connect() as conn:
        tid = kb.create_task(
            conn, title=parent_title, body=parent_body,
            assignee=parent_assignee or fleet.ops_seat, triage=True,
        )
    payload = jsonlib.dumps({"fanout": True, "rationale": "instance shape", "tasks": children})
    with patch("agent.auxiliary_client.call_llm", return_value=_aux_response(payload)):
        outcome = decomp.decompose_task(tid, author="auto-decomposer")
    return tid, outcome


def _read(outcome, root_id: str):
    with kbc.connect() as conn:
        tasks = {cid: kb.get_task(conn, cid) for cid in outcome.child_ids}
        events = {cid: kb.list_events(conn, cid) for cid in outcome.child_ids}
        root_events = kb.list_events(conn, root_id)
    return tasks, events, root_events


def _kinds(events) -> list[str]:
    return [e.kind for e in events]


# --------------------------------------------------------------------------- #
# A1 — the instance-shaped fan-out
# --------------------------------------------------------------------------- #


def test_instance_fanout_routes_the_four_code_children_to_the_domain_coder(tmp_path, monkeypatch):
    """A1: driven through the real ``decompose_task`` path. Four children are code-shaped and
    sit on the ops seat; each must land on the repo OWNERS' domain coder, not the ops seat."""
    fleet = build_fleet(tmp_path, monkeypatch, repo_leaf="yoyoflow")
    root_id, outcome = _decompose(
        fleet,
        parent_title=INSTANCE_PARENT_TITLE,
        parent_body=INSTANCE_PARENT_BODY,
        children=_instance_children(),
    )

    assert outcome.ok, outcome.reason
    tasks, _, _ = _read(outcome, root_id)
    by_title = {t.title: t for t in tasks.values()}
    assert len(by_title) == 5

    for title in CODE_CHILD_TITLES:
        assert by_title[title].assignee == fleet.coder, f"{title} -> {by_title[title].assignee}"

    assert by_title[OPS_CHILD_TITLE].assignee == fleet.ops_seat


# --------------------------------------------------------------------------- #
# A2 — an ops-shaped child keeps the ops seat
# --------------------------------------------------------------------------- #


def test_an_ops_shaped_child_keeps_the_ops_seat_even_when_it_names_a_file(tmp_path, monkeypatch):
    """A2: land / deploy / verify work is the ops seat's, whichever files it names."""
    fleet = build_fleet(tmp_path, monkeypatch)
    children = [{
        "title": "Land the engine and verify the cap on the live store",
        "body": (
            "Deploy the merged engine to `/opt/hermes_prod/yoyoflow`, restart the monitor, then "
            "prove `yoyoflow/state.py` is out of the failure path on the live run."
        ),
        "assignee": None, "parents": [],
    }]
    root_id, outcome = _decompose(
        fleet, children=children, parent_title="[land] the cap",
        parent_body=f"Route: PR in {fleet.repo_slug}.",
    )

    assert outcome.ok, outcome.reason
    tasks, events, _ = _read(outcome, root_id)
    child = tasks[outcome.child_ids[0]]
    assert child.assignee == fleet.ops_seat
    assert "ROUTED BY ROLE" not in (child.body or "")
    assert "routed_by_role" not in _kinds(events[outcome.child_ids[0]])


def test_the_instance_ops_child_is_not_renamed_off_the_ops_seat(tmp_path, monkeypatch):
    """A2, on the instance itself: the render/verify child names web.py and monitor.py — the
    parent's own DoD names them too — and still belongs to the ops seat."""
    fleet = build_fleet(tmp_path, monkeypatch)
    children = [c for c in _instance_children() if c["title"] == OPS_CHILD_TITLE]
    root_id, outcome = _decompose(
        fleet, children=children, parent_title=INSTANCE_PARENT_TITLE,
        parent_body=INSTANCE_PARENT_BODY,
    )

    assert outcome.ok, outcome.reason
    tasks, events, _ = _read(outcome, root_id)
    child = tasks[outcome.child_ids[0]]
    assert child.assignee == fleet.ops_seat
    assert "routed_by_role" not in _kinds(events[outcome.child_ids[0]])


# --------------------------------------------------------------------------- #
# A3 — the machine's choice is visible on the child
# --------------------------------------------------------------------------- #


def test_the_reroute_line_is_on_the_child_body_and_a_routed_by_role_event_exists(
    tmp_path, monkeypatch,
):
    """A3: the worker reads the re-route as the body's FIRST line, and the reviewer can see
    the event that put it there."""
    fleet = build_fleet(tmp_path, monkeypatch)
    children = [
        {"title": t, "body": b, "assignee": None, "parents": []}
        for (t, b) in INSTANCE_CHILDREN_RAW if t in CODE_CHILD_TITLES[:2]
    ]
    root_id, outcome = _decompose(
        fleet, children=children, parent_title=INSTANCE_PARENT_TITLE,
        parent_body=INSTANCE_PARENT_BODY,
    )

    assert outcome.ok, outcome.reason
    tasks, events, _ = _read(outcome, root_id)
    for cid in outcome.child_ids:
        child = tasks[cid]
        first_line = (child.body or "").splitlines()[0]
        assert first_line.startswith("ROUTED BY ROLE:"), child.body
        assert fleet.coder in first_line
        assert fleet.ops_seat in first_line  # ... (was <the LLM's pick>)

        routed = [e for e in events[cid] if e.kind == "routed_by_role"]
        assert len(routed) == 1
        payload = routed[0].payload or {}
        assert payload.get("was") == fleet.ops_seat
        assert payload.get("became") == fleet.coder
        assert payload.get("rule"), payload
        # The child keeps its own spec after the machine's line.
        assert child.body.split("\n", 1)[1].strip()


# --------------------------------------------------------------------------- #
# A4 — never guess
# --------------------------------------------------------------------------- #


def test_unresolvable_domain_records_ambiguity_and_keeps_the_llms_pick(tmp_path, monkeypatch):
    """A4: no repo, no owner stem, no board lane, and MORE than one coder in the roster — the
    pick is kept and ``routing_ambiguous`` is recorded. No exception escapes."""
    fleet = build_fleet(tmp_path, monkeypatch, extra_coders=("gadget",), checkout=False)
    children = _instance_children()[:1]
    root_id, outcome = _decompose(
        fleet, children=children,
        parent_title="Polish the write path",
        parent_body="Tighten the writer and re-run the suite. No repo is named here.",
    )

    assert outcome.ok, outcome.reason
    tasks, events, root_events = _read(outcome, root_id)
    cid = outcome.child_ids[0]
    assert tasks[cid].assignee == fleet.ops_seat
    assert "routed_by_role" not in _kinds(events[cid])

    ambiguous = [e for e in events[cid] if e.kind == "routing_ambiguous"]
    assert len(ambiguous) == 1
    payload = ambiguous[0].payload or {}
    assert payload.get("why")
    assert payload.get("child_id") == cid
    # ... and the orchestrator that owns the fan-out can see it too.
    assert "routing_ambiguous" in _kinds(root_events)


# --------------------------------------------------------------------------- #
# A5 — the ops seat is the CONFIG's, never a literal
# --------------------------------------------------------------------------- #


def test_the_ops_seat_is_read_from_config_not_from_a_baked_in_name(tmp_path, monkeypatch):
    """A5: the same instance shape, with the seat renamed in the fixture's config and roster.
    The guard follows the config: the child leaves THAT seat."""
    fleet = build_fleet(tmp_path, monkeypatch, ops_seat="night-ops")
    children = _instance_children()[:1]
    root_id, outcome = _decompose(
        fleet, children=children, parent_title=INSTANCE_PARENT_TITLE,
        parent_body=INSTANCE_PARENT_BODY,
    )

    assert outcome.ok, outcome.reason
    tasks, _, _ = _read(outcome, root_id)
    child = tasks[outcome.child_ids[0]]
    assert child.assignee == fleet.coder
    assert fleet.ops_seat in (child.body or "").splitlines()[0]


# --------------------------------------------------------------------------- #
# the resolution ladder — each rung, one case
# --------------------------------------------------------------------------- #


def test_a_single_coder_in_the_roster_is_used_when_the_domain_is_unresolved(tmp_path, monkeypatch):
    """Rule 4's second half: undecidable domain plus exactly ONE ``*-coder`` in the roster."""
    fleet = build_fleet(tmp_path, monkeypatch, checkout=False)
    assert [p for p in fleet.profiles if p.endswith("-coder")] == [fleet.coder]
    children = _instance_children()[:1]
    root_id, outcome = _decompose(
        fleet, children=children, parent_title="Polish the write path",
        parent_body="Tighten the writer and re-run the suite.",
    )

    assert outcome.ok, outcome.reason
    tasks, events, _ = _read(outcome, root_id)
    cid = outcome.child_ids[0]
    assert tasks[cid].assignee == fleet.coder
    assert "routed_by_role" in _kinds(events[cid])


def test_the_parents_own_assignee_stem_resolves_the_domain(tmp_path, monkeypatch):
    """Rung (b): the parent is assigned to a ``<stem>-<role>`` lane, and no repo is named."""
    fleet = build_fleet(tmp_path, monkeypatch, checkout=False)
    children = _instance_children()[:1]
    root_id, outcome = _decompose(
        fleet, children=children, parent_title="Polish the write path",
        parent_body="Tighten the writer and re-run the suite.",
        parent_assignee=fleet.owner_lane,
    )

    assert outcome.ok, outcome.reason
    tasks, _, _ = _read(outcome, root_id)
    assert tasks[outcome.child_ids[0]].assignee == fleet.coder


def test_the_boards_declared_lane_resolves_the_domain(tmp_path, monkeypatch):
    """Rung (c): a board that declares its owning lane routes by it."""
    fleet = build_fleet(tmp_path, monkeypatch, checkout=False, extra_coders=("gadget",))
    slug = kb.get_current_board() or kb.DEFAULT_BOARD
    meta = kbb.read_board_metadata(slug)
    meta["owner_lane"] = fleet.owner_lane
    path = kbb.board_metadata_path(slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(jsonlib.dumps(meta), encoding="utf-8")

    children = _instance_children()[:1]
    root_id, outcome = _decompose(
        fleet, children=children, parent_title="Polish the write path",
        parent_body="Tighten the writer and re-run the suite.",
    )

    assert outcome.ok, outcome.reason
    tasks, _, _ = _read(outcome, root_id)
    assert tasks[outcome.child_ids[0]].assignee == fleet.coder


def test_a_repo_with_no_in_tree_owners_uses_the_owner_registry(tmp_path, monkeypatch):
    """Rung (a), second source: an upstream-foreign repo (its tree can never carry OWNERS)
    is owned by a registry row keyed on the origin slug — the fork's own case."""
    fleet = build_fleet(tmp_path, monkeypatch, checkout=False)
    control = Path(str(tmp_path / "control-plane"))
    (control / "upstream-foreign-owners.conf").write_text(
        "# registry rows: <origin-slug>  <owner-lane>\n"
        f"{fleet.repo_slug}  {fleet.owner_lane}\n",
        encoding="utf-8",
    )
    children = _instance_children()[:1]
    root_id, outcome = _decompose(
        fleet, children=children,
        parent_title="[fix] the guard resolves in the live tree",
        parent_body=f"Route: branch off `main` of git@github.com:{fleet.repo_slug}.git, open a PR.",
    )

    assert outcome.ok, outcome.reason
    tasks, _, _ = _read(outcome, root_id)
    assert tasks[outcome.child_ids[0]].assignee == fleet.coder


def test_a_child_the_llm_already_routed_to_a_lane_is_left_alone(tmp_path, monkeypatch):
    """The guard only corrects the ops seat; a lane pick is the LLM's to make."""
    fleet = build_fleet(tmp_path, monkeypatch)
    children = [{
        "title": "Implement 1 MiB node_outputs cap + elision envelope in StateStore",
        "body": "Where: `yoyoflow/state.py`.",
        "assignee": fleet.coder, "parents": [],
    }]
    root_id, outcome = _decompose(
        fleet, children=children, parent_title=INSTANCE_PARENT_TITLE,
        parent_body=INSTANCE_PARENT_BODY,
    )

    assert outcome.ok, outcome.reason
    tasks, events, _ = _read(outcome, root_id)
    cid = outcome.child_ids[0]
    assert tasks[cid].assignee == fleet.coder
    assert "routed_by_role" not in _kinds(events[cid])
    assert not (tasks[cid].body or "").startswith("ROUTED BY ROLE:")


@pytest.mark.parametrize("bad_assignee", [None, "no-such-profile"])
def test_an_unknown_llm_assignee_is_still_normalised_to_the_seat_first(
    tmp_path, monkeypatch, bad_assignee,
):
    """The mechanism the card names: absent/unknown picks normalise to the seat, and the
    post-route is what corrects a code child out of it."""
    fleet = build_fleet(tmp_path, monkeypatch)
    children = [{
        "title": "Implement 1 MiB node_outputs cap + elision envelope in StateStore",
        "body": "Where: `yoyoflow/state.py`.",
        "assignee": bad_assignee, "parents": [],
    }]
    root_id, outcome = _decompose(
        fleet, children=children, parent_title=INSTANCE_PARENT_TITLE,
        parent_body=INSTANCE_PARENT_BODY,
    )

    assert outcome.ok, outcome.reason
    tasks, _, _ = _read(outcome, root_id)
    child = tasks[outcome.child_ids[0]]
    assert child.assignee == fleet.coder
    assert f"(was {fleet.ops_seat})" in child.body.splitlines()[0]
