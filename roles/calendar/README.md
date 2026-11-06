# Role: calendar

The household calendar: a CalDAV server (Radicale from the Ubuntu archive) as a hardened host
service, outside Docker. Module Q, docs/CONTRACTS.md §13.1, ADR 0017. Phones and desktops use
their own calendar apps; reminders are VALARMs stored on the events and fired by each device, so
there is no push code here beyond the "down" and "back up" alerts.

Nothing in this role touches Docker. The calendar deploys and runs while the boot gate holds Docker.
It listens on `127.0.0.1:5232` only; `tailscale_serve` publishes it on `https://<node>:8445/`
(its own origin, ADR 0015). Deploy it with `playbooks/calendar.yml` only (§13.4). Runbook:
`docs/runbooks/calendar.md`.

## Installed
| Path | What |
|---|---|
| `python3-radicale`, `python3-bcrypt`, `python3-passlib`, `git` | Ubuntu archive, unpinned (unattended-upgrades). Not the `radicale` package (its own unit, user and config) |
| user/group `calendar` | system account, no fixed uid, `/usr/sbin/nologin`, home `/nonexistent`, no supplementary groups, password locked |
| `/etc/homelab-calendar/` | root:calendar 0750 |
| `/etc/homelab-calendar/config` | Radicale settings (root:calendar 0640), checked by Radicale's own loader before it's written |
| `/etc/homelab-calendar/users` | htpasswd, bcrypt (root:calendar 0640), from `calendar_passwords` (vault) |
| `{{ homelab_data_dir }}/calendar/` | calendar:calendar 0750; the nightly backup picks it up (role `backups`, unchanged) |
| `…/calendar/collections/` | the calendars (`collection-root/<user>/<id>/`), and a git repo of their history |
| `homelab-calendar.service` | `python3 -m radicale --config /etc/homelab-calendar/config` |
| `homelab-calendar-alert.service` | `OnFailure=` of the calendar: "Calendar is down" push, plus a marker in `/var/lib/homelab-calendar-alert/` |
| `homelab-calendar-recovered.service` | oneshot, queued by every calendar start (`ExecStartPost=`): runs the script below in its own unit, outside the calendar's IP filter |
| `/usr/local/sbin/homelab-calendar-recovered` | `files/homelab-calendar-recovered.sh`: "Calendar back up" push, if the marker is there |

## Inputs
- `calendar_accounts` (host_vars, never committed): `[{user, calendars: [{id, name}]}]`. Each phone
  adds two CalDAV accounts, its own and `household` (shared calendars; Radicale 3.6 has no sharing
  that iOS discovers, ADR 0017).
- `calendar_passwords` (group_vars → `vault_calendar_passwords`): `{user: password}`.
- The preflight refuses, with a plain-English message:
  - no accounts;
  - a user not matching `^[a-z][a-z0-9_-]{1,31}$`, or a repeated one;
  - a calendar id not matching `^[a-z0-9-]{1,40}$`, or repeated within a user, or an empty name;
  - a password missing or outside 16 to 72 printable ASCII characters (bcrypt 5 refuses more than
    72 bytes, and passlib with it);
  - a login or calendar id named like a folder the nightly backup skips (`cache`, `log`, `logs`,
    `metadata`, …; `calendar_backup_excluded_names`, kept equal to `backups_exclude_dirs` by a test);
  - a non-integer `calendar_port`, an undefined `homelab_data_dir`;
  - a `calendar_listen_host` other than exactly `127.0.0.1`: role tailscale_serve always proxies 8445
    to `http://127.0.0.1:5232`, so any other address would deploy green behind a dead 8445;
  - a missing push-store group (`calendar_push_group`, `dashboard`; a `getent` lookup, also in check
    mode). Deploy role dashboard first. Without the group, systemd refuses to start
    homelab-calendar-recovered.service (216/GROUP), so a "Calendar is down" alert would never close
    (Integrator, VM).
- Password checks run in a `no_log` step that keeps only the account names, so the failure
  message can say which account is wrong without any value reaching the output.

## Logins and calendars
- **Users file:** created empty first (root:calendar 0640, so a hash is never world-readable), then
  `community.general.htpasswd` with `crypt_scheme: bcrypt` per account (`no_log`). It writes
  Apache-style `$2y$` hashes, which Radicale accepts (the smoke test hashes with the module's exact
  passlib call). A login no longer in `calendar_accounts` is removed: the role reads the names with
  `cut -d: -f1` (never the hashes) and sets `state: absent` for the extras. Its calendars stay on
  disk; the role never deletes collection data.
