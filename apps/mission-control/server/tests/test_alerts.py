"""Alert rules, dedup and repeat, held state for unreadable sources, and the server's alert round."""

from __future__ import annotations

import json
import unittest

from mission_control import alerts
from mission_control.alerts import Alert, plan

from . import fakes
from .harness import Harness

A = Alert("bank-sync", "warn", "Bank sync failing", "Bank sync failing", "Bank sync failing")
B = Alert("backup:x", "warn", "Backup stale: X", "Backup stale", "Backup stale: X")
REPEAT = 24 * 3600


DOWN = Alert("service:webui", "crit", "Open WebUI is down (HTTP 502)", "Open WebUI down", "Open WebUI down", 2)


def run(state: dict, rounds: list[tuple[float, list[Alert], set[str]]], deliver: bool = True) -> tuple[list[list[str]], dict]:
    """Feed rounds to plan(); returns the ids pushed per round and the final state."""
    pushed = []
    for t, active, held in rounds:
        send, state = plan(state, active, held, t, REPEAT)
        if deliver:
            alerts.mark_sent(state, send, t)
        pushed.append([a.id for a in send])
    return pushed, state


class PlanTests(unittest.TestCase):
    def test_new_alert_is_sent_once(self) -> None:
        pushed, state = run({}, [(1000.0, [A], set()), (1060.0, [A], set()), (5000.0, [A], set()),
                                 (1000.0 + REPEAT - 1, [A], set())])
        self.assertEqual(pushed, [["bank-sync"], [], [], []])
        self.assertEqual(state["bank-sync"]["since"], 1000.0)

    def test_repeat_interval(self) -> None:
        pushed, _ = run({}, [(0.0, [A], set()), (float(REPEAT), [A], set()), (REPEAT + 60.0, [A], set())])
        self.assertEqual(pushed, [["bank-sync"], ["bank-sync"], []])

    def test_only_the_new_one_of_two(self) -> None:
        pushed, _ = run({}, [(0.0, [A], set()), (60.0, [A, B], set())])
        self.assertEqual(pushed, [["bank-sync"], ["backup:x"]])

    def test_down_alert_needs_two_rounds(self) -> None:
        pushed, state = run({}, [(0.0, [DOWN], set())])
        self.assertEqual(pushed, [[]])
        self.assertEqual(state["service:webui"]["phase"], "pending")
        pushed, state = run(state, [(300.0, [DOWN], set()), (600.0, [DOWN], set())])
        self.assertEqual(pushed, [["service:webui"], []])
        self.assertEqual(state["service:webui"]["since"], 0.0)

    def test_two_of_three_rounds_raises(self) -> None:
        # down, up, down: seen in 2 of the last 3 rounds (Z2-3)
        pushed, state = run({}, [(0.0, [DOWN], set()), (300.0, [], set()), (600.0, [DOWN], set())])
        self.assertEqual(pushed, [[], [], ["service:webui"]])
        self.assertEqual(state["service:webui"]["phase"], "active")

    def test_pending_is_forgotten_after_three_clean_rounds_and_kept_while_held(self) -> None:
        pushed, state = run({}, [(0.0, [DOWN], set()), (300.0, [], set()), (600.0, [], set())])
        self.assertEqual(state["service:webui"]["recent"], [1, 0, 0])
        _, state = run(state, [(900.0, [], set())])
        self.assertNotIn("service:webui", state)  # [0, 0, 0]: not seen in the last 3 rounds
        pushed, state = run(state, [(900.0, [DOWN], set()), (1200.0, [], set()), (1500.0, [], set()),
                                    (1800.0, [DOWN], set())])
        self.assertEqual(pushed, [[], [], [], []])  # the two sightings are 3 rounds apart
        _, state = run(state, [(2100.0, [], {"service:webui"})])
        self.assertEqual(state["service:webui"]["recent"], [0, 0, 1])  # held: unchanged
        pushed, _ = run(state, [(2400.0, [DOWN], set())])
        self.assertEqual(pushed, [["service:webui"]])

    def test_escalation_only_above_the_highest_sent(self) -> None:
        crit = Alert("backup:x", "crit", "Backup failed: X", "Backup failed", "Backup failed: X")
        pushed, _ = run({}, [(0.0, [B], set()), (60.0, [crit], set()), (120.0, [B], set()), (180.0, [crit], set()),
                             (240.0, [B], set())])
        self.assertEqual(pushed, [["backup:x"], ["backup:x"], [], [], []])

    def test_escalation_from_crit_start_never_repushes(self) -> None:
        crit = Alert("backup:x", "crit", "Backup failed: X", "Backup failed", "Backup failed: X")
        pushed, _ = run({}, [(0.0, [crit], set()), (60.0, [B], set()), (120.0, [crit], set())])
        self.assertEqual(pushed, [["backup:x"], [], []])

    def test_unsent_alert_is_retried(self) -> None:
        send, state = plan({}, [A], set(), 0.0, REPEAT)
        self.assertEqual(send, [A])  # delivery failed: not marked
        send, _ = plan(state, [A], set(), 300.0, REPEAT)
        self.assertEqual(send, [A])

    def test_clearing_keeps_the_episode_for_30_min(self) -> None:
        pushed, state = run({}, [(0.0, [A], set()), (300.0, [], set())])
        self.assertEqual(state["bank-sync"]["phase"], "clearing")
        self.assertEqual(state["bank-sync"]["clear_since"], 300.0)
        pushed, state = run(state, [(1200.0, [A], set())])  # back within 30 min: same episode, quiet
        self.assertEqual(pushed, [[]])
        self.assertEqual(state["bank-sync"]["phase"], "active")
        self.assertIsNone(state["bank-sync"]["clear_since"])
        # clean again: forgotten only after 30 min clean, counted from the first clean round
        _, state = run(state, [(1500.0, [], set()), (1500.0 + 1799, [], set())])
        self.assertIn("bank-sync", state)
        _, state = run(state, [(1500.0 + 1800, [], set())])
        self.assertEqual(state, {})
        pushed, _ = run(state, [(4000.0, [A], set())])  # a new episode
        self.assertEqual(pushed, [["bank-sync"]])

    def test_held_is_kept_unchanged(self) -> None:
        _, state = run({}, [(0.0, [A, B], set())])
        before = dict(state["bank-sync"])
        pushed, state = run(state, [(60.0, [], {"bank-sync"})])
        self.assertEqual(state["bank-sync"], before)
        self.assertEqual(state["backup:x"]["phase"], "clearing")
        pushed, state = run(state, [(4000.0, [A], set())])
        self.assertEqual(pushed, [[]])
        self.assertEqual(state["bank-sync"]["since"], before["since"])
        self.assertNotIn("backup:x", state)

    def test_prefix_hold(self) -> None:
        u = Alert("unit:forge-api.service", "crit", "x", "Service failed", "Service failed: forge-api.service")
        _, state = plan({}, [u], set(), 0.0, REPEAT)
        _, state = plan(state, [], {"unit:"}, 60.0, REPEAT)
        self.assertEqual(state[u.id]["phase"], "active")

    def test_state_file_round_trip(self) -> None:
        import tempfile

        _, state = run({}, [(0.0, [A, DOWN], set())])
        with tempfile.TemporaryDirectory() as d:
            store = alerts.AlertStore(f"{d}/alerts.json")
            store.save(state)
            self.assertEqual(store.load(), state)
            with open(f"{d}/alerts.json", "w") as fh:
                json.dump({"x": {"phase": "weird"}, "bank-sync": state["bank-sync"]}, fh)
            self.assertEqual(set(store.load()), {"bank-sync"})


