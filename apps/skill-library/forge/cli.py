"""Command line: ``python3 -m forge [--root R] <command> ...`` (run from apps/skill-library)."""
from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Mapping, Sequence
from typing import TextIO

from library import Library, LibraryError, naming
from library.cli import CAP_ENV, ROOT_ENV
from library.core import DEFAULT_CAP, Clock

from .core import Forge, NotDecided

EPILOG = f"""\
The root comes from --root or ${ROOT_ENV} (no default); the cap from --cap or ${CAP_ENV}
(default 500GB), as for `python3 -m library`. Every command except status, verify and
authorize rebuilds INDEX.md afterwards.

Proposal files are UTF-8 text, one proposal per line, fields separated by a TAB; blank lines
and lines starting with # are skipped:
  propose-searches:  <query> TAB <justification>
  propose-sources:   <url> TAB <title> TAB <why> TAB <what will be taken>

exit codes: 0 ok; 1 no decision yet (decide), not approved (authorize), hash mismatch
(verify); 2 refused or failed."""


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--root", default=argparse.SUPPRESS, help=f"library root (or ${ROOT_ENV}); required")
    common.add_argument("--cap", default=argparse.SUPPRESS, help=f"soft cap (or ${CAP_ENV}; default 500GB)")
    parser = argparse.ArgumentParser(
        prog="python3 -m forge", parents=[common],
        description="Skill Forge: gated, human-approved skill requests (CONTRACTS §12.12).",
        epilog=EPILOG, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    p = sub.add_parser("new", parents=[common], help="scaffold 35-forge/<YYYY-MM-DD_kebab>/ from Noah's request")
    p.add_argument("title")
    p.add_argument("--request-file", required=True, metavar="FILE", help="Noah's words (copied verbatim)")

    p = sub.add_parser("propose-searches", parents=[common], help="append unticked search proposals to searches.md")
    p.add_argument("id")
    p.add_argument("--from", dest="src", required=True, metavar="FILE", help="lines of query<TAB>justification")

    p = sub.add_parser("propose-sources", parents=[common],
                       help="append unticked source candidates to sources.md (needs an approved search)")
    p.add_argument("id")
    p.add_argument("--from", dest="src", required=True, metavar="FILE", help="lines of url<TAB>title<TAB>why<TAB>take")

    p = sub.add_parser("record-ingest", parents=[common], help="log that an approved URL was filed in 10-sources/")
    p.add_argument("id")
    p.add_argument("url")
    p.add_argument("path", help="the filed item, relative to the library root")

    p = sub.add_parser("draft-skill", parents=[common], help="copy a draft into <id>/skill/ and write review.md")
    p.add_argument("id")
    p.add_argument("--from", dest="src", required=True, metavar="DIR", help="SKILL.md [+ code/ + manifest.md]")
    p.add_argument("--name", help="skill name (default: SKILL.md's name:, else the request's title)")

    p = sub.add_parser("decide", parents=[common], help="apply review.md's decision: approve | deny | revise")
    p.add_argument("id")

    p = sub.add_parser("revive", parents=[common], help="move 95-shelf/<skill>/ back to 35-forge/ for review")
    p.add_argument("skill")

    p = sub.add_parser("status", parents=[common], help="every request and its state (or one, with its log)")
    p.add_argument("id", nargs="?")

    p = sub.add_parser("verify", parents=[common], help="check an installed skill against its approved hash")
    p.add_argument("skill")

    p = sub.add_parser("authorize", parents=[common], help="gate check: is this exact query/URL approved now?")
    p.add_argument("kind", choices=("query", "url"))
    p.add_argument("id")
    p.add_argument("value")
    return parser


def _read_tsv(path: str, fields: int, what: str) -> list[tuple[str, ...]]:
    try:
        with open(path, "rb") as fh:
            text = fh.read().decode("utf-8")
    except OSError as exc:
        raise LibraryError(f"cannot read {path}: {exc.strerror}") from None
    except UnicodeDecodeError:
        raise LibraryError(f"{path} is not UTF-8 text") from None
    rows = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        parts = raw.split("\t")
        if len(parts) != fields:
            raise LibraryError(f"{path} line {lineno}: expected {fields} TAB-separated fields ({what}), "
                               f"got {len(parts)}")
        rows.append(tuple(parts))
    return rows


def main(argv: Sequence[str] | None = None, *, clock: Clock | None = None,
         environ: Mapping[str, str] | None = None, stdout: TextIO | None = None,
         stderr: TextIO | None = None) -> int:
    env = os.environ if environ is None else environ
    out = stdout or sys.stdout
    err = stderr or sys.stderr
    args = build_parser().parse_args(argv)
    root = getattr(args, "root", None) or env.get(ROOT_ENV)
    if not root:
        print(f"error: no library root: pass --root or set {ROOT_ENV}", file=err)
        return 2
    try:
        cap_text = getattr(args, "cap", None) or env.get(CAP_ENV)
        cap = naming.parse_size(cap_text) if cap_text else DEFAULT_CAP
    except ValueError as exc:
        print(f"error: {exc}", file=err)
        return 2

    try:
        forge = Forge(Library.open(root, clock=clock, cap_bytes=cap))
        code = _run(forge, args, out, err)
        if args.command not in ("status", "verify", "authorize") and code == 0:
            changed, count = forge.lib.index()
            print(f"INDEX.md {'updated' if changed else 'unchanged'} ({count} items)", file=out)
        return code
    except NotDecided as exc:
        print(f"not yet: {exc}", file=err)
        return 1
    except LibraryError as exc:
        print(f"error: {exc}", file=err)
        return 2
    except OSError as exc:
        print(f"error: {exc.strerror or exc}" + (f": {exc.filename}" if exc.filename else ""), file=err)
        return 2
    except ValueError as exc:  # includes UnicodeError (e.g. undecodable arguments)
        print(f"error: {' '.join(str(exc).split())}", file=err)
        return 2


def _run(forge: Forge, args: argparse.Namespace, out: TextIO, err: TextIO) -> int:
    cmd = args.command
    if cmd == "new":
        try:
            with open(args.request_file, "rb") as fh:
                text = fh.read().decode("utf-8")
        except OSError as exc:
            raise LibraryError(f"cannot read {args.request_file}: {exc.strerror}") from None
        except UnicodeDecodeError:
            raise LibraryError(f"{args.request_file} is not UTF-8 text") from None
        rid = forge.new(args.title, text)
        print(f"created 35-forge/{rid}/ (awaiting-search-approval)", file=out)
        print(rid, file=out)
        return 0
    if cmd == "propose-searches":
        rows = _read_tsv(args.src, 2, "query, justification")
        n = forge.propose_searches(args.id, [(q, w) for q, w in rows])
        print(f"{n} search(es) added to searches.md; waiting for Noah to tick them", file=out)
        return 0
    if cmd == "propose-sources":
        rows = _read_tsv(args.src, 4, "url, title, why, what will be taken")
        n = forge.propose_sources(args.id, [tuple(r) for r in rows])  # type: ignore[misc]
        print(f"{n} source(s) added to sources.md; waiting for Noah to tick them", file=out)
        return 0
    if cmd == "record-ingest":
        rel = forge.record_ingest(args.id, args.url, args.path)
        print(f"recorded {args.url} -> {rel}", file=out)
        return 0
    if cmd == "draft-skill":
        skill, digest, warning = forge.draft_skill(args.id, args.src, name=args.name)
        print(f"drafted {skill} ({digest}); waiting for Noah's decision in review.md", file=out)
        if warning:
            print(f"warning: {warning}", file=err)
        return 0
    if cmd == "decide":
        res = forge.decide(args.id)
        if res.decision == "approve":
            print(f"approved: {res.skill} -> {res.location}/ ({res.hash})", file=out)
            if res.edited:
                print(f"note: approved with Noah's edits since the draft: {', '.join(res.edited)}", file=out)
        elif res.decision == "deny":
            print(f"denied: shelved in {res.location}/ (revive with: revive {res.location.split('/')[-1]})", file=out)
        else:
            print(f"revise: {res.location} is awaiting a new draft", file=out)
        return 0
    if cmd == "revive":
        rid = forge.revive(args.skill)
        print(f"revived 35-forge/{rid}/ (awaiting-skill-review; decision reset to pending)", file=out)
        return 0
    if cmd == "status":
        if args.id:
            info = forge.request_info(args.id)
            print(info.summary(), file=out)
            print(info.raw_listing(), file=out)
            print("  log:", file=out)
            for line in info.log:
                print(f"    {line}", file=out)
            return 0
        infos, problems = forge.all_requests()
        for info in infos:
            print(info.summary(), file=out)
        for problem in problems:
            print(f"problem: {problem}", file=out)
        if not infos and not problems:
            print("no forge requests", file=out)
        return 0
    if cmd == "verify":
        res = forge.verify(args.skill)
        if res.ok:
            print(f"ok: {args.skill} matches its approved content ({res.current})", file=out)
            return 0
        print(f"MISMATCH: {args.skill} is {res.current}; it needs a new decision before it may run", file=out)
        for problem in res.problems:
            print(f"  {problem}", file=out)
        return 1
    if cmd == "authorize":
        check = forge.is_query_approved if args.kind == "query" else forge.is_url_approved
        allowed = check(args.id, args.value)
        print("approved" if allowed else "not approved", file=out)
        return 0 if allowed else 1
    raise AssertionError(f"unhandled command {cmd}")
