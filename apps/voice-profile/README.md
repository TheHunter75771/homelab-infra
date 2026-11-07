# voice-profile: Noah's writing voice (module V)

Spec: `docs/CONTRACTS.md` §12.21 (12.21.2 behaviour, 12.21.3 safety). Stdlib Python (3.12+, because the
library needs it). A oneshot CLI, run on demand on ai-box as `vprof`. It turns the `.docx` samples in
`40-writing/samples/` into cleaned texts, style stats computed in code, and a **draft** voice profile
written by the local model. Noah reviews the draft and sets `status: approved` himself; this tool never
approves anything and never touches an approved profile.

## Run

```
./voice-profile --config /etc/voice-profile/config.json build             # everything; exit 0 ok, 1 draft failed/skipped, 2 cannot run
./voice-profile --config config.example.json build --no-ai                # samples-text + stats only, no model call
./voice-profile --config config.example.json build --dry-run [--no-ai]    # write nothing; print the summary (and a valid draft)
./voice-profile --config config.example.json status                       # what is built, profile status, broker
python3 -m unittest discover -s tests -t .                                # from apps/voice-profile
```

The launcher finds `library` beside it (installed layout) or in `../skill-library`. The library must be
mounted: `.ai-library-sentinel` must be a regular file at its root, or nothing is read or written.

Config (JSON, unknown keys refused): `library_root` and `broker_url` (required; a plain
`http(s)://host:port`), `model` (`assistant`), `num_ctx` (16384), `timeout` (1800 s per broker call),
and caps: `max_samples` (200), `max_docx_bytes` (5 MiB; at most 25 MiB), `max_zip_entries` (2000),
`max_uncompressed_bytes` (200 MiB declared), `max_xml_bytes` (8 MiB inflated, also the most a config may set), `max_ratio` (200),
`min_words` (150 per kept sample), `dedupe_jaccard` (0.6), `excerpts` (4-6, default 6),
`excerpt_chars` (1200). The library cap comes from `AI_LIBRARY_CAP`, as for the library CLI.
Fixed in code (the unit has `MemoryMax=256M`): per sample at most 1 000 000 characters of text,
5000 paragraphs and 20 000 characters per paragraph (an oversized sample is excluded with a manifest
reason, never truncated); all samples together at most 2 000 000 characters (later samples, by
name, are listed as not read); header `source:`/`title:` values at most 200 characters.

## What a build does

1. **Extract** (`docx.py`): the text comes only from `word/document.xml`, so comments, headers,
   footers, footnotes and media are never read; `word/styles.xml` is read for style names and indents
   only, and `[Content_Types].xml` is byte-scanned for macro types. Tracked deletions/moves, field codes,
   text boxes, drawings, tables, hidden runs, `mc:Fallback` copies and cover-page / TOC / bibliography
   content controls are skipped; tracked insertions are kept. The central directory is walked by hand from
   the end record (count, chaining, no ZIP64, one disk) before `zipfile` parses it, and the XML is
   streamed through expat in chunks, so time and memory stay bounded. Refused (listed in the manifest,
   the run goes on): symlinks, oversized files, bad zips, too many entries, unsafe entry names (absolute,
   `..`, backslash, drive, NUL), duplicate names, encrypted entries, VBA macro parts or macro content
   types, a declared size or compression ratio over the caps (zip bombs), compression other than
   stored/deflated, XML inflating past `max_xml_bytes`, elements nested deeper than 128, any single
   tag, comment, PI or CDATA section over 64 KiB (a quote-aware byte scan ahead of expat, which
   buffers each of those whole), XML that is not UTF-8 or declares a
   DOCTYPE/ENTITY (refused by a byte scan of every chunk and again by the expat handlers), and a sample
   over the text caps. Nothing is extracted to disk and nothing is executed. Only `*.docx` is read (`.doc`, `.docm`, others
   are listed as not read); hidden files and Word's `~$` lock files are ignored.
