"""A unit-kind backup after a reboot: systemd forgets the oneshot service's result and exit time, so the
Persistent=true timer's LastTriggerUSec (kept on disk) stands in. Live false alarm 2026-10-28 (ai-box RAM upgrade):
"Backup stale: Open WebUI (ai-box) (no run since boot)"."""

from __future__ import annotations

import unittest

from mission_control import alerts as alerts_mod
from mission_control import sources
from mission_control.config import ConfigError, from_dict

from . import fakes
from .harness import Harness

SERVICE, TIMER = "ai-openwebui-backup.service", "ai-openwebui-backup.timer"
H = 3600


def rebooted(trigger_age_h: float | None, **service: str) -> dict[str, dict[str, str]]:
    units = fakes.default_units()
    units[SERVICE] = {"LoadState": "loaded", "ActiveState": "inactive", "SubState": "dead", "Result": "success",
                      "NRestarts": "0", "ExecMainExitTimestamp": "", **service}  # no run since boot
    if trigger_age_h is not None:
        units[TIMER] = {"LoadState": "loaded", "ActiveState": "active", "SubState": "waiting", "Result": "success",
                        "LastTriggerUSec": f"@{int(fakes.NOW - trigger_age_h * H)}"}
    return units


class AfterReboot(Harness):
    async def round(self) -> tuple[list[alerts_mod.Alert], dict]:
        snap = await self.state.collector.collect()
        active, _ = alerts_mod.evaluate(snap, self.cfg, fakes.NOW)
        _, cards = await self.get("/api/cards")
        item = next(i for i in cards["cards"]["servers"]["items"] if i["label"] == "Backup: Open WebUI (ai-box)")
        return [a for a in active if a.id == "backup:owui-stage"], item

    async def test_recent_timer_is_unknown_not_ok(self) -> None:
        """Z7-1: the timer says a run started, not how it ended: unknown (held), never ok, never stale."""
        fakes.set_units(self.dir / "sysd", rebooted(5))
        alerts, item = await self.round()
        self.assertEqual(alerts, [])
        self.assertEqual((item["status"], item["value"]),
                         ("unknown", "unknown, result lost at reboot; timer last fired Wed 12:30 AM"))
        snap = self.state.collector.current
        _, held = alerts_mod.evaluate(snap, self.cfg, fakes.NOW)
        self.assertIn("backup:owui-stage", held)

    async def test_failed_then_reboot_keeps_the_alert(self) -> None:
        """Z's repro 1: failed 1 h ago (crit pushed), then a reboot: the alert stays raised until a real run."""
        await self.post("/api/push/subscribe", {"subscription": fakes.fake_subscription(1)})
        failed = rebooted(1.2, ActiveState="failed", Result="exit-code",
                          ExecMainExitTimestamp=f"@{int(fakes.NOW - H)}")
        fakes.set_units(self.dir / "sysd", failed)
        sent = await self.state.alert_round()
        self.assertIn("backup:owui-stage", [a.id for a in sent])
        fakes.set_units(self.dir / "sysd", rebooted(1.2))  # the reboot: the failure is forgotten by systemd
        for _ in range(8):  # 40 min of rounds: past the 30 min clearing time if it weren't held
            self.clock.now += 300
            self.assertNotIn("backup:owui-stage", [a.id for a in await self.state.alert_round()])
            self.assertEqual(self.state.alert_store.load()["backup:owui-stage"]["phase"], "active")
        _, b = await self.get("/api/briefing")
        self.assertTrue(any("Backup failed: Open WebUI (ai-box)" in i["label"] for i in b["sections"][2]["items"]))
        _, cards = await self.get("/api/cards")
        item = next(i for i in cards["cards"]["servers"]["items"] if i["label"] == "Backup: Open WebUI (ai-box)")
        self.assertEqual(item["status"], "unknown")
        self.assertNotIn("OK", item["value"])
        # a real run reports: success clears it (after the clearing time), as usual
        ok = rebooted(0.1, ExecMainExitTimestamp=f"@{int(self.clock.now - 60)}")
        fakes.set_units(self.dir / "sysd", ok)
        for _ in range(8):
            self.clock.now += 300
            await self.state.alert_round()
        self.assertNotIn("backup:owui-stage", self.state.alert_store.load())

    async def test_reboot_mid_run_is_unknown(self) -> None:
        """Z's repro 2: the backup was running when the box rebooted: unknown, not ok."""
        fakes.set_units(self.dir / "sysd", rebooted(0.2))  # fired 12 min ago, no result since boot
        alerts, item = await self.round()
        self.assertEqual(alerts, [])
        self.assertEqual(item["status"], "unknown")
        self.assertIn("result lost at reboot", item["value"])

    async def test_old_timer_is_stale(self) -> None:
        fakes.set_units(self.dir / "sysd", rebooted(40))  # max age 30 h
        alerts, item = await self.round()
        self.assertEqual([a.level for a in alerts], ["warn"])
        self.assertIn("stale", alerts[0].text)
        self.assertEqual(item["status"], "warn")
        self.assertIn("timer last fired over 30 h ago", item["value"])

    async def test_failure_after_reboot_wins(self) -> None:
        fakes.set_units(self.dir / "sysd", rebooted(2, ActiveState="failed", Result="exit-code"))
        alerts, item = await self.round()
        self.assertEqual([a.level for a in alerts], ["crit"])
        self.assertEqual(item["status"], "down")

    async def test_catch_up_run_in_progress(self) -> None:
        fakes.set_units(self.dir / "sysd", rebooted(0.01, ActiveState="activating", SubState="start"))
        alerts, item = await self.round()
        self.assertEqual(alerts, [])
        self.assertIn("running now", item["value"])

    async def test_no_timer_still_stale(self) -> None:
        fakes.set_units(self.dir / "sysd", rebooted(None))  # the timer isn't installed (not-found)
        alerts, item = await self.round()
        self.assertEqual([a.level for a in alerts], ["warn"])
        self.assertIn("no run since boot", item["value"])

    async def test_systemctl_asked_for_the_timer(self) -> None:
        self.assertIn("LastTriggerUSec", sources.UNIT_PROPS)
        fakes.set_units(self.dir / "sysd", rebooted(5))
        snap = await self.state.collector.collect()
        self.assertEqual(snap["sources"]["units"]["data"][TIMER]["last_trigger"], int(fakes.NOW - 5 * H))


