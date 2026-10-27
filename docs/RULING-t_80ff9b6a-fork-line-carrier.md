# Ruling record: the fork line is the carrier for t_80ff9b6a

**Card:** `t_80ff9b6a` (the filing budget, enforced at the kernel's create door)
**Domain:** platform — coder implements, `platform-stl` is the design authority
**Carrier:** branch `fix/t_80ff9b6a-live-filing-hook-r3`, base `overrides/live` — the re-cut
that supersedes the reviewed cut on `fix/t_80ff9b6a-live-filing-hook` and the round-1 re-cut on
`-r2` (the house `-rN` convention for a re-cut; a force-push over a reviewed head is not
available to this lane).
**Reviewed cuts (superseded, still readable):** PR **#166** @ **`a39be98a44`**; PR **#167** @
**`163b885431`**.
**This cut:** one commit whose sha is the `-r3` branch head, and the PR opened on it names that
sha. It carries the round-1 tree PLUS the identity-class bound on the closure leg, the band-2
title form and this record.
**Content sha of the module:** `hermes_cli/kanban_filing_policy.py` as it stands on that branch.

## What this change shadows upstream

Two paths, both upstream's own, both changed in place rather than decorated:

- `hermes_cli/kanban_filing_policy.py` — new module: the roster, the precedence bands, the
  pool reading, the closure leg, the refusal record and its one printed line. It is imported
  **inside** the hook, so a host with no policy file pays nothing but two `isfile` stats.
- the `create_task` hook in `hermes_cli/kanban_db.py` — the one moment every filing surface
  reaches (the `kanban_create` tool, `hermes kanban create`, a decomposer, another lane's
  agent). The call is inert on a host with no policy file: upstream's behaviour, byte for byte.

Adjacent files are plumbing for the same seam, not a separate idea: the CLI flag
(`hermes_cli/kanban_parser.py`, `hermes_cli/kanban.py`), the tool schema
(`tools/kanban_tools_schemas.py`, `tools/kanban_tools.py`) and the suite
(`tests/hermes_cli/test_kanban_filing_policy.py`).

## Why it rides the fork line instead of an upstream PR

`platform-stl` ruled the fork line as the carrier (2026-10-27 15:20 EDT, closing
`ESC-d6e6bd8c2cf0cc91`). The reason is a measured permission wall, not a preference:

    gh api repos/NousResearch/hermes-agent --jq .permissions
    {"admin":false,"maintain":false,"pull":true,"push":false,"triage":false}

Measured 2026-10-27 with the only authenticated identity on this host
(`yoyodine-industries`): `pull` only. Admin, maintain and push are **all false**, so this
identity can neither open an upstream PR nor push a branch to upstream — and there is no other
identity on the host to try. Upstream publication is therefore not a review outcome that is
merely deferred; it is unavailable from here, and a change that waits for it would wait
forever.

The change is written to be upstream-able anyway (the inert path is exact, the module is
self-contained, nothing estate-specific is imported at rest), so the day an identity with
`push` exists, the same commit is the PR.

## What the fork line means for the estate

- The fork line (`overrides/live`) is what the host runs: the deployed install is updated from
  it by the sanctioned update routine, never by patching the live install in place.
- This record is the divergence ledger for this card: if the behaviour here ever contradicts
  upstream's documented behaviour for `create_task` on a host with no policy file, **that is a
  defect in this branch**, not a fork "feature". The inert case is the contract.
