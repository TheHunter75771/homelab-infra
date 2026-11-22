"""Lead's B2 follow-ups (after the Reviewer's approval of b27ffbc): no request bytes in aiohttp's own parse-error
logs or 400 bodies; pushes capped to what Web Push carries; future timestamps in notify.json; pushed tags within
sw.js's 64; the held summary within sw.js's 400; format characters refused in titles."""

from __future__ import annotations

import asyncio
import base64
import io
import itertools
import json
import logging
from typing import Any, ClassVar

from mission_control import notify as notify_mod
from mission_control import push as push_mod
from mission_control.config import from_dict
from mission_control.push import SubscriptionStore, load_vapid, make_pywebpush_sender, send_all
from mission_control.server import bound_port

from . import fakes
from .harness import LogCapture
from .test_notify import DEVICE, SENDERS, TOKENS, NotifyBase, at, msg
from .test_push import HAVE_PYWEBPUSH

TOKEN = TOKENS["mediaserver"]


class AiohttpLogs:
    """Everything aiohttp and the app log, formatted with tracebacks, as one string."""

    def __init__(self) -> None:
        self.stream = io.StringIO()
        self.handler = logging.StreamHandler(self.stream)
        self.handler.setFormatter(logging.Formatter("%(levelname)s %(name)s %(message)s"))
        self.mc = LogCapture()

    def __enter__(self) -> AiohttpLogs:  # noqa: PYI034
        self.logger = logging.getLogger("aiohttp")
        self._level = self.logger.level
        self.logger.setLevel(logging.DEBUG)
        self.logger.addHandler(self.handler)
        self.mc.__enter__()
        return self

    def __exit__(self, *exc: object) -> None:
        self.mc.__exit__(*exc)
        self.logger.removeHandler(self.handler)
        self.logger.setLevel(self._level)

    @property
    def text(self) -> str:
        return self.stream.getvalue() + self.mc.text


def u16(s: str) -> int:
    """Length as JavaScript counts it (UTF-16 code units): what sw.js's cuts measure."""
    return len(s.encode("utf-16-le")) // 2


