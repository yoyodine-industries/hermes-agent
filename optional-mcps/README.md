# optional-mcps — EMPTY on the yoyodine live line

Hermes ships its MCP catalog here: one `<name>/manifest.yaml` per installable MCP server. The
catalog's own header states the trust rule — *presence in this directory = approval* — so an
entry here is the host offering that integration.

On this estate the directory is deliberately EMPTY. Operator rule (2026-10-14): a third-party
integration may be used only if everything stays on yoyodine — no egress. Every entry upstream
ships is a vendor-hosted REMOTE MCP (measured 2026-10-18: 65/65 manifests carry a
`transport.url`), so every entry is a door that would take agent data off the host.

Why the prune is a COMMIT on the override line and not a tree edit (card `t_28fc3e5f`):

- an uncommitted deletion is unmanifested dirt — the next update's stash/fast-forward decides its
  fate, and the override pin store records a blob digest, so it cannot record an absence;
- the live checkout IS the override line `overrides/live`, and the updater advances it by
  merging (`hermes_cli/update_cmd.py`), never by resetting to a bare upstream tree, so a commit
  on the branch is part of every future tree;
- the one case that could bring an entry back — upstream *editing* a deleted manifest — raises a
  `CONFLICT (modify/delete)` at merge time: the update stops loudly instead of resurrecting it
  silently. Both cases are proven in `EVID-t_28fc3e5f/durability-proof.sh`.

Guards:

- `mcp-catalog-guard.py` runs in the maintenance-nightly audit. Any entry present here and not
  listed in `/opt/hermes_prod/yaan-majordomo/config/mcp-catalog-allowlist.conf` is a finding,
  filed to `platform-stl`.
- To add an entry: raise a card, land a commit that restores it, **and** add an allowlist row
  naming that card. Without the row the audit fails on the next run.
- A LOCAL (stdio) MCP does not belong here at all — it is configured in `config.yaml::mcp_servers`.
