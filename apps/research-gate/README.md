# research-gate: the Skill Forge research gate (module R)

Spec: `docs/CONTRACTS.md` §12.16.3 (behaviour) and §12.12 (the forge's trust model). Stdlib Python
(3.12+; the library needs it), no AI and no autonomy. It runs **only** the searches and fetches
Noah ticked in `35-forge/<id>/searches.md` and `sources.md`, and it re-checks each tick with the
forge (`is_query_approved` / `is_url_approved`) immediately before every network call.

## Run

```
./research-gate --config /etc/research-gate/config.json serve      # the service (poll loop)
./research-gate --config config.example.json run-once               # one pass: exit 0 ok, 1 an item failed, 2 cannot run, 3 tables unreadable
./research-gate --config config.example.json status                 # pending searches/fetches per request
python3 -m unittest discover -s tests -t .                          # from apps/research-gate
```

The launcher finds `forge` and `library` beside it (the installed layout, `/opt/research-gate/`) or
in `../skill-library` (a repo checkout). `python3 -m research_gate` works from a directory that
holds all three packages.

Config (JSON, unknown keys refused): `library_root` (required), `searxng_url`
(`http://127.0.0.1:8888`), `poll_interval` (60 s), `results_per_query` (8), `max_bytes` (5 MiB),
`fetch_timeout` (30 s, the total per fetch), `max_redirects` (3, the maximum), `user_agent`,
`ipv4_only` (false; the deploy sets true because the unit is `AF_INET`-only: names are resolved
with `AF_INET` and IPv6 answers or literals are never used, "no IPv4 address" otherwise). The
library cap comes from `AI_LIBRARY_CAP`, as for the library CLI. **No key relaxes the address
rules;** the tests' loopback allowance is a constructor argument only.

**Interface tables:** the address rules read `/proc/self/net/{if_inet6,ipv6_route,fib_trie,route}`.
systemd's `ProcSubset=pid` hides them, so the unit must not set it. `serve` and `run-once` check
the tables at startup and exit 3 with a clear message if they can't be read (IPv6 tables are not
needed with `ipv4_only`). If they become unreadable later, that pass makes no fetches; a fetch
that loses them mid-hop logs `error: not counted: ...`. Either way the URL stays pending and no
approval or attempt is used up.

## What one pass does

1. **Searches:** for each ticked query not yet run, `GET {searxng_url}/search?q=..&format=json`
   (size cap, deadline, no redirects). The top `results_per_query` http(s) results whose URL passes
   `canonical_url` go to `propose_sources` **unticked**: title stripped of the forge's refused
   characters (and of `why:`), at most 120 characters; `why` = `result for search: <query>`;
   `take` = `page text`. Snippets are never used. No page is fetched here.
2. **Fetches:** for each ticked URL not yet ingested: one DNS lookup, every answer must be public
   (refused: loopback, RFC 1918, link-local, `100.64.0.0/10`, `fc00::/7`, multicast, unspecified,
   reserved and other non-global ranges; IPv4-mapped, NAT64 `64:ff9b::/96` and 6to4 checked by
   their embedded IPv4; Teredo refused), and **the host's own networks**, re-read before every hop
   from `/proc/self/net`: every local interface address and its network at its prefix length
   (`if_inet6`, which covers `noprefixroute` global /64s that have no route), every non-default
   on-link IPv6 route (`ipv6_route`), every local IPv4 address (`fib_trie`) and every on-link IPv4
   route (`route`); embedded IPv4 forms (mapped, NAT64, 6to4, IPv4-compatible) are checked by
   their IPv4 address. Then connect to that
   address with `Host`/SNI/certificate for
   the name; redirects only to ticked URLs (re-checked, re-resolved, at most 3); one total deadline
   enforced by a watchdog; byte cap on the wire and after gzip/deflate decoding; no proxies,
   cookies or credentials. Allowed types: `text/html`, `application/xhtml+xml`, `text/plain`,
   `application/pdf`; anything else is refused and logged.
3. **Filing** into `10-sources/<topic>/` (topic = the request id's slug, as the forge derives a
   default skill name): HTML becomes plain text (`html.parser`; script, style, noscript, nav, form,
   svg, iframe and more dropped; entities decoded; whitespace collapsed; decoded with the declared
   charset only if it is a real text encoding, else UTF-8 with replacement), `text/plain` is kept byte
   for byte, PDF is kept as the original. Each is stored as a **`.txt` / `.pdf` item with a sidecar**
   (never as `.md`), so hostile text is never rendered as Markdown. The sidecar carries `source:`
   (the ticked URL), `forge:` (the request id), `fetched`, `content_type` and, after a redirect,
   `final_url`. Then `record_ingest`, then `INDEX.md` is rebuilt.

## `gate-log.md`

Append-only, plain text, in each request folder; it is also the record of what already ran.

```
<ts> | search|fetch|refuse | `<query or url>` | `<result>`
```

- `start` before each call; `ok: ...` after (final); `error: ...` (retried, at most 3 starts in
  total); `refuse | ... | fetch refused: <reason>` (final: a refused address, content type, size,
  4xx, an unticked redirect target); `... not approved at call time; skipped` (a re-check said no;
  not an attempt).
- The target and the result are both code spans (queries and canonical URLs cannot hold a
  backtick; the result is reduced to printable ASCII without backticks), so a Markdown viewer shows
  them literally: no link, autolinked bare URL, image, entity or HTML. Outside the spans a line has
  only the timestamp, the verb and the separators.
- The result is the gate's own text: error messages name only the local error type (never a
  server's status line, reason phrase or headers), and a media type is logged as a plain
  `type/subtype` token (or `invalid`).
- Errors in the log are one line without a stack trace; the journal (stderr) has the detail.

## Limits

- **Crash windows:** a crash between filing and `record_ingest` (or between `Library.file` and the
  sidecar gaining `forge:`) leaves a filed item that is not recorded; the next attempt fetches
  again and files a `-2` copy. Nothing is deleted; at most 3 attempts.
- A URL that redirects to another ticked URL is fetched once through the redirect (recorded under
  the URL Noah ticked) and once on its own.
- SearXNG at `searxng_url` is trusted configuration: the address rules do not apply to it (it is
  the gate's own loopback/bridge service, pinned by the unit's IP filter).
- Needs a writable temp dir (`PrivateTmp=yes` in the unit) to stage a fetched file for
  `Library.file`.