class RuleTests(Harness):
    async def test_rules(self) -> None:
        units = fakes.default_units()
        units["forge-api.service"].update(ActiveState="failed", SubState="failed", Result="exit-code")
        units["ai-control.service"].update(NRestarts="7")  # 7 now, 4 twenty minutes ago (history below)
        units["ai-openwebui-backup.service"].update(ExecMainExitTimestamp=f"@{int(fakes.NOW - 50 * 3600)}")
        fakes.set_units(self.dir / "sysd", units)
        self.ms.backups = {"status": "failed", "lastOk": fakes.iso(fakes.NOW - 80 * 3600)}
        self.aic.summary["services"][3]["status"] = "down"  # broker
        self.webui.override["/health"] = lambda: __import__("aiohttp").web.Response(status=500)
        assert self.state.collector is not None
        snap = await self.state.collector.collect()
        hist = {"ai-control.service": [[fakes.NOW - 1200, 4]]}
        hist = alerts.update_restarts(hist, snap, self.cfg, fakes.NOW)
        active, held = alerts.evaluate(snap, self.cfg, fakes.NOW, hist)
        by = {a.id: a for a in active}
        self.assertEqual(held, set())
        self.assertEqual(by["bank-sync"].level, "warn")
        self.assertIn("SimpleFinError", by["bank-sync"].text)
        self.assertEqual(by["unit:forge-api.service"].level, "crit")
        self.assertEqual(by["unit:ai-control.service"].text, "ai-control.service restarted 3 times in the last 60 min")
        self.assertEqual(by["unit:forge-api.service"].confirm, 2)
        self.assertEqual(by["service:broker"].confirm, 2)
        self.assertEqual(by["bank-sync"].confirm, 1)
        self.assertEqual(by["backup:owui-stage"].level, "warn")
        self.assertEqual(by["backup:mediaserver"].level, "crit")
        self.assertEqual(by["service:broker"].level, "crit")
        self.assertEqual(by["service:webui"].text, "Open WebUI is down (HTTP 500)")
        self.assertEqual(by["disk:pc:/mnt/share"].level, "crit")
        self.assertNotIn("disk:pc:/", by)
        self.assertNotIn("backup:owui-file", by)
        self.assertEqual(active[0].level, "crit")  # crit first

    async def test_sync_recent_is_quiet_and_unreadable_is_held(self) -> None:
        self.hub.state["sync_last_at"] = "2026-10-27T13:00:00"
        self.hub.state["sync_failing"] = False
        assert self.state.collector is not None
        active, held = alerts.evaluate(await self.state.collector.collect(), self.cfg, fakes.NOW)
        self.assertNotIn("bank-sync", {a.id for a in active})
        await self.restart(source_timeout_s=0.2)
        self.hub.delay = self.aic.delay = 1.0
        fakes.set_units(self.dir / "sysd", fakes.default_units(), exit=1)
        active, held = alerts.evaluate(await self.state.collector.collect(), self.cfg, fakes.NOW)
        self.assertTrue({"bank-sync", "service:broker", "disk:", "unit:", "backup:owui-stage"} <= held)


