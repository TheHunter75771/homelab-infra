"""Regression tests for Reviewer B's findings (S1-S8) on the server."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any

from aiohttp import web

from ai_control.sampler import GateSampler

from . import fakes
from .test_server import Harness


def strict_loads(text: str) -> Any:
    """json.loads that fails on NaN/Infinity tokens (what a browser's JSON.parse does)."""
    def refuse(c: str) -> Any:
        raise ValueError(c)

    return json.loads(text, parse_constant=refuse)


def replace_handler(app: web.Application, path: str, handler: Any) -> None:
    for route in app.router.routes():
        if route.resource is not None and route.resource.canonical == path:
            route._handler = handler  # noqa: SLF001 - test-only swap of a fake's route


def chunked(body: bytes, step: int, pause: float = 0.005) -> Any:
    async def handler(request: web.Request) -> web.StreamResponse:
        resp = web.StreamResponse(headers={"Content-Type": "application/json"})
        await resp.prepare(request)
        for i in range(0, len(body), step):
            await resp.write(body[i:i + step])
            await asyncio.sleep(pause)
        await resp.write_eof()
        return resp

    return handler


def single_write(body: bytes) -> Any:
    async def handler(_request: web.Request) -> web.Response:
        return web.Response(body=body, content_type="application/json")

    return handler


# ---- S1: bodies over 64 KiB ------------------------------------------------------------------------
class LargeBodyTests(Harness):
    async def test_agent_logs_large_single_and_chunked(self) -> None:
        lines = [f"2026-10-26T12:00:00+0000 pc ollama[1]: line {i} " + "x" * 200 for i in range(1000)]
        body = json.dumps({"unit": "ollama.service", "truncated": False, "lines": lines}).encode()
        self.assertGreater(len(body), 200 * 1024)
        for name, handler in (("single", single_write(body)), ("chunked", chunked(body, 16384))):
            with self.subTest(name):
                replace_handler(self.agent.app, "/v1/logs", handler)
                for _ in range(3):
                    status, data, _ = await self.get("/api/logs?source=pc:ollama&lines=1000")
                    self.assertEqual(status, 200, data)
                    self.assertEqual(len(data), 1000)
                    self.assertTrue(data[-1].startswith("2026-10-26T12:00:00+0000 pc ollama[1]: line 999 "))

    async def test_broker_recent_large_single_and_chunked(self) -> None:
        rows = [dict(fakes.RECENT[1], error="e" * 1000) for _ in range(200)]
        body = json.dumps(rows).encode()
        self.assertGreater(len(body), 200 * 1024)
        for name, handler in (("single", single_write(body)), ("chunked", chunked(body, 8192))):
            with self.subTest(name):
                replace_handler(self.broker.app, "/broker/recent", handler)
                status, data, _ = await self.get("/api/requests?limit=200")
                self.assertEqual(status, 200, data)
                self.assertEqual(len(data), 200)
                self.assertNotIn("prompt", data[-1])

    async def test_helper_logs_large_single_and_chunked(self) -> None:
        self.helper.big_lines = 1000  # ~330 KiB reply
        for chunk in (0, 4096):
            with self.subTest(chunk=chunk):
                self.helper.chunk = chunk
                status, data, _ = await self.get("/api/logs?source=docker&lines=1000")
                self.assertEqual(status, 200, data)
                self.assertEqual(len(data), 1000)
                self.assertTrue(data[-1].startswith("big line 999 "))

    async def test_cap_still_applies(self) -> None:
        from ai_control import clients

        old = clients.MAX_HTTP_JSON
        clients.MAX_HTTP_JSON = 50_000
        try:
            body = json.dumps({"unit": "u", "truncated": False, "lines": ["x" * 100] * 1000}).encode()
            replace_handler(self.agent.app, "/v1/logs", chunked(body, 8192, 0))
            status, data, _ = await self.get("/api/logs?source=pc:ollama")
            self.assertEqual((status, data), (502, {"error": "host agent: reply too large"}))
        finally:
            clients.MAX_HTTP_JSON = old


# ---- S2 + S3: hostile upstream values ---------------------------------------------------------------
class HostileValueTests(Harness):
    async def tags(self, text: str) -> dict[str, Any]:
        async def handler(_request: web.Request) -> web.Response:
            return web.Response(text=text, content_type="application/json", headers={"X-Backend": "cpu"})

        replace_handler(self.broker.app, "/api/tags", handler)
        await self.state.sampler.sample_once(self.state.session)
        async with self.http.get(self.base + "/api/summary") as resp:
            text = await resp.text()
            self.assertEqual(resp.status, 200, text)
        return strict_loads(text)

    async def test_unhashable_model_name(self) -> None:
        data = await self.tags('{"models":[{"name":["x"],"size":1},{"name":"ok:1","size":2}]}')
        self.assertEqual([m["name"] for m in data["models"]["cpu"]], ["ok:1"])  # the bad entry is dropped

    async def test_infinity_size(self) -> None:
        data = await self.tags('{"models":[{"name":"a:1","size":Infinity}]}')
        self.assertEqual(data["models"]["cpu"], {"error": "invalid JSON reply"})
        self.assertEqual(data["models"]["gpu"][0]["name"], "qwen3.5:9b")  # other sections intact

    async def test_overflowing_float(self) -> None:
        data = await self.tags('{"models":[{"name":"a:1","size":1,"modified_at":1e400}]}')
        self.assertEqual(data["models"]["cpu"], {"error": "invalid JSON reply"})

    async def test_section_postprocessing_is_guarded(self) -> None:
        def boom(*_a: Any, **_k: Any) -> Any:
            raise TypeError("synthetic bug on bad data")

        self.state.builder._routing = boom  # type: ignore[method-assign]
        await self.state.sampler.sample_once(self.state.session)
        status, data, _ = await self.get("/api/summary")
        self.assertEqual(status, 200)
        self.assertEqual(data["routing"], {"error": "invalid data from a source"})
        self.assertEqual(data["models"]["gpu"][0]["name"], "qwen3.5:9b")
        self.assertEqual(len(data["services"]), 7)

    async def test_gate_nan_errors_routing_and_body_is_valid_json(self) -> None:
        async def status_(_request: web.Request) -> web.Response:
            return web.Response(text='{"available":true,"reason":"no_game","avg30":NaN,"gpu_busy":Infinity}',
                                content_type="application/json")

        replace_handler(self.gate.app, "/status", status_)
        obs = await self.state.sampler.sample_once(self.state.session)
        self.assertEqual(obs.error, "invalid JSON reply")
        self.assertEqual(len(self.state.sampler.samples), 0)
        async with self.http.get(self.base + "/api/summary") as resp:
            text = await resp.text()
        data = strict_loads(text)
        self.assertEqual(data["routing"]["gate_error"], "invalid JSON reply")
        self.assertIsNone(data["routing"]["avg30"])

    async def test_recent_nan_is_502_valid_json(self) -> None:
        replace_handler(self.broker.app, "/broker/recent", single_write(b'[{"ts":1,"duration_ms":NaN}]'))
        async with self.http.get(self.base + "/api/requests") as resp:
            text = await resp.text()
            self.assertEqual(resp.status, 502)
        self.assertEqual(strict_loads(text), {"error": "broker: invalid JSON reply"})

    async def test_helper_nan_is_refused(self) -> None:
        orig = self.helper.reply

        def reply(req: dict[str, Any]) -> Any:
            out = orig(req)
            if req.get("op") == "localai-activity":
                out["data"]["entries"][0]["ts"] = float("nan")  # json.dumps writes NaN
            return out

        self.helper.reply = reply  # type: ignore[method-assign]
        async with self.http.get(self.base + "/api/audit/localai") as resp:
            text = await resp.text()
            self.assertEqual(resp.status, 502)
        self.assertEqual(strict_loads(text), {"error": "helper sent invalid JSON"})

    def test_json_out_refuses_nan(self) -> None:
        from ai_control.app import json_out

        with self.assertRaises(ValueError):
            json_out({"x": float("nan")})


# ---- S4: a corrupted samples file ---------------------------------------------------------------------
class CorruptSamplesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "gate-samples.jsonl")
        self.now = time.time()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def sampler(self) -> GateSampler:
        return GateSampler("http://127.0.0.1:1", self.path, clock=lambda: self.now)

    def test_bad_lines_are_skipped(self) -> None:
        good = json.dumps({"ts": self.now - 10, "gpu_busy": 5}).encode()
        with open(self.path, "wb") as fh:
            fh.write(good + b"\n\xff\xfe\x80 binary garbage\n" + b"[" * 200000 + b"\n" + b'{"ts": NaN}\n'
                     + b'{"ts": 1e400}\n{"ts":\n"str"\n[1]\n{"ts": -5}\n' + good + b"\n")
        s = self.sampler()
        with self.assertLogs("ai_control.sampler", "WARNING") as logs:
            self.assertEqual(s.load(), 2)
        self.assertIn("skipped 8 bad line(s)", "\n".join(logs.output))
        self.assertTrue(all(type(x["ts"]) is int for x in s.samples))
        self.assertTrue(os.path.exists(self.path))  # readable file: kept in place

    def test_unreadable_file_is_set_aside(self) -> None:
        os.mkdir(self.path)  # read() fails with EISDIR
        s = self.sampler()
        with self.assertLogs("ai_control.sampler", "WARNING"):
            self.assertEqual(s.load(), 0)
        self.assertTrue(os.path.isdir(f"{self.path}.corrupt-{int(self.now)}"))
        self.assertFalse(os.path.exists(self.path))
        s.samples.append({"ts": int(self.now), "gpu_busy": 1.0, "ollama_share": 0.0, "avg30": 1.0, "available": True})
        s.persist()  # the path is free again
        self.assertEqual(len(Path(self.path).read_text().splitlines()), 1)