# ---- 1. aiohttp's parse errors -------------------------------------------------------------------------------
class ParseErrorTests(NotifyBase):
    async def raw(self, data: bytes) -> bytes:
        assert self.runner is not None
        reader, writer = await asyncio.open_connection("127.0.0.1", bound_port(self.runner))
        try:
            writer.write(data)
            await writer.drain()
            return await asyncio.wait_for(reader.read(), 5)
        finally:
            writer.close()
            await writer.wait_closed()

    async def test_unparseable_requests_never_echo_or_log_the_token(self) -> None:
        t = TOKEN.encode()
        head = b"POST /api/notify HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Type: application/json\r\n"
        cases = {
            "Authorization over 8190 bytes": head + b"Authorization: Bearer " + t + b"A" * 9000 + b"\r\n\r\n",
            "a control character in Authorization": head + b"Authorization: Bearer " + t + b"\x01x\r\nContent-Length: 0\r\n\r\n",
            "a NUL in Authorization": head + b"Authorization: Bearer " + t + b"\x00\r\nContent-Length: 0\r\n\r\n",
            "a bad header after it": head + b"Authorization: Bearer " + t + b"\r\nX\x01Y: 1\r\n\r\n",
            "a header name over 8190 bytes": head + b"Authorization: Bearer " + t + b"\r\n" + b"X" * 9000 + b": 1\r\n\r\n",
            "a request line over 8190 bytes": b"POST /api/notify?" + t + b"A" * 9000 + b" HTTP/1.1\r\nHost: x\r\n\r\n",
            "the token in a bad method": t + b" /api/notify HTTP/1.1\r\nHost: x\r\n\r\n",
            "a bad chunk size": head + b"Authorization: Bearer " + t + b"\r\nTransfer-Encoding: chunked\r\n\r\nzz" + t + b"\r\n",
        }
        with AiohttpLogs() as logs:
            for name, data in cases.items():
                resp = await self.raw(data)
                self.assertRegex(resp[:13], rb"^HTTP/1\.[01] 400 ", f"{name}: {resp[:60]!r}")
                self.assertNotIn(t, resp, name)
                self.assertNotIn(t[:20], resp, f"{name}: not even a prefix")
                self.assertEqual(resp.split(b"\r\n\r\n", 1)[1], b"400 Bad Request", name)
        text = logs.text
        self.assertIn("refused a request that couldn't be parsed", text)
        self.assertIn("LineTooLong", text)
        self.assertNotIn(TOKEN, text)
        self.assertNotIn(TOKEN[:20], text)
        self.assertEqual(self.pushes(), [])
        status, _, _ = await self.notify()
        self.assertEqual(status, 202, "the server still answers normal requests")

    async def test_each_layer_holds_on_its_own(self) -> None:
        from mission_control.server import RequestBytesFilter

        server_log = logging.getLogger("aiohttp.server")
        filters = [f for f in server_log.filters if isinstance(f, RequestBytesFilter)]
        for f in filters:  # the quiet handler alone: it passes no exception on to aiohttp's log line
            server_log.removeFilter(f)
        try:
            with AiohttpLogs() as logs:
                resp = await self.raw(b"POST /api/notify HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer "
                                      + TOKEN.encode() + b"\x01\r\n\r\n")
        finally:
            for f in filters:
                server_log.addFilter(f)
        self.assertNotIn(TOKEN.encode(), resp)
        self.assertIn("Error handling request from 127.0.0.1", logs.text)
        self.assertNotIn(TOKEN, logs.text)
        self.assertNotIn("Traceback", logs.text)

    def test_redaction_keeps_the_line_and_the_type_only(self) -> None:
        from mission_control.server import RequestBytesFilter

        try:
            raise ValueError(f"bytes {TOKEN}")
        except ValueError as exc:
            record = logging.LogRecord("aiohttp.server", logging.ERROR, __file__, 1, "Error handling request from %s",
                                       ("127.0.0.1",), (type(exc), exc, exc.__traceback__))
        self.assertTrue(RequestBytesFilter().filter(record))
        out = logging.Formatter("%(message)s").format(record)
        self.assertEqual(out, "Error handling request from 127.0.0.1 (ValueError; details withheld)")
        for name in ("aiohttp.server", "aiohttp.web", "aiohttp.internal", "aiohttp.access"):
            self.assertTrue(any(isinstance(f, RequestBytesFilter) for f in logging.getLogger(name).filters), name)


# ---- 2. push size --------------------------------------------------------------------------------------------
class PushSizeTests(NotifyBase):
    async def test_a_valid_message_too_big_to_push_is_413(self) -> None:
        astral = "\U0001F525"  # 4 bytes in the request's UTF-8, 12 once the push escapes it (\\ud83d\\udd25)
        big = msg(title=astral * 80, body=astral * 300, url="/" + "a" * 500)
        raw = json.dumps(big, ensure_ascii=False).encode()
        self.assertLessEqual(len(raw), 4096, "a request within the limit ...")
        self.assertGreater(len(push_mod.encode(notify_mod.push_message("mediaserver", notify_mod.parse_message(big)))),
                           push_mod.MAX_PAYLOAD, "... whose push is not")
        status, out, _ = await self.notify(data=raw)
        self.assertEqual((status, out), (413, {"error": "too large to push"}))
        self.assertEqual(self.pushes(), [])
        self.assertFalse(self.state_file().exists(), "not counted")
        status, out, _ = await self.notify(msg(title=astral * 80, body="é" * 300, url="/" + "a" * 500))
        self.assertEqual((status, out), (202, {"status": "sent"}))
        payload = json.dumps(self.pushes()[0], separators=(",", ":")).encode()
        self.assertLessEqual(len(payload), push_mod.MAX_PAYLOAD)

    async def test_send_all_refuses_an_oversized_payload_outright(self) -> None:
        with LogCapture() as logs:
            result = await send_all(self.state.subs, self.sender, {"title": "x", "body": "y" * 4000})
        self.assertEqual(result, {"devices": 1, "delivered": 0, "failed": 1, "pruned": 0})
        self.assertEqual(self.sender.calls, [])
        self.assertIn("push not sent: 4", logs.text)

    def test_the_summary_always_fits(self) -> None:
        astral = "\U0001F525"
        held = [{"sender": "s" * 32, "tag": "t", "title": astral * 80, "at": 1.0} for _ in range(50)]
        for dropped in (0, 7, 2**53 - 51):
            out = notify_mod.summary_push(held, dropped)
            self.assertTrue(push_mod.fits(out), dropped)
            self.assertLessEqual(u16(out["body"]), 400, dropped)


