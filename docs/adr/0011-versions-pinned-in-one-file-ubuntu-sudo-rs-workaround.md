# ADR 0011: Versions pinned in one file; Ubuntu sudo-rs workaround

- Status: accepted (2026-10-23/24)

## Context
Reproducible rebuilds need exact versions. Ubuntu 26.04's default sudo-rs prompt isn't recognised by Ansible's become plugin.

## Decision
All images are pinned by tag+digest in `inventory/group_vars/all/versions.yml` (Renovate later). Python and collection pins live next to them. Ansible becomes via the classic `sudo.ws` shipped alongside sudo-rs.

## Consequences
Rebuilds are exact. Updates go through a reviewed change to one file. Revisit sudo.ws when Ansible supports sudo-rs.
