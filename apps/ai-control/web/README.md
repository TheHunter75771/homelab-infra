# AI Control web (module W)

The PWA at `/` on ai-box (`docs/CONTRACTS.md` §12.15.6). **P1 is read-only.** It talks only to server S
on the same origin (§12.15.5) and changes nothing.

## What gets deployed

**`public/`** is the whole deployable tree, as committed. There's no build step and there are no dependencies:
S serves `public/` as static files.

- `/` → `index.html`. Tabs live in the hash (`/#/routing`), so the page always stays on path `/`.
- `/sw.js` must stay at the root (scope `/`).
- File types: `.html .js .css .svg .webmanifest`.
- **CSP-clean:** works under S's CSP (`default-src 'self'; style-src 'self'; script-src 'self'; ...`), with
  no inline scripts, styles or handlers. Bar widths are set through the CSSOM.
- **No other origins:** system fonts and inline SVG icons (own drawings, in `js/dom.js`). Nothing is vendored,
  so there are no third-party licenses to ship.

## Code layout (`public/js/`)

| File | Role |
|---|---|
| `format.js` | Formatters: bytes (decimal, like Ollama), durations, relative time, tok/s, the gate window ("30 min", "1 h"), clock time in `server.timeZone`. Server timestamps are epoch seconds only (strings are rejected); the page's own times are ms. |
| `explain.js` | Plain-language sentences: routing reason → sentence, the rule, service labels, status words |
| `state.js` | The pure reducer, plus selectors (log filter, disk rows, history series, audit `since`) |
| `dom.js` | The only DOM builder: `h()` / `s()` with attribute allowlists, inline-SVG icons, badges, bars |
| `ui.js` | Cards, error and empty notes, pickers |
| `spark.js` | The 30-min sparkline (inline SVG) and its text alternative |
| `shell.js` | Header (connection, identity), offline banner, tab bar |
| `tabs/*.js` | `createX(actions)` → `{ el, update(state, now) }` for Overview, Routing, Models, Audit and Logs |
| `api.js` | Same-origin GETs, ETag, the snapshot read |
| `app.js` | The controller: dispatch, render, SSE + poll, data loading |
| `main.js` | Entry point and service-worker registration |

## Rendering rule

Every string from the server is plain text.
- **How strings are rendered:** `h()` turns string children into Text nodes (`createTextNode`, the same as
  `textContent`), and `<pre>` blocks get their text the same way.
- **Banned APIs:** there is no `innerHTML`, `outerHTML`, `insertAdjacentHTML`, `document.write`, markdown or
  `eval` in `public/`, and `test/static.test.mjs` fails if one appears.
- **Tags and attributes:** tag names are literals in the code, and attribute names come from an allowlist
  (no `on*`, `href`, `src` or `style`).
- **Server strings never go in attributes.** Log-source `<option>` values are list indexes; the labels are
  option text. A session id goes into a query only after it passes the helper's id regex, then through
  `URLSearchParams`.

## API used (§12.15.5 and the Lead's notes)

| Call | Use |
|---|---|
| `GET /api/summary` (ETag, `If-None-Match`) | first load; on every SSE `summary` event; the 10 s poll while the stream is down |
| `GET /api/events` (SSE) | `event: summary`. On error the client polls every 10 s and retries the stream after 30 s. |
| `GET /api/gate/history?minutes=` | sparkline: the gate window (`routing.window_s`) in minutes, kept within 30..1440 (on a summary change, at most every 30 s) |
| `GET /api/requests?limit=50` | recent requests (on a summary change, at most every 10 s) |
| `GET /api/logs/sources` | the Logs source picker: `[{id, label}]`. If it fails, the error shows and there is **no fallback list**; Refresh retries. |
| `GET /api/logs?source=&lines=` | Logs tab: `["line", ...]`, oldest first |
| `GET /api/audit/sudo?host=&user=&since=&limit=200` | sudo commands. `since` is integer epoch seconds (24 h / 7 d / 30 d). |
| `GET /api/audit/sessions?user=` | ai-box session list: `{"sessions": [...], "skipped": N}`. A positive integer N shows "N session logs couldn't be read and were skipped."; any other value counts as 0. |
| `GET /api/audit/replay?user=&id=&output=0\|1` | replay: the command line first (`output` is `null`), then `output=1` on "Show output". A late `output=0` reply can't hide the output. A 422 (`{"error":"this session log is damaged"}`) shows that text, offers no "Show output", and moves focus to the replay heading. |
| `GET /api/audit/localai?since=&limit=200` | localai activity (when the account is localai) |

