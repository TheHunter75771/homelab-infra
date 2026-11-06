# Role: calendar_view

The wall-screen calendar page: `apps/calendar-view` as a hardened host service on the media server.
Module R, docs/CONTRACTS.md §14.2/§14.3. A read-only agenda (today plus the next 6 days) over the
household calendar (role `calendar`, §13), fetched over loopback as a read-only viewer login (§14.1).
It's for the pantry station's wall screen, and also works on the PC and phones.

Nothing in this role touches Docker or the boot gate. The view listens on `127.0.0.1:5233` only;
`tailscale_serve` publishes it on `https://<node>:8446/` (its own origin, §14.4). It's deployed by
`playbooks/calendar.yml`, after `calendar` and before `tailscale_serve` (§14.3). No push alerts in v1:
a dead wall page is visible on the wall.

## Installed
| Path | What |
|---|---|
| `python3-icalendar`, `python3-recurring-ical-events` | Ubuntu archive, unpinned (unattended-upgrades); the major versions are asserted (6 and 3) |
| user/group `calview` | system account, no fixed uid, `/usr/sbin/nologin`, home `/nonexistent`, no supplementary groups, password locked |
| `/opt/homelab-calendar-view/` | the app: launcher `calendar-view` (0755), package `calendar_view/` and `web/` (0644), all root-owned. Anything else there is removed |
| `/etc/homelab-calendar-view/` | root:calview 0750 |
| `/etc/homelab-calendar-view/config.json` | the app's settings, with the viewer's password (root:calview 0640, `no_log`), checked by the app's own loader before it's written |
| `homelab-calendar-view.service` | `/usr/bin/python3 -I -B /opt/homelab-calendar-view/calendar-view --config /etc/homelab-calendar-view/config.json` |

## Inputs
- `calendar_view_calendars` (host_vars, never committed): what the page shows, in display order.
  ```yaml
  calendar_view_calendars:
    - { path: household/shared, label: Shared, color: "#4f8cff" }
    - { path: household/school, label: School, color: "#3ccf91" }
    - { path: alice/routines, label: Routines, color: "#f2a93b" }
  ```
  `path` is `<login>/<calendar id>`, and must be in the viewer's `read` list in role calendar's
  `calendar_viewers`.
- `calendar_view_viewer` (default `viewer`; set it to the live viewer login in host_vars) and its
  password, `calendar_passwords[calendar_view_viewer]` (group_vars → `vault_calendar_passwords`, the same
  entry role calendar hashes).
- `calendar_view_timezone` (`America/New_York`), `calendar_view_days` (7), `calendar_view_port` (5233),
  `calendar_view_listen_host` (`127.0.0.1`).
- `calendar_view_caldav_url`: role calendar's `http://<calendar_listen_host>:<calendar_port>` when the
  play runs that role, else `http://127.0.0.1:5232`.
