"""AI Control server tests: real fake sources (HTTP + Unix socket), the production runner on port 0.

Run: python3 -m unittest discover -s tests -v   (from apps/ai-control/server, with aiohttp installed)
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import socket
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any

import aiohttp

from ai_control.app import CSP, STATE
from ai_control.config import ConfigError, from_dict
from ai_control.server import bound_port, start

from . import fakes

logging.getLogger("ai_control").addHandler(logging.NullHandler())
logging.getLogger("ai_control").propagate = False


class Harness(unittest.IsolatedAsyncioTestCase):
    """Starts every fake source and the server. Background loops are off by default; tests drive the
    sampler and the summary builder directly (deterministic)."""

    background = False
    extra_config: dict[str, Any] = {}

    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.gate, self.broker, self.webui, self.agent = fakes.FakeGate(), fakes.FakeBroker(), fakes.FakeWebui(), fakes.FakeAgent()
        for s in (self.gate, self.broker, self.webui, self.agent):
            await s.start()
        self.helper = fakes.FakeHelper(str(self.dir / "helper.sock"))
        await self.helper.start()
        fakes.make_proc(self.dir / "proc")
        self.chronyc = fakes.make_chronyc(self.dir / "chrony")
        self.token_file = self.dir / "agent-token"
        fakes.write_token(self.token_file)
        web_root = self.dir / "web"
        (web_root / "js").mkdir(parents=True)
        (web_root / "index.html").write_text("<!doctype html><title>AI Control</title>")
        (web_root / "js" / "app.js").write_text("export const x = 1;\n")
        (web_root / ".secret").write_text("hidden")
        (self.dir / "outside.txt").write_text("outside")
        self.cfg_raw: dict[str, Any] = {
            "listen_host": "127.0.0.1",
            "gate_url": self.gate.url,
            "broker_url": self.broker.url,
            "webui_url": self.webui.url,
            "agent_url": self.agent.url,
            "helper_socket": self.helper.path,
            "agent_token_file": str(self.token_file),
            "chronyc": str(self.chronyc),
            "proc_root": str(self.dir / "proc"),
            "disk_mounts": [str(self.dir)],
            "static_dir": str(web_root),
            "gate_samples_path": str(self.dir / "gate-samples.jsonl"),
            "source_timeout": 0.5,
            "request_timeout": 0.5,
            "summary_ttl": 0,
            "timezone": "Etc/UTC",
            "sse_keepalive": 0.2,
            **self.extra_config,
        }
        await self.start_server()

    async def start_server(self) -> None:
        self.cfg = from_dict(self.cfg_raw)
        self.runner = await start(self.cfg, host="127.0.0.1", port=0, background=self.background)
        self.base = f"http://127.0.0.1:{bound_port(self.runner)}"
        self.state = self.runner.app[STATE]
        self.http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20, sock_read=5))

    async def stop_server(self) -> None:
        await self.http.close()
        await self.runner.cleanup()

    async def asyncTearDown(self) -> None:
        await self.stop_server()
        for s in (self.gate, self.broker, self.webui, self.agent):
            await s.close()
        await self.helper.close()
        self.tmp.cleanup()

    async def get(self, path: str, headers: dict[str, str] | None = None) -> tuple[int, Any, Any]:
        async with self.http.get(self.base + path, headers=headers) as resp:
            body = await resp.read()
            try:
                data = json.loads(body) if body else None
            except ValueError:
                data = body
            return resp.status, data, resp.headers

    async def summary(self) -> dict[str, Any]:
        await self.state.sampler.sample_once(self.state.session)
        status, data, _ = await self.get("/api/summary")
        self.assertEqual(status, 200, data)
        return data

    def freeze_aibox(self) -> None:
        """The fixture's disk mount is a temp dir on a shared filesystem whose free space moves with other
        processes; tests comparing two builds' hashes pin the ai-box resources instead."""
        frozen = {"load1": 0.5, "mem_total": 1, "mem_avail": 1, "swap_used": 0, "disk": [{"mount": "/", "total": 10, "avail": 5}]}

        async def aibox() -> dict[str, Any]:
            return dict(frozen)

        self.state.builder._aibox = aibox  # type: ignore[method-assign]

    def services(self, data: dict[str, Any]) -> dict[str, dict[str, Any]]:
        return {s["id"]: s for s in data["services"]}


