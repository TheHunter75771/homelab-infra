"""The HTTP surface: identity, guards, /api/cards, /api/briefing, the push endpoints and the static app."""

from __future__ import annotations

import json
import os
import types

from multidict import CIMultiDict

from mission_control.app import is_allowed

from . import fakes
from .harness import Harness

API_GETS = ["/api/cards", "/api/briefing", "/", "/sw.js", "/anything"]
API_POSTS = ["/api/push/subscribe", "/api/push/unsubscribe", "/api/push/test"]


class IdentityTests(Harness):
    async def test_no_identity_is_refused_everywhere(self) -> None:
        for path in API_GETS:
            status, body = await self.get(path, login=None)
            self.assertEqual(status, 403, path)
            self.assertEqual(body, {"error": "forbidden"})
        for path in API_POSTS:
            status, _ = await self.post(path, {"endpoint": "x"}, login=None)
            self.assertEqual(status, 403, path)
        self.assertEqual(self.hub.requests, [], "a refused request must not reach any source")

    async def test_other_login_is_refused(self) -> None:
        for login in ("family@example.com", "noah@example.com.evil", "noah@example.co", "", "noah"):
            status, _ = await self.get("/api/cards", login=login)
            self.assertEqual(status, 403, login)
        status, _ = await self.post("/api/push/subscribe", {"subscription": fakes.fake_subscription()},
                                    login="family@example.com")
        self.assertEqual(status, 403)
        self.assertEqual(self.state.subs.all(), [])

    async def test_login_is_byte_exact(self) -> None:
        status, body = await self.get("/api/cards", login=fakes.LOGIN)
        self.assertEqual(status, 200)
        self.assertIn("cards", body)
        zwsp = "\u200b"
        for login in (fakes.LOGIN + zwsp, zwsp + fakes.LOGIN, fakes.LOGIN.upper(), "Noah@Example.COM",
                      "noah@exa mple.com", "\uff4eoah@example.com", "noah@example.com\u00a0"):
            status, _ = await self.get("/api/cards", login=login)
            self.assertEqual(status, 403, repr(login))

    def test_login_compare_has_no_trim(self) -> None:
        # Leading/trailing spaces and tabs never reach the app over HTTP (the parser drops optional whitespace
        # around a field value, RFC 9110 5.5); the compare itself must not trim either.
        for login in (" " + fakes.LOGIN, fakes.LOGIN + " ", fakes.LOGIN + "\t", "noah\u200b@example.com", "NOAH@EXAMPLE.COM"):
            req = types.SimpleNamespace(transport=types.SimpleNamespace(get_extra_info=lambda _k: ("127.0.0.1", 5)),
                                        headers=CIMultiDict({"Tailscale-User-Login": login}))
            self.assertFalse(is_allowed(req, fakes.LOGIN), repr(login))

    async def test_duplicate_login_header_refused(self) -> None:
        for pair in ((fakes.LOGIN, fakes.LOGIN), ("evil@x.com", fakes.LOGIN), (fakes.LOGIN, "evil@x.com")):
            h = CIMultiDict([("Tailscale-User-Login", pair[0]), ("Tailscale-User-Login", pair[1])])
            async with self.http.get(self.base + "/api/cards", headers=h) as r:
                self.assertEqual(r.status, 403, pair)

    async def test_control_characters_do_not_smuggle_a_login(self) -> None:
        status, _ = await self.get("/api/cards", login="noah@example.com\tx")
        self.assertEqual(status, 403)

    async def test_healthz_needs_no_identity(self) -> None:
        status, body = await self.get("/healthz", login=None)
        self.assertEqual((status, body), (200, {"ok": True}))

    def test_peer_must_be_localhost(self) -> None:
        def req(peer: tuple[str, int] | None, login: str) -> object:
            transport = types.SimpleNamespace(get_extra_info=lambda _k: peer)
            return types.SimpleNamespace(transport=transport, headers=CIMultiDict({"Tailscale-User-Login": login}))

        self.assertTrue(is_allowed(req(("127.0.0.1", 5), fakes.LOGIN), fakes.LOGIN))
        self.assertFalse(is_allowed(req(("100.64.1.2", 5), fakes.LOGIN), fakes.LOGIN))
        self.assertFalse(is_allowed(req(None, fakes.LOGIN), fakes.LOGIN))