- The preflight refuses, with a plain-English message:
  - a listen host other than exactly `127.0.0.1` (tailscale serve proxies 8446 there), a port that
    isn't an integer or is the calendar's own;
  - a CalDAV URL other than `http://127.0.0.1:<port>` (the viewer's password goes over it in clear);
  - no calendars or more than 12, a path outside the calendar role's name rules or repeated, an empty
    or over-40-character label, a color that isn't `#rrggbb`;
  - a viewer name outside the login rules, `calendar_view_days` outside 1 to 14, a time zone that
    isn't in `/usr/share/zoneinfo`;
  - a viewer password missing or outside 16 to 72 printable ASCII characters (checked in a `no_log`
    step that keeps only a yes/no, so the message names the login, never a value);
  - when role calendar's `calendar_viewers` is in the play: no entry for the viewer, or a calendar
    shown that isn't in its `read` list. Without this the deploy would go green and every fetch would
    get a 403.

## The app
`apps/calendar-view/README.md` has the details. In short:
- Every minute it sends one CalDAV `REPORT` (calendar-query with a time-range) per calendar, to
  `http://127.0.0.1:5232/<path>/` as the viewer. It uses the configured paths only: the viewer gets 403
  on `/` and on every home (§14.1, amended), so there's no discovery.
- Recurrence, exceptions and time zones come only from `icalendar` and `recurring-ical-events`.
- A fetch is all or nothing. On a failure it keeps serving the last good agenda, marked `stale` with its
  age. With no good fetch yet, `/api/agenda` is 503, never an empty week.
- `GET /api/agenda`, `GET /healthz` (200 while the last good fetch is under 5 minutes old, else 503),
  and the page (`/`, `/static/app.css`, `/static/app.js`). GET and HEAD only, the §14.2 security
  headers on every response.
- A failed fetch is logged with the calendar path and the HTTP status (or the network error's type)
  only. The password is never logged and never on the command line.

## homelab-calendar-view.service
Per §14.3:
- `Type=exec`, `User=`/`Group=calview`, `Restart=always`, `RestartSec=5s`, `RestartMode=direct`;
- `[Unit]` `StartLimitIntervalSec=0`: no start limit, a display keeps retrying;
- `After=network.target homelab-calendar.service`, `Wants=homelab-calendar.service`, **no `Requires=`**,
  so it keeps serving its stale agenda while the calendar is down;
- no ordering on docker.service or bootgate.service;
- the dashboard's full hardening set (a test compares it directive by directive with
  `roles/dashboard/templates/homelab-dashboard.service.j2`), with no `ReadWritePaths=` at all, since
  the service writes nothing;
- plus `IPAddressDeny=any`, `IPAddressAllow=localhost`, `MemoryDenyWriteExecute=yes`, `UMask=0077`,
  `MemoryMax=128M`, `TasksMax=64`.

`systemd-analyze security` (systemd 259, offline, on the rendered unit): **1.2 OK** (target ≤ 2.0).
What remains: IP sockets and the host network (it listens on loopback for tailscale serve and reads the
calendar there), `AF_UNIX`, no `PrivateUsers=`/`ProcSubset=`/`RootDirectory=`, `@resources` in the
syscall allow list, and the localhost IP allow list. `systemd-analyze verify` is clean.

**No directive was dropped.** The app ran under the unit's `[Service]` set in a transient systemd 259
user unit (`Seccomp: 2`, `NoNewPrivs: 1`, empty capability set, `MemoryDenyWriteExecute`). It fetched
from a real Radicale 3.6.0, served the page, `/api/agenda` and `/healthz`, logged nothing unexpected,
and went stale (data kept, one log line naming the path) when Radicale was stopped. A user manager can't
apply `User=`/`Group=` or the IP filter, and `PrivateTmp=`/`ProtectHome=` were relaxed there so the
scratch copy was visible; the Integrator's live run covers those.

`-I -B`: Python sees only the distro's site-packages (no user site, no `PYTHON*` variables) and never
tries to write `__pycache__` into the read-only code dir. The role's validate step runs the same way as
root, so it never leaves a `__pycache__` for the install step to prune on the next run.

Restarts happen through handlers only: `Calendar view daemon-reload`, then `Calendar view restart`
(code, page, settings, unit, account or package changes). After start, `/healthz` must answer 200
within 30 s (10 tries, 3 s apart). 200 needs a good fetch of every calendar, as the viewer, so it proves
the whole chain. Otherwise the run stops with what to look at (`journalctl -u homelab-calendar-view`:
403 means the viewer can't read that path, 401 its password). Skipped in check mode.

## Check mode
On a fresh host nothing is installed yet, so the package version check runs only when the probe can
answer. The config file isn't validated (nothing is written), and the start and health check are
skipped; everything else reports what it would change. `tests/test_offline.yml` runs the install and
config steps in check mode on a fresh directory with an account that doesn't exist, and checks that
nothing was written.

## Tests
```bash
python3 -m unittest discover -s roles/calendar_view/tests -v
ansible-playbook -i localhost, -c local roles/calendar_view/tests/test_offline.yml   # needs the collections
python3 -m unittest discover -s apps/calendar-view/tests -t apps/calendar-view -v   # the app (its README)
```
- `test_render.py`: every template rendered with the role defaults, the committed group_vars and
  placeholder host vars (`view_render.py`).
  - The config is exactly the app's keys, the password comes from `calendar_passwords[viewer]`, colors
    are lowercased and labels trimmed. The app's own loader accepts it, and a password with quotes,
    backslashes and `%` survives the JSON.
  - The CalDAV URL follows role calendar's host and port.
  - The unit has the contract's directives, the dashboard's full hardening set, no `Requires=` and no
    docker or gate ordering, no writable path, no secret, and a recorded score ≤ 2.0.
  - **no_log:** every task that uses `calendar_passwords` (in any form, or through a variable derived
    from it, or by rendering a template that does) has `no_log: true`. Exactly two tasks touch it. The
    one derived name, `calendar_view_password_ok`, is checked to be a yes/no. A self-test plants each
    form and checks the guard catches it.
  - The preflight pins the listen address. The test also checks module C's 8446 handler points at
    `127.0.0.1:5233`, and skips until that handler lands.
  - The install lists match the app's tree, the config task validates with `-I -B --check-config`,
    the health check has its retries, and the handler names are unique in the repo.
- `test_offline.yml` (no root, a scratch dir; the role's task files with a handler that counts restarts):
  - the preflight passes good inputs and stops 9 bad ones, each with its message;
  - install, then strays planted (a removed module, a `__pycache__`, a stray file), then install prunes
    them, and a third install changes nothing;
  - the config is written 0640 and accepted by the installed app; a re-run changes nothing; a value
    the app refuses never reaches the file;
  - check mode on a fresh dir writes nothing.