# ---- summary -------------------------------------------------------------------------------------
class SummaryTests(Harness):
    async def test_all_sources_ok(self) -> None:
        data = await self.summary()
        self.assertEqual(set(data), {"server", "routing", "services", "resources", "models", "clock", "timers", "alerts"})
        self.assertEqual(data["server"]["timeZone"], "Etc/UTC")
        self.assertIsNone(data["server"]["identity"])  # no Tailscale headers sent
        r = data["routing"]
        self.assertEqual((r["to"], r["reason"], r["avg30"], r["gpu_busy"], r["ollama_share"]), ("gpu", "no_game", 12.5, 20, 15.0))
        self.assertEqual((r["uid_match"], r["threshold"], r["window_s"], r["gate_error"], r["gate_error_kind"]), (True, 50.0, 1800, None, None))
        svc = self.services(data)
        self.assertEqual({k: v["status"] for k, v in svc.items()},
                         dict.fromkeys(("gate", "pc-ollama", "cpu-ollama", "broker", "webui", "helper", "agent"), "ok"))
        self.assertTrue(all(type(v["since"]) is int for v in svc.values()))
        self.assertIs(type(data["server"]["time"]), int)
        res = data["resources"]
        self.assertEqual(res["aibox"]["load1"], 0.52)
        self.assertEqual(res["aibox"]["mem_total"], 16000000 * 1024)
        self.assertEqual(res["aibox"]["swap_used"], 500000 * 1024)
        self.assertEqual(res["aibox"]["disk"][0]["mount"], str(self.dir))
        self.assertEqual(res["pc"]["disk"][0]["mount"], "/")
        self.assertEqual(res["vram"], {"model": "qwen3.5:9b", "size_vram": 7_000_000_000})
        m = data["models"]
        self.assertEqual([x["name"] for x in m["gpu"]], ["qwen3.5:9b"])
        self.assertEqual([x["name"] for x in m["cpu"]], ["qwen3.5:4b", "nomic-embed-text:latest"])  # alias dropped
        self.assertEqual(m["cpu"][0]["quant"], "Q4_K_M")
        self.assertEqual(m["cpu"][0]["modified"], 1790812800)  # Ollama's RFC 3339 text → epoch seconds
        self.assertEqual(m["gpu"][0], {"name": "qwen3.5:9b", "size": 6_000_000_000, "quant": "Q4_K_M", "modified": 1792900000})
        self.assertEqual(m["loaded"]["gpu"][0]["expires_at"], 1793000300)
        self.assertEqual(m["alias"], {"assistant": {"gpu": "qwen3.5:9b", "cpu": "qwen3.5:4b"}})
        self.assertEqual(m["loaded"]["gpu"][0]["name"], "qwen3.5:9b")
        self.assertIn("error", m["loaded"]["cpu"])  # the broker routes /api/ps to the GPU right now
        self.assertEqual(data["clock"], {"synced": True, "offset_ms": -0.25, "source": "192.0.2.1"})
        self.assertEqual(data["timers"][0]["unit"], "homelab-ai-digest.timer")
        self.assertEqual(data["alerts"], [])

    async def test_cpu_loaded_models_when_broker_routes_to_cpu(self) -> None:
        self.broker.ps_backend = "cpu"
        data = await self.summary()
        self.assertEqual(data["models"]["loaded"]["cpu"][0]["name"], "qwen3.5:4b")

    async def test_routing_when_gaming(self) -> None:
        self.gate.status.update(available=False, reason="game_busy", avg30=80.0, game="SomeGame")
        self.broker.routing_to = "cpu"
        r = (await self.summary())["routing"]
        self.assertEqual((r["to"], r["reason"], r["game"], r["avg30"]), ("cpu", "game_busy", "SomeGame", 80.0))

    async def test_alerts(self) -> None:
        # alias target missing on the GPU side, low disk on the PC, clock unsynced
        orig_ollama, orig_disk = dict(fakes.AGENT_OLLAMA), dict(fakes.AGENT_DISK)
        try:
            fakes.AGENT_OLLAMA["models"] = [{"name": "other:1b", "size": None, "quant": None, "modified": None}]
            fakes.AGENT_DISK["disk"] = [{"mount": "/", "total": 1000, "avail": 30}, {"mount": "/mnt/share", "error": "statfs timed out"}]
            (self.chronyc.parent / "chrony.out").write_text(fakes.CHRONY_UNSYNCED)
            data = await self.summary()
        finally:
            fakes.AGENT_OLLAMA.clear()
            fakes.AGENT_OLLAMA.update(orig_ollama)
            fakes.AGENT_DISK.clear()
            fakes.AGENT_DISK.update(orig_disk)
        ids = {a["id"]: a["level"] for a in data["alerts"]}
        self.assertEqual(ids, {"alias-missing:assistant:gpu": "warn", "disk-low:pc:/": "crit", "clock-unsynced": "warn"})

    async def test_docker_inactive_marks_cpu_ollama_down(self) -> None:
        self.helper.docker = "inactive"
        data = await self.summary()
        svc = self.services(data)
        self.assertEqual(svc["cpu-ollama"]["status"], "down")
        self.assertIn("docker inactive", svc["cpu-ollama"]["detail"])
        self.assertIn({"id": "down:cpu-ollama", "level": "crit",
                       "text": "ai-box CPU Ollama is down: docker inactive"}, data["alerts"])

    async def test_since_tracks_status_changes(self) -> None:
        now = [time.time()]
        self.state.builder.clock = lambda: now[0]
        first = self.services(await self.summary())["webui"]["since"]
        now[0] += 5
        self.assertEqual(self.services(await self.summary())["webui"]["since"], first)
        self.webui.modes["/health"] = "error"
        now[0] += 5
        changed = self.services(await self.summary())["webui"]
        self.assertEqual(changed["status"], "down")
        self.assertGreater(changed["since"], first)