class CorruptSamplesStartupTests(Harness):
    async def test_server_starts_on_corrupt_files(self) -> None:
        path = Path(self.cfg.gate_samples_path)
        for content in (b"\xff\xfe" * 1000 + b"\n" + b"[" * 300000, None):
            await self.stop_server()
            if content is None:
                path.unlink(missing_ok=True)
                path.mkdir()
            else:
                path.write_bytes(content)
            await self.start_server()
            self.assertEqual((await self.get("/healthz"))[0], 200)
            self.assertEqual((await self.get("/api/gate/history"))[:2], (200, []))


# ---- S5: trailing newline and other near-miss params ---------------------------------------------------
class TrailingNewlineTests(Harness):
    CASES = (
        "/api/gate/history?minutes=30%0A",
        "/api/gate/history?minutes=%D9%A3%D9%A0",
        "/api/gate/history?minutes=%EF%BC%93%EF%BC%90",
        "/api/gate/history?minutes=99999999999999999999",
        "/api/audit/sudo?user=claude&since=1700000000%0A",
        "/api/audit/sudo?user=claude%0A",
        "/api/audit/sudo?user=CLAUDE",
        "/api/audit/replay?user=claude&id=00/00/01%0A",
        "/api/audit/replay?user=claude&id=ABC123%0A",
        "/api/audit/replay?user=claude&id=a%00b",
        "/api/logs?source=pc:ollama%0A",
        "/api/logs?source=docker%0A",
        "/api/logs?source=docker&lines=1%0A",
        "/api/logs?source=docker&lines=%F0%9D%9F%8F",
        "/api/requests?limit=5%0A",
    )

    async def test_trailing_newline_refused(self) -> None:
        for path in self.CASES:
            status, data, _ = await self.get(path)
            self.assertEqual(status, 400, (path, data))
        self.assertEqual(self.helper.requests, [])
        self.assertEqual(self.agent.hits, [])

    def test_config_regexes_refuse_newline(self) -> None:
        from ai_control.config import ConfigError, from_dict

        for raw in ({"log_sources": [{"id": "docker\n", "label": "x"}]}, {"timezone": "Etc/UTC\n"}):
            with self.assertRaises(ConfigError, msg=raw):
                from_dict(raw)


