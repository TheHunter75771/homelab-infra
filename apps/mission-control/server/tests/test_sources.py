"""Collectors: per-source timeouts and errors (never a crash), Money Hub's read-only client, and the parsing of
each source."""

from __future__ import annotations

import datetime as dt
import os
import socket
import time
import unittest
from typing import ClassVar

from aiohttp import web

from mission_control import sources, views
from mission_control.net import SourceError, make_session
from mission_control.sources import MoneyHub, due_dates, parse_show, unit_status

from . import fakes
from .harness import Harness


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class TimeoutTests(Harness):
    extra: ClassVar[dict[str, float]] = {"source_timeout_s": 0.3}

    async def test_slow_source_becomes_an_error_card(self) -> None:
        self.aic.delay = 3.0
        started = time.monotonic()
        status, body = await self.get("/api/cards")
        took = time.monotonic() - started
        self.assertEqual(status, 200)
        self.assertLess(took, 2.0)
        card = body["cards"]["ai_control"]
        self.assertEqual(card["status"], "unknown")
        self.assertEqual(card["error"], "timed out after 0.3s")
        self.assertEqual(body["cards"]["money"]["status"], "warn")  # the others are untouched
        self.assertIsNone(body["cards"]["school"]["error"])

    async def test_slow_source_in_the_briefing(self) -> None:
        self.erau.delay = 3.0
        status, body = await self.get("/api/briefing")
        self.assertEqual(status, 200)
        school = body["sections"][0]
        self.assertEqual(school["items"][0]["label"], "School data is still loading")  # stale-while-revalidate
        self.assertEqual(school["items"][0]["value"], "the ERAU dashboard is slow to answer")
        self.assertTrue(body["lockscreen"]["body"].startswith("Briefing ready: deadlines unavailable, 3 bills"))

    async def test_hanging_systemctl_is_killed(self) -> None:
        fakes.set_units(self.dir / "sysd", fakes.default_units(), sleep=5)
        started = time.monotonic()
        status, body = await self.get("/api/cards")
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertEqual(status, 200)
        servers = body["cards"]["servers"]
        self.assertEqual(servers["items"][0]["status"], "unknown")
        self.assertIn("timed out", servers["items"][0]["value"])
        backup = next(i for i in servers["items"] if i["label"] == "Backup: Open WebUI (ai-box)")
        self.assertEqual(backup["status"], "unknown")


class ErrorTests(Harness):
    async def test_dead_and_broken_sources(self) -> None:
        dead = f"http://127.0.0.1:{free_port()}"
        self.erau.override["/api/snapshot"] = web.Response(body=b"{not json", content_type="application/json")
        self.hub.override["/api/state"] = web.json_response({"error": "boom"}, status=500)
        self.hub.override["/api/bills"] = web.json_response({"error": "boom"}, status=500)
        self.hub.override["/api/budget"] = web.json_response({"error": "boom"}, status=500)
        await self.restart(aicontrol_url=dead, webui_url=dead)
        status, body = await self.get("/api/cards")
        self.assertEqual(status, 200)
        c = body["cards"]
        self.assertEqual(c["ai_control"]["error"], "connection refused or unreachable")
        self.assertEqual(c["ai"]["status"], "down")
        self.assertEqual(c["school"]["error"], "invalid JSON reply")
        self.assertEqual(c["money"]["error"], "HTTP 500")
        status, body = await self.get("/api/briefing")
        self.assertEqual(status, 200)
        for text in (str(body), str(c)):
            self.assertNotIn("127.0.0.1", text, "an error message must not carry a URL")

    async def test_wrong_shapes(self) -> None:
        self.aic.summary = {"services": "nope"}
        self.erau.payload = {"snapshot": {"tasks": "nope"}}
        self.ms.backups = "nope"  # type: ignore[assignment]
        _, body = await self.get("/api/cards")
        self.assertEqual(body["cards"]["ai_control"]["error"], "unexpected reply shape")
        self.assertEqual(body["cards"]["school"]["error"], "unexpected reply shape")
        ms = next(i for i in body["cards"]["servers"]["items"] if i["label"] == "Backup: Mediaserver")
        self.assertEqual(ms["status"], "unknown")

    async def test_redirect_is_not_followed(self) -> None:
        def redirect() -> web.StreamResponse:
            raise web.HTTPFound("http://169.254.169.254/")

        self.erau.override["/api/snapshot"] = redirect
        _, body = await self.get("/api/cards")
        self.assertEqual(body["cards"]["school"]["error"], "HTTP 302")

    async def test_unconfigured_sources(self) -> None:
        await self.restart(moneyhub_url=None, erau_url=None, ha_url=None, aicontrol_url=None, webui_url=None)
        _, body = await self.get("/api/cards")
        for cid in ("money", "school", "home", "ai_control"):
            self.assertEqual(body["cards"][cid]["status"], "setup", cid)
        self.assertEqual(self.hub.requests, [])