class IsolationTests(Harness):
    """Each source down or slow → only its own section errors."""

    OTHER_SECTIONS = ("resources", "models", "clock", "timers")

    async def check(self, broken: set[str], expect_service: dict[str, str]) -> dict[str, Any]:
        t0 = time.monotonic()
        data = await self.summary()
        self.assertLess(time.monotonic() - t0, 3 * self.cfg.source_timeout + 1.0, "a slow source held the summary")
        svc = {k: v["status"] for k, v in self.services(data).items()}
        for sid, want in expect_service.items():
            self.assertEqual(svc[sid], want, (sid, data["services"]))
        for sid, st in svc.items():
            if sid not in expect_service:
                self.assertEqual(st, "ok", (sid, data["services"]))
        sections = {
            "aibox": data["resources"]["aibox"],
            "pc": data["resources"]["pc"],
            "gpu_models": data["models"]["gpu"],
            "cpu_models": data["models"]["cpu"],
            "alias": data["models"]["alias"],
            "clock": data["clock"],
            "timers": data["timers"],
        }
        for name, section in sections.items():
            is_err = isinstance(section, dict) and set(section) == {"error"}
            self.assertEqual(is_err, name in broken, (name, section))
        return data

    async def test_gate_down(self) -> None:
        self.state.sampler.url = f"http://127.0.0.1:{fakes.free_port()}/status"
        data = await self.check(set(), {"gate": "down"})
        self.assertEqual(data["routing"]["to"], "gpu")  # the broker's own answer still says where it routes
        self.assertIn("connection refused", data["routing"]["gate_error"])
        self.assertEqual([a["id"] for a in data["alerts"]], ["gate-unreachable"])

    async def test_gate_slow(self) -> None:
        self.gate.modes["/status"] = "slow"
        data = await self.check(set(), {"gate": "down"})
        self.assertIn("timed out", data["routing"]["gate_error"])

    async def test_gate_503_is_warn(self) -> None:
        self.gate.modes["/status"] = "503"
        await self.check(set(), {"gate": "warn"})

    async def test_broker_down(self) -> None:
        self.broker.modes["*"] = "error"
        data = await self.check({"cpu_models", "alias"}, {"broker": "down"})
        self.assertEqual((data["routing"]["to"], data["routing"]["reason"]), (None, "broker_down"))
        self.assertIn({"id": "down:broker", "level": "crit", "text": "GPU broker is down: HTTP 500; container running (healthy)"},
                      data["alerts"])

    async def test_broker_slow(self) -> None:
        self.broker.modes["*"] = "slow"
        await self.check({"cpu_models", "alias"}, {"broker": "down"})

    async def test_broker_cpu_list_unavailable(self) -> None:
        self.broker.tags_warning = True
        await self.check({"cpu_models"}, {})

    async def test_webui_down(self) -> None:
        self.webui.modes["/health"] = "error"
        await self.check(set(), {"webui": "down"})

    async def test_webui_slow(self) -> None:
        self.webui.modes["/health"] = "slow"
        await self.check(set(), {"webui": "down"})

    async def test_helper_down(self) -> None:
        await self.helper.close()
        Path(self.helper.path).unlink(missing_ok=True)
        await self.check({"timers"}, {"helper": "down", "cpu-ollama": "unknown"})

    async def test_helper_slow(self) -> None:
        self.helper.mode = "slow"
        await self.check({"timers"}, {"helper": "down", "cpu-ollama": "unknown"})

    async def test_helper_unpinned_shapes(self) -> None:
        self.helper.mode = "unpinned"  # bare lists: the old tolerant parser took these, the pinned one doesn't
        data = await self.check({"timers"}, {"helper": "down", "cpu-ollama": "unknown"})
        self.assertIn("pinned shape", self.services(data)["helper"]["detail"])

    async def test_missing_container(self) -> None:
        orig = list(fakes.CONTAINERS)
        try:
            fakes.CONTAINERS[0] = {**fakes.CONTAINERS[0], "state": "missing", "health": None, "started_at": None}
            data = await self.summary()
        finally:
            fakes.CONTAINERS[:] = orig
        self.assertEqual((self.services(data)["cpu-ollama"]["status"], self.services(data)["cpu-ollama"]["detail"]),
                         ("down", "container missing"))

    async def test_helper_garbage(self) -> None:
        self.helper.mode = "garbage"
        await self.check({"timers"}, {"helper": "down", "cpu-ollama": "unknown"})

    async def test_agent_down(self) -> None:
        await self.agent.close()
        await self.check({"pc", "gpu_models"}, {"agent": "down", "pc-ollama": "unknown"})

    async def test_agent_slow(self) -> None:
        self.agent.modes["*"] = "slow"
        await self.check({"pc", "gpu_models"}, {"agent": "down", "pc-ollama": "unknown"})

    async def test_agent_unpinned_shapes(self) -> None:
        # each Y path in a shape the old tolerant parser took: only that part errors, and the agent is down
        for path, broken, pc_ollama in (("/v1/services", set(), "unknown"), ("/v1/ollama", {"gpu_models"}, "ok"),
                                        ("/v1/disk", {"pc"}, "ok")):
            with self.subTest(path=path):
                self.agent.modes = {path: "unpinned"}
                data = await self.check(broken, {"agent": "down", "pc-ollama": pc_ollama} if pc_ollama != "ok" else {"agent": "down"})
                self.assertIn(f"host agent {path} reply doesn't match its pinned shape", self.services(data)["agent"]["detail"])

    async def test_agent_partial_errors_stay_in_their_part(self) -> None:
        orig_ollama, orig_svc = dict(fakes.AGENT_OLLAMA), dict(fakes.AGENT_SERVICES)
        try:
            fakes.AGENT_OLLAMA["models"] = {"error": "unreachable"}
            fakes.AGENT_SERVICES["services"] = [{"id": "ollama", "unit": "ollama.service", "error": "systemctl timed out"},
                                                orig_svc["services"][1]]
            data = await self.check({"gpu_models"}, {"pc-ollama": "unknown"})
        finally:
            fakes.AGENT_OLLAMA.clear()
            fakes.AGENT_OLLAMA.update(orig_ollama)
            fakes.AGENT_SERVICES.clear()
            fakes.AGENT_SERVICES.update(orig_svc)
        self.assertEqual(data["models"]["gpu"], {"error": "gaming PC Ollama: unreachable"})
        self.assertEqual(data["models"]["loaded"]["gpu"][0]["name"], "qwen3.5:9b")  # the other half still reported
        self.assertEqual(self.services(data)["pc-ollama"]["detail"], "systemctl: systemctl timed out")

    async def test_agent_bad_token(self) -> None:
        self.agent.token = "a-different-synthetic-token"
        data = await self.check({"pc", "gpu_models"}, {"agent": "down", "pc-ollama": "unknown"})
        self.assertIn("rejected the token", self.services(data)["agent"]["detail"])

    async def test_agent_token_file_missing(self) -> None:
        self.token_file.unlink()
        data = await self.check({"pc", "gpu_models"}, {"agent": "down", "pc-ollama": "unknown"})
        self.assertEqual(data["resources"]["pc"], {"error": "agent token file unreadable"})

    async def test_chrony_fails(self) -> None:
        (self.chronyc.parent / "chrony.rc").write_text("1")
        await self.check({"clock"}, {})

    async def test_chrony_slow(self) -> None:
        (self.chronyc.parent / "chrony.sleep").write_text("3")
        await self.check({"clock"}, {})

    async def test_proc_unreadable(self) -> None:
        (self.dir / "proc" / "meminfo").unlink()
        await self.check({"aibox"}, {})

    async def test_everything_down(self) -> None:
        await self.agent.close()
        await self.helper.close()
        Path(self.helper.path).unlink(missing_ok=True)
        self.broker.modes["*"] = "error"
        self.webui.modes["/health"] = "error"
        self.gate.modes["/status"] = "error"
        (self.chronyc.parent / "chrony.rc").write_text("1")
        status, data, _ = await self.get("/api/summary")
        self.assertEqual(status, 200)
        self.assertEqual(data["resources"]["aibox"]["load1"], 0.52)  # the local source still answers


# ---- guards --------------------------------------------------------------------------------------
class GuardTests(Harness):
    async def test_non_get_methods_are_405(self) -> None:
        for method in ("POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"):
            for path in ("/api/summary", "/healthz", "/", "/api/nope"):
                async with self.http.request(method, self.base + path, headers={"X-Homelab": "1"}) as resp:
                    self.assertEqual(resp.status, 405, (method, path))
                    self.assertEqual(resp.headers["Allow"], "GET")
                    self.assertEqual(resp.headers["Content-Security-Policy"], CSP)
                    if method != "HEAD":
                        self.assertIn("error", await resp.json())

    async def test_sec_fetch_site(self) -> None:
        for value in ("cross-site", "same-site", "bogus"):
            status, data, headers = await self.get("/api/summary", {"Sec-Fetch-Site": value})
            self.assertEqual(status, 403, value)
            self.assertEqual(data, {"error": "cross-site request refused"})
            self.assertEqual(headers["Cache-Control"], "no-store")
            self.assertEqual((await self.get("/api/gate/history", {"Sec-Fetch-Site": value}))[0], 403)
            self.assertEqual((await self.get("/api/events", {"Sec-Fetch-Site": value}))[0], 403)
        for value in ("same-origin", "none", None):
            status, _, _ = await self.get("/api/gate/history", {"Sec-Fetch-Site": value} if value else None)
            self.assertEqual(status, 200, value)
        # the shell and static files are exempt
        self.assertEqual((await self.get("/", {"Sec-Fetch-Site": "cross-site"}))[0], 200)

    async def test_csp_and_no_store_everywhere(self) -> None:
        await self.state.sampler.sample_once(self.state.session)
        for path, want_status in (("/api/summary", 200), ("/api/gate/history", 200), ("/healthz", 200),
                                  ("/api/nope", 404), ("/api/logs?source=evil", 400), ("/", 200), ("/js/app.js", 200)):
            status, _, headers = await self.get(path)
            self.assertEqual(status, want_status, path)
            self.assertEqual(headers["Content-Security-Policy"], CSP, path)
            self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
            if path.startswith("/api") or path == "/healthz":
                self.assertEqual(headers["Cache-Control"], "no-store", path)

    async def raw(self, request: bytes) -> bytes:
        reader, writer = await asyncio.open_connection("127.0.0.1", bound_port(self.runner))
        writer.write(request)
        await writer.drain()
        data = await asyncio.wait_for(reader.read(65536), 5)
        writer.close()
        return data

    async def test_malformed_requests_are_400_json(self) -> None:
        for req in (
            b"GET /a b HTTP/1.1\r\nHost: x\r\n\r\n",
            b"GET /\x01\x02 HTTP/1.1\r\nHost: x\r\n\r\n",
            b"GET http://[bad/ HTTP/1.1\r\nHost: x\r\n\r\n",
            b"GET /api/summary HTTP/1.1\r\nBad Header\r\n\r\n",
        ):
            reply = await self.raw(req)
            head, _, body = reply.partition(b"\r\n\r\n")
            self.assertRegex(head.split(b"\r\n")[0], rb"^HTTP/1\.[01] 400 ", (req, reply[:200]))
            self.assertIn(b"Content-Security-Policy: " + CSP.encode(), head)
            self.assertIn(b"Cache-Control: no-store", head)
            self.assertEqual(json.loads(body), {"error": "bad request"})
        for path in ("/api/logs?source=%ZZ", "/api/gate/history?minutes=%E2%28", "/%ZZ", "/api/audit/replay?user=claude&id=%00"):
            status, data, headers = await self.get(path)
            self.assertIn(status, (200, 400, 404), path)
            self.assertNotEqual(status, 500)
            self.assertEqual(headers["Content-Security-Policy"], CSP)
        self.assertEqual((await self.get("/healthz"))[0], 200)  # still alive

    async def test_no_traceback_on_internal_error(self) -> None:
        async def boom(*_a: Any, **_k: Any) -> Any:
            raise RuntimeError("secret internal detail")

        self.state.builder.get = boom  # type: ignore[method-assign]
        status, data, _ = await self.get("/api/summary")
        self.assertEqual((status, data), (500, {"error": "internal error"}))


