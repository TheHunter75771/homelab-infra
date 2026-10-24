# ADR 0008: Security patches automatic, reboots manual

- Status: accepted (2026-10-23/24)

## Context
Under 'everything waits', a surprise reboot during a drive fault takes all services down.

## Decision
unattended-upgrades for security origins only, `Automatic-Reboot false`. Reboots are the owner's choice.

## Consequences
Patched without surprise outages. Kernel updates wait for a manual reboot.