- **Parameters:** only the ones listed are sent, with no cache-busters and no HEAD requests.
- **Failures:** a section that fails shows its own error, and the rest of the page keeps working.

## Behaviour

- **Routing sentences:**
  - "Answers are going to the gaming PC's GPU because no game is running."
  - "…to ai-box's CPU because a game is running (X) and the GPU has averaged 62% over 30 min (limit 50%)."
  - Also covered: `below_threshold`, `game_no_samples`, the gate unreachable (`gate_error`), `broker_down` ("The
    router (broker) is down. AI answers aren't available."), and unknown or contradictory reasons.
  - The window comes from `routing.window_s` everywhere (sentences, the rule, the Routing numbers, the
    sparkline caption and legend). Without it the text says "the gate's averaging window", never a guess.
- **Recent requests:** `alias` is `null` for non-alias requests; the model name shows then.
- **Gate errors:** classified from S's `routing.gate_error_kind` (`http|network|stale|shape|null`) and
  `gate_http_status`, never by parsing `gate_error`: "the GPU gate replied with an error (HTTP 503: no fresh
  sample)" (503 is the gate's no-fresh-sample status, §12.9), "isn't answering (…)", "no fresh reading (…)",
  "replied with something AI Control can't use (…)". A missing or unknown kind gets "can't be read (…)".
- **Sudo events:** rows whose `event` isn't `command` get a badge (icon + word): "Refused", "Wrong password",
  or "Unusual entry" for an unknown value, plus a left rule. The list opens with a count such as
  "3 refused, 1 wrong-password attempt in this period."
- **Ambiguous sudo rows:** `ambiguous === true` (a non-bool counts as false) adds a "Check session recording"
  badge (question-mark icon + words) and visible help text: "This line's details can't be trusted on their
  own. The recorded session is authoritative." The summary counts them ("1 needs checking").
- **Replay focus:** Replay moves focus to the replay heading (`#au-replay-h`, `tabindex=-1`). "Show output"
  keeps its id while loading (`aria-disabled`, `aria-busy`), then focus moves to the output
  (`#replay-output`). If the output fails, focus goes to the heading. The output `<pre>` keeps `tabindex=0`
  so it stays in the tab order and can be scrolled from the keyboard.
- **Status:** status is never shown by colour alone. Every status has an icon shape plus a word ("OK", "Warning",
  "Down", "Unknown", "Critical").
- **Accessibility:**
  - focus-visible rings;
  - `prefers-reduced-motion` respected;
  - 44 px targets;
  - focus stays on a control when the view re-renders (same id);
  - works at 360 px (bottom tab bar), with a top tab bar and two columns from 900 px.
- **Sparkline:** the SVG is `role="img"`, labelled by a visible caption that states the current, peak and
  average values, the limit and how long the GPU was unavailable.