class IdentityTests(Harness):
    HEADERS = {"Tailscale-User-Login": "someone@example.invalid", "Tailscale-User-Name": "Some One"}

    async def test_identity_from_loopback_peer(self) -> None:
        await self.state.sampler.sample_once(self.state.session)
        status, data, _ = await self.get("/api/summary", self.HEADERS)
        self.assertEqual(status, 200)
        self.assertEqual(data["server"]["identity"], {"login": "someone@example.invalid", "name": "Some One"})

    async def test_identity_ignored_from_other_peer(self) -> None:
        await self.state.sampler.sample_once(self.state.session)
        # Same server, but the TCP peer is 127.0.0.2 (Linux routes all of 127/8 to lo).
        conn = aiohttp.TCPConnector(local_addr=("127.0.0.2", 0))
        async with aiohttp.ClientSession(connector=conn) as other:
            async with other.get(self.base + "/api/summary", headers=self.HEADERS) as resp:
                data = await resp.json()
        self.assertEqual(resp.status, 200)
        self.assertIsNone(data["server"]["identity"])

    async def test_identity_header_control_chars_stripped(self) -> None:
        await self.state.sampler.sample_once(self.state.session)
        _s, data, _ = await self.get("/api/summary", {"Tailscale-User-Login": "a\tb" + "x" * 400})
        self.assertEqual(data["server"]["identity"]["login"], "ab" + "x" * 254)
        self.assertIsNone(data["server"]["identity"]["name"])


class EtagTests(Harness):
    async def test_etag_304(self) -> None:
        await self.state.sampler.sample_once(self.state.session)
        # Freeze the build so the second request sees the same summary.
        built = await self.state.builder.get(0)
        self.state.builder.current = built
        object.__setattr__(self.cfg, "summary_ttl", 3600)
        status, _, headers = await self.get("/api/summary")
        etag = headers["ETag"]
        self.assertEqual(status, 200)
        status, body, headers = await self.get("/api/summary", {"If-None-Match": etag})
        self.assertEqual(status, 304)
        self.assertIsNone(body)
        self.assertEqual(headers["ETag"], etag)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual((await self.get("/api/summary", {"If-None-Match": '"other"'}))[0], 200)
        # A different identity is a different representation.
        _s, _b, h2 = await self.get("/api/summary", IdentityTests.HEADERS)
        self.assertNotEqual(h2["ETag"], etag)

    async def test_etag_ignores_server_time_only(self) -> None:
        await self.state.sampler.sample_once(self.state.session)
        real_aibox = self.state.builder._aibox
        self.freeze_aibox()
        now = [time.time()]
        self.state.builder.clock = lambda: now[0]
        a = await self.state.builder.build()
        now[0] += 2
        b = await self.state.builder.build()
        self.assertNotEqual(a.data["server"]["time"], b.data["server"]["time"])
        # /proc is static in the fixture, so only server.time moved
        self.assertEqual(a.core_hash, b.core_hash)
        self.state.builder._aibox = real_aibox  # type: ignore[method-assign]
        c = await self.state.builder.build()
        self.assertNotEqual(c.core_hash, b.core_hash)


# ---- sampler -------------------------------------------------------------------------------------
class SamplerTests(Harness):
    async def test_ring_trim_persist_reload(self) -> None:
        s = self.state.sampler
        now = time.time()
        s.samples.clear()
        s.samples.extend({"ts": now - 90000 + i, "gpu_busy": 1.0, "ollama_share": 1.0, "avg30": 1.0, "available": True}
                         for i in range(3))  # older than 24 h
        await s.sample_once(self.state.session)
        self.assertEqual(len(s.samples), 1)  # trimmed
        self.assertEqual(s.samples[0]["gpu_busy"], 20.0)
        s.persist()
        path = Path(self.cfg.gate_samples_path)
        lines = path.read_text().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(oct(path.stat().st_mode & 0o777), "0o640")
        self.assertEqual([p.name for p in self.dir.iterdir() if p.name.startswith(".gate-samples")], [])  # no tmp left
        # restart: the new server reloads the file, skipping junk and stale lines
        await self.stop_server()  # (cleanup persists the ring again)
        with path.open("a") as fh:
            fh.write("not json\n")
            fh.write(json.dumps({"ts": now - 100000, "gpu_busy": 5}) + "\n")
            fh.write(json.dumps({"ts": now - 60, "gpu_busy": 7, "ollama_share": None, "avg30": 3, "available": False}) + "\n")
        await self.start_server()
        status, hist, _ = await self.get("/api/gate/history?minutes=30")
        self.assertEqual(status, 200)
        self.assertEqual([h["gpu_busy"] for h in hist], [7.0, 20.0])
        self.assertEqual(set(hist[0]), {"ts", "gpu_busy", "ollama_share", "avg30", "available"})

    async def test_cleanup_persists(self) -> None:
        await self.state.sampler.sample_once(self.state.session)
        await self.stop_server()
        self.assertEqual(len(Path(self.cfg.gate_samples_path).read_text().splitlines()), 1)
        await self.start_server()

    async def test_failed_fetch_adds_no_sample(self) -> None:
        self.gate.modes["/status"] = "error"
        obs = await self.state.sampler.sample_once(self.state.session)
        self.assertEqual(obs.error, "HTTP 500")
        self.assertEqual(len(self.state.sampler.samples), 0)

    async def test_downsampled_to_300(self) -> None:
        s = self.state.sampler
        now = time.time()
        s.samples.clear()
        s.samples.extend({"ts": now - 86000 + i * 10, "gpu_busy": float(i % 100), "ollama_share": 0.0, "avg30": 1.0,
                          "available": i % 50 != 0} for i in range(8600))
        for minutes in (30, 600, 1440):
            status, hist, _ = await self.get(f"/api/gate/history?minutes={minutes}")
            self.assertEqual(status, 200)
            self.assertLessEqual(len(hist), 300)
            self.assertGreater(len(hist), 100 if minutes > 30 else 150)
            ts = [h["ts"] for h in hist]
            self.assertEqual(ts, sorted(ts))
            self.assertGreaterEqual(ts[0], now - minutes * 60 - 1)
        self.assertIn(False, [h["available"] for h in hist])  # a bucket with one unavailable sample shows it

    async def test_ring_bounded(self) -> None:
        s = self.state.sampler
        self.assertEqual(s.samples.maxlen, int(86400 / 10) + 16)