# ---- S7: tests that kill the surviving mutants -----------------------------------------------------------
class MutantKillerTests(Harness):
    async def test_static_symlinks_out_of_root_not_served(self) -> None:
        web_root = Path(self.cfg.static_dir)
        os.symlink(self.dir, web_root / "link")  # a symlinked directory
        os.symlink(self.dir / "outside.txt", web_root / "esc")  # a symlinked file
        for path in ("/link/outside.txt", "/esc"):
            status, body, _ = await self.get(path)
            self.assertEqual(status, 200, path)  # the SPA fallback, not the file
            self.assertNotIn(b"outside", body, path)
            self.assertIn(b"AI Control", body, path)

    async def test_since_in_the_future_refused(self) -> None:
        future = int(time.time()) + 3 * 86400
        for path in (f"/api/audit/sudo?user=claude&since={future}", f"/api/audit/sudo?host=pc&user=claude&since={future}",
                     f"/api/audit/localai?since={future}"):
            status, data, _ = await self.get(path)
            self.assertEqual(status, 400, (path, data))
        soon = int(time.time()) + 3600  # within the day of slack
        self.assertEqual((await self.get(f"/api/audit/localai?since={soon}"))[0], 200)
        self.assertEqual(self.helper.requests[-1]["since"], soon)

    async def test_requests_never_pass_unlisted_fields(self) -> None:
        status, data, _ = await self.get("/api/requests?limit=200")
        self.assertEqual(status, 200)
        self.assertTrue(all("prompt" not in r and "response" not in r for r in data))
        self.assertNotIn("SHOULD NEVER", json.dumps(data))

    async def test_sse_cap_exact_fails_fast(self) -> None:
        import aiohttp

        timeout = aiohttp.ClientTimeout(total=5, sock_read=2)
        streams = []
        try:
            for i in range(9):
                resp = await asyncio.wait_for(self.http.get(self.base + "/api/events", timeout=timeout), 5)
                streams.append(resp)
                want = 200 if i < 8 else 503
                self.assertEqual(resp.status, want, f"stream {i + 1}")
        finally:
            for s in streams:
                s.close()