- **passlib first:** `community.general.htpasswd` checks for passlib on the target before it looks
  at check mode, and a check run on a fresh host has installed nothing yet. So the role first probes
  the module's interpreter (`import passlib.apache`, `check_mode: false`).
  - In check mode without passlib, both htpasswd steps skip, with a note saying so.
  - In a real run without it (apt has just installed python3-passlib, so it shouldn't happen), an
    assert fails with the reason, where `no_log` can't hide it.
  - `tests/test_check_mode.yml` runs the real `tasks/config.yml` in check mode on a fresh host, once
    with an interpreter that can't import passlib and once with one that can. It also runs
    `tasks/preflight.yml` in check mode, once with an existing push group and once with a missing
    one, which must stop the run with the plain-English message.
- Radicale re-reads the users file on every login (`htpasswd_cache` is off), so adding, removing or
  re-keying a login needs no restart.
- **Calendars** are ensured over the server's own API, as their owner: `PROPFIND` Depth 0, and on
  a 404 `MKCALENDAR` with the display name (XML-escaped) and a VEVENT-only
  `supported-calendar-component-set`, so iOS doesn't also list each calendar under Reminders.
  `changed` only on create, `no_log` on both. The role never renames or deletes a calendar: a changed `name` in host_vars does nothing
  to an existing one.
- Before the calendars, the run checks that an unauthenticated `PROPFIND /` gets 401 and an
  authenticated one 207 (with retries). Both, and the calendar steps, are skipped in check mode.

## Undo history
`collections/` is a git repo owned by `calendar`, created once (`git init --initial-branch=main`,
`creates:`), with repo-local `user.name "Household Calendar"`, `user.email
calendar@homelab.invalid` (a placeholder) and `gc.autoDetach false`. A `.gitignore` leaves out
`.Radicale.cache`, `.Radicale.lock` and `.Radicale.tmp-*`. The storage hook
`git add -A && (git diff --cached --quiet || git commit -q -m "Changes by "%(user)s)` commits
every change, as `Changes by <login>`. `%(user)s` sits outside the double quotes on purpose: Radicale
shell-quotes the login before it fills it in, so any login, even one with quotes or `;` in it, stays
one argument (defence in depth; the preflight already limits logins to `[a-z0-9_-]`).
- git runs as `calendar`, because git refuses a repo owned by another user. It goes through
  `runuser -u calendar --` from root, not `become_user`: the automation user's sudo rule
  (`playbooks/bootstrap.yml`, `community.general.sudoers` without `runas`) allows root only. The
  settings are read first (`git config --get`) and written only when they differ, so a re-run is `changed=0`.
- The identity is repo-local because the service has no home (`/nonexistent`, `ProtectHome=yes`).
- `gc.autoDetach false`: an automatic gc runs inside the hook, under Radicale's storage lock,
  instead of in the background next to the following change's commit.
- In check mode on a fresh host the account and the repo don't exist yet, so the git steps are
  skipped there, as are the users-file steps until passlib is installed (above). Everything else
  reports what it would change.
- The very first write (a user's first login creates `/<user>/`) commits the `.gitignore`. After
  that, one commit per change and none for a request that changed nothing. A role re-run commits nothing.

## What Radicale 3.6.0 actually does (checked in its source and on a real server)
- **`permit_delete_collection` / `permit_overwrite_collection`** are read from **`[rights]`**
  (`radicale/app/__init__.py`). With both `False`: `DELETE /<user>/<id>/` and a whole-calendar `PUT`
  are 403, while deleting one event still works. `MKCALENDAR` is unaffected.
- **The hook needs no escaping.** Radicale reads the config with `RawConfigParser` (no
  interpolation), so `%(user)s` survives the parser. It then %-formats the hook with shell-quoted
  values (`radicale/storage/multifilesystem/lock.py`: `user`, `path`, `to_path`, `cwd`,
  `request`), so a literal `%` in a hook would have to be `%%`; this hook has none. The hook runs
  through `/bin/sh` with cwd = `filesystem_folder`, inside the storage lock, before the response
  is sent. Radicale SIGKILLs the hook's process group when it ends, and logs (but doesn't fail the
  request on) a non-zero exit.
- **`[web] type = none`:** `GET /` is a 302 to `/.web`, which answers `200 text/plain` "Radicale
  works!" without a login. No UI, and nothing else is reachable without a login.
- **`max_resource_size`** defaults to 10000000, which Radicale caps at 80% of
  `max_content_length` with a warning at every start. The config sets it to that cap (8000000),
  the one option beyond the contract's list.
- **bcrypt:** with `htpasswd_encryption = bcrypt` Radicale refuses to start when bcrypt is 5.x and
  passlib is older than 1.9.3 (`radicale.utils.passlib_libpass_supports_bcrypt`). Ubuntu 26.04 has
  `python3-bcrypt` 5.0.0 and `python3-passlib` 1.9.3 (the libpass fork), which passes. The role asserts
  Radicale's own check, plus the version series (`importlib.metadata.version("radicale")` starts with
  `calendar_radicale_series`, the major.minor of `versions.yml`'s `python_packages.radicale`), after the
  install and before anything is configured.

## homelab-calendar.service
Per §13.1:
- `Type=exec`, `User=`/`Group=calendar`, `Restart=always`, `RestartSec=5s`, and `RestartMode=direct`
  (systemd ≥ 254; Integrator finding, see Alerts);
- `[Unit]` `StartLimitIntervalSec=600`, `StartLimitBurst=5`, `After=network.target`, `OnFailure=homelab-calendar-alert.service`;
- no ordering on docker.service or bootgate.service;
- the dashboard's full hardening set (a test compares it directive by directive with
  `roles/dashboard/templates/homelab-dashboard.service.j2`), with `ReadWritePaths={{ homelab_data_dir }}/calendar`;
