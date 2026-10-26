# skill-library: the AI Library core

The pure-Python core that files, indexes, caps and checks the **AI Library**: a folder of plain
files (sources, notes, skills, writing samples, finance logs) that Noah and the AI on ai-box share.
Spec: `docs/CONTRACTS.md` §12.11 (module L). Python 3.12+, **standard library only**.

The library itself lives on the gaming PC's NTFS share drive (`/mnt/share/ai-library`, exported to
ai-box later by Samba). The share and mount roles come later. This package never assumes that
path: the root is always given explicitly.

## Why

- **One layout everyone follows.** People drop files from Windows, the AI reads and writes over
  Samba, and Phase 5 features build on top. A fixed layout, file names and a small header make the
  library predictable for all three.
- **Nothing is ever lost.** Items are copied in, moved, or archived, never deleted or overwritten.
- **Safe on a share.** Every write is confined to the root (symlinks and `..` refused), atomic
  (temp file + rename), and works on NTFS: no POSIX permissions, and names that differ only in
  case count as the same name.

## Layout (created by `init`)

```
README.md                 the rules, for Noah and for the AI (written once, never overwritten)
INDEX.md                  generated: one line per item, grouped by folder
00-inbox/                 drop zone
10-sources/<topic>/       originals (+ sidecars)
20-notes/<topic>/
30-skills/<skill>/        SKILL.md, manifest.md, CHANGELOG.md + supporting files; code/** (any names)
35-forge/                 Skill Forge requests (module F; not indexed or checked here)
40-writing/{samples,drafts}/
50-finance/log/
90-archive/               retired items under their original path; inbox-log.md
95-shelf/                 denied skills kept by the forge (not indexed or checked here)
```

- **Names:** `YYYY-MM-DD_kebab-title.ext` (date = day added). Topic/skill folders: kebab-case,
  1-48 characters, single hyphens, not a Windows device name (`con`, `nul`, `com1`, ...).
- **Header:** every `.md` starts with `title, source, added, tags, summary, related` in a small
  YAML subset (see `library/frontmatter.py`). A non-Markdown item gets a sidecar `<name>.<ext>.md`.
- **Exempt from the naming rule:** `README.md`, `INDEX.md` (no header either),
  `90-archive/inbox-log.md`, every `*.md` page at the top of a skill folder (`SKILL.md`,
  `manifest.md`, `CHANGELOG.md`, `rules.md`, `examples.md`, ...; the header is still required),
  anything under `30-skills/<skill>/code/` (no rules at all), and anything still in
  `00-inbox/` (or archived straight from it). `35-forge/` and `95-shelf/` belong to the forge:
  `check` only reports symlinks and temp files there, and `INDEX.md` leaves them out.

## CLI

Run from this folder (or with it on `PYTHONPATH`):

```
python3 -m library --root /path/to/library init
python3 -m library --root R file ~/Downloads/paper.pdf --topic gpu --title "ROCm guide" --tags gpu,rocm --summary "..."
python3 -m library --root R inbox --default-topic unsorted
python3 -m library --root R inbox --topic-map topics.txt [--default-topic unsorted]
python3 -m library --root R index
python3 -m library --root R cap
python3 -m library --root R archive 10-sources/gpu/2026-10-26_rocm-guide.pdf
python3 -m library --root R check
```

- **Root:** `--root` or `AI_LIBRARY_ROOT`. There is no default. `init` refuses a root whose parent
  is missing (an unmounted share) or a non-empty folder that is not a library; every other command
  refuses a folder without the layout.
- **Cap:** `--cap` or `AI_LIBRARY_CAP` (default `500GB`, decimal; `GiB` etc. for binary). Usage is
  the total size of the regular files under the root (symlinks are not followed). `file` and `inbox`
  warn when the result would be at 90 % or more and refuse when it would pass 100 % (a refused
  `file` writes nothing; `inbox` stops at the item that would not fit).
- **file** copies (the original is untouched) into `10-sources/<topic>/`. A Markdown file keeps its
  body and gets a complete header (existing values kept, flags override, `added` = today); a malformed
  header is refused, not guessed at. Taken names get `-2`, `-3`, ..., compared case-insensitively.
