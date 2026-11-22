# Role `mc_notify_client`

`mc-notify` for any sending host (B2-CONTRACT.md §5). It pushes a message to Noah's phone through Mission Control's
`POST /api/notify` on ai-box (role `mission_control`), so no host needs its own Web Push sender. The role also
installs `notify-failure@.service`, which a unit names in `OnFailure=` to report its own failure. It is Python 3
standard library only, so the host needs no venv and no packages.

## What it installs

| What | system scope (default; the play needs `become`) | user scope (`systemd --user`, e.g. the gaming PC; `become: false`) |
|---|---|---|
| CLI | `/usr/local/bin/mc-notify` root 0755 (`files/mc_notify.py`, `#!/usr/bin/python3 -I`) | `~/.local/bin/mc-notify` 0755 |
| Config | `/etc/mc-notify/` root 0700; `config.json` root 0600: `{"url", "token_file"}` | `~/.config/mc-notify/` 0700; `config.json` 0600 |
| Token | `/etc/mc-notify/token` root 0600, from the vault, `no_log`; written last, only into a real 0700 directory (a symlinked one is refused before anything is written) | `~/.config/mc-notify/token` 0600, the same way |
| Unit | `/etc/systemd/system/notify-failure@.service` (template, oneshot, sandboxed root without capabilities) | `~/.config/systemd/user/notify-failure@.service` (`templates/user/`, no sandbox; see "systemd --user") |

Nothing is enabled and nothing is sent. The role ends by running `mc-notify --help`, which is read-only.

## Inputs

| Var | Default | What |
|---|---|---|
| `mc_notify_client_token` | `""` (preflight refuses) | **Vault only.** This host's sender token, 32-256 visible ASCII characters. The same value is `mission_control_notify_tokens[<this sender>]` for ai-box. It is never printed: preflight checks it in a `no_log` task, and the token file's task is `no_log` |
| `mc_notify_client_url` | `https://ai-box.<this host's tailnet suffix>:8443/api/notify` (from `tailnet_fqdn`) | must be `https://<name>.ts.net[:port]/api/notify`: no user info, query or fragment. Set it in host_vars on a host without `tailnet_fqdn` |
| `mc_notify_client_scope` | `system` | `system` or `user` (above). The user scope reads the connecting user's home and uid from facts (`gather_subset: min`); preflight refuses a home that is empty, `/`, relative, or has `..`, `//` or a trailing `/` |

```yaml
# A server (system units), e.g. in its playbook with become: true
- role: mc_notify_client
  vars:
    mc_notify_client_token: "{{ vault_mc_notify_tokens['backup-box'] }}"

# The gaming PC's user timers (play: hosts workstations, become: false, gather_subset [min])
- role: mc_notify_client
  vars:
    mc_notify_client_scope: user
    mc_notify_client_token: "{{ vault_mc_notify_tokens['gaming-pc'] }}"
```

**Add a sender end to end:** see role `mission_control`'s README, "Notify senders and the Apps card". In short: make
a token, put it in the vault, set it as `mission_control_notify_tokens[<name>]` for ai-box and as
`mc_notify_client_token` here, then converge ai-box first and this host second.

## mc-notify

```
mc-notify --title T [--body B] --tag t [--url U] [--urgent]
mc-notify --unit=UNIT [--title T] [--body B] [--url U] [--urgent]
```

Write `--unit=UNIT` with the `=` (the units do): a unit name may start with `-` (the root mount is `-.mount`), and
`--unit -.mount` would read it as an option. Every argument must be valid UTF-8, else exit 2.

- **Config:** `$MC_NOTIFY_CONFIG`, else `/etc/mc-notify/config.json`, else `~/.config/mc-notify/config.json`. The
  first that exists is used. A path the caller can't even look at counts as absent: for a non-root user, the
  root-only `/etc/mc-notify/` is skipped. When `$MC_NOTIFY_CONFIG` is set, only that file is used. Both units set it.
  - The config must have exactly `url` and `token_file`. Unknown keys are refused and only counted, never echoed (one
    may be a token pasted into the file). It must belong to the caller or root, and must not be writable by group
    or others (whoever can write it can send the token elsewhere).
  - The token file must be a regular file (not a symlink), owned by the caller, mode **0600 or tighter**. Otherwise
    exit 2.
- **`--unit=UNIT`:** tag `unit-<UNIT>` (sanitized). When not given, the title is `<UNIT> failed` (cut to 80 with
  `…` for very long names) and the body is `journalctl -u <UNIT>`. `--unit` and `--tag` can't be combined.
