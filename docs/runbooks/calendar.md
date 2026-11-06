# Runbook: the household calendar

A CalDAV server (Radicale) on the media server, tailnet only, at `https://<node>.<tailnet>.ts.net:8445/`.
Role `calendar` (docs/CONTRACTS.md §13, ADR 0017). Phones and desktops sync with their own calendar
apps and keep a copy offline. Reminders live on the events and fire on each device, even while the
box or the tailnet is down.

**Deploy only with `playbooks/calendar.yml`**, never `site.yml` (§13.4). Every change below ends with:
```bash
.venv/bin/ansible-playbook playbooks/calendar.yml --check --diff   # see what it would do
.venv/bin/ansible-playbook playbooks/calendar.yml                  # then a second run: expect changed=0
```

Logins: each person has their own (`<user>`), and both phones also add the shared login `household`
for calendars you both see. A login sees only its own calendars.

Paths on the box (`C` is used below):
```bash
C=/srv/homelab/data/calendar/collections        # calendars: $C/collection-root/<user>/<calendar-id>/<event>.ics
```

**Stopping the calendar by hand:** stop the wall-screen view (`homelab-calendar-view`) first, and start
it again once the calendar is back. The view has `Wants=homelab-calendar.service`, so any start or
restart of the view, its own automatic ones included, starts the calendar again in the middle of your work.

## Add an iPhone
The phone must be on the tailnet: the Tailscale app connected, with **VPN On Demand** on (Tailscale →
Settings → VPN On Demand), or sync stops until it's reconnected. Alarms still fire without it.
1. Settings → Calendar → Accounts → Add Account → Other → **Add CalDAV Account** (on iOS 18 and later,
   Calendar is under Settings → Apps).
2. Server: `<node>.<tailnet>.ts.net:8445`. User Name: the person's login. Password: from the password
   manager. Description: e.g. "Home calendar". Tap Next.
3. If it says it can't verify the account: tap **Advanced Settings** and set Use SSL on, Port `8445`,
   Account URL `https://<node>.<tailnet>.ts.net:8445/<user>/`, then Next again.
4. Leave Calendars on, Save.
5. **Repeat steps 1 to 4 for the shared login `household`** (Description: "Household"). Both phones
   add it; that's how shared calendars work (Radicale 3.6 has no sharing that iOS discovers).
6. Settings → Calendar → **Default Calendar**: pick the one new events should go to. To check: open
   Calendar → Calendars, pull down to refresh, and the account's calendars are listed.

## Add Thunderbird (desktop)
Tailscale must be connected on that computer.
1. Calendar tab → **New Calendar…** (the `+` next to Calendars) → **On the Network** → Next.
2. Username: the login. Location: `https://<node>.<tailnet>.ts.net:8445/`. Find Calendars, enter the
   password (let Thunderbird save it), tick the calendars, Subscribe.
3. If it finds nothing, use one calendar's address as the Location:
   `https://<node>.<tailnet>.ts.net:8445/<user>/<calendar-id>/`.
4. Repeat for `household`.

## Add or remove a user
1. **Add:** in `inventory/host_vars/<host>.yml`, add `{ user: <login>, calendars: [{ id: personal, name: Personal }] }`
   to `calendar_accounts`. Logins: 2 to 32 characters, a lowercase letter first, then lowercase letters, digits, `_` or `-`.
   Calendar ids: lowercase letters, digits and `-`. The preflight refuses names the nightly backup skips
   (`cache`, `log`, `logs`, `metadata`, …).
2. Give it a password: `.venv/bin/ansible-vault edit inventory/group_vars/media_servers/vault.yml`, add
   `<login>: "<password>"` under `vault_calendar_passwords`. Generate one with
   `python3 -c 'import secrets;print(secrets.token_urlsafe(18))'` (16 to 72 characters), and save it
   in the password manager.