- plus `IPAddressDeny=any`, `IPAddressAllow=localhost`, `MemoryDenyWriteExecute=yes`, `UMask=0027`, `MemoryMax=192M`, `TasksMax=64`;
- `ExecStartPost=-+/usr/bin/systemctl start --no-block homelab-calendar-recovered.service` (amended
  §13.1, see Alerts).

`systemd-analyze security` (systemd 259, offline, on the rendered unit): **1.2 OK** (target ≤ 2.0).
What remains: AF_INET sockets and the host network (it listens on loopback for tailscale serve),
no `PrivateUsers=`/`ProcSubset=`, `@resources` in the syscall allow list, and the group-readable `UMask`.

**No directive was dropped.** Radicale 3.6.0 and the git hook ran under exactly this sandbox set,
`MemoryDenyWriteExecute` and `SystemCallFilter=@system-service ~@privileged` included, in a
transient systemd 259 unit (`Seccomp: 2` on the process). MKCALENDAR, PUT and DELETE each committed
once, an unchanged PUT and a refused DELETE added nothing, and the hook logged no error.
`User=`/`Group=` and the IP filter can't be exercised in a user manager; the Integrator's VM covers them.
Python 3.14's experimental JIT, where a distro builds it in, is off unless `PYTHON_JIT=1`, and
`MemoryDenyWriteExecute` would block it; never set that variable for this unit.

Restarts happen through handlers only: `Calendar daemon-reload`, then `Calendar restart` (config,
unit or account changes). The users file and the alert and recovered units never restart the calendar. After 5
failed starts systemd refuses even a fixed start for 10 minutes, so the run clears a `failed` state
(`systemctl reset-failed`) before it starts the service: re-running the playbook is the fix.

## Alerts
- **Down:** after 5 failed starts in 10 minutes the unit is `failed` and `homelab-calendar-alert.service`
  (oneshot, `SuccessExitStatus=2`, the `homelab-backup-alert.service` pattern) first touches
  `/var/lib/homelab-calendar-alert/down` (`StateDirectory=`), then runs
  `homelab-notify --tag calendar --title "Calendar is down" --body "homelab-calendar.service failed. See: journalctl -u homelab-calendar"`.
  - **Only then:** with the default `RestartMode=normal`, every auto-restart passes through the failed
    state and fires `OnFailure=`. The Integrator's VM got 5 "Calendar is down" calls for one broken
    config, and a single crash that healed on its first restart would push "down" and then "back up".
  - `RestartMode=direct` skips that state on auto-restarts, and the start limit's final failure still
    fires `OnFailure=` once. Checked in a systemd 259 user manager with `StartLimitBurst=5`:
    - a crash loop ran the `OnFailure=` unit exactly once (5 times with `normal`);
    - one crash followed by a good restart ran it zero times (once with `normal`).
