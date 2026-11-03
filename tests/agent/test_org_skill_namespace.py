"""Personal snapshot-entry provenance — the org-skill namespace no longer exists in core.

What this module used to cover (token-gated ``.active_org`` discovery, the ``[org-shared]``
label, the org provenance header, the org delete guard and the auto-propose ``org_sharing``
note), together with Skill Sync (``hermes sync``, ``tools/skills_sync_client*``), was removed
from core in 64ad33e32d; that code lives in the private NousResearch/hermes-skill-sync repo
if the feature ever comes back. This fork's live-override generation (37b2a9e7d5) re-added a
pre-removal copy of the module, so its 18 org/sync assertions imported names that no longer
exist (``skills_sync_client``, ``skills_sync_client_org``, ``sku.ORG_MIRROR_DIR_NAME``,
``prompt_builder.get_all_skills_dirs``) and could only fail. What survives is the single
assertion that still covers live behaviour: ``_build_snapshot_entry`` must leave a personal
entry untouched now that there is no org layer to add provenance from.
"""

import pytest

from agent.prompt_builder import _build_snapshot_entry


@pytest.fixture(autouse=True)
def _legacy_flat_skills_index(monkeypatch):
    """The flat skills index is retired by default (card t_cc3c6951); its listing/provenance
    renderer now runs only behind the escape hatch. The dark default is guarded by
    tests/agent/test_skills_index_deprecated.py."""
    monkeypatch.setenv("HERMES_SKILLS_INDEX", "flat")


def _mk_skill(root, rel, name=None, body="# body\n"):
    d = root
    for part in rel.split("/"):
        d = d / part
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name or rel.split('/')[-1]}\ndescription: d\n---\n{body}",
        encoding="utf-8",
    )
    return d


class TestSnapshotEntryProvenance:
    def test_personal_entry_unchanged(self, tmp_path):
        skills = tmp_path / "skills"
        d = _mk_skill(skills, "devops/beta", name="beta")
        entry = _build_snapshot_entry(d / "SKILL.md", skills, {"name": "beta"}, "d")
        assert "org_id" not in entry
        assert entry["category"] == "devops"
