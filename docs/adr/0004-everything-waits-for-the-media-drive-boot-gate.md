# ADR 0004: Everything waits for the media drive (boot gate)

- Status: accepted (2026-10-23/24)

## Context
The USB media drive sometimes appears minutes late at cold boot and has dropped off while running. `nofail` fstab mounts let apps start against an empty folder.

## Decision
A systemd oneshot gate verifies UUID + sentinel + a write test before Docker may start (`Requires=`). Then a repair ladder, at most one power-cycle (and only during boot), then a push alert. A retry timer and a drop-out watcher stop the media apps if the drive is lost.

## Consequences
No silent empty-library state and no boot loops. Docker, and therefore every container, is down while the drive is missing; SSH and Tailscale stay up.
