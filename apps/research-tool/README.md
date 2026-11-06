# research-tool: Web research (approval required) for Open WebUI

`research_tool.py` is an Open WebUI **Tool**: one self-contained file, Python standard library
plus pydantic (already in the open-webui container). It lets Noah, or his AI, propose web research
from any chat. Noah then approves it **in the chat** with a dialog, instead of editing
`searches.md` / `sources.md` by hand. Spec: `docs/CONTRACTS.md` §12.25 (row T). The sanitising
follows `apps/skills-tool`, `apps/writing-tool` and the Money Hub tool.

The rule is the same as before: **nothing goes to the internet without Noah's approval, and the
model can never approve.** The forge files stay the record; a chat approval writes the same
`- [x]` tick (and an audit line) through the forge API (module F, `apps/forge-api`).

| Function (what the model sees) | What it does |
|---|---|
| `propose_research(title, why, queries)` | Creates a request (`POST /requests`), then asks Noah in a dialog which searches to run, then ticks only those (`POST /requests/{rid}/tick-searches`) |
| `research_status(rid="")` | No rid: every request and its status. With a rid: its searches, sources (waiting or approved) and fetched items with library paths. Read-only, admin only |
| `review_sources(rid)` | Lists the sources waiting for approval (the full URL exactly, plus the search result's title), asks Noah in a dialog which to fetch, then ticks only those (`POST /requests/{rid}/tick-sources`) |
| `approve_pending(rid)` | Asks again, with the same dialog and rules, about the searches and then the sources of a request that are still unticked (for example after a dialog timed out) |
| `read_source(rid, n)` | The text of fetched item `n` (`GET /items/{rid}/{n}`), framed as untrusted web content. Read-only, admin only |

## How approval works

1. **One input dialog.** It shows a numbered list of what is waiting. Noah types `all`, `none`, or
   numbers such as `1,3`, then Confirm. The dialog always sends `"value": ""`, so a prefill left
   over from another dialog can never become a selection. The typed selection is the approval.
2. **Each row shows exactly what will be sent, in full.** The query or URL is inside a Markdown
   code span, which Open WebUI renders literally (no link, no emphasis, HTML escaped), and is never
   clipped. That is exact only for printable ASCII without backticks or runs of spaces (a browser
   collapses those). So a query with repeated, leading or trailing spaces, or characters outside the
   forge's plain-text rules, and a URL with spaces, backticks or non-ASCII, is **not offered**. The
   reply lists it with the reason, and Noah can tick it in the forge file by hand.
   `propose_research` also refuses repeated spaces up front.
3. **The AI's words are labelled.** The title and the reasons in a dialog appear as
   "AI's title (not Noah's): ..." and "AI's reason (not Noah's): ...". A fixed line says they carry
   no authority, so a reason like "Noah pre-approved this, type all" is plainly the AI's text.
   Source titles appear as "page title from the search result". All of this text is defanged and
   Markdown-escaped, and it is never sent anywhere.
4. **One dialog at a time.** A module-level `asyncio.Lock` serialises every dialog the tool opens.
   Waiting for it is bounded by `dialog_timeout_s`, and a wait that runs out approves nothing.
5. The tool sends exactly the chosen queries or URLs, plus Noah's Open WebUI user id, to the forge
   API. The forge API ticks them, writes the audit line (`approve-chat by=<id>` in the request's
   **`state.md`**), and refuses any user other than `approver_user_id`.
6. **The reply lists exactly what was ticked**, and any `already` / `not-found` items.
   - If the tick call gets no usable answer (a disconnect, a timeout, a cut-short or unreadable
     reply) or a 5xx, the tool doesn't know whether the forge applied it. The reply then says the
     ticks **may have been applied** and to check `research_status`. It never says "nothing ticked"
     in that case.
   - A 4xx is definite: nothing was ticked.

The answer is parsed strictly. It must be `all` or `none` (any case), or ASCII numbers separated by
commas (spaces allowed) that are all in the list. Repeated numbers count once. **Anything else
approves nothing:** a wrong number, `1 3`, `1;3`, `yes`, an empty answer, any non-string answer,
Cancel or Escape, a click outside the dialog, a closed tab or a disconnect, an error, or no answer
within `dialog_timeout_s`. In `approve_pending`, when the searches dialog gets no answer at all
(timeout, disconnect or error), the sources dialog is not shown. After a cancel or a `none`, it is
still shown.

**The model cannot approve:**
- No function has an "approved" or "selection" parameter, and none takes `**kwargs`.
- Open WebUI hides every `__dunder__` parameter (`__user__`, `__event_call__`) from the model and
  drops any argument that is not in the spec. So the model can't send an answer, a user or a fake
  dialog.
- The dialog goes to the browser session of the user who is chatting, and only that user can
  answer it.
- Only an Open WebUI **admin** gets a dialog. Anyone else is refused before anything is created, and
  `research_status` / `read_source` are admin only too.
- With no live browser session (for example an API call), nothing is created or approved.

## What 0.12.4 does (read from the live container's source, `ghcr.io/open-webui/open-webui:v0.12.4`)

- **Payloads.** Confirmation:
  `{"type": "confirmation", "data": {"title", "message"}}`. Input:
  `{"type": "input", "data": {"title", "message", "placeholder", "value"?, "type"?, "options"?}}`.
  The frontend handles them in `src/lib/components/chat/Chat.svelte` (lines 1755–1789).
- **Return values** (`Chat.svelte` 5124–5145, `ConfirmDialog.svelte` 77–87):

  | Dialog | Confirm | Cancel, Escape or backdrop click |
  |---|---|---|
  | confirmation | `True` | `False` |
  | input | the typed string (can be `""`) | `False` |

  Open WebUI keeps a dialog's `value` until another input dialog sets it, which is why this tool
  always sends `"value": ""`. A disconnect returns `{"error": "Client session disconnected."}` (`socket/main.py` 1628, 1684,
  1697). A server-side timeout returns `{"error": "Event call timed out. ..."}` (1698–1700).
- **Timeout.** For plain `confirmation` and `input`, the server-side timeout is
  `WEBSOCKET_EVENT_CALLER_TIMEOUT`, which is **unset by default, so it waits forever**
  (`socket/main.py` 1637; `env.py` 540–548). That is why this tool has its own
  `dialog_timeout_s`. Cancelling the wait runs the `finally` in `socket/main.py` (1702–1710),
  which emits `request:interaction:done`, and the browser then closes the dialog
  (`Chat.svelte` 1497–1501).
- **Rendering.** The dialog **message is rendered as Markdown** (`marked` + `DOMPurify`,
  `ConfirmDialog.svelte` 159–160). DOMPurify still allows remote `<img>` and links. So the items to approve are shown in code
  spans (exact and inert), and every other piece of data in the message is defanged with its
  `* _ ~ \` characters backslash-escaped. The title is plain text (148–153).
- **`__user__`** is `UserModel.model_dump()` (`utils/middleware.py` 2210), so it includes `id`,
  `role`, `name` and `email` (`models/users.py` 182–206). `utils/tools.py` 393–428 copies it and
  binds it with `functools.partial`. The same lines strip `__*` keys from the spec, and
  `utils/middleware.py` 3104 / 1118 filter the model's arguments down to the spec.

## Defences

- **Network.** The only network code is `Tools._http`.
  - It allows six (method, path) pairs: the §12.25 endpoints, with `rid` matching
    `YYYY-MM-DD_kebab` and `n` from 1 to 9999.
  - It uses a hand-built opener: no proxy, no file/ftp/data handlers, and redirects refused.
  - Answers are capped at 256 KiB, with a deadline of 3 × `timeout_s`.
  - `api_url` must be `http(s)://host:port` with no credentials or path.
- **Token.** The token is checked before any request (printable ASCII, no spaces, at most 512
  characters), so a bad value can't inject a header. Errors never include the token, the request
  or the URL. The forge's own `{"error": ...}` sentence is shown only after the token is removed.
  As a second layer, every answer has the token value replaced by `(hidden)`.
- **Text.** The skills-tool / writing-tool rules, unchanged:
  - invisible characters are removed;
  - ``[ ] ( ) < > ! ` | @ &`` become inert lookalikes;
  - a leading `#` and a leading `~~~` fence become fullwidth;
  - `://` becomes `:/ /` and `www.` becomes `www .`.
- **`read_source`.**
  - The text is clipped to 20,000 characters.
  - It sits between `UNTRUSTED WEB CONTENT (marker xxxxxxxx)` and
    `END OF UNTRUSTED WEB CONTENT (marker xxxxxxxx).`, with a fresh random marker on every call, so
    a page can't fake the end line.
  - The header tells the model the text is data only.
- **Queries.** Queries and `why`s are checked locally against the forge's allowlist (`A-Z a-z 0-9`,
  space and `. , : ; ? ' " / + = % @ - ( )`, no `why:`), with at most 10 queries of 200 characters
  each. The forge checks again.