class MoneyHubReadOnlyTests(Harness):
    start_server = False

    async def test_every_request_is_an_allowlisted_get(self) -> None:
        cfg = self.config()
        async with make_session({}) as s:
            data = await sources.moneyhub(s, cfg, fakes.NOW)
        self.assertIn("sync", data)
        self.assertTrue(self.hub.requests)
        for method, path, query in self.hub.requests:
            self.assertEqual(method, "GET")
            self.assertIn(path, sources.MONEYHUB_ALLOWED)
            self.assertLessEqual(set(query), set(sources.MONEYHUB_ALLOWED[path]))

    async def test_write_methods_are_refused_before_any_request(self) -> None:
        async with make_session({}) as s:
            hub = MoneyHub(s, self.hub.url, 2.0)
            for method in ("POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "get"):
                with self.assertRaises(SourceError) as cm:
                    await hub.call(method, "/api/state")
                self.assertIn("read-only", str(cm.exception))
            for path in ("/api/sync", "/api/bills/1/paid", "/api/transactions", "/api/state/../sync", "/"):
                with self.assertRaises(SourceError):
                    await hub.get(path)
            for params in ({"period": "2026-10", "paid": "1"}, {"period": "2026-13"}, {"period": "x&paid=1"}):
                with self.assertRaises(SourceError):
                    await hub.get("/api/bills", params)
        self.assertEqual(self.hub.requests, [])

    async def test_figures(self) -> None:
        async with make_session({}) as s:
            data = await sources.moneyhub(s, self.config(), fakes.NOW)
        self.assertTrue(data["sync"]["failing"])
        self.assertEqual(data["sync"]["last_ok"], dt.datetime(2026, 10, 5, 13, 0, 26, 147836, tzinfo=dt.UTC).timestamp())
        b = data["bills"]
        self.assertEqual([(x["due"], x["name"]) for x in b["due"]],
                         [("2026-10-28", "Car insurance"), ("2026-10-30", "Phone"), ("2026-11-01", "Rent")])
        self.assertEqual(b["total_cents"], 15025 + 4500 + 120000)
        self.assertEqual(b["undated_unpaid"], 1)
        self.assertEqual(data["budget"], {"month": "2026-10", "left_cents": 9000, "over": 1, "near": 1, "categories": 3})
        self.assertEqual({p for _m, p, _q in self.hub.requests}, {"/api/state", "/api/bills", "/api/budget"})
        self.assertEqual(sorted(q.get("period", "") for _m, p, q in self.hub.requests if p == "/api/bills"),
                         ["2026-10", "2026-11"])

    async def test_one_failed_part_only_blanks_itself(self) -> None:
        self.hub.override["/api/budget"] = web.json_response({}, status=503)
        async with make_session({}) as s:
            data = await sources.moneyhub(s, self.config(), fakes.NOW)
        self.assertEqual(data["budget"], {"error": "HTTP 503"})
        self.assertEqual(data["bills"]["count"], 3)

    def test_due_dates(self) -> None:
        self.assertEqual(due_dates("31st", "2026-02"), [dt.date(2026, 2, 28)])
        self.assertEqual(due_dates("qtrly 7/20", "2026-10"), [dt.date(2026, 10, 20)])
        self.assertEqual(due_dates("qtrly 7/20", "2026-11"), [])
        self.assertIsNone(due_dates("biweekly", "2026-10"))
        self.assertIsNone(due_dates("0th", "2026-10"))


class ErauTests(Harness):
    start_server = False

    async def test_deadlines(self) -> None:
        async with make_session({}) as s:
            d = await sources.erau(s, self.config(), fakes.NOW)
        self.assertEqual([(x["course"], x["what"], x["title"]) for x in d["upcoming"]],
                         [("ABCD 101", "Due", "Essay 2: Rhetorical analysis"), ("CS 118", "Start", "Project"),
                          ("MATH 111", "Due", "Quiz 4")])
        self.assertEqual(d["count"], 3)
        self.assertEqual(d["overdue"], 1)
        self.assertTrue(d["upcoming"][1]["early"])
        self.assertNotIn("inbox", str(d))

    async def test_no_data_and_sign_in(self) -> None:
        self.erau.payload = {"snapshot": None, "status": {"needsSignIn": True}, "settings": {}}
        async with make_session({}) as s:
            d = await sources.erau(s, self.config(), fakes.NOW)
        self.assertTrue(d["no_data"])
        self.assertTrue(d["needs_sign_in"])

    async def test_static_host_map(self) -> None:
        """The ERAU URL keeps its tailnet name (TLS checks it); the connection goes to the mapped address."""
        port = int(self.erau.url.rsplit(":", 1)[1])
        cfg = self.config(erau_url=f"http://homebk-edge.example.ts.net:{port}",
                          host_map={"homebk-edge.example.ts.net": "127.0.0.1"})
        async with make_session(dict(cfg.host_map)) as s:
            d = await sources.erau(s, cfg, fakes.NOW)
        self.assertEqual(d["count"], 3)


class UnitTests(unittest.TestCase):
    def test_parse_show(self) -> None:
        text = ("Id=a.service\nLoadState=loaded\nActiveState=failed\nSubState=failed\nResult=exit-code\nNRestarts=5\n"
                "ExecMainExitTimestamp=@1793000000\n\nId=b.service\nLoadState=not-found\nActiveState=inactive\n")
        u = parse_show(text)
        self.assertEqual(u["a.service"]["restarts"], 5)
        self.assertEqual(u["a.service"]["exit_ts"], 1793000000)
        self.assertEqual(unit_status(u["a.service"]), ("down", "failed (exit-code)"))
        self.assertEqual(unit_status(u["b.service"])[0], "unknown")
        self.assertEqual(unit_status({"load": "loaded", "active": "activating", "sub": "auto-restart"}),
                         ("warn", "restarting"))
        # the cumulative NRestarts alone is not "restarting" (Z-3): only an increase within the window is
        self.assertEqual(unit_status({"load": "loaded", "active": "active", "sub": "running", "restarts": 40}),
                         ("ok", "active (running)"))
        self.assertEqual(unit_status({"load": "loaded", "active": "inactive", "result": "success"}), ("ok", "idle"))

    def test_backup_file(self) -> None:
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "LAST_OK"
            p.write_text("2026-10-28T03:45:10Z\n")
            self.assertEqual(sources.backup_from_file(str(p), 36, fakes.NOW)["status"], "ok")
            self.assertEqual(sources.backup_from_file(str(p), 1, fakes.NOW)["status"], "stale")
            p.write_text("garbage")
            self.assertEqual(sources.backup_from_file(str(p), 36, fakes.NOW)["status"], "unknown")
            p.write_text("2027-01-01T00:00:00Z")
            self.assertEqual(sources.backup_from_file(str(p), 36, fakes.NOW)["detail"], "timestamp in the future")
            self.assertEqual(sources.backup_from_file(str(Path(d) / "nope"), 36, fakes.NOW)["status"], "unknown")
            p.write_text("2026-10-28T03:45:10Z")
            p.chmod(0)
            if os.geteuid() != 0:  # root reads it anyway
                self.assertEqual(sources.backup_from_file(str(p), 36, fakes.NOW)["detail"], "status file not readable")

    def test_backup_unit(self) -> None:
        ok = {"load": "loaded", "active": "inactive", "result": "success", "exit_ts": int(fakes.NOW - 3600)}
        self.assertEqual(sources.backup_from_unit(ok, 30, fakes.NOW)["status"], "ok")
        self.assertEqual(sources.backup_from_unit({**ok, "exit_ts": int(fakes.NOW - 40 * 3600)}, 30, fakes.NOW)["status"],
                         "stale")
        self.assertEqual(sources.backup_from_unit({**ok, "result": "exit-code", "active": "failed"}, 30, fakes.NOW)["status"],
                         "failed")
        self.assertEqual(sources.backup_from_unit({**ok, "exit_ts": None}, 30, fakes.NOW)["status"], "stale")


class MediaserverAndBackupsTests(Harness):
    start_server = False

    async def test_mediaserver_mapping(self) -> None:
        cfg = self.config()
        for status, want in (("ok", "ok"), ("stale", "stale"), ("failed", "failed"), ("none", "stale"), ("weird", "unknown")):
            self.ms.backups = {"status": status, "lastOk": None}
            async with make_session({}) as s:
                d = await sources.mediaserver(s, cfg, fakes.NOW)
            self.assertEqual(d["status"], want, status)

    async def test_views_backups(self) -> None:
        cfg = self.config()
        async with make_session({}) as s:
            snap = await sources.Collector(cfg, s, lambda: fakes.NOW).collect()
        self.assertEqual([b["status"] for b in views.backups(snap, cfg, fakes.NOW)], ["ok", "ok", "ok"])


if __name__ == "__main__":
    unittest.main()
