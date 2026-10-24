# homelab-infra: working rules for Claude

Public repo. Owner: Noah. Build mode (decided 2026-10-23): **Claude builds, Noah reviews.**

- Team rules: builders own exactly the paths in `docs/CONTRACTS.md` §10 and stay in their lane.
  Only the Reviewer approves. The Reviewer is adversarial and names specific failures, or says
  exactly what was tested. Interface changes go to the Lead. The Lead presents to Noah only after
  every task is Reviewer-approved and the Integrator has proven deploy + idempotence on the host.
- Never commit identifiers (hostnames, IPs, tailnet names, domains, emails, UUIDs) or secrets.
  Identifiers go in `inventory/host_vars/` (git-ignored); secrets go in the vault.
- Versions only from `inventory/group_vars/all/versions.yml`.
- Run Ansible tools with `</dev/null 2>&1 | cat` (stdio must be blocking).
- Never touch the edge VPS's other services (finance app, school dashboard, game relay);
  never `nft -f` or flush there.