class RealPushSizeTests(NotifyBase):
    """Measured with the real pywebpush: aes128gcm adds 103 bytes, and push services take a 4096-byte body."""

    start_server = False

    async def asyncSetUp(self) -> None:  # no server, no device: only the sender and a fake push service
        await super(NotifyBase, self).asyncSetUp()

    async def test_the_cap_encrypts_to_at_most_4096_bytes(self) -> None:
        if not HAVE_PYWEBPUSH:
            self.skipTest("pywebpush not installed")
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec

        svc = fakes.FakePushService()
        await svc.start()
        self.fakes.append(svc)
        key = ec.generate_private_key(ec.SECP256R1()).public_key().public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
        store = SubscriptionStore(str(self.state_dir / "subscriptions.json"))
        store.add({"endpoint": f"{svc.url}/d", "keys": {"p256dh": base64.urlsafe_b64encode(key).rstrip(b"=").decode(),
                                                        "auth": fakes.b64(16)}, "label": "d"})
        cfg = from_dict({**self.cfg_raw, "push_timeout_s": 5})
        sender = make_pywebpush_sender(load_vapid(cfg.vapid_file), cfg)
        frame = len(push_mod.encode({"title": "", "body": ""}))
        message = {"title": "", "body": "x" * (push_mod.MAX_PAYLOAD - frame)}
        self.assertEqual(len(push_mod.encode(message).encode()), push_mod.MAX_PAYLOAD)
        self.assertEqual((await send_all(store, sender, message))["delivered"], 1)
        body = svc.bodies[-1][2]
        self.assertEqual(len(body), push_mod.MAX_PAYLOAD + 103)
        self.assertLessEqual(len(body), 4096)


# ---- 3. future timestamps ------------------------------------------------------------------------------------
class FutureTimestampTests(NotifyBase):
    extra: ClassVar[dict[str, Any]] = {"notify_senders": SENDERS, "notify_rate_per_hour": 3}

    def poison(self, **entries: Any) -> None:
        self.state_file().write_text(json.dumps({"dedupe": {}, "rate": {}, "held": [], "dropped": 0, **entries}))

    async def test_a_poisoned_file_cannot_silence_or_rate_limit(self) -> None:
        m = notify_mod.parse_message(msg())
        key = notify_mod.dedupe_key("mediaserver", m)
        later = fakes.NOW + 30 * 86400
        self.poison(dedupe={key: later}, rate={"mediaserver": [later] * 3})
        self.assertEqual((await self.notify())[1], {"status": "sent"}, "neither deduped nor 429")
        saved = self.state.notify_store.load(fakes.NOW)
        self.assertEqual(saved["rate"], {"mediaserver": [fakes.NOW]})

    async def test_slack_is_five_minutes(self) -> None:
        m = notify_mod.parse_message(msg())
        key = notify_mod.dedupe_key("mediaserver", m)
        self.poison(dedupe={key: fakes.NOW + 299})
        self.assertEqual((await self.notify())[1], {"status": "deduped"}, "a little clock skew is still trusted")
        self.poison(dedupe={key: fakes.NOW + 301})
        self.assertEqual((await self.notify())[1], {"status": "sent"})

    async def test_a_clock_stepped_back_does_not_silence_or_rate_limit(self) -> None:
        for i in range(3):
            self.assertEqual((await self.notify(msg(tag=f"t{i}")))[1], {"status": "sent"})
        self.clock.now = fakes.NOW - 3600  # NTP stepped the clock back an hour
        self.assertEqual((await self.notify(msg(tag="t0")))[1], {"status": "sent"}, "not deduped, not 429")
        self.assertEqual(self.state.notify_store.load(self.clock.now)["rate"]["mediaserver"], [fakes.NOW - 3600])

    def test_load_drops_future_entries(self) -> None:
        now = fakes.NOW
        raw = {"dedupe": {"a" * 64: now + 301, "b" * 64: now + 299}, "rate": {"pc": [now + 301, now - 5]}}
        out = notify_mod._valid_state(raw, now)
        self.assertEqual((out["dedupe"], out["rate"]), ({"b" * 64: now + 299}, {"pc": [now - 5]}))
        many = {"dedupe": {f"{i:064x}": now + 86400 for i in range(notify_mod.MAX_DEDUPE)} | {"c" * 64: now}}
        self.assertEqual(notify_mod._valid_state(many, now)["dedupe"], {"c" * 64: now}, "future ones take no slot")

    def test_prune_drops_future_entries_too(self) -> None:
        now = fakes.NOW
        state = {"dedupe": {"a" * 64: now + 301, "b" * 64: now + 299, "c" * 64: now - 10},
                 "rate": {"pc": [now + 301, now + 299, now - 10], "edge": [now + 9999]}, "held": [], "dropped": 0}
        out = notify_mod.prune(state, now, 1800)
        self.assertEqual(set(out["dedupe"]), {"b" * 64, "c" * 64})
        self.assertEqual(out["rate"], {"pc": [now + 299, now - 10]})

    def test_held_entries_are_kept_with_their_time_clamped(self) -> None:
        now = fakes.NOW
        h = {"sender": "pc", "tag": "t", "title": "ok", "at": now + 86400}
        out = notify_mod._valid_state({"held": [h], "dropped": 2}, now)
        self.assertEqual(out["held"], [{**h, "at": now}], "a held message is never dropped for its time")
        self.assertEqual(out["dropped"], 2)
        self.assertEqual(notify_mod._valid_state({"held": [h]})["held"][0]["at"], now + 86400, "no now: as stored")