# ---- S8: integer timestamps and the persist race --------------------------------------------------------
class IntTimestampTests(Harness):
    async def test_ints(self) -> None:
        await self.state.sampler.sample_once(self.state.session)
        data = (await self.get("/api/summary"))[1]
        self.assertIs(type(data["server"]["time"]), int)
        self.assertTrue(all(type(s["since"]) is int for s in data["services"]))
        hist = (await self.get("/api/gate/history"))[1]
        self.assertTrue(hist and all(type(h["ts"]) is int for h in hist))
        # downsampled buckets too
        s = self.state.sampler
        now = time.time()
        s.samples.extend({"ts": int(now - 1700 + i), "gpu_busy": 1.0, "ollama_share": 0.0, "avg30": 1.0,
                          "available": True} for i in range(1000))
        hist = (await self.get("/api/gate/history"))[1]
        self.assertLessEqual(len(hist), 300)
        self.assertTrue(all(type(h["ts"]) is int for h in hist))

    async def test_persist_writes_a_snapshot_off_loop(self) -> None:
        s = self.state.sampler
        now = int(time.time())
        s.samples.extend({"ts": now - 500 + i, "gpu_busy": 1.0, "ollama_share": 0.0, "avg30": 1.0, "available": True}
                         for i in range(400))
        started = asyncio.Event()
        loop = asyncio.get_running_loop()
        orig_write = s.write
        seen: list[tuple[type, int]] = []

        def slow_write(samples: list[dict[str, Any]]) -> None:
            seen.append((type(samples), len(samples)))  # asserted on the loop, so a failure can't hang the test
            loop.call_soon_threadsafe(started.set)
            time.sleep(0.2)
            orig_write(samples)

        s.write = slow_write  # type: ignore[method-assign]
        task = asyncio.create_task(s.persist_async())
        await asyncio.wait_for(started.wait(), 5)
        for i in range(200):  # the loop keeps mutating and reading the ring while the thread writes
            s.samples.append({"ts": now + i, "gpu_busy": 2.0, "ollama_share": 0.0, "avg30": 1.0, "available": True})
            s.history(30)
            if i % 20 == 0:
                await asyncio.sleep(0.01)
        await task
        self.assertEqual(seen, [(list, 400)])  # a snapshot list, never the live deque
        self.assertEqual(len(Path(self.cfg.gate_samples_path).read_text().splitlines()), 400)