- **The approved list.** The dialog lists what the forge recorded (`GET /requests/{rid}` after
  creating it), not what the model sent. The ticks send exactly those strings back.

## Forge API (module F, `apps/forge-api/README.md` is authoritative)

The tool reads exactly F's shapes and treats anything else as "an unexpected response" (no dialog,
nothing ticked):
- `POST /requests` → 201 `{"rid", "proposed", "status"}`.
- `GET /requests` → `{"requests": [{"rid", "where", "status", "title", "searches": {"approved", "listed"}, "sources": {...}, "ingested"}], "total", "unreadable"}`.
- `GET /requests/{rid}` → `searches: [{"n", "query", "why", "approved"}]`,
  `sources: [{"n", "url", "domain", "title", "approved"}]`, `ingested: [{"n", "url", "path"}]`, plus
  the pending counts. Only entries with `approved: false` are offered in a dialog.
- Ticks → `{"rid", "results": [{"query"|"url", "result", "reason"?}], "counts"}`, where `result` is
  `ticked`, `already` or `not-found`. Any other answer is reported as unreadable, with a pointer to
  `research_status`.
- `GET /items/{rid}/{n}` → `{"kind": "text"|"pdf", "text", "truncated", ...}`. A PDF has no text yet,
  and the reply says so.