class GuardTests(Harness):
    async def test_methods(self) -> None:
        async with self.http.put(self.base + "/api/cards", headers={"Tailscale-User-Login": fakes.LOGIN}) as r:
            self.assertEqual(r.status, 405)
        async with self.http.head(self.base + "/api/cards", headers={"Tailscale-User-Login": fakes.LOGIN}) as r:
            self.assertEqual(r.status, 405)
        status, _ = await self.get("/api/push/test")
        self.assertEqual(status, 405)

    async def test_cross_site_refused(self) -> None:
        for site in ("cross-site", "same-site"):
            status, _ = await self.get("/api/cards", headers={"Sec-Fetch-Site": site})
            self.assertEqual(status, 403, site)
            status, _ = await self.post("/api/push/subscribe", {}, headers={"Sec-Fetch-Site": site})
            self.assertEqual(status, 403, site)

    async def test_post_needs_custom_header_and_json(self) -> None:
        status, _ = await self.post("/api/push/subscribe", {}, headers={"X-Mission-Control": "0"})
        self.assertEqual(status, 403)
        status, _ = await self.post("/api/push/subscribe", {}, headers={"Content-Type": "text/plain"})
        self.assertEqual(status, 415)

    async def test_query_and_unknown_api(self) -> None:
        self.assertEqual((await self.get("/api/cards?x=1"))[0], 400)
        self.assertEqual((await self.get("/api/nope"))[0], 404)

    async def test_security_headers(self) -> None:
        async with self.http.get(self.base + "/api/cards", headers={"Tailscale-User-Login": fakes.LOGIN}) as r:
            self.assertEqual(r.headers["Cache-Control"], "no-store")
            self.assertIn("frame-ancestors 'none'", r.headers["Content-Security-Policy"])
            self.assertEqual(r.headers["X-Content-Type-Options"], "nosniff")

    async def test_static_and_traversal(self) -> None:
        status, body = await self.get("/sw.js")
        self.assertEqual(status, 200)
        self.assertIn("push", body)
        (self.dir / "secret.txt").write_text("outside")
        status, body = await self.get("/../secret.txt")
        self.assertNotIn("outside", str(body))
        status, body = await self.get("/%2e%2e/secret.txt")
        self.assertNotIn("outside", str(body))


class CardsTests(Harness):
    async def test_cards_shape(self) -> None:
        status, body = await self.get("/api/cards")
        self.assertEqual(status, 200)
        self.assertEqual(body["generated_at"], int(fakes.NOW))
        self.assertEqual(body["config"]["vapid_public_key"], self.vapid["publicKey"])
        self.assertEqual(body["config"]["open_webui_url"], "https://ai-box.example.ts.net/")
        self.assertEqual(set(body["cards"]), {"ai", "ai_control", "money", "school", "home", "servers", "alerts"})
        for cid, card in body["cards"].items():
            self.assertEqual(set(card), {"status", "headline", "items", "link", "error", "updated_at"}, cid)
            self.assertIn(card["status"], ("ok", "warn", "down", "unknown", "setup"), cid)
        c = body["cards"]
        self.assertEqual(c["ai"]["status"], "ok")
        self.assertEqual(c["money"]["status"], "warn")
        self.assertEqual(c["money"]["headline"], "Bank sync failing")
        self.assertIn({"label": "Budget left (2026-10)", "value": "$90.00", "status": "warn"}, c["money"]["items"])
        self.assertEqual(c["school"]["headline"], "3 deadlines in the next 48 h")
        self.assertEqual(c["home"]["status"], "setup")  # no token file yet
        self.assertEqual(c["alerts"]["status"], "down")  # the 95%-full disk is crit
        texts = json.dumps(body)
        self.assertNotIn("PRIVATE INBOX TEXT", texts)
        self.assertNotIn("endpointHost", texts)

    async def test_cache(self) -> None:
        await self.get("/api/cards")
        await self.get("/api/briefing")
        self.assertEqual(self.hub.hits("/api/state"), 1)
        self.clock.now += 61
        await self.get("/api/cards")
        self.assertEqual(self.hub.hits("/api/state"), 2)

    async def test_home_with_token(self) -> None:
        self.ha_token.write_text(fakes.HA_TOKEN + "\n")
        _, body = await self.get("/api/cards")
        home = body["cards"]["home"]
        self.assertEqual(home["status"], "ok")
        self.assertIn({"label": "Living room", "value": "71.2 °F", "status": None}, home["items"])
        self.assertNotIn(fakes.HA_TOKEN, json.dumps(body))