- **Back up** (§13.1 as amended 2026-11-06): the push runs in **its own unit**,
  `homelab-calendar-recovered.service`. An `ExecStartPost=` process, `+` or not, stays in the
  calendar's cgroup, and `IPAddressDeny=any` applies to the whole cgroup (systemd.service(5),
  systemd.resource-control(5)), so a push from there could never leave the box.
  - **The calendar unit only queues it:** `ExecStartPost=-+/usr/bin/systemctl start --no-block homelab-calendar-recovered.service`.
    - `+`: root, because the calendar account may not start units.
    - `--no-block`: queue the job and return. The recovered unit is ordered `After=` the calendar,
      so waiting for it from inside the calendar's own start would deadlock.
    - `-`: a refused queueing (the unit missing or masked) never fails the calendar's start.
  - Checked in a systemd 259 user manager with the same shape. The start returned at once and the
    recovered unit ran after the main unit was active. A recovered unit exiting 1, or missing, left
    the main unit active. Without `-`, a missing unit failed the start; without `--no-block`, the
    start hung until its timeout and failed.
  - **The recovered unit:**
    - oneshot, `After=network-online.target homelab-calendar.service`, `Wants=network-online.target`;
    - the alert unit's `StateDirectory=homelab-calendar-alert` (the marker), `SyslogIdentifier=homelab-calendar-recovered`;
    - `StartLimitIntervalSec=0` (a refused start would swallow the push), `TimeoutStartSec=5min`;
    - never enabled, with no `Requires=`/`BindsTo=`/`PartOf=`, so it can't stop or hold up the calendar.
    - It has no IP filter. Its sandbox keeps exactly what homelab-notify needs:
      - root with **no capabilities** (`CapabilityBoundingSet=` empty), plus
        `SupplementaryGroups=dashboard` (`calendar_push_group`: role dashboard's `dashboard_user` when
        the play runs that role, else its value; a test keeps the two equal);
      - the push store is readable by that group: dir 0750, `vapid.json` root:dashboard 0640, and
        `subscriptions.json` 0640, which the dashboard re-sets with `fchmod` on every rewrite whatever
        its UMask. A store root couldn't read would fail the push, and one it couldn't see would
        count as "no devices";
      - everything homelab-notify writes is root's own (role notifier: state dir root 0755,
        `history.jsonl` root 0644, its dedupe state and lock 0600), so no DAC override is needed;
      - a test reads those modes from roles dashboard and notifier and from the dashboard server's
        store, so a change there fails here;
      - `ProtectSystem=strict` with `ReadWritePaths=-/var/lib/homelab-notify` (its dedupe state and
        `history.jsonl`, all root's), `ProtectHome`, `PrivateTmp`, `PrivateDevices`, `PrivateIPC`;
      - `RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6`, `SystemCallFilter=@system-service ~@privileged`,
        `MemoryDenyWriteExecute`, the `Protect*`/`Restrict*` set and `LockPersonality`.
    - `systemd-analyze security`: **1.7 OK** (1.9 with the capability). What remains: root, IP
      sockets on the host network with no IP allow list, no `PrivateUsers=`/`ProcSubset=`,
      `@resources`, the default UMask.
    - **Proven with the real homelab-notify** (`roles/notifier/files/homelab_notify.py`, pywebpush
      2.5.0), run through the real script under this sandbox in a transient systemd 259 unit, with a
      throwaway subscription at `https://example.com/`. It answered `HTTP 405`, so DNS, TLS to an
      internet host, payload encryption and VAPID signing all worked, and the request got out.
      `history.jsonl` was written, syslog reached the journal through `/dev/log`, and the marker was
      removed. The same with `MemoryDenyWriteExecute`, so it stays.
    - The same sandbox ran the new script end to end: the real Radicale's 401, `systemctl show` and
      `is-active` (no errors under the syscall filter), then the real notifier's push.
    - Root without capabilities, and the group, can't be reproduced in a user manager: the
      Integrator's one real-notifier run on the host proves them (see the runbook).
  - **The script:** no marker, exits 0 at once. With a marker, "back up" needs Radicale to really
    serve, not just a port that accepts:
    1. Up to 30 s for an unauthenticated `PROPFIND /` to answer **401**: plain bash over `/dev/tcp`,
       HTTP/1.0, so there's no curl dependency. A connection that closes, or any other status (another
       server on the port, a failing Radicale), keeps waiting.
    2. Its `InvocationID` (`systemctl show`), then a 5 s settle period (`calendar_recovered_settle_seconds`).
    3. The service is still active with the same `InvocationID`: a server that bound and then died,
       or restarted, gets no "back up".
    4. Then `--tag calendar --title "Calendar back up"`, and the marker goes.
  - Any check failing: no push, the marker stays for the next start, and the log says which check
    failed ("keeping the alert open"). It always exits 0.
  - Its settings come from the recovered unit's `Environment=` lines; the script's fallbacks match
    the role defaults (a test checks both).