class SamplerLoopTests(Harness):
    background = True
    extra_config = {"sample_interval": 0.05, "persist_interval": 0.2, "summary_interval": 0.05}

    async def test_background_loop_samples_and_persists(self) -> None:
        path = Path(self.cfg.gate_samples_path)
        for _ in range(100):
            if path.exists() and len(path.read_text().splitlines()) >= 3:
                break
            await asyncio.sleep(0.05)
        self.assertGreaterEqual(len(path.read_text().splitlines()), 3)
        self.assertGreaterEqual(len(self.state.sampler.samples), 3)


# ---- SSE -----------------------------------------------------------------------------------------
class EventTests(Harness):
    async def open_stream(self) -> aiohttp.ClientResponse:
        resp = await self.http.get(self.base + "/api/events", timeout=aiohttp.ClientTimeout(total=10))
        return resp

    async def wait_clients(self, n: int) -> None:
        for _ in range(100):
            if len(self.state.hub.clients) == n:
                return
            await asyncio.sleep(0.02)
        self.fail(f"hub has {len(self.state.hub.clients)} clients, wanted {n}")

    async def test_client_cap_and_release(self) -> None:
        streams = [await self.open_stream() for _ in range(8)]
        try:
            for s in streams:
                self.assertEqual(s.status, 200)
                self.assertEqual(s.headers["Content-Type"], "text/event-stream")
                self.assertEqual(s.headers["Cache-Control"], "no-store")
                self.assertEqual(s.headers["Content-Security-Policy"], CSP)
            await self.wait_clients(8)
            ninth = await asyncio.wait_for(self.open_stream(), 5)
            streams.append(ninth)
            self.assertEqual(ninth.status, 503, "the 9th stream must be refused")
            self.assertEqual(await ninth.json(), {"error": "too many event streams"})
            self.assertEqual(ninth.headers["Retry-After"], "10")
            streams.remove(ninth)
            streams.pop().close()
            await self.wait_clients(7)
            again = await self.open_stream()
            self.assertEqual(again.status, 200)
            streams.append(again)
        finally:
            for s in streams:
                s.close()
        await self.wait_clients(0)

    async def test_ping_on_change(self) -> None:
        await self.state.sampler.sample_once(self.state.session)
        await self.state.builder.build()
        resp = await self.open_stream()
        try:
            first = await asyncio.wait_for(resp.content.readuntil(b"\n\n"), 5)  # retry:
            first += await asyncio.wait_for(resp.content.readuntil(b"\n\n"), 5)
            self.assertIn(b"event: summary", first)
            self.assertIn(self.state.builder.current.core_hash.encode(), first)
            (self.dir / "proc" / "loadavg").write_text("9.99 0 0 1/1 1\n")
            await self.state.builder.build()
            new_hash = self.state.builder.current.core_hash.encode()
            for _ in range(20):
                chunk = await asyncio.wait_for(resp.content.readuntil(b"\n\n"), 5)
                if chunk.startswith(b":"):
                    continue  # keepalive
                self.assertIn(new_hash, chunk)
                break
            else:
                self.fail("no summary ping")
        finally:
            resp.close()

    async def test_no_ping_when_hash_unchanged(self) -> None:
        self.freeze_aibox()
        await self.state.sampler.sample_once(self.state.session)
        await self.state.builder.build()
        pings = []
        self.state.builder.on_change.append(lambda b: pings.append(b.core_hash))
        await self.state.builder.build()
        self.assertEqual(pings, [])

    async def test_backpressure_drop(self) -> None:
        hub = self.state.hub

        class FakeTransport:
            size = 0

            def is_closing(self) -> bool:
                return False

            def get_write_buffer_size(self) -> int:
                return self.size

        class FakeRequest:
            transport = FakeTransport()

        req = FakeRequest()
        self.assertFalse(hub.too_backed_up(req))  # type: ignore[arg-type]
        req.transport.size = hub.buffer_limit + 1
        self.assertTrue(hub.too_backed_up(req))  # type: ignore[arg-type]
        self.assertEqual(hub.buffer_limit, 1024 * 1024)

    async def test_stalled_client_is_dropped_and_slot_freed(self) -> None:
        # A client that never reads: shrink our socket buffers, then let pings + keepalives pile up.
        hub = self.state.hub
        hub.buffer_limit = 2048
        hub.write_timeout = 0.3
        sock = socket.socket()
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024)
        sock.connect(("127.0.0.1", bound_port(self.runner)))
        sock.sendall(b"GET /api/events HTTP/1.1\r\nHost: x\r\n\r\n")
        try:
            await self.wait_clients(1)
            # flood: each notify queues one event; the transport fills since nobody reads
            big = "x" * 60000
            for _ in range(400):
                hub.notify(big)
                await asyncio.sleep(0.005)
                if not hub.clients:
                    break
            await self.wait_clients(0)
            self.assertGreaterEqual(hub.dropped, 1)
        finally:
            sock.close()