- **Tags:** lowercased; every character outside `[a-z0-9._-]` becomes `-`; cut to 64. The result must start with a
  letter or digit (MC1's rule), else exit 2.
- **Checked before sending (exit 2), with MC1's own rules:**
  - the title is 1-80 characters after stripping and one line: no control or format characters (Unicode `Cc`
    and `Cf`: bidi overrides and isolates, zero-width spaces and joiners, a BOM), no line breaks, no lone
    surrogates;
  - the body is at most 300 characters;
  - `--url` is at most 512 printable ASCII characters, no backslash. It is either a `/path` (no `//`) or an
    `https://` URL on a `*.ts.net` host, without credentials and without `//` after the scheme.
- **Request:** one `POST` with `Authorization: Bearer <token>` and `Content-Type: application/json`.
  - The JSON holds `title` and `tag`, plus only the optional fields that were given: `body`, `url`, and `urgent`
    only as `true`. MC1 answers 400 to a `null`, a wrong type or an unknown key.
  - **10 s in all.** The name lookup, connect, TLS handshake and answer share one deadline: the exchange runs in a
    worker thread that the CLI waits for 10 s in total, so neither a slow-drip server nor a stuck resolver can
    stretch it. No single socket operation may take longer than 10 s either. TLS is verified with the default
    context.
  - No proxy: `*_proxy` variables are ignored.
  - No redirects: a 3xx is an error, and the token never follows it.
  - `http://` is refused, except `http://127.0.0.1[:port]` when `MC_NOTIFY_ALLOW_INSECURE_LOCAL=1` (tests only;
    `https` is still verified then).
- **Exit codes:** **0** = HTTP 202, prints MC1's status (`sent`, `deduped` or `held`); **3** = HTTP 429 (rate
  limited; prints `Retry-After` when it is a number); **2** = usage or config error; **1** = anything else (other
  HTTP codes, network, TLS, timeout).
- **The token is never printed**, not even on errors:
  - messages never quote the server's answer (only a whitelisted field name on a 400 and a numeric `Retry-After`);
  - they never quote an exception's text (which can quote the `Authorization` header), an unknown config key or
    the token file;
  - a line holding any 8 consecutive characters of the token is written as `<redacted>` instead.

## OnFailure

```ini
# /etc/systemd/system/<unit>.service.d/50-notify.conf   (or ~/.config/systemd/user/<unit>.service.d/ for a user unit)
[Unit]
OnFailure=notify-failure@%n.service
```

Each failure of `<unit>` starts `notify-failure@<unit>.service` once, which runs `mc-notify --unit=<unit>`. On the
phone it shows as "<sender>: <unit> failed", tag `n.<sender>.unit-<unit>`, so a repeat replaces the older notice.
MC1's dedupe (30 min by default) and rate limit apply. The notice unit has no `OnFailure=` of its own, so a failed
push never loops. Its journal (`journalctl -u 'notify-failure@*'`, identifier `mc-notify`) says why.

**Never put it in a catch-all drop-in** (`/etc/systemd/system/service.d/`, or `~/.config/systemd/user/service.d/`):
that gives `notify-failure@` itself an `OnFailure=`, and a failed notice would start another notice, and so on. One
drop-in per watched unit.

## systemd --user