class Unit(unittest.TestCase):
    def test_backup_from_unit(self) -> None:
        svc = {"load": "loaded", "active": "inactive", "result": "success", "exit_ts": None}
        timer = {"load": "loaded", "last_trigger": int(fakes.NOW + 2 * H)}
        self.assertEqual(sources.backup_from_unit(svc, 30, fakes.NOW, timer)["status"], "unknown")  # future
        ran = {**svc, "exit_ts": int(fakes.NOW - 40 * H)}
        recent = {"load": "loaded", "last_trigger": int(fakes.NOW - H)}
        self.assertEqual(sources.backup_from_unit(ran, 30, fakes.NOW, recent)["status"], "stale",
                         "a run since boot is the record; the timer doesn't override it")

    def test_config_timer(self) -> None:
        base = {"allowed_login": fakes.LOGIN}
        b = {"id": "x", "label": "X", "kind": "unit", "unit": SERVICE}
        self.assertEqual(from_dict({**base, "backups": [b]}).backups[0].timer, TIMER)
        self.assertEqual(from_dict({**base, "backups": [{**b, "timer": "other.timer"}]}).backups[0].timer, "other.timer")
        self.assertIsNone(from_dict({**base, "backups": [{**b, "timer": None}]}).backups[0].timer)
        self.assertIsNone(from_dict({**base, "backups": [{**b, "unit": "x.socket"}]}).backups[0].timer)
        for bad in ("x.service", "-x.timer", "x timer", 5):
            with self.assertRaises(ConfigError):
                from_dict({**base, "backups": [{**b, "timer": bad}]})
