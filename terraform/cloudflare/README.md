# Cloudflare as code (OpenTofu)

The zone's Cloudflare configuration, which was first set up by hand in the dashboard
(`docs/runbooks/cloudflare.md`), captured as OpenTofu code. The existing objects are adopted
with `import {}` blocks (`imports.tf`): nothing is created, changed or deleted.

**Before every apply, `./plan-guard.sh` must pass; apply that exact saved plan
(`tofu apply <plan>`), never a fresh one.** Never apply without a reviewed plan, and never
`tofu destroy` this module: it is the live public edge
(the homepage, public Jellyfin, the game address and email). Every resource carries
`prevent_destroy`, so a destroy fails anyway (see [Deletion protection](#deletion-protection)).

**Never paste `tofu plan`, `tofu show` or `tofu state` output into a GitHub issue, PR or
comment.** The repo is public, and that output prints the domain, the ids, the VPS address and
the destination email in plain text. Share the `Plan:` summary line only, or review on the
machine that ran the plan.

## What is managed

| File | Resources |
|---|---|
| `dns.tf` | All 8 DNS records: apex CNAME → Pages, `watch.` CNAME → the tunnel (proxied), `game.` A → the edge VPS (DNS-only), the Email Routing MX ×3, DKIM and SPF |
| `zone.tf` | Always Use HTTPS, minimum TLS 1.2, TLS 1.3, SSL mode `full` (+ automatic mode `custom` so it stays put), HSTS (180 days, subdomains, nosniff, no preload) |
| `cache.tf` | The zone's custom cache rules: bypass the cache for the whole `watch.` hostname |
| `tunnel.tf` | The media server tunnel's remotely managed ingress: `watch.` → `http://jellyfin:8096`, everything else → 404 |
| `pages.tf` | The apex's custom-domain binding to the homepage's Pages project |
| `email.tf` | Email Routing: settings, the destination address, the `hello@` forward rule, the (disabled) catch-all |
| `imports.tf` | One `import {}` block per resource (21) |
| `plan-guard.sh` | Plans, then fails if any change deletes or replaces something (tests: `tests/test_plan_guard.sh`) |

Every identifier (domain, account/zone/tunnel/record ids, the VPS address, the destination
email, the DKIM key, the MX priorities, the Pages project name) is a variable whose value lives
only in the git-ignored `terraform.tfvars`. Copy `terraform.tfvars.example` to start one.

## What is deliberately not managed

- **The homepage's status Worker and its route** (`<domain>/status.json`). `wrangler` deploys
  both from the homepage's own repository (`worker/wrangler.toml`). If this module owned them
  too, every `wrangler deploy` and every `tofu apply` would undo the other's view: two tools
  fighting over one object. One owner: wrangler.
- **The Pages project itself** (Git source, build command, env vars, deployments). Its build
  settings change together with the site's build script, so they belong with the site's repo
  and the Pages dashboard. On top of that, with provider 5.28.1 an import-only plan of the
  project is not clean (it plans an in-place change: empty lists vs. null, computed build
  fields), its state would carry deployment metadata and env vars that hold personal data, and
  a wrong edit to its name plans a replace, which deletes the site. Only the custom domain is
  managed here, which is the part this repo's architecture (ADR 0016) depends on.
- **The tunnel object** (`cloudflare_zero_trust_tunnel_cloudflared`). The ingress config only
  needs its id. Owning the tunnel would add its secret to the state, and a wrong edit would plan
  a destroy-and-recreate that cuts the box off until a new token is vaulted and deployed. The
  token stays in the Ansible vault (`vault_cloudflared_tunnel_token`).
- **Other tunnels, zones and Access applications in the account.** They serve other projects.
  This zone has no Access applications.
- **Cloudflare's managed rulesets** (DDoS, the free WAF ruleset, normalization) and zone
  settings left at their defaults (Bot Fight Mode is off by default and must stay off for the
  `watch.` hostname, see the runbook).
- **`cloudflare_email_routing_dns`.** Email Routing created the MX/SPF/DKIM records, and they
  are imported as plain DNS records in `dns.tf` instead, so the zone's whole record set reads in
  one place and no two resources own the same record. The MX and DKIM records are read-only in
  the API: change them through Email Routing, not here.
- **Page Rules** (legacy). An account-owned API token cannot read them (API error 1011), so this
  module cannot see them. None are expected; check the dashboard if in doubt.

## Secrets and keys

Two files, both mode 600, both outside the repo:

| File | Contents |
|---|---|
| `~/.config/homelab-infra/cloudflare.env` | `CLOUDFLARE_API_TOKEN=...` (an account-owned token) |
| `~/.config/homelab-infra/tofu-state.key` | `TF_VAR_state_passphrase=...` (from `openssl rand -base64 32`) |

**State and plan files are encrypted client-side** (OpenTofu `encryption` block in
`versions.tf`: PBKDF2 key from the passphrase, AES-GCM, `enforced = true` for both). The state
holds the destination email, every id and the DNS contents, so it is never stored in plain
text. Without the passphrase, `tofu init`/`plan` stop with an error instead of writing an
unencrypted state.

- **Back up the passphrase in Bitwarden** (a secure note, e.g. "homelab-infra OpenTofu state
  passphrase"). The encrypted state (`terraform.tfstate`, git-ignored) can then be copied to any
  backup safely.
- **If the passphrase is lost,** the state cannot be read. Nothing in Cloudflare is affected:
  move the old state aside, set a new passphrase, put the import blocks and the `*_id` values
  back, and adopt everything again with a reviewed import-only plan.
- Never print either file. Load them into the environment only:
  `set -a; . file; set +a`.

## How to run

```bash
cd terraform/cloudflare
set -a
. ~/.config/homelab-infra/cloudflare.env
. ~/.config/homelab-infra/tofu-state.key
set +a

tofu init                                  # uses the committed .terraform.lock.hcl
./plan-guard.sh --keep cloudflare.tfplan   # read-only plan + delete check; keeps the plan only if it passes
tofu show cloudflare.tfplan                # review it on this machine (never paste it anywhere)
# Once the plan is reviewed, apply exactly the checked file, never a fresh plan:
tofu apply cloudflare.tfplan
rm cloudflare.tfplan
```

`plan-guard.sh` runs `tofu plan` into a private temp dir (0700, removed on exit), reads the plan
with `tofu show -json`, and **fails if any resource change contains a delete**, which includes
every replace. It prints only the `Plan:` summary line and the addresses of offending resources,
never attribute values, and it cross-checks its count against tofu's own summary (it fails
closed on a mismatch). With `--keep PATH` it saves the checked plan (encrypted, mode 600; the
`*.tfplan` name is git-ignored), so the plan that was checked is the plan that gets applied;
`tofu apply` refuses a saved plan if the state changed since. It never applies. A deliberate,
reviewed deletion runs it with `ALLOW_DESTROY=1`, which still lists every delete.

**The first apply only imports.** Its plan must read exactly:

```
Plan: 21 to import, 0 to add, 0 to change, 0 to destroy.
```

Anything else (a change, an add, a destroy) means the code no longer matches the live
configuration: stop and fix the code, don't apply. After the import, `tofu plan` must say
`No changes.` From then on the import blocks are no-ops; remove them and the `*_id` variables in
a reviewed follow-up.

**After adoption, change Cloudflare through this code.** A dashboard edit shows up as drift in
the next plan; either codify it or let the reviewed apply revert it.

### Deletion protection

Every resource has `lifecycle { prevent_destroy = true }`. That is intentional: a
`tofu destroy` or a variable edit that forces a replace would otherwise plan deletes of the
apex, `watch.` and email records, the Pages domain, the tunnel ingress or the destination
address. Destroying the Email Routing settings would also disable Email Routing, which removes
its MX records. With the guard, any such plan stops with an error instead.

**The guard lives in the resource block, so it cannot stop the block's own removal.** Deleting
a resource block, or renaming it without a `moved {}` block, plans a plain destroy that
`prevent_destroy` does not catch. `plan-guard.sh` catches it (any delete fails the guard), and
the plan review backs it up:

- **Any plan with `to destroy` above 0 is rejected** unless the change set out to delete that
  exact object; only then is `ALLOW_DESTROY=1` used.
- **Renaming a resource block:** add a `moved {}` block in the same change; the plan must still
  show `0 to destroy`.
- **Removing something on purpose:** first remove its `prevent_destroy` in a reviewed change,
  then delete it with its own reviewed plan (`ALLOW_DESTROY=1 ./plan-guard.sh --keep ...`).
  Never remove the guard and delete in one unreviewed step.

### DKIM selector rotation

The DKIM record's name carries Cloudflare's selector (`cf2024-1._domainkey`). If Cloudflare
rotates the selector or the key, a plan may show the old selector's record to be created again
(Cloudflare already removed it), a content change, or, once the name is edited, a replace (a
delete plus a create, which the guard stops). Treat any of these as a rotation, not drift:
check the live records, update `dns.tf` (the name) and `terraform.tfvars` (`email_dkim_txt`, the
record id) to match the new record, and re-adopt it with an import, in one reviewed change. The
plan must end with no create and no delete. Never apply it blind.

### Finding the ids (read-only API calls)

```bash
api() { curl -sS -H "Authorization: Bearer $CLOUDFLARE_API_TOKEN" "https://api.cloudflare.com/client/v4/$1"; }
api "zones?name=<domain>"                                   | jq '.result[0] | {id, account: .account.id}'
api "zones/<zone_id>/dns_records?per_page=100"              | jq '.result[] | {id, type, name, content, priority}'
api "zones/<zone_id>/rulesets/phases/http_request_cache_settings/entrypoint" | jq '.result.id'
api "accounts/<account_id>/cfd_tunnel?is_deleted=false"     | jq '.result[] | {id, name, status}'
api "accounts/<account_id>/email/routing/addresses"         | jq '.result[] | {id, email}'
api "zones/<zone_id>/email/routing/rules"                   | jq '.result[] | {id, matchers}'
```

## Versions

- OpenTofu `~> 1.11` (tested with 1.11.5). The `encryption` block is OpenTofu-only.
- Provider `cloudflare/cloudflare` pinned to exactly `5.28.1`; `.terraform.lock.hcl` is
  committed. Upgrade deliberately: bump the pin, `tofu init -upgrade`, and the plan must still
  show no changes.