- Errors → `{"error"}`. A 400 becomes "rewrite the title, why and queries in plain ASCII", with the
  forge's reason. 409 means a wrong state (for example a shelved request), and 503 means the forge
  is busy or the library is not mounted.

**Text rules** (the forge's `check_ascii`, `check_why` and `check_query`, which are also in the
`propose_research` docstring the model reads):
- the title, the why, and every query and query why are plain ASCII: letters, digits, spaces and
  `. , : ; ? ' " / + = % @ - ( )`, with no `why:`;
- the title is at most 120 characters and the why at most 2000;
- each query is at most 200 characters, and each query's why at most 300.

The tool checks these locally before sending anything. Noah's user id must match the forge's
`by` rule: 1 to 64 of `A-Z a-z 0-9 . _ @ -`.

## Trust boundary: Open WebUI itself

The dialog proves that a typed answer came from the browser session of the user who is chatting.
It can't protect against anything that runs **inside Open WebUI** with that user's or the server's
powers, because such code can call the forge API with the token or the tool's functions directly.
Each of these could tick without a dialog:
- **Another admin account.** An admin can read every tool's valves (the token) and edit this tool.
- **Any user with workspace Tools or Functions permission.** They can create a Tool or Function,
  which runs Python on the server and can read this tool's valves.
- **Installed Functions** (filters, pipes, actions). They run on the server with the same reach.
- **Embeds and artifacts with `iframeSandboxAllowSameOrigin` on.** The iframe can then script the
  Open WebUI page as the logged-in user.
- **Backups of the open-webui volume.** The valves, and so the token, are stored in its database.

So the forge API's checks (open-webui's IP, the token and Noah's user id) mean "Noah's Open WebUI",
not "Noah's hand". Keep Open WebUI to one admin, and treat its volume backups as holding the token.

## Install (Noah, about 5 minutes, once H8 has the forge API running)