class PushEndpointTests(Harness):
    async def test_subscribe_test_unsubscribe(self) -> None:
        sub = fakes.fake_subscription(3)
        status, body = await self.post("/api/push/subscribe", {"subscription": sub, "label": "iPhone"})
        self.assertEqual((status, body), (200, {"ok": True}))
        stored = self.state.subs.all()
        self.assertEqual([s["label"] for s in stored], ["iPhone"])
        self.assertEqual(os.stat(self.state_dir / "subscriptions.json").st_mode & 0o777, 0o600)
        status, _ = await self.post("/api/push/test", {"endpoint": sub["endpoint"]})
        self.assertEqual(status, 200)
        self.assertEqual(self.sender.calls[-1][1]["tag"], "mc-test")
        status, _ = await self.post("/api/push/unsubscribe", {"endpoint": sub["endpoint"]})
        self.assertEqual(status, 200)
        status, _ = await self.post("/api/push/unsubscribe", {"endpoint": sub["endpoint"]})
        self.assertEqual(status, 404)
        status, _ = await self.post("/api/push/test", {"endpoint": sub["endpoint"]})
        self.assertEqual(status, 404)

    async def test_subscribe_validation(self) -> None:
        bad = [
            {**fakes.fake_subscription(), "endpoint": "http://fcm.googleapis.com/x"},
            {**fakes.fake_subscription(), "endpoint": "https://evil.example.com/x"},
            {**fakes.fake_subscription(), "endpoint": "https://fcm.googleapis.com.evil.com/x"},
            {**fakes.fake_subscription(), "endpoint": "https://user:pw@fcm.googleapis.com/x"},
            {**fakes.fake_subscription(), "keys": {"p256dh": "short", "auth": "x"}},
            "not an object",
        ]
        for sub in bad:
            status, body = await self.post("/api/push/subscribe", {"subscription": sub})
            self.assertEqual(status, 400, sub)
            self.assertIn("error", body)
        ok = fakes.fake_subscription(4, host="web.push.apple.com")
        self.assertEqual((await self.post("/api/push/subscribe", {"subscription": ok}))[0], 200)

    async def test_max_subscriptions(self) -> None:
        await self.restart(max_subscriptions=2)
        for i in (1, 2):
            self.assertEqual((await self.post("/api/push/subscribe", {"subscription": fakes.fake_subscription(i)}))[0], 200)
        status, body = await self.post("/api/push/subscribe", {"subscription": fakes.fake_subscription(3)})
        self.assertEqual(status, 400)
        self.assertIn("too many devices", body["error"])
        # re-subscribing an existing device replaces it
        self.assertEqual((await self.post("/api/push/subscribe", {"subscription": fakes.fake_subscription(2)}))[0], 200)

    async def test_test_push_to_expired_device_prunes_it(self) -> None:
        sub = fakes.fake_subscription(5)
        await self.post("/api/push/subscribe", {"subscription": sub})
        self.sender.status[sub["endpoint"]] = 410
        status, _ = await self.post("/api/push/test", {"endpoint": sub["endpoint"]})
        self.assertEqual(status, 410)
        self.assertEqual(self.state.subs.all(), [])

    async def test_body_limits(self) -> None:
        status, _ = await self.post("/api/push/subscribe", {"subscription": "x" * 9000})
        self.assertEqual(status, 413)
        h = {"Content-Type": "application/json", "X-Mission-Control": "1", "Tailscale-User-Login": fakes.LOGIN}
        async with self.http.post(self.base + "/api/push/subscribe", data=b"[1,2]", headers=h) as r:
            self.assertEqual(r.status, 400)
        async with self.http.post(self.base + "/api/push/subscribe", data=b'{"a": NaN}', headers=h) as r:
            self.assertEqual(r.status, 400)
