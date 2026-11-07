# Mission Control web (module MC2)

The Mission Control PWA (`docs/CONTRACTS.md` §12.29). It's a static page that talks only to its own server
(MC1, `apps/mission-control/server/`) on the same origin, which ai-box serves at `https://<ai-box>:8443`. v1
is **read-only**. The only writes are this device's push subscription.

## What gets deployed

**`public/`** is the whole deployable tree, as committed. There's no build step, no dependencies and no external
CDNs. MC1 serves `public/` as static files.

| Path | What |
|---|---|
| `/` → `index.html` | The page. Cards live on one page; `#card-<id>` jumps to one, and `#notify` jumps to the notifications panel. |
| `/sw.js` | The service worker. **It must be served at the root** so its scope is `/`. |
| `/manifest.webmanifest` | name/short_name "Mission Control", `display: standalone`, `#161826` theme and background |
| `/icons/*` | `icon.svg`, `icon-192.png`, `icon-512.png`, `maskable-512.png`, `apple-touch-icon.png` (180), `badge-96.png` (white on transparent, for Android's status bar) |
| `/css/app.css`, `/js/*.js` | ES modules, no bundler |

**What MC1 needs to do when serving it:**
- **Content types:** `.html` `text/html`, `.js` `text/javascript`, `.css` `text/css`, `.svg` `image/svg+xml`,
  `.png` `image/png`, `.webmanifest` `application/manifest+json`.
- **`Cache-Control: no-cache`** on `/`, `/sw.js` and `/manifest.webmanifest`, so updates are picked up.
- **The CSP the page is written for.** It has no inline script, style or handler (`test/static.test.mjs`), so
  it works under:
  `default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; connect-src 'self'; manifest-src 'self'; worker-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'`
  plus `Referrer-Policy: no-referrer` and `X-Content-Type-Options: nosniff`.

## API: the JSON shapes this page expects (to reconcile with MC1)

These are the general rules. The UI tolerates any deviation: a wrong type becomes a fallback, an unknown status
becomes "Unknown", a bad link is dropped, and a missing card shows "The server didn't report this card".

- All timestamps are **epoch seconds** (numbers). Strings are ignored.
- **`status`** is one of `ok | warn | down | unknown | setup`. Aliases are accepted: `good`/`up`/`healthy` → ok,
  `warning`/`stale`/`degraded` → warn, `error`/`fail`/`failed`/`crit`/`critical` → down. Anything else → unknown.
- **Strings** are shown as plain text, trimmed and capped at 500 characters. Numbers are shown as written.
- **Links** must be absolute `http:` or `https:` URLs with no user or password in them. Anything else, such as
  `javascript:`, `data:` or a relative path, is not rendered.
- **Errors** use any 4xx/5xx with `{"error": "text"}`. The page shows "HTTP 502" (and "no answer" for a network
  failure). It doesn't show the body text.

### `GET /api/cards`

```json
{
  "generated_at": 1761652800,
  "config": {
    "open_webui_url": "https://ai-box.<tailnet>.ts.net",
    "vapid_public_key": "B…(base64url, 65-byte P-256 point)…"
  },
  "cards": {
    "ai":         { "status": "ok", "headline": "Answers go to the gaming PC GPU",
                    "items": [{ "label": "Open WebUI", "value": "healthy", "status": "ok" }],
                    "link": "https://…", "error": null, "updated_at": 1761652790 },
    "ai_control": { … },
    "money":      { … },
    "school":     { … },
    "home":       { "status": "setup", "headline": "Connect Home Assistant", "items": [], "link": "http://…:8123/", … },
    "servers":    { … },
    "alerts":     { "status": "down", "headline": "1 alert",
                    "items": [{ "label": "Bank sync failing for 52 h", "status": "down" }], "link": null, … }
  }
}
```

- **`config.open_webui_url`:** the base for the AI card's deep link, and the AI card's link if `cards.ai.link` is
  null. If it's missing or not http(s), the "Open in AI" box is turned off and says why.
- **`config.vapid_public_key`:** the applicationServerKey for subscribing. It must match
  `^[A-Za-z0-9_-]{40,200}$`; otherwise the Enable button is off ("The server hasn't sent its push key yet").
- **A card:** `{status, headline, items, link, error, updated_at}`. Every field is optional.
  - **`items`:** up to 12 are shown, plus "and N more". Each item is `{label, value?, status?}` or a bare string.
    `status` is optional; when it's there, an icon and a screen-reader word go next to the item.
  - **`error`:** a string (null when there's no error). It shows as "Couldn't read this source: <error>". If
    there's an `error` but no `status`, the card reads Down.
  - **Card ids:** `ai`, `ai_control`, `money`, `school`, `home`, `servers`, `alerts`, and the optional `apps`.
    Unknown ids are ignored.
- **`cards.apps`** (B1, optional): the server sends it only when its config has `app_links`. Its `items` are
  `{label, url}` (other fields are ignored, and so are the card's `status`, `headline` and `link`). With no `apps`
  card (or none with a usable label) the page has no Apps section at all, so it is exactly the eight cards.
- **The server formats values.** The page doesn't do money or date maths: it shows `label` and `value` as
  given, so MC1 decides how much detail (for example, budget amounts) appears.
- **Home before the token exists:** `{"status": "setup", "headline": "Connect Home Assistant", "link": "<HA URL>"}`.
  The card then adds the "add a long-lived token on ai-box" note.

### `GET /api/briefing` (today's)

```json
{
  "date": "2026-10-28",
  "generated_at": 1761645600,
  "headline": "Wednesday 28 October",
  "summary": "1 deadline, 2 bills, no alerts",
  "sections": [
    { "id": "school",  "title": "School: next 48 h", "items": [{ "label": "MATH 111 quiz 4", "value": "Wed 23:59" }], "empty": "Nothing due." },
    { "id": "bills",   "title": "Bills this week",   "items": [{ "label": "Phone bill", "value": "Thu" }], "empty": "No bills due." },
    { "id": "alerts",  "title": "Alerts",            "items": [], "empty": "No alerts." },
    { "id": "backups", "title": "Backups",           "items": [{ "label": "All backups fresh", "status": "ok" }], "empty": "" }
  ]
}
```

- **`date`:** `YYYY-MM-DD` in America/New_York. The header shows it as "Wednesday 28 October".
- **`headline`:** shown if present; otherwise the page formats `date`.
- **`sections`:** drawn in order. Items use the card item shape. If a section has no items, its `empty` text is
  shown.

### Push: `POST /api/push/subscribe | unsubscribe | test`

Every POST sends `Content-Type: application/json` **and `X-Mission-Control: 1`**. MC1 should refuse a POST
without that header (a cross-site form can't set it), on top of the `Tailscale-User-Login` check.

| Call | Body | Success |
|---|---|---|
| `subscribe` | `{"subscription": <PushSubscription.toJSON()>, "label": "iPhone"}` | any 2xx. Re-subscribing the same endpoint should update it, not duplicate it. |
| `unsubscribe` | `{"endpoint": "https://…"}` | 2xx; a 404 (already gone) is treated as success |
| `test` | `{"endpoint": "https://…"}` | 2xx once the push is sent to that endpoint |

Response bodies aren't read.

### Push payload (what MC1 sends through web push)

```json
{ "title": "Morning briefing", "body": "Briefing ready: 3 deadlines, 2 bills, 1 alert", "tag": "briefing-2026-10-28", "card": "briefing" }
```

- **`card`** (one of the eight ids, including `briefing`): a tap opens `/#card-<card>`. If the app is already
  open, the tap focuses that window and scrolls to the card, and focus moves to the card's heading.
- **`url`** (optional): a same-origin path such as `/#card-money`, used when there's no `card`. Other origins
  are ignored and the tap opens `/` instead.
- **`tag`:** replaces an earlier notification with the same tag and still alerts (`renotify`). Use a dated tag for
  the briefing, and `alert-<id>` for alerts.
- **Limits:** the title is capped at 120 characters and the body at 400. If the payload isn't JSON, its text
  becomes the body, under the title "Mission Control".

## Behaviour

- **Cards:** in this order: Briefing, AI, AI Control, Money, School, Home, Servers & backups, Alerts.
  - Layout: 1 column on a phone, 2 from 720 px, and 3 from 1100 px.
  - Every status is shown as an icon shape plus a word ("OK", "Warning", "Down", "Unknown", "Set up"), never by
    colour alone.
- **Tap-through:** each card except Briefing and Alerts links to its app (from the card's `link`). The link opens
  in a new context with `rel="noopener noreferrer"`, and has a visible "↗" icon and screen-reader text
  "(opens in a new window)".
- **Apps card** (B1): after Alerts, while the server sends one. One tap-through per app in the server's order (at
  most 12), built like the other tap-throughs (`extLink` → `safeHref`; the label is a Text node). A url that isn't
  plain http(s) without credentials shows its label as text, not a link. No status badge (it's a list of links).
  The section is built once and put on or taken off the page as the server sends it or stops, and each link keeps
  its id (`app-link-<n>`), so keyboard focus survives a refresh.
- **AI card ("Ask anything" box, button "Open in AI"):** the question goes into
  `<open_webui_url>?models=agent_router.auto&q=<question>`.
  - **It only opens the question, ready to send.** Open WebUI 0.12.4 prefills its message box from `?q=` and
    never sends it; Noah presses send there. `?models=` is applied only when `agent_router.auto` is in his model
    list. The wording says so: the help line reads "Opens your question in Open WebUI, ready to send.", and
    nothing on the card says "ask" or "send" as if it had happened (`render.test.mjs` checks this).
  - Each value is percent-encoded with `encodeURIComponent`, so `&`, `#`, `?` and `+` in a question can't add
    parameters or end the query.
  - The URL opens with `window.open(…, '_blank', 'noopener,noreferrer')`. The keyboard's Enter key is labelled
    Go (`enterkeyhint="go"`), not Send.
  - The question is capped at 2000 characters.
  - The server holds no API key for this.
  - The input element survives every refresh, so nothing Noah types is lost.
- **Refresh:** on load, every 60 s while the page is visible, when it becomes visible again, when the device
  comes back online, and from the header button.
- **Failures:** the banner (role=status) says what happened.
  - The server didn't answer: "Can't reach Mission Control."
  - The server answered with an error: "Mission Control answered with an error (HTTP 502)."
  - If the page has a copy, it adds "Showing what it reported 12 min ago."; if not, it adds "Cards fill in when
    it's back.", and every card shows its own note.
  - If the briefing fails, it falls back to the same snapshot logic, with its own message.
- **Notifications panel (`#notify`):**
  - **iPhone or iPad in a Safari tab:** four steps (Share → Add to Home Screen → open it from the Home Screen →
    Enable notifications), with no button. iOS only allows web push for a Home Screen app.
  - **Blocked:** if permission was denied, it shows how to re-allow it on iPhone and on Android or desktop. It
    also covers an insecure context, an unsupported browser, and a service worker that didn't start.
  - **Off:** "Enable notifications". `Notification.requestPermission()` runs straight from the tap, which iOS
    requires. The button is off when there's no server key.
  - **On:** "Send a test" and "Turn off". Results show in a live region. While a request is running, the buttons
    are `aria-disabled`, not `disabled`, so focus stays on them.
  - **Expired:** if `/api/push/test` or `/api/push/subscribe` answers **410 or 404**, the subscription is gone.
    The page drops the browser side (`pushManager` unsubscribe, errors ignored), switches the panel to Off, and
    shows "This device's notifications expired. Turn them on again." Other errors keep the subscription and
    show "The server couldn't … (HTTP n)".
  - **Turn off:** a 2xx, 410 or 404 from `/api/push/unsubscribe` drops the browser side and shows Off. Any other
    failure (a 500, no answer) shows "The server couldn't remove this device (…)" and keeps the panel On, so
    Turn off can be retried.
- **Service worker:**
  - **Shell:** precached atomically per version and served from the cache, so a 502 never replaces it.
  - **`/api/cards` and `/api/briefing`:** each 200 JSON object is saved with `savedAt` for the offline copy.
  - **Push:** see above.
  - **`pushsubscriptionchange`:** registers the new subscription with the saved device label, and unsubscribes
    the old endpoint.
- **Accessibility:**
  - a skip link;
  - each card is a `<section>` labelled by its heading;
  - `:focus-visible` rings;
  - 44 px targets;
  - a 16 px input, so iOS doesn't zoom;
  - `prefers-reduced-motion` respected;
  - WCAG contrast of at least 4.5:1 for every text and background pair, checked against the real tokens.
- **Look:** the Nocturne palette (`#161826` ground, `#9184d9` blurple accent), shared with Money Hub, Dashboard v2
  and AI Control. Fonts are Inter when installed, otherwise the system font stack. No font files are shipped.

## Rendering rule (server data is untrusted)

- **Text only:** every string from the server becomes a Text node (`h()` → `createTextNode`). There is **no
  `innerHTML`, `outerHTML`, `insertAdjacentHTML`, `document.write` or `eval`** in `public/`, and
  `test/static.test.mjs` fails if one appears.
- **Tags and attributes:** tag names are literals, and attribute names come from an allowlist in `dom.js` that
  `test/dom.test.mjs` pins. The allowlist has no `on*`, `href`, `src` or `style`.
- **Links:** an href is set only by `dom.extLink()`, after `model.safeHref()` (absolute http(s), no credentials,
  normalised by the URL parser), or by `dom.hashLink()` (literal `#ids` from the code). No other file sets
  `href` (static test).

## Icons

`npm run icons` (`scripts/make-icons.mjs`) draws the radar mark (rings, crosshair, fading sweep, one blip) from code.
- It writes the SVG and rasterises the PNGs itself (4×4 supersampled), using only `node:zlib`.
- The maskable icon keeps the mark inside the 80% safe zone.
- The tests check that the committed files are exactly what the script draws (pixel-for-pixel), that the safe zone
  holds, and that the badge is pure white on transparent.

## Development

```sh
npm test          # node:test, no dependencies (Node ≥ 22); about 8 s (the icon pixel check is most of it)
npm run stamp     # after changing anything in public/: rewrites the shell list + version in sw.js
npm run icons     # redraw public/icons/ (then npm run stamp)
npm run dev       # local preview: public/ with the CSP above + a fake API from test/fixtures.mjs on 127.0.0.1:8097
                  #   --scenario normal|broken|down|hostile|slow ; switch live: GET /__scenario?name=down
```

The preview server is for local testing only and is never deployed. All fixtures are synthetic (`*.example.test`).

### Tests (`test/`)

| File | What |
|---|---|
| `model.test.mjs` | `normApps` (absent or junk → no card, http(s) links only, 12 max). The **deep link**: round-trips for `& # ? + % / quotes newline unicode`; `a=b&models=evil` can't add a parameter; the exact encoded form; a bad base → no link. Also `safeHref` (javascript:/data:/vbscript:/blob:/file:/credentials/relative refused), `statusKey` (incl. `__proto__`), and tolerant `normalizeCards` / `normalizeBriefing`. |
| `render.test.mjs` | the **Apps card**: absent unless sent (the page is the eight cards), order, links, labels as text for bad links, the 12 cap, taken off and put back, focus kept, drawn from the saved copy. **Error states** per card: a source error, an unknown status, a missing card, a bad link, a bad Open WebUI base, the server down with no copy, the server erroring with a copy (banner + age), the briefing failing, and stale data kept on a later failure. Also normal rendering and links, the ask submit (encoded URL, typed text survives refresh), every push-panel state (the iOS steps), focus kept across re-render, and the controller opening the card on a worker `open-card` message. |
| `hostile.test.mjs` | every string (cards, the Apps card, items, errors, statuses, briefing, snapshot, push message) is `<img src=x onerror=alert(1)> javascript:alert(1) "><script>…`. It proves no element or attribute comes from that text, hrefs are only normalised http(s), and the text shows verbatim. |
| `sw.test.mjs` | the worker in a fake scope: precache, the shell served through a 502, snapshots saved on 200 objects only. **Push** → `showNotification` with icon, badge, tag and card. Odd or forged payloads never point off-origin. **`notificationclick`** opens `/#card-<id>` when no window is open, or focuses the open window and posts `open-card` (a window on another origin is never used). Also `pushsubscriptionchange`, and that **cross-origin requests** (another host, port or scheme) are never answered from the shell or saved as a snapshot. |
| `push.test.mjs` | `pushEnv` (iPhone Safari tab, iPadOS-as-Mac, standalone, denied, insecure, unsupported); subscribe/test/unsubscribe POST bodies and the `X-Mission-Control` header; key rotation; readable errors. **Expired subscription:** 410/404 on test or subscribe drops the local subscription (even if that throws), and the controller's panel goes from On to Off with the expired line; a 500 does not. Unsubscribe: a 500 is an error and stays On; 410/404 drop it. Every `window.open` is `_blank` + `noopener,noreferrer`, and every rendered external link has `rel="noopener noreferrer"`. |
| `dom.test.mjs` | allowlists pinned; `h()` refuses `href`/`style`/string handlers; `extLink` / `hashLink` refuse everything but http(s) / literal ids |
| `static.test.mjs` | banned APIs; no view sets `href`/`src`/`on*`; CSP-clean `index.html`; no other origins; imports resolve; `sw.js` stamped. The **manifest** is valid (names, standalone, colours match the CSS, icon files exist with their real PNG sizes, 192/512 any + 512 maskable, apple-touch 180, badge 96). Icons match the script. The maskable safe zone holds. CSS a11y basics; **WCAG contrast** on the tokens. |
| `mutation.test.mjs` | 19 mutants, each against the test that must catch it, plus an unmutated control that must pass. The mutants: card headline / item label / every Text node / briefing title / error note / Apps label rendered via `innerHTML` or `insertAdjacentHTML` (hostile test); the deep link **without URL encoding**, or with `encodeURI`, and `safeHref` letting any scheme through (model test); the SW **ignoring `notificationclick`**, opening `/` instead of the card, or not telling an open window (sw test); the SW saving error replies as the offline copy (sw test); a card's error not rendered (render test); a 410 not recognised as "gone", the controller ignoring "gone" so a 410 leaves the panel On, unsubscribe swallowing a server 500, and `window.open` without `noopener` (push test); the SW answering cross-origin requests (sw test). |
| `dom-shim.test.mjs` | (the same file as AI Control's) what the shim's HTML parser builds (attributes quoted / unquoted / bare, entities, void and self-closing tags, comments closed by `-->`, `--!>`, `<!-->` or `<!--->` with markup after them), and that its regexes stay linear: inputs that make a backtracking tag pattern blow up (CodeQL js/redos) parse in under 1 s each, in a child process that is killed if it hangs |
| `dev-server.test.mjs` | the preview server on a free port, switched with `GET /__scenario?name=`: each scenario name serves its cards (`down` a 502), and unknown names, `constructor` / `__proto__` / `toString` among them, serve the plain cards without crashing the server |

`test/dom-shim.mjs` (copied from AI Control's web module) is a small DOM with a real HTML parser behind
`innerHTML`. That parser is what lets the hostile test catch an innerHTML regression. Its regexes are linear; the
end-tag walk up the open-element stack is not (quadratic on many unmatched end tags). It is stricter than a
browser in the unsafe direction: a quote or `<` inside an attribute name or unquoted value
(`<img src=x' onerror=…>`, `<img x"y onerror=…>`, `<img src=x<1 onerror=…>`), a quoted value with no space before
the next attribute (`<img src="x"onerror=…>`) and `/` between attributes (`<img/src=x onerror=…>`) parse as text
here while a browser builds the element. So hostile fixtures must not rely on those forms (the list is in the
comment above `parseHTML`).

**CI (the Lead's file):** add a job like `dashboard-web`: Node 22, `working-directory: apps/mission-control/web`,
`npm test`. There's no `npm ci`, because there are no dependencies.