2. **Clean** (`clean.py`): drops the cover block (the leading short lines: title-page styles, name,
   course code, instructor, school, dates, short title-like lines), the reference list (a "References" /
   "Works Cited" / "Bibliography" ... heading and everything after it, plus hanging-indent entries), block
   quotes (a quote-like style id or name, also through `styles.xml` and its basedOn chain, or a left
   indent of 0.5 inch or more beyond the default style's, set directly or by the style, on a non-list
   paragraph), paragraphs that are one quotation, and quoted spans longer than one sentence or 40 words
   (replaced by `[quotation omitted]`): “double”, "straight", mismatched “mixed", ‘single’ (a ’ before a
   letter is an apostrophe), and a curly opener never closed in the paragraph. **Peer reviews** (after
   NFKC and dash folding, a file name, heading or short top line matching
   `peer<sep>(review|evaluation|feedback|critique|assessment)`, "workshop feedback" or "review of
   <name>('s) draft") are excluded unless the file name is listed in `40-writing/include.txt` (one per
   line, `#` comments, any case). **Near-duplicates:** 5-word shingles over the whole cleaned document; Jaccard >= 0.6 groups
   documents (transitively) and keeps the longest, then the latest (mtime), then by name; then any paragraph
   that repeats one already kept (Jaccard >= 0.6) is dropped, and a sample left under `min_words` is
   excluded. Shingles are stored as integers, and a shingle found in more than 10 kept paragraphs is
   not used to find duplicate candidates, so the work stays near-linear. Headings are kept as `## `
   lines, list items as `- ` lines.
3. **Stats** (`stats.py`): sentence and paragraph length distributions, vocabulary richness (TTR, MATTR,
   hapax share, word length), top transitions and openers, punctuation per 1000 words, first/second/third
   person, contractions and "n't" share, a passive-voice estimate, citation habits. Written to
   `40-writing/style-stats.md`.
4. **Draft** (`prompt.py`, `broker.py`, `validate.py`): a fixed system prompt, the stats and 4-6
   representative excerpts (closest to the median sentence length, about 120 words, one per document per
   round), each fenced as data with a random nonce (`<stats id=N>`, `<excerpt k id=N>`; tag-like `<` inside
   becomes `‹`), the fixed template. `POST /api/chat` (no proxy, no redirects, 1 MiB cap, one deadline,
   temperature 0.2). Refused: a reply cut at the length limit, a `prompt_eval_count` at or above
   `num_ctx` - 256 (the prompt was cut), and anything the validator rejects: over 12 KiB, unprintable,
   header not `status: draft`, missing/extra/duplicate/empty sections (Tone, Sentence style, Word choice,
   Structure, Do, Don't, Examples), Do/Don't without `- ` items, fewer than 2 Before/After pairs, `###`
   outside Examples, fences, `>` lines, rules, nested headings, checkboxes anywhere, links, URLs, web/e-mail
   addresses, images, footnotes, HTML. Code re-renders the file: its own header (`status: draft`), title,
   note and the sections in order. Fewer than 4 usable excerpts: no model call.

## Writes

`40-writing/samples-text/<slug>.md` (header `source:` = the original file name, `generator:
voice-profile`), `40-writing/samples-text/MANIFEST.md` (kept, merged, excluded and why, archived, left
alone), `40-writing/style-stats.md`, and `40-writing/voice-profile.md`, or `voice-profile.next.md` when
the main file is not a draft. The main profile is replaced only when its header parses and says
`status: draft`; approved, unparseable or any other status is left alone (checked again after the model
answers, and once more just before the write). A draft that is replaced (Noah may have edited it) is
first copied to `90-archive/40-writing/` by `archive.copy_archive` with the identity (device, inode,
size, mtime) of the very file just read as a draft: it is removed only if it is still that file (see
below), so a save Noah makes at any moment keeps his file and stops the build. The new profile is
then written only if its name is absent (`archive.write_new`: temp file, fsync, `link(2)` that fails
on `EEXIST`, or an `O_EXCL` placeholder where hard links are missing). If
`.next` is also not a draft, the model is not called. The profile's note reads: "Noah reviews and
approves this profile; the AI uses it only for drafts he reviews." A header holding FF, VT, NEL,
U+2028/2029 or a lone CR counts as unreadable, so that file is protected.

All writes: `Library.abs_path` (no `..`, no symlink anywhere on the path, the target included), a refusal
of names that differ only in case (NTFS), `safefs.atomic_write_bytes` (temp + rename), the cap checked
first. A file is rewritten only when its bytes change (an unchanged rebuild keeps `added:` and is
byte-identical), and only if it carries `generator: voice-profile`: a hand-made file of the same name
stops the build instead of being replaced. An output whose sample is no longer kept is moved to
`90-archive/` with `Library.archive` (never deleted), before anything new is written. In the unit,
`40-writing` and `90-archive/40-writing` are separate mounts, so that rename fails with `EXDEV`;
`archive.py` then moves it itself, entirely through folder fds opened from the library root one
component at a time with `O_DIRECTORY|O_NOFOLLOW` (a folder swapped after that is never used): it
copies the file (opened `O_NOFOLLOW` in its folder) to an `O_EXCL` temp file in the archive folder,
checks the source did not change during the copy, fsyncs the file and the folder, claims the final
name with an empty `O_EXCL` placeholder (`-2`, `-3`, ... case-insensitively, never clobbering),
renames the temp file over it and fsyncs the folder. Then it checks the source name is still the
copied file, renames it to a temp name in its own folder (exactly one inode leaves the name),
checks that inode again, and only then unlinks it and fsyncs the folder; a file saved in between is
linked back to its name instead (never over a newer one). Any failure before the unlink removes the
temp file and placeholder and keeps the source.

## Limits

- The symlink checks are checks, not locks (the library's documented limit): a local process swapping a
  folder between check and rename could redirect a write. Single writer assumed.
- "Latest" is the file's mtime; copying samples onto the share may reset it, so ties fall to length/name.
- Cover and quote detection are heuristics; the manifest's per-file counts show what was cut.
- The model may write a poor profile; validation checks shape and safety, not quality. Noah's review is
  the quality gate.
