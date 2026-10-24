# ADR 0013: The dashboard runs on the host, outside Docker

- Status: accepted (2026-10-24)

## Context
The boot gate holds **all of Docker** when the media drive is missing at boot. A dashboard in Docker would be unreachable on exactly the nights it's needed most. The design's "bad night" view assumes it stays live.

## Decision
Run Dashboard v2 as a hardened systemd service (Node 22 from the distro) under a dedicated unprivileged user, with no ordering on Docker or the gate. Docker operations go through a small root helper (`homelab-dockerctl`, socket-activated, peer-credential checked) that exposes an exact allowlist of operations. The dashboard never gets the Docker socket. The helper never touches Docker while docker.service is inactive, because socket activation would start Docker and re-run the gate.

## Consequences
The live view works on every night, including a failed boot. There's one more host service plus a privileged helper to keep minimal and well-tested. push-core is absorbed: same port, same push store.