# ---- 2b. the summary's back-off ------------------------------------------------------------------------------
class FlushBackoffTests(NotifyBase):
    extra: ClassVar[dict[str, Any]] = {"notify_senders": SENDERS, "notify_rate_per_hour": 1000,
                                       "quiet_hours": {"start": "22:00", "end": "07:00"}}

    async def test_failed_summary_backs_off_to_an_hour(self) -> None:
        self.clock.now = at(23, 0)
        await self.notify()
        self.sender.status[DEVICE["endpoint"]] = 503
        self.clock.now = t0 = at(7, 0, 29)
        tries = []
        for _ in range(16 * 60):  # one check a minute for 16 hours
            before = len(self.pushes("n.held"))
            await self.state.notify_flush()
            if len(self.pushes("n.held")) > before:
                tries.append(round((self.clock.now - t0) / 60))
            self.clock.now += 60
        gaps = [b - a for a, b in itertools.pairwise(tries)]
        self.assertEqual(gaps[:7], [1, 2, 4, 8, 16, 32, 60])
        self.assertTrue(all(g == 60 for g in gaps[6:]), gaps)
        self.sender.status[DEVICE["endpoint"]] = 201
        self.clock.now = at(7, 30, 30)  # the next morning: past the next try, outside the window
        self.assertTrue(await self.state.notify_flush())
        self.assertEqual((self.state.flush_failures, self.state.flush_retry_at), (0, 0.0), "reset after a delivery")

    async def test_push_disabled_backs_off_too(self) -> None:
        self.clock.now = at(23, 0)
        await self.notify()
        self.state.sender = None
        self.clock.now = at(7, 0, 29)
        with LogCapture() as logs:
            for _ in range(10):
                await self.state.notify_flush()
                self.clock.now += 60
        self.assertEqual(logs.text.count("but push is disabled"), 4, "at 0, 1, 3 and 7 min")