The user copy (`templates/user/notify-failure@.service.j2`) runs the user's own CLI with the user's own config and
token. It leaves out the system copy's sandbox on purpose. Most of those settings need privileges a user manager
doesn't have, and a setting the manager can't apply fails the unit, which would silence the notice. It also has no
`network-online.target`, because the user manager has no such unit. It is installed only in user scope, by the
play that already runs as the desktop user (role `backup_pull`'s pattern).

## Tests

```sh
roles/mc_notify_client/tests/run.sh                               # all of it; no root, no network, nothing sent
python3 -m unittest discover -s roles/mc_notify_client/files/tests -v   # the CLI only
```

1. **`tests/check_no_log.py`:** every task that names `mc_notify_client_token` is `no_log`. No assert checks the
   token itself (only the no_log verdict), and no assert's `fail_msg`, `success_msg` or `msg` templates it. `run.sh`
   also runs it on `tests/fixtures/check_no_log_mutants.yml`: a message templating the token, a condition on the
   token and a write without `no_log` must each fail, and an assert whose prose names the variable must not.
2. **`files/tests/test_mc_notify.py`** (stdlib `unittest`, 50 tests) against a local fake Mission Control (HTTP on
   127.0.0.1, and HTTPS with a throwaway self-signed certificate when `openssl` exists). Every run, in a subprocess
   or in-process, also checks that **no 8 consecutive characters** of the token, or of its base64 at any of the 3
   alignments, are in stdout or stderr (a test proves that check fails on a cut token).
   - **Exit codes:**
     - 0 on 202, printing `sent`/`deduped`/`held`, or `accepted` for an odd body;
     - 3 on 429, with and without a usable `Retry-After`;
     - 1 on 200, 201, 204, 400, 401, 403, 404, 405, 413, 415, 500, 502 and 503, on five redirect codes (the
       redirect target receives nothing), on a closed port, and on a hanging server;
     - 1 within the deadline on a **slow-drip** answer (a byte every 0.1 s, so no single read ever times out) and
       on a **stuck name lookup** (`getaddrinfo` replaced, no real DNS); the deadline is 10 s;
     - 2 on 14 usage errors and on 5 arguments that aren't UTF-8, with nothing sent.
   - **Config:** `MC_NOTIFY_CONFIG` missing is an error, not a fallback; the order env, then `/etc`, then home; 13
     bad configs (unknown keys only counted: "1 unknown key", "2 unknown keys", and a token pasted as a key);
     group- or other-writable configs. http works only to 127.0.0.1 and only with the switch exactly `1`:
     localhost, 127.0.0.2, `[::1]`, `127.0.0.1.nip.io`, user info, `0x7f000001` and a tailnet name over http are
     all refused.
   - **Token file:** 10 loose modes refused, 0600 and 0400 accepted; a symlink, missing, a directory and a FIFO
     (no hang); contents empty, blank, with a space, a newline, a NUL, non-ASCII, or over 4 KiB; surrounding
     whitespace dropped.
   - **Tags and `--unit`:** sanitizing cases; the sent tag cut to 64; the unit's defaults, a given title or body
     winning one by one, an escaped templated unit name, `--unit=-.mount`, a long unit still within the limits.
   - **Request:**
     - its exact shape;
     - only given fields: no `null`, nothing beyond the five keys, `urgent` only as `true`;
     - the title one line: control characters, line breaks and the `Cf` format characters (zero-width space and
       joiner, bidi override and isolate, BOM) are refused, a lone surrogate too, while emoji and a multi-line body
       pass;
     - the url rule (7 good, 22 bad);
     - proxy variables ignored.
   - **Token never printed:** a server echoing it in the status, the error field, the body and `Retry-After`; a
     token or a 12-character piece of one echoed as a status comes out `<redacted>`; exceptions quoting it; error
     paths, including a token pasted as a config key.
   - **HTTPS:** an unknown certificate is refused, even with the test switch; a trusted one works; the wrong name
     is refused.
3. **`tests/test_mc_notify_client.yml`** with `-vvv --diff`:
   - **system scope** into a scratch dir:
     - the derived URL;
     - 32 preflight refusals: scope, URL (http, off the tailnet, a lookalike, another route, the origin only,
       user info, query, fragment, ports 99999 and 0, capitals, the default without `tailnet_fqdn`, not a
       string), the user scope's home (empty, `/`, relative, `..`, a trailing `/`, `//`), paths, and the token
       (empty, null, 31 or 257 characters, a space, a newline, non-ASCII, a number). Nothing is written by a
       refused run;
     - the config tasks' order in `tasks/main.yml`: the guard, the 0700 directory, config.json, then the token;
     - modes, owner and contents; the installed CLI's own loaders accept what the role wrote;
     - a second run is idempotent (same checksums and mtimes);
     - a drifted token and config are put back;
     - a symlinked config directory is refused by the guard, and nothing reaches the directory it points at;
   - **user scope** with a scratch home: the user paths, owner and group omitted, the user unit without the
     sandbox;
   - **both units** with the real paths: equal to `tests/expected/` literally, the contract settings named
     (`--unit=%i`), and `systemd-analyze verify` on an instance of each.
4. **Leak check:** `run.sh` greps the whole output for its canary token, as text and base64 at 3 alignments, and for
   any 12 consecutive characters of it. Any hit fails.

## Notes

- The sending host must resolve `ai-box.<tailnet>.ts.net`, through MagicDNS or a hosts entry. TLS needs the name,
  and mc-notify has no address pinning.
- The tailnet ACL must let the sender reach ai-box:8443.
- **Rotating a token:** set the new value in the vault, converge ai-box (the old hash is replaced), then this host.