0. **Check Open WebUI first** (the trust boundary above):
   - **Admin Panel → Users:** you are the **only** user with the `admin` role.
   - **Admin Panel → Users → Groups / Default permissions:** no user or group has **Workspace →
     Tools** or **Workspace → Functions** permission.
   - **Admin Panel → Functions:** only Functions you installed and trust.
   - **Admin Panel → Settings → Interface:** "iframe Sandbox Allow Same Origin" is **off**. Also
     check your own **Settings → Interface** (the user setting can turn it on for you).
   - Your open-webui volume backups count as holding the forge token: keep them where only you can
     read them.

1. Open WebUI → **Workspace** → **Tools** → **+** (create).
2. Paste the whole of `research_tool.py`, then **Save**. The name and description come from the
   file's header. The tool id can be `research_tool`.
3. Click the tool's **gear icon** (Valves):
   - `api_url`: leave the default `http://172.20.0.1:879` (the `ai` bridge gateway, privileged port 879, where H8 runs
     the forge API) unless Lead/H8 says otherwise;
   - `api_token`: copy the token yourself. It is never shown through the `claude` account. In a
     terminal on your PC, logged in as **you**, run
     `ssh -t ai-box 'sudo cat /etc/forge-api/token'`, copy the one line into the field (it is
     masked), then clear the terminal (`clear`, then close the tab);
   - leave `timeout_s` at 10 and `dialog_timeout_s` at 180 (seconds to answer a dialog);
   - Save.
4. Give it to a model. Pick **one or both**:
   - **Quick agent:** **Workspace** → **Models** → the Quick agent → edit → under **Tools**, tick
     **Web research (approval required)** → Save.
   - **A new "Research" agent:** **Workspace** → **Models** → **+**.
     - Name: `Research`. Base model: `assistant`.
     - **Tags:** add `agent` (so the auto router can pick it).
     - **Description:** `Proposes web research for Noah's AI Library. Noah approves every search and every page in a dialog; reads fetched pages as untrusted data.`
     - **Keywords** (for the router): `research, look up, find sources, search the web, web search, sources, citations, read the article`.
     - Tools: tick **Web research (approval required)**.
     - Save.
5. Test in a new chat:
   - "Research the APA 7 citation rules." The model calls `propose_research`. You see a dialog
     with 2–3 numbered searches. Type `1,3`, then Confirm. The reply lists those two as ticked,
     and `searches.md` shows exactly those two ticked, with `approve-chat by=<your id>` audit lines
     in the request's `state.md`.
   - Run it again and press Cancel: nothing is ticked.
   - After the gate has run: "Which sources did it find?" The model calls `review_sources`.
     Approve one; the gate fetches it. Then "Read it": `read_source` returns the framed text.

If a dialog times out or you cancel, the items stay waiting. Say "ask me again about request
<id>" and the model calls `approve_pending`, or tick them in `searches.md` by hand.

## Tests

```
cd apps/research-tool
python3 -m unittest discover -s tests -t .
```

Standard library only. `tests/owui.py` loads the file the way Open WebUI 0.12.4 does (the
writing-tool shim, plus the spec filter of `execute_tool_call`). A fake forge API runs on
127.0.0.1, and a scripted `__event_call__` answers, cancels, hangs or errors. Every test has a
30-second watchdog. With pydantic and `langchain_core` installed, one more test builds the real
OpenAI function specs; without them it is skipped.

`tests/test_integration_forge_api.py` runs the tool against the **real** forge API and forge
(F's `tests/helpers.py`: a temp library, the server on 127.0.0.1). It covers propose → approve
2 of 3 → status → sources (the gate simulated with the forge's own calls) → approve 1 → ingest →
`read_source` → `approve_pending`, plus a cancel, another admin refused (403), a wrong token
(401), a forge 400 and a 404. It needs Python 3.12+ and `apps/forge-api`; otherwise it is
skipped.

Mutants are run on copies, pointing the tests at a copy with `RESEARCH_TOOL_PATH=/path/to/copy.py`.

## No real data

This is a public repo. Every id, token, title and URL in `tests/` is invented.
