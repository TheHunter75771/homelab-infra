# ADR 0002: Claude builds, the owner reviews (team rules)

- Status: accepted (2026-10-23/24)

## Context
The owner wanted the box serving again quickly and chose to review rather than author every file.

## Decision
Builders each own exactly one module. An adversarial Reviewer is the only one who can approve. An Integrator proves deploy and idempotence on the real host. Interface changes go through the Lead and `docs/CONTRACTS.md`.

## Consequences
Higher throughput, and each module is reviewed independently. The owner reviews diffs and ADRs instead of writing the roles.
