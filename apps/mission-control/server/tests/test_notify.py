"""POST /api/notify (B2): token auth in place of the login guard (for that route and method only), allow_from,
the body rules, every status code, dedupe, the rolling-hour rate, quiet hours with the held queue and its summary,
the state file, no message text or token in the logs; the new config keys; and the Apps card (B1)."""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import hashlib
import io
import json
import os
import tempfile
import types
import unittest
import zoneinfo
from pathlib import Path
from typing import Any, ClassVar
from unittest import mock

import yarl
from multidict import CIMultiDict

from mission_control import app as app_mod
from mission_control import notify as notify_mod
from mission_control.__main__ import main
from mission_control.config import EMPTY_SHA256, ConfigError, from_dict
from mission_control.server import bound_port, start

from . import fakes
from .harness import Harness, LogCapture

HERE = Path(__file__).resolve().parent.parent
TZ = zoneinfo.ZoneInfo("America/New_York")
TOKENS = {  # fixtures, not real secrets
    "mediaserver": "synthetic-notify-token-media-1f2e9a",
    "pc": "synthetic-notify-token-pc-9c8b77",
    "edge": "synthetic-notify-token-edge-77aa31",
}


def sha(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


SENDERS = [{"name": n, "token_sha256": sha(t)} for n, t in TOKENS.items()]
DEVICE = {"endpoint": "https://fcm.googleapis.com/fcm/send/notify-device", "keys": {"p256dh": "x", "auth": "y"},
          "label": "Pixel"}


def at(hour: int, minute: int = 0, day: int = 28) -> float:
    """Epoch seconds for a local (America/New_York) time on October `day`, 2026."""
    return dt.datetime(2026, 10, day, hour, minute, tzinfo=TZ).timestamp()


def msg(**kw: Any) -> dict[str, Any]:
    return {"title": "Backup failed", "body": "journalctl -u homelab-backup", "tag": "backup", **kw}


class NotifyBase(Harness):
    extra: ClassVar[dict[str, Any]] = {"notify_senders": SENDERS, "notify_rate_per_hour": 1000}

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.state.subs.add(DEVICE)

    async def notify(self, body: Any = None, *, token: str | None = TOKENS["mediaserver"], data: Any = None,
                     headers: dict[str, str] | CIMultiDict[str] | None = None, path: str = "/api/notify",
                     method: str = "POST", content_type: str | None = "application/json") -> tuple[int, Any, Any]:
        h: CIMultiDict[str] = CIMultiDict()
        if content_type is not None:
            h["Content-Type"] = content_type
        if token is not None:
            h["Authorization"] = f"Bearer {token}"
        for k, v in (headers or {}).items():
            h.add(k, v)
        payload = data if data is not None else json.dumps(msg() if body is None else body)
        url = yarl.URL(self.base + path, encoded=True)  # sent as written: no client-side path normalising
        async with self.http.request(method, url, data=payload, headers=h, allow_redirects=False) as r:
            raw = await r.read()
            try:
                out = json.loads(raw)
            except ValueError:
                out = raw.decode("utf-8", "replace")
            return r.status, out, r.headers

    def pushes(self, tag_prefix: str = "n.") -> list[dict[str, Any]]:
        return [p for _ep, p in self.sender.calls if p["tag"].startswith(tag_prefix)]

    def state_file(self) -> Path:
        return self.state_dir / "notify.json"


# ---- auth and the guard --------------------------------------------------------------------------------------
class AuthTests(NotifyBase):
    async def test_sent_with_the_house_payload(self) -> None:
        status, body, _ = await self.notify(msg(title="  Backup failed  "))
        self.assertEqual((status, body), (202, {"status": "sent"}))
        self.assertEqual(self.pushes(), [{"title": "mediaserver: Backup failed", "body": "journalctl -u homelab-backup",
                                          "tag": "n.mediaserver.backup", "url": "/"}])
        status, _, _ = await self.notify(msg(tag="disk", url="https://ai-box.example.ts.net:8443/#card-servers"),
                                         token=TOKENS["edge"])
        self.assertEqual(status, 202)
        self.assertEqual(self.pushes()[-1]["tag"], "n.edge.disk")
        self.assertEqual(self.pushes()[-1]["url"], "https://ai-box.example.ts.net:8443/#card-servers")

    async def test_every_auth_failure_is_the_same_401(self) -> None:
        answers = set()
        cases: list[dict[str, Any]] = [
            {"token": None},
            {"token": "wrong-token"},
            {"token": ""},
            {"token": None, "headers": {"Authorization": "Basic " + TOKENS["pc"]}},
            {"token": None, "headers": {"Authorization": TOKENS["pc"]}},
            {"token": None, "headers": {"Authorization": "Bearer  " + TOKENS["pc"]}},
            {"token": TOKENS["pc"] + " x"},
            {"token": TOKENS["pc"].upper()},
            {"token": TOKENS["pc"][:-1]},
            {"token": sha(TOKENS["pc"])},  # the hash itself is not the token
            {"token": TOKENS["pc"], "headers": {"Authorization": "Bearer " + TOKENS["pc"]}},  # two headers
            {"token": None, "headers": {"Tailscale-User-Login": fakes.LOGIN}},  # Noah's login is not a token
        ]
        for case in cases:
            status, body, headers = await self.notify(**case)
            self.assertEqual(status, 401, case)
            answers.add((json.dumps(body), headers.get("WWW-Authenticate"), headers.get("Content-Type")))
        self.assertEqual(answers, {('{"error": "unauthorized"}', "Bearer", "application/json; charset=utf-8")})
        self.assertEqual(self.sender.calls, [])
        self.assertFalse(self.state_file().exists(), "a refused request writes no state")

    async def test_scheme_is_case_insensitive(self) -> None:
        status, _, _ = await self.notify(token=None, headers={"Authorization": "bearer " + TOKENS["pc"]})
        self.assertEqual(status, 202)

    async def test_no_senders_configured_means_401_for_everyone(self) -> None:
        await self.restart(notify_senders=[])
        for token in (*TOKENS.values(), "", "x"):
            self.assertEqual((await self.notify(token=token))[0], 401, token)

    def test_every_sender_is_compared_with_no_early_exit(self) -> None:
        cfg = self.config()
        real = notify_mod.hmac.compare_digest
        for token, expect in ((TOKENS["mediaserver"], "mediaserver"), (TOKENS["edge"], "edge"), ("nope", None),
                              (None, None)):
            with mock.patch.object(notify_mod.hmac, "compare_digest", side_effect=real) as cmp:
                header = [] if token is None else [f"Bearer {token}"]
                found = notify_mod.match_sender(cfg.notify_senders, header)
            self.assertEqual(found.name if found else None, expect)
            self.assertEqual(cmp.call_count, 3, f"{token}: one compare per sender, hit or miss")
            self.assertTrue(all(len(c.args[0]) == 64 for c in cmp.call_args_list), "hashes are compared, not tokens")

    def test_empty_token_never_matches(self) -> None:
        sender = types.SimpleNamespace(name="x", token_sha256=EMPTY_SHA256, allow_from=())
        for header in ([], ["Bearer "], ["Bearer"], [""]):
            self.assertIsNone(notify_mod.match_sender((sender,), header), header)  # type: ignore[arg-type]

    async def test_no_login_needed_and_no_custom_header(self) -> None:
        status, _, _ = await self.notify(headers={"X-Mission-Control": "0"})
        self.assertEqual(status, 202)

    async def test_guard_still_applies_to_every_other_route(self) -> None:
        auth = {"Authorization": f"Bearer {TOKENS['pc']}"}
        for path in ("/api/cards", "/api/briefing", "/", "/sw.js", "/api/nope"):
            status, _ = await self.get(path, login=None, headers=auth)
            self.assertEqual(status, 403, path)
        for path in ("/api/push/subscribe", "/api/push/unsubscribe", "/api/push/test"):
            status, _ = await self.post(path, {"endpoint": DEVICE["endpoint"]}, login=None, headers=auth)
            self.assertEqual(status, 403, path)
        for path in ("/api/notify/", "/api//notify", "/API/notify", "/api/notify%2f", "/api/notify/x", "/api/notify;x",
                     "//api/notify", "/api/notify/."):
            status, body, _ = await self.notify(path=path)
            self.assertEqual((status, body), (403, {"error": "forbidden"}), path)
        # %6E is "n": a spelling of the same path. It reaches notify (token required) or the guard, never past both.
        self.assertIn((await self.notify(path="/api/%6Eotify", token=None))[0], (401, 403))
        self.assertEqual(self.pushes(), [])

    async def test_other_methods_are_405_behind_the_guard(self) -> None:
        for method in ("GET", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"):
            status, _, headers = await self.notify(method=method, headers={"Tailscale-User-Login": fakes.LOGIN})
            self.assertEqual(status, 405, method)
            self.assertEqual(headers["Allow"], "POST", method)
            status, _, _ = await self.notify(method=method)  # no login: the guard answers first
            self.assertEqual(status, 403, method)
        self.assertEqual(self.pushes(), [])

    async def test_cross_site_browser_request_refused(self) -> None:
        status, body, _ = await self.notify(headers={"Sec-Fetch-Site": "cross-site"})
        self.assertEqual((status, body), (403, {"error": "cross-site request refused"}))

    def test_token_route_needs_the_local_peer_and_the_notify_handler(self) -> None:
        def req(peer: str, method: str = "POST", path: str = "/api/notify", handler: Any = app_mod.notify) -> Any:
            return types.SimpleNamespace(method=method, path=path, match_info=types.SimpleNamespace(handler=handler),
                                         transport=types.SimpleNamespace(get_extra_info=lambda _k: (peer, 5)))

        self.assertTrue(app_mod._token_route(req("127.0.0.1")))
        self.assertFalse(app_mod._token_route(req("100.64.1.2")))
        self.assertFalse(app_mod._token_route(req("127.0.0.1", method="GET")))
        self.assertFalse(app_mod._token_route(req("127.0.0.1", path="/api/notify/")))
        self.assertFalse(app_mod._token_route(req("127.0.0.1", handler=app_mod.api_not_found)))


class AllowFromTests(NotifyBase):
    extra: ClassVar[dict[str, Any]] = {
        "notify_senders": [{**SENDERS[0], "allow_from": ["100.64.0.10", "fd7a:115c:a1e0::10"]}, SENDERS[1]],
        "notify_rate_per_hour": 1000,
    }

    async def test_last_hop_must_match(self) -> None:
        ok = ["100.64.0.10", "fd7a:115c:a1e0:0::10", "1.2.3.4, 100.64.0.10", "  100.64.0.10  ", "evil, 100.64.0.10"]
        for i, xff in enumerate(ok):
            status, _, _ = await self.notify(msg(tag=f"ok{i}"), headers={"X-Forwarded-For": xff})
            self.assertEqual(status, 202, xff)
        bad = ["100.64.0.11", "100.64.0.10, 1.2.3.4", "100.64.0.10,", "", "not-an-ip", "::ffff:100.64.0.10",
               "100.64.0.10:443", "[fd7a:115c:a1e0::10]"]
        for xff in bad:
            status, body, _ = await self.notify(headers={"X-Forwarded-For": xff})
            self.assertEqual((status, body), (403, {"error": "forbidden"}), repr(xff))
        status, _, _ = await self.notify()  # no X-Forwarded-For at all
        self.assertEqual(status, 403)
        two = CIMultiDict([("X-Forwarded-For", "100.64.0.10"), ("X-Forwarded-For", "100.64.0.99")])
        self.assertEqual((await self.notify(headers=two))[0], 403, "several headers: the last hop of the last one")
        two = CIMultiDict([("X-Forwarded-For", "100.64.0.99"), ("X-Forwarded-For", "100.64.0.10")])
        self.assertEqual((await self.notify(msg(tag="two"), headers=two))[0], 202)

    async def test_without_allow_from_any_address(self) -> None:
        for xff in (None, "203.0.113.9"):
            status, _, _ = await self.notify(msg(tag=f"pc{int(xff is None)}"), token=TOKENS["pc"],
                                             headers={"X-Forwarded-For": xff} if xff else None)
            self.assertEqual(status, 202, xff)

    async def test_403_comes_after_auth(self) -> None:
        status, _, _ = await self.notify(token="wrong", headers={"X-Forwarded-For": "1.2.3.4"})
        self.assertEqual(status, 401)


# ---- the body ------------------------------------------------------------------------------------------------
class BodyTests(NotifyBase):
    async def test_content_type(self) -> None:
        for ct in ("text/plain", "application/x-www-form-urlencoded", None, "application/jsonx"):
            status, body, _ = await self.notify(content_type=ct)
            self.assertEqual((status, body), (415, {"error": "JSON only"}), ct)
        status, _, _ = await self.notify(content_type="application/json; charset=utf-8")
        self.assertEqual(status, 202)

    async def test_size(self) -> None:
        base = json.dumps(msg())
        exact = base[:-1] + " " * (notify_mod.MAX_REQUEST - len(base)) + "}"
        self.assertEqual(len(exact.encode()), 4096)
        self.assertEqual((await self.notify(data=exact))[0], 202)
        status, body, _ = await self.notify(data=exact[:-1] + " }")
        self.assertEqual((status, body), (413, {"error": "body too large"}))

        async def chunked(n: int) -> Any:
            for _ in range(n):
                yield b" " * 1024
                await asyncio.sleep(0.01)
            yield json.dumps(msg(tag="chunked")).encode()

        self.assertEqual((await self.notify(data=chunked(5)))[0], 413, "no Content-Length: counted as it streams")

    async def test_body_split_across_packets_is_read_whole(self) -> None:
        async def slow() -> Any:
            raw = json.dumps(msg(tag="slow")).encode()
            for i in range(0, len(raw), 7):
                yield raw[i:i + 7]
                await asyncio.sleep(0.01)

        status, body, _ = await self.notify(data=slow())
        self.assertEqual((status, body), (202, {"status": "sent"}))

        async def slow_sub() -> Any:  # the same reader serves the push endpoints
            raw = json.dumps({"subscription": fakes.fake_subscription(7)}).encode()
            for i in range(0, len(raw), 50):
                yield raw[i:i + 50]
                await asyncio.sleep(0.01)

        h = {"Content-Type": "application/json", "X-Mission-Control": "1", "Tailscale-User-Login": fakes.LOGIN}
        async with self.http.post(self.base + "/api/push/subscribe", data=slow_sub(), headers=h) as r:
            self.assertEqual(r.status, 200)

    async def test_field_rules(self) -> None:
        bad: list[tuple[Any, str]] = [
            ({k: v for k, v in msg().items() if k != "title"}, "title"),
            (msg(title=""), "title"),
            (msg(title="   "), "title"),
            (msg(title="x" * 81), "title"),
            (msg(title="line one\nline two"), "title"),
            (msg(title="tab\there"), "title"),
            (msg(title="nul\x00"), "title"),
            (msg(title="sep\u2028x"), "title"),
            (msg(title="\ud800"), "title"),
            (msg(title=5), "title"),
            (msg(title=None), "title"),
            (msg(body="x" * 301), "body"),
            (msg(body=None), "body"),
            (msg(body=["x"]), "body"),
            ({k: v for k, v in msg().items() if k != "tag"}, "tag"),
            (msg(tag=""), "tag"),
            (msg(tag="Backup"), "tag"),
            (msg(tag="-backup"), "tag"),
            (msg(tag=".x"), "tag"),
            (msg(tag="backup\n"), "tag"),
            (msg(tag="a b"), "tag"),
            (msg(tag="a/b"), "tag"),
            (msg(tag="x" * 65), "tag"),
            (msg(tag=7), "tag"),
            (msg(url=None), "url"),
            (msg(url=""), "url"),
            (msg(url="//evil.example.com/x"), "url"),
            (msg(url="/a//b"), "url"),
            (msg(url="/\\evil.example.com"), "url"),
            (msg(url="/a b"), "url"),
            (msg(url="/café"), "url"),
            (msg(url="card-alerts"), "url"),
            (msg(url="http://ai-box.x.ts.net/"), "url"),
            (msg(url="HTTPS://ai-box.x.ts.net/"), "url"),
            (msg(url="https://ai-box.x.ts.net.evil.com/"), "url"),
            (msg(url="https://evil.com/?.ts.net"), "url"),
            (msg(url="https://evil.com#.ts.net"), "url"),
            (msg(url="https://evil.com\\.ts.net/"), "url"),
            (msg(url="https://user:pw@ai-box.x.ts.net/"), "url"),
            (msg(url="https://user@ai-box.x.ts.net/"), "url"),
            (msg(url="https://ts.net/"), "url"),
            (msg(url="https://ai-box.x.ts.net:99999/"), "url"),
            (msg(url="https://ai-box.x.ts.net//x"), "url"),
            (msg(url="javascript:alert(1)"), "url"),
            (msg(url="/" + "a" * 512), "url"),
            (msg(url=["/"]), "url"),
            (msg(urgent="true"), "urgent"),
            (msg(urgent=1), "urgent"),
            (msg(urgent=None), "urgent"),
            (msg(extra=1), "unknown field"),
            (msg(card="alerts"), "unknown field"),
        ]
        for body, field in bad:
            status, out, _ = await self.notify(body)
            self.assertEqual((status, out), (400, {"error": field}), body)
        for raw, err in ((b"[1, 2]", "body must be a JSON object"), (b'"x"', "body must be a JSON object"),
                         (b"{", "invalid JSON"), (b'{"title": NaN}', "invalid JSON"), (b"\xff\xfe", "invalid JSON")):
            status, out, _ = await self.notify(data=raw)
            self.assertEqual((status, out), (400, {"error": err}), raw)
        status, out, _ = await self.notify(path="/api/notify?x=1")
        self.assertEqual((status, out), (400, {"error": "no parameters accepted"}))
        self.assertEqual(self.pushes(), [])

    async def test_accepted_edges(self) -> None:
        good = [
            msg(title="x", tag="a"),
            msg(title="x" * 80, tag="x" * 64),
            msg(title="  padded  ", tag="b"),
            msg(title="Disk 95% \U0001F525 \u2764\ufe0f", tag="c"),
            msg(tag="d", body=""),
            msg(tag="e", body="x" * 300),
            msg(tag="f", body="two\nlines"),
            msg(tag="g", url="/"),
            msg(tag="h", url="/#card-servers"),
            msg(tag="i", url="https://ai-box.example.ts.net"),
            msg(tag="j", url="https://ai-box.example.ts.net:8443/x?y=1#z"),
            msg(tag="k", url="https://AI-BOX.EXAMPLE.TS.NET/"),
            msg(tag="l.m_n-0", urgent=True),
            msg(tag="o", urgent=False),
            {"title": "only the required ones", "tag": "p"},
        ]
        for body in good:
            status, out, _ = await self.notify(body)
            self.assertEqual((status, out), (202, {"status": "sent"}), body)
        last = self.pushes()[-1]
        self.assertEqual((last["body"], last["url"]), ("", "/"))

    async def test_503_when_push_is_not_set_up(self) -> None:
        self.state.sender = None
        status, out, _ = await self.notify()
        self.assertEqual((status, out), (503, {"error": "push is not set up on the server"}))
        self.assertFalse(self.state_file().exists(), "nothing counted, nothing held")
        self.assertEqual((await self.notify(msg(title="")))[0], 400, "the body is checked first")
        self.assertEqual((await self.notify(token="nope"))[0], 401, "auth before anything else")

    async def test_unhandled_error_is_a_500_without_details(self) -> None:
        with mock.patch.object(self.state.notify_store, "save", side_effect=OSError("disk full")), \
                LogCapture() as logs:
            status, out, _ = await self.notify(msg(title="secret-title-zz"))
        self.assertEqual((status, out), (500, {"error": "internal error"}))
        self.assertEqual(self.pushes(), [], "the state couldn't be written, so nothing was sent")
        self.assertNotIn("secret-title-zz", logs.text)


# ---- dedupe and rate -----------------------------------------------------------------------------------------
class DedupeTests(NotifyBase):
    async def test_window(self) -> None:
        self.assertEqual((await self.notify())[1], {"status": "sent"})
        self.clock.now += 1799
        self.assertEqual((await self.notify())[:2], (202, {"status": "deduped"}))
        self.assertEqual(len(self.pushes()), 1)
        self.clock.now += 1  # 1800 s after the first: a new message
        self.assertEqual((await self.notify())[1], {"status": "sent"})
        self.assertEqual(len(self.pushes()), 2)

    async def test_the_window_runs_from_the_accepted_copy(self) -> None:
        await self.notify()
        for _ in range(2):  # repeats every 10 min are deduped, and don't stretch the window
            self.clock.now += 600
            self.assertEqual((await self.notify())[1], {"status": "deduped"})
        self.clock.now += 600  # 30 min after the accepted copy (10 min after the last repeat)
        self.assertEqual((await self.notify())[1], {"status": "sent"})

    async def test_key_is_sender_tag_title_body(self) -> None:
        await self.notify()
        variants = [{"token": TOKENS["pc"]}, {"body": msg(tag="other")}, {"body": msg(title="Backup failed again")},
                    {"body": msg(body="different")}]
        for v in variants:
            self.assertEqual((await self.notify(**v))[1], {"status": "sent"}, v)
        self.assertEqual((await self.notify(msg(url="/#card-servers", urgent=True)))[1], {"status": "deduped"},
                         "url and urgent are not part of the key")

    async def test_zero_turns_it_off(self) -> None:
        await self.restart(notify_dedupe_seconds=0)
        for _ in range(3):
            self.assertEqual((await self.notify())[1], {"status": "sent"})

    async def test_undelivered_push_does_not_block_the_retry(self) -> None:
        self.sender.status[DEVICE["endpoint"]] = 503
        self.assertEqual((await self.notify())[1], {"status": "sent"})
        self.sender.status[DEVICE["endpoint"]] = 201
        self.clock.now += 60
        self.assertEqual((await self.notify())[1], {"status": "sent"}, "the failed copy left no dedupe entry")
        self.clock.now += 60
        self.assertEqual((await self.notify())[1], {"status": "deduped"})

    async def test_no_devices_still_dedupes(self) -> None:
        self.state.subs.remove(DEVICE["endpoint"])
        self.assertEqual((await self.notify())[1], {"status": "sent"})
        self.assertEqual((await self.notify())[1], {"status": "deduped"})

    async def test_state_holds_a_hash_never_the_text(self) -> None:
        await self.notify(msg(title="Title-At-Rest-q1", body="Body-At-Rest-q2", url="/Url-At-Rest-q3"))
        raw = self.state_file().read_text()
        for s in ("Title-At-Rest-q1", "Body-At-Rest-q2", "Url-At-Rest-q3", TOKENS["mediaserver"]):
            self.assertNotIn(s, raw)
        self.assertEqual(os.stat(self.state_file()).st_mode & 0o777, 0o600)


class RateTests(NotifyBase):
    extra: ClassVar[dict[str, Any]] = {"notify_senders": SENDERS, "notify_rate_per_hour": 3}

    async def test_rolling_hour_and_retry_after(self) -> None:
        for i in range(3):
            self.assertEqual((await self.notify(msg(tag=f"t{i}")))[1], {"status": "sent"})
            self.clock.now += 10
        status, out, headers = await self.notify(msg(tag="t3"))
        self.assertEqual((status, out), (429, {"error": "rate limited"}))
        self.assertEqual(headers["Retry-After"], str(3600 - 30))
        self.assertEqual(len(self.pushes()), 3)
        self.assertEqual((await self.notify(token=TOKENS["pc"]))[0], 202, "per sender")
        self.clock.now = fakes.NOW + 3599
        status, _, headers = await self.notify(msg(tag="t3"))
        self.assertEqual((status, headers["Retry-After"]), (429, "1"))
        self.clock.now = fakes.NOW + 3600  # the first accept leaves the hour
        self.assertEqual((await self.notify(msg(tag="t3")))[1], {"status": "sent"})
        self.assertEqual((await self.notify(msg(tag="t4")))[0], 429)

    async def test_deduped_dont_count_but_held_do(self) -> None:
        await self.notify()
        for _ in range(5):
            self.assertEqual((await self.notify())[1], {"status": "deduped"})
        await self.restart(quiet_hours={"start": "05:00", "end": "06:00"})  # NOW is 05:30 local
        self.assertEqual((await self.notify(msg(tag="h1")))[1], {"status": "held"})
        self.assertEqual((await self.notify(msg(tag="h2")))[1], {"status": "held"})
        self.assertEqual((await self.notify(msg(tag="h3")))[0], 429)
        self.assertEqual((await self.notify(msg(tag="u", urgent=True)))[0], 429, "urgent skips quiet hours only")

    def test_retry_after_bounds(self) -> None:
        self.assertEqual(notify_mod.retry_after([100.0], 100.0), 3600)
        self.assertEqual(notify_mod.retry_after([100.0, 50.0], 3649.5), 1)
        self.assertEqual(notify_mod.retry_after([100.0], 99999.0), 1)
        self.assertEqual(notify_mod.retry_after([10000.0], 100.0), 3600, "a clock that went back")


# ---- quiet hours ---------------------------------------------------------------------------------------------
class QuietWindowTests(unittest.TestCase):
    def test_crossing_midnight(self) -> None:
        w = (22 * 60, 7 * 60)
        inside = [at(22), at(22, 1), at(23, 59), at(0, 0, 29), at(3, 0, 29), at(6, 59, 29)]
        outside = [at(21, 59), at(7, 0, 29), at(12), at(7, 1, 29)]
        for t in inside:
            self.assertTrue(notify_mod.in_quiet_hours(w, t, "America/New_York"), dt.datetime.fromtimestamp(t, TZ))
        for t in outside:
            self.assertFalse(notify_mod.in_quiet_hours(w, t, "America/New_York"), dt.datetime.fromtimestamp(t, TZ))

    def test_same_day_window_off_and_zone(self) -> None:
        w = (13 * 60, 14 * 60 + 30)
        self.assertTrue(notify_mod.in_quiet_hours(w, at(13), "America/New_York"))
        self.assertTrue(notify_mod.in_quiet_hours(w, at(14, 29), "America/New_York"))
        self.assertFalse(notify_mod.in_quiet_hours(w, at(14, 30), "America/New_York"))
        self.assertFalse(notify_mod.in_quiet_hours(w, at(12, 59), "America/New_York"))
        self.assertFalse(notify_mod.in_quiet_hours(None, at(13), "America/New_York"))
        self.assertTrue(notify_mod.in_quiet_hours(w, at(13), "America/New_York"))
        self.assertFalse(notify_mod.in_quiet_hours(w, at(13), "UTC"), "the configured time zone decides")

    def test_summary(self) -> None:
        held = [{"sender": f"s{i}", "tag": "t", "title": f"T{i}", "at": 1.0} for i in range(3)]
        self.assertEqual(notify_mod.summary_push(held, 0), {"title": "Held overnight: 3", "body": "s0: T0\ns1: T1\ns2: T2",
                                                            "tag": "n.held", "url": "/"})
        held = [{"sender": "s", "tag": "t", "title": f"T{i}", "at": 1.0} for i in range(6)]
        self.assertEqual(notify_mod.summary_push(held, 0)["body"], "s: T0\ns: T1\ns: T2\ns: T3\ns: T4\n…and 1 more")
        self.assertEqual(notify_mod.summary_push(held[:5], 0)["body"].count("\n"), 4, "exactly 5: no 'more' line")
        out = notify_mod.summary_push(held, 4)
        self.assertEqual((out["title"], out["body"].splitlines()[-1]), ("Held overnight: 10", "…and 5 more"))


class QuietHoursTests(NotifyBase):
    extra: ClassVar[dict[str, Any]] = {"notify_senders": SENDERS, "notify_rate_per_hour": 1000,
                                       "quiet_hours": {"start": "22:00", "end": "07:00"}}

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.clock.now = at(23, 30)

    async def test_held_then_one_summary_at_the_end(self) -> None:
        self.assertEqual((await self.notify(msg(title="First")))[1], {"status": "held"})
        self.clock.now = at(2, 0, 29)
        self.assertEqual((await self.notify(msg(title="Second", tag="disk"), token=TOKENS["pc"]))[1], {"status": "held"})
        self.assertEqual(self.pushes(), [], "nothing pushed during quiet hours")
        self.assertFalse(await self.state.notify_flush(), "still inside the window")
        self.clock.now = at(6, 59, 29)
        self.assertFalse(await self.state.notify_flush())
        self.clock.now = at(7, 0, 29)
        self.assertTrue(await self.state.notify_flush())
        self.assertEqual(self.pushes(), [{"title": "Held overnight: 2", "body": "mediaserver: First\npc: Second",
                                          "tag": "n.held", "url": "/"}])
        for _ in range(3):
            self.clock.now += 60
            self.assertFalse(await self.state.notify_flush())
        self.assertEqual(len(self.pushes()), 1, "one summary, once")
        self.assertEqual((await self.notify(msg(title="Daytime")))[1], {"status": "sent"})

    async def test_urgent_is_sent_at_once(self) -> None:
        self.assertEqual((await self.notify(msg(urgent=True)))[1], {"status": "sent"})
        self.assertEqual(self.pushes()[0]["tag"], "n.mediaserver.backup")
        self.assertEqual((await self.notify(msg(urgent=True)))[1], {"status": "deduped"}, "dedupe still applies")

    async def test_dedupe_applies_inside_the_window(self) -> None:
        self.assertEqual((await self.notify())[1], {"status": "held"})
        self.assertEqual((await self.notify())[1], {"status": "deduped"})
        self.assertEqual(len(self.state.notify_store.load()["held"]), 1)

    async def test_cap_drops_the_oldest_and_counts_it(self) -> None:
        with LogCapture() as logs:
            for i in range(53):
                self.assertEqual((await self.notify(msg(title=f"Msg {i:02d}", tag=f"t{i}")))[1], {"status": "held"})
        saved = self.state.notify_store.load()
        self.assertEqual(len(saved["held"]), 50)
        self.assertEqual(saved["dropped"], 3)
        self.assertEqual(saved["held"][0]["title"], "Msg 03")
        self.assertIn("held queue full (50); dropped the oldest (mediaserver t2)", logs.text)
        self.clock.now = at(8, 0, 29)
        self.assertTrue(await self.state.notify_flush())
        [summary] = self.pushes()
        self.assertEqual(summary["title"], "Held overnight: 53")
        self.assertEqual(summary["body"], "\n".join([f"mediaserver: Msg {i:02d}" for i in range(3, 8)] + ["…and 48 more"]))
        self.assertEqual(self.state.notify_store.load()["dropped"], 0)

    async def test_summary_kept_while_undelivered_or_push_disabled(self) -> None:
        await self.notify()
        self.clock.now = at(7, 30, 29)
        self.sender.status[DEVICE["endpoint"]] = 500
        self.assertFalse(await self.state.notify_flush())
        self.assertEqual(len(self.state.notify_store.load()["held"]), 1)
        self.clock.now += 60  # past the first back-off (test_review_b2 has the whole curve)
        sender, self.state.sender = self.state.sender, None
        self.assertFalse(await self.state.notify_flush())
        self.state.sender = sender
        self.sender.status[DEVICE["endpoint"]] = 201
        self.assertFalse(await self.state.notify_flush(), "inside the back-off: not tried")
        self.clock.now += 120
        self.assertTrue(await self.state.notify_flush())
        self.assertEqual(self.state.notify_store.load()["held"], [])
        self.assertEqual([p["tag"] for p in self.pushes()], ["n.held", "n.held"])

    async def test_no_devices_empties_the_queue(self) -> None:
        await self.notify()
        self.state.subs.remove(DEVICE["endpoint"])
        self.clock.now = at(7, 30, 29)
        self.assertTrue(await self.state.notify_flush())
        self.assertEqual(self.state.notify_store.load()["held"], [])

    async def test_turning_quiet_hours_off_flushes(self) -> None:
        await self.notify()
        await self.restart(quiet_hours=None)
        self.assertTrue(await self.state.notify_flush())
        self.assertEqual(self.pushes()[0]["title"], "Held overnight: 1")

    async def test_held_queue_and_counters_survive_a_restart(self) -> None:
        await self.notify(msg(title="Before restart"))
        await self.restart()
        self.assertEqual((await self.notify(msg(title="Before restart")))[1], {"status": "deduped"})
        self.clock.now = at(7, 5, 29)
        self.assertTrue(await self.state.notify_flush())
        self.assertEqual(self.pushes()[0]["body"], "mediaserver: Before restart")

    async def test_the_background_loop_sends_the_summary(self) -> None:
        await self.notify(msg(title="Overnight"))
        with mock.patch.object(app_mod, "NOTIFY_TICK_S", 0.02):
            runner = await start(self.cfg, port=0, background=True, sender=self.sender, clock=self.clock)
            try:
                await asyncio.sleep(0.1)
                self.assertEqual(self.pushes("n.held"), [], "inside the window: the loop holds")
                self.clock.now = at(7, 1, 29)
                for _ in range(200):
                    if self.pushes("n.held"):
                        break
                    await asyncio.sleep(0.02)
                await asyncio.sleep(0.1)  # more ticks: still one summary
                self.assertEqual([p["title"] for p in self.pushes("n.held")], ["Held overnight: 1"])
                names = {t.get_name() for t in runner.app[app_mod.STATE].tasks}
                self.assertEqual(names, {"alerts", "notify"})
                self.assertTrue(bound_port(runner))
            finally:
                await runner.cleanup()
        self.assertEqual(runner.app[app_mod.STATE].tasks, [], "cleanup cancels the loop")


# ---- the state file ------------------------------------------------------------------------------------------
class StateFileTests(NotifyBase):
    async def test_corrupt_or_poisoned_state_is_fresh_state(self) -> None:
        key = "a" * 64
        poison = {
            "not json": "{not json",
            "a list": "[1, 2]",
            "deep": '{"dedupe": ' * 100000,
            "a 5000-digit int": '{"dropped": ' + "9" * 5000 + "}",
            "NaN times": json.dumps({"dedupe": {key: float("nan")}, "rate": {"mediaserver": [float("nan")] * 5}}),
            "huge times": '{"dedupe": {"' + key + '": 1e999}, "rate": {"mediaserver": [' + "9" * 400 + "]}}",
            "far past": json.dumps({"dedupe": {key: 1000}, "rate": {"mediaserver": [1000] * 2000}}),
            "bools": json.dumps({"dedupe": {key: True}, "rate": {"mediaserver": [True]}, "dropped": True}),
            "bad keys": json.dumps({"dedupe": {"x": fakes.NOW}, "rate": {"BAD NAME": [fakes.NOW]}}),
            "bad held": json.dumps({"held": [{"sender": "x", "tag": "t", "title": "a\nb", "at": fakes.NOW},
                                             {"sender": "X", "tag": "t", "title": "a", "at": fakes.NOW},
                                             {"sender": "x", "tag": "-t", "title": "a", "at": fakes.NOW},
                                             {"sender": "x", "tag": "t", "title": "", "at": fakes.NOW},
                                             {"sender": "x", "tag": "t", "title": "a", "at": "now"},
                                             {"sender": "x", "tag": "t", "title": "a", "at": fakes.NOW, "body": "x"},
                                             "x"], "dropped": 7}),
        }
        with LogCapture() as logs:
            for name, raw in poison.items():
                self.state_file().write_text(raw)
                loaded = self.state.notify_store.load(fakes.NOW)
                self.assertEqual(loaded, notify_mod.fresh(), name)
                self.assertEqual((await self.notify(msg(tag=f"p{len(self.pushes())}")))[1], {"status": "sent"}, name)
        self.assertIn("notify state unreadable (ValueError); starting empty", logs.text)
        self.assertIn("notify state unreadable (RecursionError); starting empty", logs.text)

    def test_good_entries_kept_bad_dropped_and_bounded(self) -> None:
        now = fakes.NOW
        good_held = {"sender": "pc", "tag": "t", "title": "ok", "at": now}
        raw = {"dedupe": {"b" * 64: now, "c" * 64: float("nan")},
               "rate": {"pc": [now, now - 10, -5], "edge": "x"},
               "held": [good_held, {**good_held, "title": " padded "}] + [good_held] * 60, "dropped": 4}
        out = notify_mod._valid_state(raw, now)
        self.assertEqual(out["dedupe"], {"b" * 64: now})
        self.assertEqual(out["rate"], {"pc": [now - 10, now]})
        self.assertEqual(len(out["held"]), 50)
        self.assertEqual(out["dropped"], 4)
        many = {"dedupe": {f"{i:064x}": now - i for i in range(notify_mod.MAX_DEDUPE + 50)}}
        kept = notify_mod._valid_state(many, now)["dedupe"]
        self.assertEqual(len(kept), notify_mod.MAX_DEDUPE)
        self.assertIn(f"{0:064x}", kept, "the newest are kept")
        self.assertEqual(notify_mod._valid_state({"held": [], "dropped": 9}, now)["dropped"], 0)

    async def test_round_trip(self) -> None:
        await self.restart(quiet_hours={"start": "05:00", "end": "06:00"})
        await self.notify()
        saved = json.loads(self.state_file().read_text())
        self.assertEqual(self.state.notify_store.load(fakes.NOW), saved)
        self.assertEqual(saved["held"], [{"sender": "mediaserver", "tag": "backup", "title": "Backup failed",
                                          "at": fakes.NOW}])
        self.assertEqual(saved["rate"], {"mediaserver": [fakes.NOW]})


# ---- logging -------------------------------------------------------------------------------------------------
class NotifyLogTests(NotifyBase):
    extra: ClassVar[dict[str, Any]] = {
        "notify_senders": [SENDERS[0], {**SENDERS[1], "allow_from": ["100.64.0.10"]}, SENDERS[2]],
        "notify_rate_per_hour": 2, "quiet_hours": {"start": "05:00", "end": "06:00"},
    }

    async def test_logs_name_sender_tag_and_outcome_only(self) -> None:
        secret = {"title": "Secret-Title-7f3a", "body": "Secret-Body-91c2", "tag": "disk-full",
                  "url": "/Secret-Url-55e0"}
        with LogCapture() as logs:
            await self.notify(secret)  # held
            await self.notify(secret)  # deduped
            await self.notify({**secret, "title": "Secret-Title-2", "urgent": True})  # sent
            await self.notify({**secret, "title": "Secret-Title-3"})  # 429
            await self.notify(secret, token="Wrong-Token-Secret-0d0d")
            await self.notify(token=None, headers={"Authorization": "Basic Secret-Basic-4e4e"})
            await self.notify(secret, token=TOKENS["pc"])  # 403
            await self.notify({**secret, "tag": "Secret-Bad-Tag"}, token=TOKENS["edge"])
            await self.notify({**secret, "url": "javascript:Secret-Bad-Url"}, token=TOKENS["edge"])
            await self.notify({**secret, "Secret-Key-9": 1}, token=TOKENS["edge"])
            await self.notify(data=b"{Secret-Bad-Json", token=TOKENS["edge"])
            await self.notify(content_type="text/plain", data=b"Secret-Plain", token=TOKENS["edge"])
            self.sender.raise_for.add(DEVICE["endpoint"])
            await self.notify({**secret, "title": "Secret-Title-4", "urgent": True}, token=TOKENS["edge"])
            self.clock.now += 3600
            await self.state.notify_flush()
        text = logs.text
        for outcome in ("mediaserver disk-full: held", "mediaserver disk-full: deduped", "mediaserver disk-full: sent",
                        "mediaserver disk-full: rate limited", "refused: unauthorized", "pc refused: not from an allowed",
                        "edge refused: bad tag", "edge refused: bad url", "edge refused: bad unknown field",
                        "edge refused: invalid JSON", "edge refused: not JSON", "held summary of 1 message"):
            self.assertIn(outcome, text)
        leaks = [*secret.values(), "Secret", *TOKENS.values(), *(sha(t) for t in TOKENS.values())]
        leaks.remove("disk-full")
        for s in leaks:
            self.assertNotIn(s, text)


# ---- config --------------------------------------------------------------------------------------------------
class NotifyConfigTests(unittest.TestCase):
    BASE: ClassVar[dict[str, str]] = {"allowed_login": fakes.LOGIN}
    S: ClassVar[dict[str, str]] = {"name": "mediaserver", "token_sha256": "a" * 64}

    def test_defaults(self) -> None:
        cfg = from_dict(self.BASE)
        self.assertEqual((cfg.notify_senders, cfg.quiet_hours, cfg.notify_dedupe_seconds, cfg.notify_rate_per_hour,
                          cfg.app_links), ((), None, 1800, 30, ()))

    def test_accepted(self) -> None:
        cfg = from_dict({**self.BASE, "notify_senders": [
            {**self.S, "allow_from": ["100.64.0.1", "FD7A:115C::1"]},
            {"name": "a" * 32, "token_sha256": "b" * 64, "allow_from": []},
            {"name": "0-9", "token_sha256": "0123456789abcdef" * 4},
        ], "quiet_hours": {"start": "23:59", "end": "00:00"}, "notify_dedupe_seconds": 0, "notify_rate_per_hour": 1000,
            "app_links": [{"label": "x" * 32, "url": "http://100.64.0.1:8096/web/index.html?x=1"}] * 1
            + [{"label": f"App {i}", "url": f"https://a{i}.ts.net/"} for i in range(11)]})
        self.assertEqual(cfg.notify_senders[0].allow_from, ("100.64.0.1", "fd7a:115c::1"))
        self.assertEqual(cfg.quiet_hours, (23 * 60 + 59, 0))
        self.assertEqual(len(cfg.app_links), 12)
        self.assertEqual(from_dict({**self.BASE, "notify_dedupe_seconds": 86400, "notify_rate_per_hour": 1,
                                    "quiet_hours": {"start": "00:00", "end": "23:59"}}).notify_rate_per_hour, 1)

    def test_refusals(self) -> None:
        s = self.S
        bad = [
            {"notify_senders": {}},
            {"notify_senders": "mediaserver"},
            {"notify_senders": [s, "x"]},
            {"notify_senders": [{**s, "token": "x"}]},
            {"notify_senders": [{"token_sha256": "a" * 64}]},
            {"notify_senders": [{**s, "name": "Media"}]},
            {"notify_senders": [{**s, "name": "media_server"}]},
            {"notify_senders": [{**s, "name": "a b"}]},
            {"notify_senders": [{**s, "name": ""}]},
            {"notify_senders": [{**s, "name": "a" * 33}]},
            {"notify_senders": [{**s, "name": "x\n"}]},
            {"notify_senders": [{**s, "name": 5}]},
            {"notify_senders": [s, {**s, "token_sha256": "b" * 64}]},  # name twice
            {"notify_senders": [{**s, "token_sha256": "A" * 64}]},
            {"notify_senders": [{**s, "token_sha256": "a" * 63}]},
            {"notify_senders": [{**s, "token_sha256": "a" * 65}]},
            {"notify_senders": [{**s, "token_sha256": "g" * 64}]},
            {"notify_senders": [{**s, "token_sha256": "a" * 64 + "\n"}]},
            {"notify_senders": [{**s, "token_sha256": EMPTY_SHA256}]},
            {"notify_senders": [{"name": "plain"}]},
            {"notify_senders": [s, {**s, "name": "other"}]},  # hash twice
            {"notify_senders": [{**s, "allow_from": "100.64.0.1"}]},
            {"notify_senders": [{**s, "allow_from": ["100.64.0.0/10"]}]},
            {"notify_senders": [{**s, "allow_from": ["ai-box"]}]},
            {"notify_senders": [{**s, "allow_from": [""]}]},
            {"notify_senders": [{**s, "allow_from": [1]}]},
            {"notify_senders": [{**s, "allow_from": ["fd7a::1", "FD7A:0::1"]}]},  # the same address twice
            {"notify_senders": [{**s, "allow_from": [f"100.64.0.{i}" for i in range(17)]}]},
            {"notify_senders": [{"name": f"s{i}", "token_sha256": f"{i:064x}"} for i in range(1, 34)]},
            {"quiet_hours": "22:00-07:00"},
            {"quiet_hours": {"start": "22:00"}},
            {"quiet_hours": {"start": "22:00", "end": "07:00", "zone": "UTC"}},
            {"quiet_hours": {"start": "24:00", "end": "07:00"}},
            {"quiet_hours": {"start": "7:00", "end": "08:00"}},
            {"quiet_hours": {"start": "22:60", "end": "07:00"}},
            {"quiet_hours": {"start": "22:00:00", "end": "07:00"}},
            {"quiet_hours": {"start": "22:00\n", "end": "07:00"}},
            {"quiet_hours": {"start": 2200, "end": 700}},
            {"quiet_hours": {"start": "07:00", "end": "07:00"}},
            {"quiet_hours": []},
            {"notify_dedupe_seconds": -1},
            {"notify_dedupe_seconds": 86401},
            {"notify_dedupe_seconds": 1.5},
            {"notify_dedupe_seconds": True},
            {"notify_dedupe_seconds": "1800"},
            {"notify_dedupe_seconds": None},
            {"notify_rate_per_hour": 0},
            {"notify_rate_per_hour": 1001},
            {"notify_rate_per_hour": False},
            {"notify_rate_per_hour": 30.0},
            {"app_links": {}},
            {"app_links": [{"label": "A", "url": "https://a.ts.net/"}] * 13},
            {"app_links": ["https://a.ts.net/"]},
            {"app_links": [{"label": "A"}]},
            {"app_links": [{"url": "https://a.ts.net/"}]},
            {"app_links": [{"label": "A", "url": "https://a.ts.net/", "icon": "x"}]},
            {"app_links": [{"label": "", "url": "https://a.ts.net/"}]},
            {"app_links": [{"label": "   ", "url": "https://a.ts.net/"}]},
            {"app_links": [{"label": "x" * 33, "url": "https://a.ts.net/"}]},
            {"app_links": [{"label": "a\nb", "url": "https://a.ts.net/"}]},
            {"app_links": [{"label": 5, "url": "https://a.ts.net/"}]},
            {"app_links": [{"label": "A", "url": "ftp://a.ts.net/"}]},
            {"app_links": [{"label": "A", "url": "javascript:alert(1)"}]},
            {"app_links": [{"label": "A", "url": "https://u:p@a.ts.net/"}]},
            {"app_links": [{"label": "A", "url": "https://u@a.ts.net/"}]},
            {"app_links": [{"label": "A", "url": "https://a.ts.net/#x"}]},
            {"app_links": [{"label": "A", "url": "/relative"}]},
            {"app_links": [{"label": "A", "url": "https://a.ts.net/ x"}]},
            {"app_links": [{"label": "A", "url": "https://a.ts.net/" + "x" * 300}]},
            {"app_links": [{"label": "A", "url": None}]},
        ]
        for extra in bad:
            with self.assertRaises(ConfigError, msg=extra):
                from_dict({**self.BASE, **extra})

    def test_bad_config_refuses_to_start(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.json"
            p.write_text(json.dumps({**self.BASE, "notify_senders": [{**self.S, "token_sha256": "A" * 64}]}))
            for cmd in (["check-config"], ["serve"]):
                with contextlib.redirect_stderr(io.StringIO()) as err:
                    self.assertEqual(main(["--config", str(p), *cmd]), 2)
                self.assertIn("token_sha256 must be 64 lowercase hex", err.getvalue())

    def test_example_config_has_the_new_keys(self) -> None:
        raw = json.loads((HERE / "config.example.json").read_text())
        cfg = from_dict(raw)
        self.assertTrue(cfg.notify_senders)
        self.assertTrue(cfg.app_links)
        self.assertIsNotNone(cfg.quiet_hours)


# ---- the Apps card (B1) --------------------------------------------------------------------------------------
class AppsCardTests(Harness):
    LINKS: ClassVar[list[dict[str, str]]] = [
        {"label": "Open WebUI", "url": "http://ai-box.example.ts.net:8080/"},
        {"label": "Jellyfin", "url": "http://100.64.0.5:8096/web/"},
        {"label": "Money Hub", "url": "https://homebk-edge.example.ts.net/money/"},
    ]

    async def test_absent_when_empty(self) -> None:
        _, body = await self.get("/api/cards")
        self.assertNotIn("apps", body["cards"])

    async def test_links_in_config_order(self) -> None:
        await self.restart(app_links=self.LINKS)
        status, body = await self.get("/api/cards")
        self.assertEqual(status, 200)
        apps = body["cards"]["apps"]
        self.assertEqual(apps, {
            "status": "ok", "headline": "3 apps", "link": None, "error": None, "updated_at": None,
            "items": [{"label": a["label"], "value": "", "status": None, "url": a["url"]} for a in self.LINKS],
        })
        self.assertEqual(set(body["cards"]) - {"apps"}, {"ai", "ai_control", "money", "school", "home", "servers",
                                                         "alerts"})

    async def test_apps_card_needs_the_login_like_every_card(self) -> None:
        await self.restart(app_links=self.LINKS)
        self.assertEqual((await self.get("/api/cards", login=None))[0], 403)


class NotifyRouteWithoutSendersTests(Harness):
    async def test_default_config_answers_401(self) -> None:
        h = {"Content-Type": "application/json", "Authorization": "Bearer anything"}
        async with self.http.post(self.base + "/api/notify", data=json.dumps(msg()), headers=h) as r:
            self.assertEqual((r.status, await r.json()), (401, {"error": "unauthorized"}))


class FlushLoopSurvivesErrors(NotifyBase):
    async def test_a_failing_flush_is_logged_and_retried(self) -> None:
        calls = []

        async def boom() -> bool:
            calls.append(1)
            raise RuntimeError("boom")

        with mock.patch.object(app_mod, "NOTIFY_TICK_S", 0.01), mock.patch.object(self.state, "notify_flush", boom), \
                LogCapture() as logs:
            task = asyncio.create_task(self.state.notify_loop())
            for _ in range(100):
                if len(calls) >= 3:
                    break
                await asyncio.sleep(0.01)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self.assertGreaterEqual(len(calls), 3)
        self.assertIn("held-message flush failed", logs.text)


if __name__ == "__main__":
    unittest.main()