# ---- endpoints -----------------------------------------------------------------------------------
class EndpointTests(Harness):
    async def test_healthz(self) -> None:
        self.assertEqual((await self.get("/healthz"))[:2], (200, {"ok": True}))

    async def test_requests(self) -> None:
        status, data, _ = await self.get("/api/requests?limit=5")
        self.assertEqual(status, 200)
        self.assertEqual(len(data), 5)
        self.assertEqual(set(data[0]), {"ts", "path", "alias", "model", "backend", "status", "duration_ms",
                                        "eval_count", "eval_tok_s", "error"})  # no prompt
        # null alias (a plain model name) passes through as null, not "None" or ""
        self.assertEqual(data[0], {"ts": 2000.0, "path": "/api/embed", "alias": None, "model": "nomic-embed-text",
                                   "backend": "cpu", "status": 200, "duration_ms": 40, "eval_count": None,
                                   "eval_tok_s": None, "error": None})
        self.assertEqual(data[1]["alias"], "assistant")
        self.assertEqual(len((await self.get("/api/requests"))[1]), 50)
        self.assertEqual(len((await self.get("/api/requests?limit=200"))[1]), 200)
        for mode in ("wrapped", "nondict"):
            self.broker.modes["/broker/recent"] = mode
            status, data, _ = await self.get("/api/requests")
            self.assertEqual((status, data), (502, {"error": "broker recent reply doesn't match its pinned shape"}), mode)
        self.broker.modes["/broker/recent"] = "error"
        status, data, _ = await self.get("/api/requests")
        self.assertEqual((status, data), (502, {"error": "broker: HTTP 500"}))

    async def test_logs_aibox(self) -> None:
        status, data, _ = await self.get("/api/logs?source=ollama-cpu&lines=3")
        self.assertEqual(status, 200)
        self.assertEqual(data[0], "synthetic ollama-cpu line 0")
        self.assertEqual(self.helper.requests[-1], {"op": "logs", "source": "ollama-cpu", "lines": 3})
        self.assertEqual((await self.get("/api/logs?source=homelab-ai-stack"))[0], 200)
        self.assertEqual(self.helper.requests[-1], {"op": "logs", "source": "homelab-ai-stack", "lines": 200})
        for gone in ("homelab-ai-stack-start", "homelab-ai-stack-x"):
            self.assertEqual((await self.get(f"/api/logs?source={gone}"))[0], 400, gone)

    async def test_log_sources(self) -> None:
        status, data, headers = await self.get("/api/logs/sources")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Cache-Control"], "no-store")
        ids = [d["id"] for d in data]
        self.assertEqual(ids, ["homelab-ai-stack", "ollama-cpu", "gpu-broker", "open-webui", "docker", "tailscaled",
                               "chrony", "ai-control", "aictl-helper", "pc:ollama", "pc:gpu-gate"])
        self.assertEqual(data[0], {"id": "homelab-ai-stack", "label": "ai-box: AI stack startup"})
        self.assertTrue(all(set(d) == {"id", "label"} and d["label"] for d in data))
        # every listed source is accepted by /api/logs
        for sid in ids:
            self.assertEqual((await self.get(f"/api/logs?source={sid}&lines=1"))[0], 200, sid)

    async def test_logs_pc(self) -> None:
        status, data, _ = await self.get("/api/logs?source=pc:gpu-gate&lines=10")
        self.assertEqual(status, 200)
        self.assertIn("synthetic line", data[0])
        path, query, _ = self.agent.hits[-1]
        self.assertEqual((path, query), ("/v1/logs", {"unit": "gpu-gate", "lines": "10"}))

    async def test_audit_sudo(self) -> None:
        status, data, _ = await self.get("/api/audit/sudo?user=claude&since=1700000000&limit=10")
        self.assertEqual(status, 200)
        self.assertEqual(self.helper.requests[-1], {"op": "sudo-commands", "user": "claude", "limit": 10, "since": 1700000000})
        self.assertEqual(data[0]["user"], "claude")
        self.assertEqual([r["event"] for r in data], ["command", "refused"])
        self.assertEqual([r["ambiguous"] for r in data], [False, True])
        status, data, _ = await self.get("/api/audit/sudo?host=pc&user=localai")
        self.assertEqual(status, 200)
        self.assertEqual(set(data[0]), {"ts", "user", "runas", "cwd", "command", "event", "ambiguous"})  # "extra" dropped
        self.assertEqual((data[0]["event"], data[0]["ambiguous"]), ("auth_fail", True))
        self.assertEqual(self.agent.hits[-1][1], {"user": "localai", "limit": "100"})

    async def test_audit_sessions_and_replay(self) -> None:
        status, body, _ = await self.get("/api/audit/sessions?user=claude")
        self.assertEqual((status, set(body), body["skipped"]), (200, {"sessions", "skipped"}, 1))
        data = body["sessions"]
        self.assertEqual(data[0]["id"], "00/00/01")
        self.assertEqual(set(data[0]), {"id", "ts", "runas", "tty", "cwd", "command"})
        self.assertIs(type(data[0]["ts"]), int)  # epoch seconds, as X reports them
        self.assertEqual(data[0]["ts"], 1793000000)
        self.assertEqual(self.helper.requests[-1], {"op": "sudo-sessions", "user": "claude", "limit": 200})
        status, data, _ = await self.get("/api/audit/replay?user=claude&id=00/00/01")
        self.assertEqual(status, 200)
        self.assertIsNone(data["output"])  # nulled even though this fake helper sent it
        self.assertEqual((data["id"], data["command"], data["output_truncated"]), ("00/00/01", "/usr/bin/id", False))
        self.assertEqual(self.helper.requests[-1], {"op": "sudo-replay", "user": "claude", "id": "00/00/01",
                                                    "max_bytes": 262144, "output": False})
        status, data, _ = await self.get("/api/audit/replay?user=localai&id=ABC123&output=1")
        self.assertEqual((status, data["output"]), (200, "uid=0(root)"))
        self.assertEqual(data["id"], "AB/C1/23")  # X's id (path form), not the client's
        self.assertEqual(self.helper.requests[-1]["id"], "ABC123")
        self.assertTrue(self.helper.requests[-1]["output"])
        self.assertIs(type(data["ts"]), int)
        status, data, _ = await self.get("/api/audit/replay?id=00%2F00%2F99&user=claude")  # encoded / works too
        self.assertEqual((status, data), (404, {"error": "helper: not-found: no such session"}))
        status, data, _ = await self.get("/api/audit/replay?user=claude&id=00/00/98")
        self.assertEqual((status, data), (422, {"error": "this session log is damaged"}))  # no helper text, no path

    async def test_audit_localai(self) -> None:
        status, data, _ = await self.get("/api/audit/localai?limit=5")
        self.assertEqual((status, data[0]["comm"]), (200, "python3"))
        self.assertEqual(self.helper.requests[-1], {"op": "localai-activity", "limit": 5})

    async def test_helper_refusal_and_outage(self) -> None:
        self.helper.mode = "refuse"
        status, data, _ = await self.get("/api/audit/localai")
        self.assertEqual((status, data), (400, {"error": "helper: bad-request: synthetic refusal"}))
        self.helper.mode = "slow"
        status, data, _ = await self.get("/api/audit/localai")
        self.assertEqual(status, 502)
        self.assertIn("timed out", data["error"])

    async def test_sudo_rows_require_boolean_ambiguous(self) -> None:
        orig_helper = self.helper.reply
        for bad in ("missing", "true", 1, None):
            with self.subTest(bad=bad):
                def reply(req: dict[str, Any], bad: Any = bad) -> Any:
                    out = orig_helper(req)
                    if req.get("op") == "sudo-commands":
                        row = out["data"]["commands"][0]
                        if bad == "missing":
                            del row["ambiguous"]
                        else:
                            row["ambiguous"] = bad
                    return out

                self.helper.reply = reply  # type: ignore[method-assign]
                status, data, _ = await self.get("/api/audit/sudo?user=claude")
                self.assertEqual((status, data), (502, {"error": "helper sudo-commands reply doesn't match its pinned shape"}))
        self.helper.reply = orig_helper  # type: ignore[method-assign]
        orig_entries = [dict(e) for e in fakes.AGENT_SUDO_ENTRIES]
        try:
            for bad in ("missing", "yes", 0):
                with self.subTest(pc=bad):
                    fakes.AGENT_SUDO_ENTRIES[:] = [dict(e) for e in orig_entries]
                    if bad == "missing":
                        del fakes.AGENT_SUDO_ENTRIES[0]["ambiguous"]
                    else:
                        fakes.AGENT_SUDO_ENTRIES[0]["ambiguous"] = bad
                    status, data, _ = await self.get("/api/audit/sudo?host=pc&user=claude")
                    self.assertEqual((status, data), (502, {"error": "host agent /v1/sudo reply doesn't match its pinned shape"}))
        finally:
            fakes.AGENT_SUDO_ENTRIES[:] = orig_entries

    async def test_sessions_skipped_must_be_int(self) -> None:
        orig = self.helper.reply

        def reply(req: dict[str, Any]) -> Any:
            out = orig(req)
            if req.get("op") == "sudo-sessions":
                del out["data"]["skipped"]
            return out

        self.helper.reply = reply  # type: ignore[method-assign]
        status, data, _ = await self.get("/api/audit/sessions?user=claude")
        self.assertEqual((status, data), (502, {"error": "helper sudo-sessions reply doesn't match its pinned shape"}))

    async def test_replay_truncated_flag_passes_through(self) -> None:
        orig = self.helper.reply

        def reply(req: dict[str, Any]) -> Any:
            out = orig(req)
            if req.get("op") == "sudo-replay":
                out["data"]["output_truncated"] = True  # e.g. a line over 8 KiB
            return out

        self.helper.reply = reply  # type: ignore[method-assign]
        status, data, _ = await self.get("/api/audit/replay?user=claude&id=00/00/01&output=1")
        self.assertEqual((status, data["output_truncated"]), (200, True))

    async def test_unpinned_agent_shapes_are_502(self) -> None:
        self.agent.modes["*"] = "unpinned"
        for path, ypath in (("/api/logs?source=pc:ollama", "/v1/logs"), ("/api/audit/sudo?host=pc&user=claude", "/v1/sudo")):
            status, data, _ = await self.get(path)
            self.assertEqual((status, data), (502, {"error": f"host agent {ypath} reply doesn't match its pinned shape"}), path)

    async def test_unpinned_helper_shapes_are_502(self) -> None:
        self.helper.mode = "unpinned"
        for path in ("/api/logs?source=docker", "/api/audit/sudo?user=claude", "/api/audit/sessions?user=claude",
                     "/api/audit/replay?user=claude&id=00/00/01", "/api/audit/localai"):
            status, data, _ = await self.get(path)
            self.assertEqual(status, 502, path)
            self.assertIn("pinned shape", data["error"], path)

    async def test_static_and_spa_fallback(self) -> None:
        status, body, headers = await self.get("/")
        self.assertEqual(status, 200)
        self.assertIn(b"AI Control", body)
        status, body, headers = await self.get("/js/app.js")
        self.assertEqual(status, 200)
        self.assertIn("javascript", headers["Content-Type"])
        for path in ("/overview", "/audit/sessions", "/.secret", "/js/missing.js"):
            status, body, _ = await self.get(path)
            self.assertEqual(status, 200, path)
            self.assertIn(b"AI Control", body, path)
        for path in ("/api/nope", "/api", "/api/"):
            status, data, _ = await self.get(path)
            self.assertEqual((status, data), (404, {"error": "no such endpoint"}), path)

    async def test_static_traversal(self) -> None:
        reader, writer = await asyncio.open_connection("127.0.0.1", bound_port(self.runner))
        for target in (b"/../outside.txt", b"/js/../../outside.txt", b"/%2e%2e/outside.txt", b"/js/%2E%2E/%2e%2e/outside.txt"):
            writer.write(b"GET " + target + b" HTTP/1.1\r\nHost: x\r\n\r\n")
            await writer.drain()
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
            length = int([h for h in head.split(b"\r\n") if h.lower().startswith(b"content-length")][0].split(b":")[1])
            body = await reader.readexactly(length)
            self.assertNotIn(b"outside", body, target)
        writer.close()