class AlertRoundTests(Harness):
    async def test_round_dedups_and_repeats(self) -> None:
        self.state.subs.add({"endpoint": "https://fcm.googleapis.com/a", "keys": {"p256dh": "x", "auth": "y"},
                             "label": "Pixel"})
        sent = await self.state.alert_round()
        self.assertEqual({a.id for a in sent}, {"bank-sync", "disk:pc:/mnt/share"})
        self.assertEqual(len(self.sender.calls), 1)
        payload = self.sender.calls[0][1]
        self.assertEqual(payload["tag"], "mc-alerts")
        self.assertEqual(payload["body"], "Disk over 90%; Bank sync failing")
        saved = json.loads((self.state_dir / "alerts.json").read_text())
        self.assertEqual(set(saved), {"bank-sync", "disk:pc:/mnt/share"})
        self.clock.now += 300
        self.assertEqual(await self.state.alert_round(), [])
        self.assertEqual(len(self.sender.calls), 1)
        self.clock.now += 24 * 3600  # the repeat interval; the ai-box backup has also gone stale by now
        again = {a.id for a in await self.state.alert_round()}
        self.assertEqual(again, {"bank-sync", "disk:pc:/mnt/share", "backup:owui-stage"})
        self.assertEqual(len(self.sender.calls), 2)

    async def test_failed_delivery_is_retried(self) -> None:
        ep = "https://fcm.googleapis.com/a"
        self.state.subs.add({"endpoint": ep, "keys": {"p256dh": "x", "auth": "y"}, "label": "Pixel"})
        self.sender.status[ep] = 503
        await self.state.alert_round()
        self.clock.now += 300
        self.sender.status[ep] = 201
        self.assertEqual(len(await self.state.alert_round()), 2)
        self.clock.now += 300
        self.assertEqual(await self.state.alert_round(), [])

    async def test_alerts_card_shows_since(self) -> None:
        await self.state.alert_round()
        self.clock.now += 7200
        _, body = await self.get("/api/cards")
        items = body["cards"]["alerts"]["items"]
        self.assertEqual(items[0]["value"], "since 2 h ago")
