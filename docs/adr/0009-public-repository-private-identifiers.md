# ADR 0009: Public repository, private identifiers

- Status: accepted (2026-10-23/24)

## Context
The repo is portfolio evidence, but must not reveal the network.

## Decision
Hostnames, addresses, domains and UUIDs live only in git-ignored `inventory/host_vars`. Secrets live in ansible-vault. gitleaks runs in pre-commit and CI.

## Consequences
Safe to publish. Reproducing the setup needs a filled-in private inventory.