- **inbox** moves top-level inbox files into `10-sources/<topic>/`. A topic map is lines of
  `<topic> <glob>`; the first glob matching the file name (any case) wins, e.g. `rocm *rocm*` or
  `papers *.pdf`. A dropped `x.pdf.md` next to `x.pdf` is its sidecar and moves with it.
  - **Lossless:** the inbox moves (it does not copy), so a header is only completed, never changed.
    A header with comments, a list where text belongs (`title`/`source`/`summary`), non-list
    `tags`/`related`, an `added` other than today, or a malformed header leaves the file in the inbox
    with the reason. `file` copies, so it may merge.
  - **Logged:** before anything moves, `90-archive/inbox-log.md` must be a regular, writable UTF-8
    file. Each move is bracketed: `moving [id] `src` -> `dest`` before, `done [id] ...` after, or
    `undone [id] <reason>` when it was rolled back. The `moving` line also records the item's inode
    (`(inode N)`). A `moving` line with neither means the run was interrupted; the next `inbox` run
    deals with it first, and **recovery never unlinks anything**:
    - item still in the inbox, destination absent or an empty placeholder: marked `undone`; the item
      is filed again under a fresh name, and the old empty placeholders stay for `check` to report;
    - item still in the inbox *and* the destination holds the moved file (non-empty, or the recorded
      inode): "both exist; check by hand" in `problems` (exit 1); the entry stays pending and that
      name is not filed until the new file is moved aside, after which the next run finishes the move;
    - item moved: finished (a stranded sidecar joins it, the header is completed) only if the
      destination is dated the log line's day and has the recorded inode. A destination without
      the recorded inode (a hand-written entry, or a Markdown item whose header rewrite gave it a
      new inode just before a crash) is "check by hand" in `problems`: nothing changes and the
      entry stays pending until a person appends `- <time> done [id] checked by hand` to the log.
    Names in the log are escaped (backticks, control characters), so a file name cannot forge a line.
  - **Left with a reason:** symlinks, folders, names that are not valid UTF-8, a sidecar whose item
    is missing or not a regular file (never filed as a note of its own), and files with no topic.
    Dotfiles, `desktop.ini` and `Thumbs.db` are ignored.
- **index** rebuilds `INDEX.md`. The output depends only on the files (never on the clock), and an
  unchanged library leaves the file untouched (same bytes and mtime). Hidden files and folders
  (`.git`, `.recycle`, ...) and names that are not valid UTF-8 are skipped (`check` reports them).
  `file`, `inbox` and `archive` reindex automatically.
- **archive** takes a path relative to the root (or absolute inside it). It moves an item with its
  sidecar, or a whole folder, to `90-archive/<same path>`. It refuses layout folders, root files, and
  anything already archived.
- **check** reports and never fixes. The kinds are `missing-layout`, `unknown-entry`, `misplaced`,
  `bad-dir-name`, `bad-name`, `missing-front-matter`, `bad-front-matter`, `missing-field`,
  `bad-field`, `bad-encoding`, `missing-sidecar`, `orphan-sidecar`, `empty-placeholder` (a dated
  name that is an empty file with no header or sidecar: reserved by an interrupted run, never
  filled), `case-collision`, `symlink`, `hidden-file` and `temp-file`, one per line as
  `kind<TAB>path: message`.

Exit codes: `0` ok. `1` means `check` found problems, `inbox` left items behind (or could not log
a finished move), or `cap` is at 90 % or more. `2` means refused or failed (bad input, a malformed
value, unsafe path, cap reached, `inbox` stopped at the cap, not a library, I/O error), always as
one `error:` line. `3` means `cap` is at 100 % or more. Output escapes undecodable bytes and
control characters in names, so every result is one line.

The same operations are available as a library: `from library import Library`,
`Library.open(root, clock=..., cap_bytes=...)`, then `.file()`, `.inbox()`, `.index()`,
`.cap_status()`, `.archive()`, `.check()`. The clock is injectable for tests. Building blocks for
other modules (the forge) are public too: `abs_path(rel)` (confined, symlink-checked path),
`ensure_dir(rel)`, `write_new(rel, text)` (atomic, never overwrites), `cap_gate(incoming)` and
`move_dir(src_rel, dest_rel)`. The old private names (`_abs`, `_ensure_dir`, `_write_new`,
`_cap_gate`) remain as aliases.

## Tests

```
cd apps/skill-library && python3 -m unittest discover -s tests -v
# or from the repo root (CI):
python3 -m unittest discover -s apps/skill-library/tests -t apps/skill-library -v
```

Every test runs in a fresh temporary directory and never touches a real path.

## Limits (by design)

- One writer at a time, and no hostile local user racing the tool. O_EXCL name claims mean even a
  race never overwrites, but whole operations are not serialized. Paths are checked for symlinks
  component by component just before use, not opened through directory file descriptors: a process
  that swaps a folder for a symlink between that check and the write can redirect the write. The
  later Samba role should therefore keep clients from creating symlinks on the share (no Unix
  extensions, no wide links).
- A source that grows while `file` copies it is refused (the copy is bounded by the size the cap
  check counted), not truncated.
- Every front-matter value is a string or a list of strings. Unlike YAML, `null`, `true`, `42` and
  `2026-10-26` are plain text. The subset has no multi-line values, block scalars or nesting, and a
  ` #` comment inside a one-line `[...]` list is refused. `check` reports all of these with a line
  number.
- Run `inbox` on the host where the share is mounted with ntfs3 (the gaming PC), not over Samba.
  Interrupted-move recovery identifies a moved file by its inode. ntfs3 inode numbers (MFT
  records) survive a rename, but a CIFS client mount without `serverino` makes up inode numbers
  that can change, so recovery would refuse moves as "check by hand".
- Case-insensitive name checks use Python's `str.casefold()`. NTFS compares names with its own
  upcase table, which differs for a few characters (e.g. dotless `ı` upcases to `I` on NTFS but does
  not casefold to `i`). Names the tool creates are ASCII, so this only matters for hand-made names.
- `init` cannot tell an unmounted mount point from an empty folder if the parent exists. The mount
  role must make sure the share is mounted before anything runs `init`.
