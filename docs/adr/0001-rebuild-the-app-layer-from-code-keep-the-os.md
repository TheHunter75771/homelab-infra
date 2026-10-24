# ADR 0001: Rebuild the app layer from code, keep the OS

- Status: accepted (2026-10-23/24)

## Context
The server's app layer had drifted (13 compose projects, unpinned images), and a media-drive outage went unnoticed for 8 days because alerts were never wired up.

## Decision
Wipe the app layer and both data areas, keep the Ubuntu LTS install, and rebuild everything from this repository with Ansible. No OS reinstall (no autoinstall in the MVP).

## Consequences
Faster than a reinstall, and the OS stays known-good. The repo becomes the only source of truth. Autoinstall moves to the restore-drill work later.