## Tests
```bash
python3 -m unittest discover -s roles/calendar/tests -v
ansible-playbook --check -i localhost, -c local roles/calendar/tests/test_check_mode.yml   # needs the collections
```
Stdlib, PyYAML and Jinja2; `radicale` (the versions.yml pin, plus `bcrypt`) for the server tests,
which skip cleanly without it. Under CI (`CI=true`) a missing PyYAML or Jinja2 fails instead of skipping.
- `test_render.py` (a): every template rendered with the role defaults, the committed group_vars and
  placeholder host vars (`role_render.py`).
  - The config parses with `RawConfigParser` and holds every §13.1 option, with exactly one extra.
  - Radicale's own loader accepts it, and the hook fills in the login the way Radicale does.
  - The service unit matches the contract and the dashboard's hardening set, with no Docker or gate
    ordering and an ExecStartPost= that only queues the recovered unit.
  - The recovered unit has the amended contract's shape: the alert unit's marker dir, no IP filter,
    no dependency that could hold up the calendar, and the sandbox above.
  - Both units record a score ≤ 2.0. The alert unit, the `.gitignore` and the script's environment
    (now set by the recovered unit) agree.
  - The hook, run through `/bin/sh` with a stub git, keeps hostile logins (`a"b`, `x; touch pwned`,
    `$(…)`, `it's`, `a b`) one argument, and runs nothing.
  - Root without capabilities is enough: the push store's and the notifier's modes, read from their roles.
  - The playbook is the targeted deploy.
  - **no_log:** every task that uses `calendar_passwords` in any form gets `no_log: true`. Forms
    covered: `[ ]`, `.get(`, filters, a loop over it and its `item`, a condition. The same goes for
    any variable derived from it (set_fact keys, `register`, task `vars`), followed to a fixpoint.
    - Uses are found inside Jinja (`{{ }}`, `{% %}`, and raw `when`/`that`/`until` values), so a
      mention in a message doesn't count.
    - The one exception is `calendar_password_ok`, which the preflight prints. A test checks it's
      built as `… | map(attribute='key') | list`, account names only, by a `no_log` task.
    - A self-test plants each form and checks the guard catches it.
- `test_server.py` (b): a real Radicale from the rendered config on a free loopback port.
  - Logins are hashed exactly as the htpasswd module does; the repo is set up with the argv and
    settings in `tasks/history.yml`; calendars are created with the body in `tasks/calendars.yml`.
  - 401 without a login or with a wrong one, 207 with one.
  - MKCALENDAR 201 with the display name kept and VEVENT the only advertised component (409 on a
    second create).
  - Another login's PROPFIND, REPORT, GET, PUT, DELETE and MKCALENDAR are all 403.
  - A whole-calendar DELETE or PUT is 403.
  - The four fixtures (weekly + EXDATE, all-day, across the 2026-11-01 New York DST change, two
    VALARMs) round-trip with every property intact.
  - One commit per change (`Changes by alice`), by the placeholder identity with no home and no system
    git config, and none for an unchanged PUT. The tree stays clean, and no cache, lock or temp file is tracked.
  - The recovered script's raw HTTP/1.0 PROPFIND gets this real server's 401, so a marker leads to one push.
- `test_recovered.py` (c): the script with stub `homelab-notify` and `systemctl` on `PATH`, and a real
  loopback HTTP server.
  - No marker: no push and no systemctl call.
  - Marker, a 401, and the same active invocation: exactly one push, and the marker is gone.
  - A notifier exiting 1 or 2, or missing, still exits 0.
  - No push and the marker kept when: nothing listens; the answer is 200, 207, 500 or 503; the
    server accepts and closes; the invocation changes or the service stops during the settle
    period; or there's no invocation.
  - shellcheck when installed (CI's shellcheck step covers `roles/*/files/*.sh`, hence the `.sh` name).