class RawSessionIdTests(Harness):
    async def test_old_replay_path_gone_and_raw_dot_ids_refused(self) -> None:
        reader, writer = await asyncio.open_connection("127.0.0.1", bound_port(self.runner))
        for target in (b"/api/audit/sessions/00/00/01?user=claude", b"/api/audit/sessions/a/../b?user=claude",
                       b"/api/audit/replay?user=claude&id=a/../b", b"/api/audit/replay?user=claude&id=../x",
                       b"/api/audit/replay?user=claude&id=a/%2e%2e/b", b"/api/audit/replay?user=claude&id=//etc"):
            writer.write(b"GET " + target + b" HTTP/1.1\r\nHost: x\r\n\r\n")
            await writer.drain()
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
            length = int([h for h in head.split(b"\r\n") if h.lower().startswith(b"content-length")][0].split(b":")[1])
            await reader.readexactly(length)
            self.assertRegex(head.split(b"\r\n")[0], rb" 40[04] ", target)
        writer.close()
        self.assertEqual([r for r in self.helper.requests if r["op"] == "sudo-replay"], [])


class BadParamTests(Harness):
    CASES = (
        "/api/summary?x=1",
        "/api/events?x=1",
        "/api/gate/history?minutes=29",
        "/api/gate/history?minutes=1441",
        "/api/gate/history?minutes=abc",
        "/api/gate/history?minutes=-30",
        "/api/gate/history?minutes=30&minutes=60",
        "/api/gate/history?minutes=+60",
        "/api/gate/history?minutes=60.0",
        "/api/gate/history?bogus=1",
        "/api/requests?limit=0",
        "/api/requests?limit=201",
        "/api/logs",
        "/api/logs?source=evil",
        "/api/logs?source=pc:sshd",
        "/api/logs?source=../../etc/passwd",
        "/api/logs?source=ollama-cpu&lines=0",
        "/api/logs?source=ollama-cpu&lines=1001",
        "/api/audit/sudo",
        "/api/audit/sudo?user=root",
        "/api/audit/sudo?user=claude&host=nas",
        "/api/audit/sudo?user=claude&since=abc",
        "/api/audit/sudo?user=claude&since=-5",
        "/api/audit/sudo?user=claude&since=99999999999",
        "/api/audit/sudo?user=claude&limit=501",
        "/api/audit/sessions",
        "/api/audit/sessions?user=admin",
        "/api/audit/replay?id=ABC123",
        "/api/audit/replay?user=claude",
        "/api/audit/replay?user=claude&id=",
        "/api/audit/replay?user=claude&id=ABC123&output=2",
        "/api/audit/replay?user=claude&id=ABC123&output=true",
        "/api/audit/replay?user=claude&id=ABC123&id=ABC124",
        "/api/audit/replay?user=claude&id=..%2F..%2Fetc",
        "/api/audit/replay?user=claude&id=%2Fetc%2Fpasswd",
        "/api/audit/replay?user=claude&id=00/00/",
        "/api/audit/replay?user=claude&id=" + "a" * 65,
        "/api/audit/replay?user=claude&id=a%20b",
        "/api/logs/sources?x=1",
        "/api/audit/localai?limit=0",
        "/api/audit/localai?since=now",
    )

    async def test_bad_params_are_400(self) -> None:
        before = len(self.helper.requests), len(self.agent.hits)
        for path in self.CASES:
            status, data, headers = await self.get(path)
            self.assertEqual(status, 400, (path, data))
            self.assertEqual(set(data), {"error"}, path)
            self.assertEqual(headers["Content-Type"].split(";")[0], "application/json")
        self.assertEqual((len(self.helper.requests), len(self.agent.hits)), before)  # nothing forwarded