# ---- 4. tags within sw.js's 64 -------------------------------------------------------------------------------
class TagLengthTests(NotifyBase):
    async def test_long_tags_are_hashed_and_never_collide(self) -> None:
        a = "x" * 49 + "-alpha-one"  # 59 characters; the two share their first 49
        b = "x" * 49 + "-beta-two"
        exact = "y" * 50  # n.mediaserver. + 50 = 64: fits as it is
        for tag in (a, b, a, exact):
            self.assertEqual((await self.notify(msg(tag=tag)))[0], 202)
            self.clock.now += 2000  # past the dedupe window
        tags = [p["tag"] for p in self.pushes()]
        self.assertTrue(all(len(t) <= 64 for t in tags), tags)
        self.assertNotEqual(tags[0], tags[1], "tags sharing 49 characters stay apart")
        self.assertEqual(tags[0], tags[2], "the same tag gives the same pushed tag (replace-on-device)")
        self.assertEqual(tags[3], "n.mediaserver." + exact)
        self.assertTrue(tags[0].startswith("n.mediaserver.~"))
        # The SW's own cut (64 units, then "…") would have made the two equal; ours never needs it.
        sw_clip = [t if len(t) <= 64 else t[:63] + "…" for t in ("n.mediaserver." + a, "n.mediaserver." + b)]
        self.assertEqual(sw_clip[0], sw_clip[1])

    def test_the_longest_sender_and_tag(self) -> None:
        m = notify_mod.parse_message(msg(tag="z" * 64))
        tag = notify_mod.push_message("s" * 32, m)["tag"]
        self.assertLessEqual(len(tag), 64)
        self.assertTrue(tag.startswith("n." + "s" * 32 + ".~"))
        literal = notify_mod.push_message("s" * 32, notify_mod.parse_message(msg(tag="z" * 29)))["tag"]
        self.assertEqual(literal, "n." + "s" * 32 + "." + "z" * 29, "64 exactly: literal")


# ---- 5. the summary within sw.js's 400 -----------------------------------------------------------------------
class SummaryLengthTests(NotifyBase):
    def test_body_at_most_400_with_the_more_line(self) -> None:
        long_title = "T" * 80
        held = [{"sender": "s" * 32, "tag": "t", "title": f"{i}{long_title}"[:80], "at": 1.0} for i in range(50)]
        out = notify_mod.summary_push(held, 3)
        lines = out["body"].split("\n")
        self.assertEqual(len(lines), 6)
        self.assertEqual(lines[-1], "…and 48 more")
        self.assertLessEqual(u16(out["body"]), 400)
        self.assertTrue(all(line.startswith("s" * 32 + ": ") for line in lines[:5]), "the sender is never cut")
        self.assertTrue(all(line.endswith("…") for line in lines[:5]), "cut titles end in …")
        self.assertEqual(out["title"], "Held overnight: 53")

    def test_short_titles_are_not_cut_and_lend_their_room(self) -> None:
        held = [{"sender": "pc", "tag": "t", "title": "Disk full", "at": 1.0}] * 4
        held += [{"sender": "mediaserver", "tag": "t", "title": "B" * 80, "at": 1.0}]
        body = notify_mod.summary_push(held, 0)["body"]
        self.assertEqual(body, "\n".join(["pc: Disk full"] * 4 + ["mediaserver: " + "B" * 80]), "all fits: as is")

    def test_astral_titles_counted_as_sw_js_counts(self) -> None:
        held = [{"sender": "pc", "tag": "t", "title": "\U0001F525" * 80, "at": 1.0} for _ in range(5)]
        body = notify_mod.summary_push(held, 0)["body"]
        self.assertLessEqual(u16(body), 400)
        body.encode("utf-8")  # raises on half of a surrogate pair: none is left behind

    async def test_end_to_end(self) -> None:
        await self.restart(quiet_hours={"start": "05:00", "end": "06:00"})
        for i in range(8):
            await self.notify(msg(title=("Z" * 79 + str(i)), tag=f"t{i}"), token=TOKENS["mediaserver"])
        self.clock.now += 3600
        self.assertTrue(await self.state.notify_flush())
        body = self.pushes()[0]["body"]
        self.assertLessEqual(u16(body), 400)
        self.assertTrue(body.endswith("\n…and 3 more"))


# ---- format characters in titles -----------------------------------------------------------------------------
class FormatCharacterTests(NotifyBase):
    async def test_cf_refused_in_titles(self) -> None:
        for title in ("evil \u202etxt.exe", "zero\u200bwidth", "\u2066isolate\u2069", "soft\u00adhyphen",
                      "\U0001F468\u200d\U0001F4BB", "bom\ufeff"):
            status, out, _ = await self.notify(msg(title=title))
            self.assertEqual((status, out), (400, {"error": "title"}), repr(title))
        self.assertEqual((await self.notify(msg(title="\U0001F525 Disk 95%")))[0], 202)
        self.assertEqual((await self.notify(msg(title="\u2764\ufe0f ok", tag="vs")))[0], 202, "a variation selector is Mn")