# ---- routing.gate_error_kind / gate_http_status (Lead, W question) ---------------------------------------
class GateErrorKindTests(Harness):
    async def routing(self) -> dict[str, Any]:
        await self.state.sampler.sample_once(self.state.session)
        status, data, _ = await self.get("/api/summary")
        self.assertEqual(status, 200)
        return data["routing"]

    async def test_ok(self) -> None:
        r = await self.routing()
        self.assertEqual((r["gate_error"], r["gate_error_kind"], r["gate_http_status"]), (None, None, 200))

    async def test_http(self) -> None:
        for mode, code in (("error", 500), ("503", 503)):
            self.gate.modes["/status"] = mode
            r = await self.routing()
            self.assertEqual((r["gate_error_kind"], r["gate_http_status"]), ("http", code), mode)
        self.assertEqual(r["gate_error"], "gate has no fresh sample (HTTP 503)")

    async def test_network(self) -> None:
        for setup in ("refused", "slow"):
            if setup == "refused":
                self.state.sampler.url = f"http://127.0.0.1:{fakes.free_port()}/status"
            else:
                self.state.sampler.url = f"{self.gate.url}/status"
                self.gate.modes["/status"] = "slow"
            r = await self.routing()
            self.assertEqual((r["gate_error_kind"], r["gate_http_status"]), ("network", None), setup)

    async def test_shape(self) -> None:
        self.gate.modes["/status"] = "garbage"  # 200 with HTML
        r = await self.routing()
        self.assertEqual((r["gate_error_kind"], r["gate_http_status"], r["gate_error"]), ("shape", 200, "invalid JSON reply"))
        self.gate.modes["/status"] = "ok"
        self.gate.status = {"reason": "no_game"}  # JSON without a boolean `available`
        r = await self.routing()
        self.assertEqual((r["gate_error_kind"], r["gate_http_status"]), ("shape", 200))

    async def test_stale(self) -> None:
        status, data, _ = await self.get("/api/summary")  # no sample taken yet
        self.assertEqual((data["routing"]["gate_error_kind"], data["routing"]["gate_http_status"]), ("stale", None))
        await self.state.sampler.sample_once(self.state.session)
        later = time.time() + 3600
        self.state.builder.clock = lambda: later
        data = (await self.get("/api/summary"))[1]
        r = data["routing"]
        self.assertEqual((r["gate_error"], r["gate_error_kind"]), ("gate sample is stale", "stale"))


# ---- Round-2 follow-ups: safe() logging, vram unknown vs none, NaN from a local computation ----------------
class SafeFollowUpTests(Harness):
    def setUp(self) -> None:
        from ai_control import summary

        summary._safe_logged.clear()

    async def test_local_nan_errors_only_its_section(self) -> None:
        async def aibox() -> dict[str, Any]:
            return {"load1": float("nan"), "mem_total": 1, "mem_avail": 1, "swap_used": 0, "disk": []}

        self.state.builder._aibox = aibox  # type: ignore[method-assign]
        await self.state.sampler.sample_once(self.state.session)
        async with self.http.get(self.base + "/api/summary") as resp:
            text = await resp.text()
            self.assertEqual(resp.status, 200, text)
        data = strict_loads(text)
        self.assertEqual(data["resources"]["aibox"], {"error": "invalid data from a source"})
        self.assertEqual(data["resources"]["pc"]["disk"][0]["mount"], "/")

    async def test_safe_logs_traceback_rate_limited(self) -> None:
        from ai_control import summary

        def boom(*_a: Any, **_k: Any) -> Any:
            raise TypeError("synthetic bug")

        self.state.builder._routing = boom  # type: ignore[method-assign]
        await self.state.sampler.sample_once(self.state.session)
        with self.assertLogs("ai_control.summary", "WARNING") as logs:
            for _ in range(3):
                await self.state.builder.build()
        hits = [r for r in logs.records if "summary routing" in r.getMessage()]
        self.assertEqual(len(hits), 1)  # once per (section, type) per 10 min
        self.assertIsNotNone(hits[0].exc_info)
        self.assertEqual(hits[0].exc_info[0], TypeError)
        summary._safe_logged[("routing", "TypeError")] -= summary.SAFE_LOG_INTERVAL + 1
        with self.assertLogs("ai_control.summary", "WARNING") as logs:
            await self.state.builder.build()
        self.assertEqual(len([r for r in logs.records if "summary routing" in r.getMessage()]), 1)

    async def test_vram_unknown_is_error_not_null(self) -> None:
        data = (await self.summary_once())["resources"]
        self.assertEqual(data["vram"]["model"], "qwen3.5:9b")
        orig = dict(fakes.AGENT_OLLAMA)
        try:
            fakes.AGENT_OLLAMA["loaded"] = {"error": "unreachable"}
            self.assertEqual((await self.summary_once())["resources"]["vram"], {"error": "gaming PC Ollama: unreachable"})
            fakes.AGENT_OLLAMA["loaded"] = []
            self.assertIsNone((await self.summary_once())["resources"]["vram"])  # positively nothing loaded
        finally:
            fakes.AGENT_OLLAMA.clear()
            fakes.AGENT_OLLAMA.update(orig)
        await self.agent.close()
        self.assertIn("error", (await self.summary_once())["resources"]["vram"])

    async def summary_once(self) -> dict[str, Any]:
        await self.state.sampler.sample_once(self.state.session)
        status, data, _ = await self.get("/api/summary")
        self.assertEqual(status, 200)
        return data


if __name__ == "__main__":
    unittest.main()