3. Run `playbooks/calendar.yml` (above). It adds the login and creates the calendars. No restart is needed.
4. **Remove:** delete the entry from `calendar_accounts` and from the vault, then run the playbook.
   The login is removed from the users file, so its devices stop syncing. **Its calendars stay on
   disk** (and in git and the backups) under `$C/collection-root/<login>/`. The role never deletes them.

**Add a calendar** to an existing login: add `{ id, name }` to its `calendars` and run the playbook.
**Rename** one: rename it on a phone (the role never renames). **Delete** one (phones can't, on purpose):
1. Remove it from `calendar_accounts` first, or the next run creates it again.
2. Remove it and commit, as the service account, with the service stopped (`git rm` leaves the
   ignored `.Radicale.cache` behind, hence the `rm -rf`):
   ```bash
   sudo systemctl stop homelab-calendar-view   # first: it would start the calendar again
   sudo systemctl stop homelab-calendar
   sudo -u calendar git -C "$C" rm -r -q collection-root/<user>/<calendar-id>
   sudo -u calendar rm -rf "$C/collection-root/<user>/<calendar-id>"
   sudo -u calendar git -C "$C" commit -q -m "Delete calendar <user>/<calendar-id> (runbook)"
   sudo systemctl start homelab-calendar
   sudo systemctl start homelab-calendar-view
   ```
   The devices drop it on their next sync. It can still be restored from git (below).

## Viewers (read-only logins)
A **viewer** is a login that can only read the calendars listed for it. The wall-screen view
(CONTRACTS §14) logs in as one. It owns no calendars, has no home of its own (the role leaves it an
empty one so Radicale doesn't log a warning on every poll), and can't change anything. Every write,
every unlisted calendar, every home and the root are refused with 403. Accounts are unaffected: they
keep exactly the rights they had (`owner_only`).

**Add a viewer:**
1. In `inventory/host_vars/<host>.yml`, add it under `calendar_viewers`, listing each calendar as
   `<owner>/<calendar id>`. Each one must be a calendar in `calendar_accounts`, and the name follows the
   login rules above and can't also be an account:
   ```yaml
   calendar_viewers:
     - { user: <viewer>, read: [household/shared, household/school, <login>/routines] }
   ```
2. Its password goes in the vault, like any login's: `<viewer>: "<password>"` under
   `vault_calendar_passwords` (16 to 72 characters, saved in the password manager).
3. Run `playbooks/calendar.yml` (above). It adds the login and the read rule (`/etc/homelab-calendar/rights`),
   restarting the calendar (a few seconds; phones retry by themselves).

**Change what it reads:** edit its `read` list and run the playbook. **Remove it:** delete the entry
and its vault password, then run the playbook. The login goes, its rule goes, and its empty home stays (harmless).

**Check a viewer by hand** (from the box; the password is in the password manager):
```bash
curl -s -o /dev/null -w '%{http_code}\n' -u <viewer> -X PROPFIND -H 'Depth: 0' http://127.0.0.1:5232/household/shared/   # 207
curl -s -o /dev/null -w '%{http_code}\n' -u <viewer> -X PROPFIND -H 'Depth: 0' http://127.0.0.1:5232/household/          # 403
```

**The switch to these rules (and the rollback).** Moving from 244eb8b's `owner_only` to the `from_file`
rules is a rights change, even though accounts see no difference (tested path by path). If a phone
loses access after it, roll back by redeploying 244eb8b from a clean checkout:
```bash
git status --short                       # commit or stash your work first
git switch --detach 244eb8b
.venv/bin/ansible-playbook playbooks/calendar.yml --check --diff
.venv/bin/ansible-playbook playbooks/calendar.yml
git switch -                             # back to your branch
```
- That run writes `type = owner_only` back and restarts the calendar. Then:
  - The viewer logins are removed: 244eb8b removes every login that isn't an account. So the wall view
    stops until the new rules are back.
  - The rights file and the viewers' empty homes stay on disk, unused and harmless.
  - It also re-applies 244eb8b's `tailscale serve` config, so a port added later, such as the view's
    8446, goes until you redeploy the current branch.
- **Don't** just change `type =` in `/etc/homelab-calendar/config` by hand. Under `owner_only` a viewer
  login would get a home of its own, with write rights.

## Reset a password
Change `vault_calendar_passwords.<login>` in the vault and run `playbooks/calendar.yml`. That
re-hashes the users file and needs no restart. Then update the password on every device that uses that
login (iPhone: Settings → Calendar → Accounts → the account → Account → Password). See also
`rotate-a-secret.md`.

## Restore one event from the git history
Every change is a commit (`Changes by <login>`). Run git **as the service account**, so new files
keep their owner:
```bash
# Event files have generated names, so find the event by its title: every commit that added or removed it.
sudo -u calendar git -C "$C" log -S'SUMMARY:<title>' --stat --format='%h %ad %s' --date=local -- collection-root/<user>/
# The newest one is the delete (or the bad edit); <rev>^ is the version before it. Check, then restore:
sudo -u calendar git -C "$C" show <rev>^:collection-root/<user>/<calendar-id>/<event>.ics | grep -E '^(SUMMARY|DTSTART)'
sudo -u calendar git -C "$C" checkout <rev>^ -- collection-root/<user>/<calendar-id>/<event>.ics
sudo -u calendar git -C "$C" commit -q -m "Restore <event> from <rev>^ (runbook)"
```
No restart: Radicale reads the files on each request. Let the clients resync (iPhone: Calendar →
Calendars → pull down to refresh; Thunderbird: right-click the calendar → Synchronize).
Never `git reset` or `git checkout` a whole calendar on a live system. Stop the view, then the calendar,
first (`sudo systemctl stop homelab-calendar-view`, then `sudo systemctl stop homelab-calendar`), and start
the calendar, then the view, afterwards.

**A change you expect isn't in `git log`?** A failed hook only logs an error. The change itself is
saved, and the next change that commits picks it up, under that change's login. Look for it:
```bash
sudo journalctl -u homelab-calendar --no-pager | grep -i 'storage hook'   # "Execution of storage hook not successful"
```

## Restore everything from the nightly backup
The nightly archive (`/srv/homelab/backups/appdata/appdata-<UTC time>.tar`, role `backups`) holds
`files.tar.gz` with all of `/srv/homelab/data/`, including `calendar/` and its git history.
1. Pick the archive and check it against its manifest, in a root-only folder (as in
   `restore-from-scratch.md` §4). **Stop if any line isn't OK.**
   ```bash
   R=/var/tmp/restore; sudo install -d -m 0700 "$R"
   sudo tar -xpf /srv/homelab/backups/appdata/appdata-<stamp>.tar -C "$R"
   sudo sh -c "cd $R && sha256sum -c MANIFEST.sha256"
   ```
2. Stop the view, then the calendar, and move the current data aside (keep it until the restore is proven):
   ```bash
   sudo systemctl stop homelab-calendar-view   # first: it would start the calendar again
   sudo systemctl stop homelab-calendar
   sudo mv /srv/homelab/data/calendar /srv/homelab/data/calendar.before-restore
   ```
3. Extract only the calendar, keeping the owners and modes stored in the archive, then clean up:
   ```bash
   sudo tar -xpzf "$R/files.tar.gz" --numeric-owner -C /srv/homelab/data ./calendar
   sudo rm -rf "$R"
   ```
   If the `calendar` account was re-created since (a new uid), fix the owner: `sudo chown -R calendar:calendar /srv/homelab/data/calendar`.
   Then check the history. A backup taken mid-commit can hold a stale lock:
   ```bash
   sudo -u calendar rm -f "$C/.git/index.lock"
   sudo -u calendar git -C "$C" status --short   # expect nothing, or only the changes after the last commit
   sudo -u calendar git -C "$C" fsck             # "dangling" objects are harmless; errors are not
   ```
   The backup keeps the commits, objects and refs, so `git log` and single-event restores work. It
   doesn't keep `.git/logs/` (the reflog): role `backups` skips every folder named `logs`, so `git reflog`
   starts empty after a restore.
4. Run `playbooks/calendar.yml`. It checks the layout and the repo, starts the calendar and proves
   401/207, then starts the view again (it deploys both). Check `sudo -u calendar git -C "$C" log -1` shows the last change you expect.
5. On each device, refresh. Events changed since that backup are lost on the server, but a phone
   that still has them offline may upload them again. Check the calendars on a phone before
   removing `calendar.before-restore`.

## "Calendar is down" push
**It means:** `homelab-calendar.service` failed 5 times within 10 minutes and systemd stopped retrying.
A crash that heals on an automatic restart sends nothing, and one incident sends one push.
The phones keep working from their offline copy, and alarms still fire. Nothing syncs until it's fixed.
When it runs again you get **"Calendar back online"**, which closes the alert on the dashboard.

First three checks:
1. **Why did it stop?**
   ```bash
   systemctl status homelab-calendar --no-pager
   sudo journalctl -u homelab-calendar -b --no-pager | tail -40
   ```
   Look for `Failed to load config file` / `Invalid htpasswd file` / `Failed to load rights file` (a bad
   config, users or rights file),
   `bcrypt` (a package update broke the bcrypt/passlib pair), or `Address already in use` (something
   else on 5232).
2. **Did something change?**
   - The packages: `grep -E 'radicale|bcrypt|passlib' /var/log/apt/history.log | tail`, and
     `python3 -c 'import importlib.metadata as m; print(m.version("radicale"))'` (the role expects the
     series in `versions.yml`).
   - The config: `sudo ls -l /etc/homelab-calendar/`.
   - The disk: `df -h /srv/homelab` (it can't write when the system disk is full).
3. **Fix it and run `playbooks/calendar.yml`.** The role clears the failed state (systemd refuses even
   a fixed start for 10 minutes otherwise), restarts the service and checks 401/207. By hand instead:
   `sudo systemctl reset-failed homelab-calendar && sudo systemctl start homelab-calendar`.

**No "Calendar back online" after the fix?** Every calendar start queues `homelab-calendar-recovered.service`,
which sends that push from its own unit once the server answers (within 30 s). Look at it:
`systemctl status homelab-calendar-recovered --no-pager` and `sudo journalctl -u homelab-calendar-recovered -b`.
`homelab-notify exited 1` means the push itself failed (its reason is on the line before).
`status=216/GROUP` means the push group (`dashboard`) is missing: deploy the dashboard, then run
`playbooks/calendar.yml`, whose preflight refuses to go on without the group. `calendar not
answering … keeping the alert open` means the server wasn't up in time. The marker stays, so the next start
tries again. To prove the push path end to end (this sends one real push to every subscribed device):
```bash
sudo touch /var/lib/homelab-calendar-alert/down && sudo systemctl start homelab-calendar-recovered
sudo journalctl -u homelab-calendar-recovered -n 5 --no-pager   # expect "delivered to N/N device(s)"
```

Phones can't sync but there's no push? `sudo journalctl -u homelab-calendar -b | grep -i 'rights file'`
first: rules are tried in order, and a request that reaches a broken one fails with a 500 ("Error in
section … of rights file") while the service keeps running. The role validates the file before writing it, so this means it was
edited by hand; run `playbooks/calendar.yml` to put it back. Then check the tailnet: is the Tailscale app connected on
the phone, and does `https://<node>.<tailnet>.ts.net:8445/` show "Radicale works!" in a browser on the
tailnet (there's no web UI; that line is all it serves without a login)? Then check
`tailscale serve status` on the box (8445 → `http://127.0.0.1:5232`).