# ---- secrets -------------------------------------------------------------------------------------
class TokenTests(Harness):
    async def test_token_never_in_logs_or_responses(self) -> None:
        buf = io.StringIO()
        handler = logging.StreamHandler(buf)
        handler.setLevel(logging.DEBUG)
        loggers = [logging.getLogger(n) for n in ("ai_control", "aiohttp", "asyncio")]
        old = [(lg.level, lg.propagate) for lg in loggers]
        for lg in loggers:
            lg.addHandler(handler)
            lg.setLevel(logging.DEBUG)
        bodies: list[bytes] = []
        try:
            for mode in ("ok", "error", "slow", "garbage", "badtoken", "down"):
                if mode == "badtoken":
                    self.agent.token = "another-synthetic-token"
                elif mode == "down":
                    await self.agent.close()
                else:
                    self.agent.modes["*"] = mode
                await self.state.sampler.sample_once(self.state.session)
                await self.state.builder.build()
                for path in ("/api/summary", "/api/logs?source=pc:ollama", "/api/audit/sudo?host=pc&user=claude"):
                    async with self.http.get(self.base + path) as resp:
                        bodies.append(await resp.read())
                        bodies.append(str(resp.headers).encode())
        finally:
            for lg, (level, prop) in zip(loggers, old):
                lg.removeHandler(handler)
                lg.setLevel(level)
        # The agent did receive it (so the test exercises the real header path)...
        self.assertTrue(any(h[2].get("Authorization") == f"Bearer {fakes.AGENT_TOKEN}" for h in self.agent.hits))
        # ...but it never appears in a response or a log line.
        token = fakes.AGENT_TOKEN.encode()
        for body in bodies:
            self.assertNotIn(token, body)
        self.assertNotIn(fakes.AGENT_TOKEN, buf.getvalue())
        self.assertIn("rejected the token", b"".join(bodies).decode())


# ---- config --------------------------------------------------------------------------------------
class ConfigTests(unittest.TestCase):
    def test_defaults(self) -> None:
        cfg = from_dict({})
        self.assertEqual((cfg.listen_host, cfg.listen_port), ("127.0.0.1", 8095))
        self.assertEqual(cfg.helper_socket, "/run/aictl-helper.sock")
        self.assertEqual(cfg.gate_samples_path, "/var/lib/ai-control/gate-samples.jsonl")
        self.assertEqual((cfg.sample_interval, cfg.persist_interval, cfg.history_hours), (10.0, 300.0, 24.0))
        self.assertEqual((cfg.sse_max_clients, cfg.sse_buffer_limit), (8, 1024 * 1024))
        self.assertLessEqual(cfg.source_timeout, 4.0)

    def test_rejects(self) -> None:
        for raw in (
            {"nope": 1},
            {"source_timeout": 5},
            {"request_timeout": 11},
            {"gate_url": "ftp://x"},
            {"helper_socket": "relative.sock"},
            {"listen_port": 0},
            {"listen_port": True},
            {"log_sources": ["docker"]},
            {"log_sources": []},
            {"log_sources": [{"id": "Bad Name", "label": "x"}]},
            {"log_sources": [{"id": "pc:sshd", "label": "x"}]},
            {"log_sources": [{"id": "docker", "label": ""}]},
            {"log_sources": [{"id": "docker", "label": "x", "extra": 1}]},
            {"log_sources": [{"id": "docker", "label": "x"}, {"id": "docker", "label": "y"}]},
            {"gate_window": 10},
            {"timezone": "../etc"},
            {"disk_mounts": []},
            {"sse_max_clients": 0},
        ):
            with self.assertRaises(ConfigError, msg=raw):
                from_dict(raw)

    def test_example_config_is_valid(self) -> None:
        path = Path(__file__).resolve().parent.parent / "config.example.json"
        from_dict(json.loads(path.read_text()))


class ConfiguredSourcesTests(Harness):
    extra_config = {"log_sources": [{"id": "docker", "label": "ai-box: Docker"}, {"id": "pc:ollama", "label": "PC Ollama"}],
                    "gate_window": 900}

    async def test_sources_and_window_from_config(self) -> None:
        self.assertEqual((await self.get("/api/logs/sources"))[1],
                         [{"id": "docker", "label": "ai-box: Docker"}, {"id": "pc:ollama", "label": "PC Ollama"}])
        self.assertEqual((await self.get("/api/logs?source=docker"))[0], 200)
        self.assertEqual((await self.get("/api/logs?source=ollama-cpu"))[0], 400)  # not configured
        self.assertEqual((await self.get("/api/logs?source=pc:gpu-gate"))[0], 400)
        self.assertEqual((await self.summary())["routing"]["window_s"], 900)


class ChronyParseTests(unittest.TestCase):
    def test_parse(self) -> None:
        from ai_control.hostinfo import parse_chrony

        self.assertEqual(parse_chrony(fakes.CHRONY_OK), {"synced": True, "offset_ms": -0.25, "source": "192.0.2.1"})
        self.assertEqual(parse_chrony(fakes.CHRONY_UNSYNCED)["synced"], False)
        with self.assertRaises(ValueError):
            parse_chrony("garbage")


if __name__ == "__main__":
    unittest.main()
