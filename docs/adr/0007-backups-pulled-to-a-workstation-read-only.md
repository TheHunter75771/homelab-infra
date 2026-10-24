# ADR 0007: Backups pulled to a workstation, read-only

- Status: accepted (2026-10-23/24)

## Context
Off-box copies are needed, but the server should hold no credentials to other machines.

## Decision
Nightly integrity-checked archives on the server (sqlite `.backup` + a manifest). A workstation user timer pulls them through an `rrsync -ro`-restricted, source-restricted key. The workstation's existing restic job adds the second copy.

## Consequences
A compromised server can't reach the workstation. Two copies in one house is an accepted risk. The media library itself is not backed up.
