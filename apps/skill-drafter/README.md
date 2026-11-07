# skill-drafter: the Skill Forge drafter (module K2)

Spec: `docs/CONTRACTS.md` §12.19 (K2 row, Noah's flow) and §12.12 (the forge's trust model).
Stdlib Python (3.12+; the library needs it). It turns a forge request's ingested sources into a
draft `SKILL.md` through the local model and files it with `forge.draft_skill`. It never decides,
revives, ticks or edits `review.md`; Noah's review is the gate.

## Run

```
./skill-drafter --config /etc/skill-drafter/config.json serve      # the service (poll loop)
./skill-drafter --config config.example.json run-once               # one pass: exit 0 ok, 1 an attempt failed, 2 cannot run
./skill-drafter --config config.example.json status                 # per request: due / waiting (why) / done / failed
python3 -m unittest                                                 # from apps/skill-drafter
```

The launcher finds `forge` and `library` beside it (installed layout) or in `../skill-library`.

Config (JSON, unknown keys refused): `library_root` and `broker_url` (required; a plain
`http(s)://host:port`), `model` (`assistant`), `poll_interval` (120 s), `max_source_chars` (16000,
the budget for all source text together), `num_ctx` (16384, sent to Ollama so the prompt is not
cut from the front), `timeout` (1200 s per broker call; the CPU model writes about 8 tokens/s),
`max_attempts` (3 per round). The library cap comes from `AI_LIBRARY_CAP`, as for the library CLI.

## When a draft is due

- **initial:** `awaiting-source-approval`, at least one ingested source, no `skill/` or
  `review.md`, and every ticked URL settled by the research gate (ingested, refused, or out of
  attempts per `gate-log.md`). Drafting earlier would leave the states where the gate may fetch
  and strand Noah's other ticks.
- **revise:** `awaiting-skill-redraft` (Noah set `decision: revise` and ran `forge decide`) with
  notes in `review.md`: a `notes:` header value, or a `# Notes` / `## Notes` section, or text he
  added around the forge's instructions. Without notes it waits, and it also waits while a ticked
  URL is unsettled. A redraft keeps the skill name.
- **Name clash:** a new draft whose name already exists in `30-skills/` (any case) is filed as
  `<name>-2`, `-3`, ... (kept within 48 characters), and the `ok:` line says so. A revise keeps
  its name.

## What one attempt does

1. **Prompt:** a fixed system prompt; the request title; Noah's request text; on a revise, his
   notes and the previous draft; each source as `<source N id=NONCE>` ... `</source N id=NONCE>`
   with a random per-prompt nonce, tag-like `<` (and its full-width/small-form lookalikes)
   replaced by `‹`, invisible characters dropped; sources cut fairly to `max_source_chars`
   (short ones whole, the rest shared equally); the fixed template; a closing reminder that source
   text is data. PDFs are named but have no text yet.
2. **Broker:** `POST /api/chat`, `stream: false`, `think: false`, temperature 0.2. No proxy, no
   redirects, 1 MiB reply cap, one overall deadline. A reply cut at the length limit is refused,
   and so is one whose `prompt_eval_count` is at or above `num_ctx` − 256 (the prompt was cut);
   both count as attempts.
3. **Validation** (`validate.py`): ≤ 16 KiB, printable, header `name` (kebab-case) /
   `description` / `version: 1`, `# <name>`, the five sections once each (headings may be
   indented up to 3 spaces), numbered Rules, unticked Checklist boxes, ≥ 2 Input/Output examples.
   Refused anywhere: fenced code (``` or ~~~), `>` blockquotes, headings indented 4+ spaces or
   nested in a list, `###` headings named like a section, `---`/`===`/`***` lines (rules,
   underlines, YAML-like blocks); URLs, `//host`, web or e-mail addresses, links, images,
   footnotes, wiki links, `]:` (link definitions, even split over lines), HTML tags and entities;
   and any checkbox-like line outside Checklist (NFKC-folded, after stripping any run of spaces,
   `>` and list markers).
4. **Render:** code writes the header (library keys + name, description, version), the sections in
   template order, and Sources = the ingested URLs as code spans. The model's Sources text is
   discarded.
5. **File:** if the request still needs this very round (it is re-assessed after the model
   answers), write to a temp dir and call `forge.draft_skill(rid, tmp, name=...)`.

## `drafter-log.md`

Its own file in the request folder, not `gate-log.md` (that file's header says only the gate
writes it, and its ledger parses it). Same line shape, both fields code spans:

    2026-10-27T10:00:00-04:00 | draft | `initial 3f2a9c1b04de` | `invalid: missing sections: Examples`

A round is `initial` or `revise` plus a
hash of its inputs (ingested sources, previous draft hash, notes): new inputs start a fresh round,
so a `failed:` round never blocks a later one. Results: `start`, `ok: ...`, `invalid: ...`,
`error: ...`, `error: not counted: ...` (inputs changed while drafting), `failed: ...` (after
`max_attempts`), `skipped: ...` (no source text), `note: sources changed after this draft ...`
(once, when an ingest lands after the draft was filed: a known limit; Noah revises to include it).
Each `ok:` line ends with `sources <hash>`, the set of ingested sources the draft used. An unreachable broker writes nothing and uses
no attempt (journal only), so an outage can't fill the log or burn retries.

## Limits

- Notes are read from `review.md` directly (read-only) until the forge has an API for them.
- `gate-log.md` is parsed with a copy of the gate's ledger rules (`GATE_MAX_ATTEMPTS = 3`).
- The model may still write a poor card; validation checks shape and safety, not quality. Noah's
  review is the quality gate.
