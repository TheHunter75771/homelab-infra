"""One digest run: read Money Hub, compute the facts, maybe add a guarded AI summary, write the day.

Exit codes: 0 = written; 1 = written, but some Money Hub read failed (the entry says which);
2 = nothing written (config, library or log problem). Notion and the AI never change the code.
"""
from __future__ import annotations

import datetime as dt
import logging
import os
import stat
import sys
import zoneinfo

from library import SENTINEL_NAME, Library, LibraryError

from . import broker, facts, guard, logfile, notion, render
from .moneyhub import MoneyHub

log = logging.getLogger("finance_digest")

AI_OFF = "AI summary off (disabled in the config)."


def today_in(tz_name: str) -> dt.date:
    return dt.datetime.now(zoneinfo.ZoneInfo(tz_name)).date()


def ai_summary(cfg: dict, f: facts.Facts, facts_text: str) -> tuple[str | None, str]:
    """(guarded summary or None, the line written when there is none)."""
    if not cfg["ai_summary"]:
        return None, AI_OFF
    if all(getattr(f, name) is None for name in facts.SECTIONS):
        log.info("AI summary skipped: Money Hub gave no facts")
        return None, guard.NO_FIGURES
    try:
        raw = broker.summarise(cfg["broker_url"], cfg["moneyhub_url"], cfg["model"], facts_text,
                               cfg["broker_timeout_s"])
    except broker.BrokerError as e:
        log.warning("AI summary skipped: %s", e)
        return None, guard.UNAVAILABLE
    summary, why = guard.check(raw)
    if summary is None:
        log.warning("AI summary skipped: %s", why)
        return None, guard.SKIPPED
    return summary, guard.SKIPPED


def send_to_notion(cfg: dict, day: dt.date, blocks: list, summary: str | None, note: str, connect=None) -> str:
    """"off", "sent" or "failed". Never raises: Notion never fails the run."""
    try:
        conf = notion.load_config(cfg["notion_file"])
        if conf is None:
            log.info("Notion: off (no %s)", cfg["notion_file"])
            return "off"
        token, page = conf
        children = notion.to_blocks(day.isoformat(), blocks, summary, note, render.AI_LABEL, render.GENERATED_BY)
        kwargs = {"timeout": cfg["notion_timeout_s"]}
        if connect is not None:
            kwargs["connect"] = connect
        notion.append(token, page, children, **kwargs)
        log.info("Notion: appended %s", day.isoformat())
        return "sent"
    except notion.NotionError as e:
        log.warning("Notion: not updated: %s", e)
    except Exception as e:  # noqa: BLE001 - Notion is optional; its token is never in this message
        log.warning("Notion: not updated: unexpected %s", type(e).__name__)
    return "failed"


def sentinel_problem(lib: Library) -> str | None:
    """None if the mount sentinel is a regular file at the library root (not a symlink)."""
    try:
        st = os.lstat(lib.root / SENTINEL_NAME)
    except FileNotFoundError:
        return f"{SENTINEL_NAME} is missing (is the library share mounted?)"
    except OSError as e:
        return f"{SENTINEL_NAME} can't be checked ({e.strerror})"
    if not stat.S_ISREG(st.st_mode):
        return f"{SENTINEL_NAME} is not a regular file"
    return None


def run(cfg: dict, *, day: dt.date | None = None, dry_run: bool = False, out=None, notion_connect=None) -> int:
    out = out or sys.stdout
    day = day or today_in(cfg["timezone"])
    lib = None
    if not dry_run:
        try:
            lib = Library.open(cfg["library_root"])
        except LibraryError as e:
            log.error("the AI Library can't be used: %s", e)
            return 2
        problem = sentinel_problem(lib)
        if problem:
            log.error("the AI Library can't be used: %s; nothing written", problem)
            return 2
    hub = MoneyHub(cfg["moneyhub_url"], cfg["moneyhub_timeout_s"])
    f = facts.compute(hub, day, cfg["bills_window_days"])
    for section, message in sorted(f.errors.items()):
        log.warning("Money Hub %s: %s", section, message)
    blocks = facts.blocks(f)
    summary, note = ai_summary(cfg, f, render.facts_markdown(blocks))
    entry = render.entry_markdown(day, blocks, summary, note)
    code = 1 if f.errors else 0
    if dry_run:
        out.write(entry)
        out.flush()
        return code
    try:
        action = logfile.write_entry(lib, day, entry)
    except (logfile.LogError, LibraryError, OSError) as e:
        log.error("the log was not written: %s", e)
        return 2
    log.info("%s %s in %s", action, day.isoformat(), logfile.log_rel(day))
    send_to_notion(cfg, day, blocks, summary, note, notion_connect)
    return code