- **Logs:**
  - a source picker (S's allowlist plus `pc:ollama`, `pc:gpu-gate`);
  - 100–1000 lines;
  - a filter that runs on the device (case-insensitive, all words);
  - auto-refresh every 10 s, **off by default**, and only while the tab is visible.
- **Replay:**
  - The command line shows by default; "Show output" fetches `output=1`.
  - The output goes in a `<pre>` as text, with a cap notice: "Replays are capped at 262 KB; this one is
    complete.", or "Output was cut off." when `output_truncated` (a size cap or a line over 8 KiB).
    `output_damaged` gets a note.
- **Service worker (`sw.js`):**
  - **Shell:** precached atomically per version, and served from the cache, so a 502 never replaces it.
  - **Summary:** each 200 `/api/summary` is saved with `savedAt`. When the summary can't be refreshed, the page keeps
    the last good copy (or the saved one) under a banner, with two cases:
    - no answer (network): "Can't reach ai-box. Showing what it reported 12 min ago (15:48)."
    - an HTTP error or an unreadable summary: "ai-box answered with an error (HTTP 502). Showing what it
      reported 15 s ago (18:55)." The header says "Server error" instead of "Offline".
  - **Other requests:** everything else goes to the network.

## Development

```sh
npm test          # node:test, no dependencies (Node ≥ 22)
npm run stamp     # after changing anything in public/: rewrites the shell list + version in sw.js
npm run dev       # local preview: public/ + a fake API from test/fixtures.mjs, on 127.0.0.1:8095
                  #   --scenario normal|gaming|pcoff|down|hostile ; switch live: GET /__scenario?name=offline
```

The preview server is for local testing only and is never deployed.

### Tests (`test/`)

| File | What |
|---|---|
| `format.test.mjs` | bytes, durations, relative time, tok/s, window, clock, epoch-only timestamps |
| `api.test.mjs` | every URL W requests: fixed paths, only documented params, the replay id in a query |
| `explain.test.mjs` | the reason-to-sentence mapping, every branch |
| `state.test.mjs` | reducers (stale replies dropped, validation, replay flow), selectors, offline banner text |
| `smoke.test.mjs` | the shell and all five tabs rendered in a DOM shim from fake summaries: normal, gaming → CPU, PC off, everything down, plus replay/logs/offline states |
| `hostile.test.mjs` | every string (summary, history, requests, logs, audit, replay, errors) is `<img src=x onerror=alert(1)> javascript:alert(1) "><script>…` and the test proves no element or attribute comes from it, and that it shows up as text |
| `mutation.test.mjs` | runs the guarding test against mutated copies of `public/`: renders switched to `innerHTML` / `insertAdjacentHTML` (hostile test), a non-200 summary saved as the snapshot (sw test), `href` / `onclick` added to the allowlists (dom test). Every mutant must fail, and the unmutated control must pass every guarding test. |
| `dom.test.mjs` | pins `dom.js`'s attribute allowlists to an exact set; `h()` / `s()` refuse anything else |
| `sw.test.mjs` | the worker in a fake scope: precache, shell served through a 502, snapshot saved on 200 only (JSON 5xx bodies and a 304 never replace it) |
| `static.test.mjs` | banned APIs, CSP-clean `index.html`, no other origins, imports resolve, `sw.js` stamped, manifest |
| `dom-shim.test.mjs` | what the shim's HTML parser builds (attributes quoted / unquoted / bare, entities, void and self-closing tags, comments closed by `-->`, `--!>`, `<!-->` or `<!--->` with markup after them), and that its regexes stay linear: inputs that make a backtracking tag pattern blow up (CodeQL js/redos) parse in under 1 s each, in a child process that is killed if it hangs |
| `dev-server.test.mjs` | the preview server on a free port, switched with `GET /__scenario?name=`: each scenario name serves its fixture (`offline` a 502 on every API path), and unknown names, `constructor` / `__proto__` / `toString` among them, serve the normal summary without crashing the server |

`test/dom-shim.mjs` is a small DOM with a real (small) HTML parser behind `innerHTML`. That parser is what lets
the hostile test catch a regression. Its regexes are linear; the end-tag walk up the open-element stack is not
(quadratic on many unmatched end tags). It is stricter than a browser in the unsafe direction: a quote or `<`
inside an attribute name or unquoted value (`<img src=x' onerror=…>`, `<img x"y onerror=…>`,
`<img src=x<1 onerror=…>`), a quoted value with no space before the next attribute (`<img src="x"onerror=…>`)
and `/` between attributes (`<img/src=x onerror=…>`) parse as text here while a browser builds the element. So
hostile fixtures must not rely on those forms (the list is in the comment above `parseHTML`). All fixtures are
synthetic.
