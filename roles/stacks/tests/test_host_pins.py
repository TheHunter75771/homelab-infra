"""Unit tests for role stacks' host image pin merge (docs/CONTRACTS.md §11.7.4; stdlib only, no host needed).

    python3 -m unittest discover -s roles/stacks/tests -v

Pins below are made up (digests are repeated hex digits); repositories mirror the shape of the real ones.
"""

import base64
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "filter_plugins"))

import host_pins  # noqa: E402
from host_pins import (  # noqa: E402
    repository,
    stacks_host_pins_merge as merge,
    stacks_host_pins_sync_plan as sync_plan,
    stacks_replace_once as replace_once,
)


def pin(repo, tag, digit):
    return "%s:%s@sha256:%s" % (repo, tag, digit * 64)


SONARR_REPO = "lscr.io/linuxserver/sonarr"
SONARR_OLD = pin(SONARR_REPO, "4.0.17.2952-ls312", "a")
SONARR_NEW = pin(SONARR_REPO, "4.0.20.3014-ls7", "b")
SONARR_OTHER = pin(SONARR_REPO, "4.0.19.3000-ls5", "c")
RADARR = pin("lscr.io/linuxserver/radarr", "6.1.1.10360-ls303", "d")
SEERR = pin("fallenbagel/jellyseerr", "2.7.3", "e")

APPS = {"sonarr": "Sonarr", "radarr": "Radarr", "jellyseerr": "Jellyseerr"}
IMAGES = {"sonarr": SONARR_OLD, "radarr": RADARR, "jellyseerr": SEERR,
          "cloudflared": pin("cloudflare/cloudflared", "2026.5.0", "f")}


def record(**entries):
    pins = {}
    for name, (image, previous) in entries.items():
        pins["media/" + name] = {"image": image, "previous": previous, "at": "2026-11-28T12:00:00Z"}
    return json.dumps({"version": 1, "pins": pins})


class Repository(unittest.TestCase):
    def test_tag_and_digest(self):
        self.assertEqual(repository(SONARR_OLD), SONARR_REPO)

    def test_digest_only_and_tag_only(self):
        self.assertEqual(repository("repo/app@sha256:" + "1" * 64), "repo/app")
        self.assertEqual(repository("repo/app:1.2"), "repo/app")
        self.assertEqual(repository("app"), "app")

    def test_registry_port_is_not_a_tag(self):
        self.assertEqual(repository("registry.example:5000/team/app:1.2@sha256:" + "1" * 64), "registry.example:5000/team/app")
        self.assertEqual(repository("registry.example:5000/team/app"), "registry.example:5000/team/app")

    def test_empty(self):
        self.assertIsNone(repository(""))
        self.assertIsNone(repository(None))


class NoRecord(unittest.TestCase):
    def test_absent_file_renders_the_repo(self):
        r = merge(None, IMAGES, APPS)
        self.assertEqual(r["errors"], [])
        self.assertEqual(r["warnings"], [])
        self.assertEqual(r["images"], {k: IMAGES[k] for k in APPS})
        self.assertEqual(r["remove"], [])

    def test_empty_pins_object(self):
        r = merge(json.dumps({"version": 1, "pins": {}}), IMAGES, APPS)
        self.assertEqual((r["errors"], r["remove"], r["delete"]), ([], [], False))

    def test_missing_repo_image_is_an_error(self):
        r = merge(None, {"sonarr": SONARR_OLD}, APPS)
        self.assertTrue(any("images.radarr" in e for e in r["errors"]))


class Rules(unittest.TestCase):
    def test_synced_entry_is_removed_and_file_deleted(self):
        r = merge(record(sonarr=(SONARR_OLD, pin(SONARR_REPO, "4.0.1-ls1", "9"))), IMAGES, APPS)
        self.assertEqual(r["errors"], [])
        self.assertEqual(r["synced"], ["media/sonarr"])
        self.assertEqual(r["remove"], ["media/sonarr"])
        self.assertTrue(r["delete"])
        self.assertEqual(r["content"], "")
        self.assertEqual(r["images"]["sonarr"], SONARR_OLD)
        self.assertEqual(r["warnings"], [])

    def test_repo_behind_renders_the_host_pin(self):
        r = merge(record(sonarr=(SONARR_NEW, SONARR_OLD)), IMAGES, APPS)
        self.assertEqual(r["errors"], [])
        self.assertEqual(r["behind"], ["media/sonarr"])
        self.assertEqual(r["images"]["sonarr"], SONARR_NEW)
        self.assertEqual(r["images"]["radarr"], RADARR)
        self.assertEqual(r["remove"], [])
        self.assertEqual(len(r["warnings"]), 1)
        self.assertIn("Sonarr", r["warnings"][0])
        self.assertIn("ansible-playbook playbooks/sync-pins.yml", r["warnings"][0])

    def test_conflict_fails_naming_app_pins_and_ways_out(self):
        r = merge(record(sonarr=(SONARR_NEW, SONARR_OTHER)), IMAGES, APPS)
        self.assertEqual(len(r["errors"]), 1)
        msg = r["errors"][0]
        for part in ("Sonarr", SONARR_OLD, SONARR_NEW, "playbooks/sync-pins.yml", "-e stacks_drop_host_pins=true",
                     "not touched"):
            self.assertIn(part, msg)
        self.assertEqual(r["remove"], [])
        self.assertEqual(r["images"]["sonarr"], SONARR_OLD)

    def test_drop_repo_wins_and_entries_removed(self):
        content = record(sonarr=(SONARR_NEW, SONARR_OTHER), radarr=(pin("lscr.io/linuxserver/radarr", "6.2", "7"), RADARR))
        r = merge(content, IMAGES, APPS, drop=True)
        self.assertEqual(r["errors"], [])
        self.assertEqual(sorted(r["dropped"]), ["media/radarr", "media/sonarr"])
        self.assertEqual(sorted(r["remove"]), ["media/radarr", "media/sonarr"])
        self.assertTrue(r["delete"])
        self.assertEqual(r["images"]["sonarr"], SONARR_OLD)
        self.assertEqual(r["images"]["radarr"], RADARR)
        self.assertEqual(len(r["warnings"]), 2)

    def test_mixed_keeps_behind_and_rewrites(self):
        content = record(sonarr=(SONARR_NEW, SONARR_OLD), radarr=(RADARR, pin("lscr.io/linuxserver/radarr", "6.0", "8")))
        r = merge(content, IMAGES, APPS)
        self.assertEqual(r["errors"], [])
        self.assertEqual(r["remove"], ["media/radarr"])
        self.assertFalse(r["delete"])
        doc = json.loads(r["content"])
        self.assertEqual(doc["version"], 1)
        self.assertEqual(list(doc["pins"]), ["media/sonarr"])
        self.assertEqual(doc["pins"]["media/sonarr"]["image"], SONARR_NEW)
        # the rewritten record merges the same way again: nothing more to remove (idempotent)
        again = merge(r["content"], IMAGES, APPS)
        self.assertEqual((again["errors"], again["remove"], again["images"]["sonarr"]), ([], [], SONARR_NEW))

    def test_one_conflict_blocks_everything(self):
        content = record(sonarr=(SONARR_NEW, SONARR_OTHER), radarr=(RADARR, RADARR))
        r = merge(content, IMAGES, APPS)
        self.assertEqual(len(r["errors"]), 1)
        self.assertEqual(r["remove"], [])

    def test_base64_input(self):
        b64 = base64.b64encode(record(sonarr=(SONARR_NEW, SONARR_OLD)).encode()).decode()
        r = merge(b64, IMAGES, APPS, b64=True)
        self.assertEqual((r["errors"], r["images"]["sonarr"]), ([], SONARR_NEW))


class Matching(unittest.TestCase):
    def test_matched_by_repository_not_service_name(self):
        content = json.dumps({"version": 1, "pins": {"media/series": {
            "image": SONARR_NEW, "previous": SONARR_OLD, "at": "2026-11-28T12:00:00Z"}}})
        r = merge(content, IMAGES, APPS)
        self.assertEqual((r["errors"], r["images"]["sonarr"]), ([], SONARR_NEW))

    def test_unknown_service_ignored_with_warning(self):
        other = pin("example/unknown", "1.0", "1")
        content = json.dumps({"version": 1, "pins": {"media/unknown": {
            "image": other, "previous": pin("example/unknown", "0.9", "2"), "at": "x"}}})
        r = merge(content, IMAGES, APPS)
        self.assertEqual(r["errors"], [])
        self.assertEqual(r["ignored"], ["media/unknown"])
        self.assertEqual(len(r["warnings"]), 1)
        self.assertEqual(r["remove"], [])
        self.assertEqual(r["images"], {k: IMAGES[k] for k in APPS})

    def test_other_stack_ignored_with_warning(self):
        content = json.dumps({"version": 1, "pins": {"utils/sonarr": {
            "image": SONARR_NEW, "previous": SONARR_OLD, "at": "x"}}})
        r = merge(content, IMAGES, APPS)
        self.assertEqual((r["errors"], r["ignored"], r["images"]["sonarr"]), ([], ["utils/sonarr"], SONARR_OLD))

    def test_known_app_on_a_different_repository_fails(self):
        moved = pin("ghcr.io/example/sonarr", "5.0", "3")
        content = json.dumps({"version": 1, "pins": {"media/sonarr": {
            "image": moved, "previous": pin("ghcr.io/example/sonarr", "4.0", "4"), "at": "x"}}})
        r = merge(content, IMAGES, APPS)
        self.assertEqual(len(r["errors"]), 1)
        self.assertIn("Sonarr", r["errors"][0])

    def test_repository_shared_by_two_apps_fails(self):
        images = dict(IMAGES, radarr=pin(SONARR_REPO, "x", "5"))
        r = merge(record(sonarr=(SONARR_NEW, SONARR_OLD)), images, APPS)
        self.assertEqual(len(r["errors"]), 1)
        self.assertIn("more than one", r["errors"][0])

    # Reviewer M2: the drop flag must be a real way out of every case that names the app by service.
    MOVED = pin("ghcr.io/example/sonarr", "5.0", "3")
    MOVED_PREV = pin("ghcr.io/example/sonarr", "4.0", "4")

    def moved_record(self, name="media/sonarr"):
        return json.dumps({"version": 1, "pins": {name: {"image": self.MOVED, "previous": self.MOVED_PREV, "at": "x"}}})

    def assertDropped(self, r, name, key):
        self.assertEqual(r["errors"], [])
        self.assertEqual(r["dropped"], [name])
        self.assertEqual(r["remove"], [name])
        self.assertTrue(r["delete"])
        self.assertEqual(r["images"], {k: IMAGES[k] for k in APPS})
        self.assertEqual(len(r["warnings"]), 1)
        self.assertIn(APPS[key], r["warnings"][0])
        self.assertIn("dropping the host pin", r["warnings"][0])
        self.assertIn(IMAGES[key], r["warnings"][0])

    def test_moved_repository_error_names_the_drop_flag(self):
        r = merge(self.moved_record(), IMAGES, APPS)
        self.assertEqual(len(r["errors"]), 1)
        self.assertIn("-e stacks_drop_host_pins=true", r["errors"][0])
        self.assertEqual(r["remove"], [])

    def test_moved_repository_with_drop_repo_wins(self):
        self.assertDropped(merge(self.moved_record(), IMAGES, APPS, drop=True), "media/sonarr", "sonarr")

    def test_moved_repository_with_drop_keeps_other_entries(self):
        doc = json.loads(self.moved_record())
        doc["pins"]["media/radarr"] = {"image": pin("lscr.io/linuxserver/radarr", "6.2", "7"), "previous": RADARR, "at": "x"}
        r = merge(json.dumps(doc), IMAGES, APPS, drop=True)
        self.assertEqual(r["errors"], [])
        self.assertEqual(sorted(r["remove"]), ["media/radarr", "media/sonarr"])
        r = merge(json.dumps(doc), IMAGES, APPS)  # without drop: the moved repository still fails
        self.assertEqual(len(r["errors"]), 1)
        self.assertEqual(r["remove"], [])

    def test_shared_repository_without_drop_fails(self):
        images = dict(IMAGES, radarr=pin(SONARR_REPO, "x", "5"))
        r = merge(record(sonarr=(SONARR_NEW, SONARR_OLD)), images, APPS)
        self.assertEqual(len(r["errors"]), 1)
        self.assertIn("-e stacks_drop_host_pins=true", r["errors"][0])

    def test_shared_repository_with_drop_attributed_by_service(self):
        images = dict(IMAGES, radarr=pin(SONARR_REPO, "x", "5"))
        r = merge(record(sonarr=(SONARR_NEW, SONARR_OLD)), images, APPS, drop=True)
        self.assertEqual(r["errors"], [])
        self.assertEqual((r["dropped"], r["remove"]), (["media/sonarr"], ["media/sonarr"]))
        self.assertEqual(r["images"]["sonarr"], SONARR_OLD)
        self.assertEqual(r["images"]["radarr"], images["radarr"])

    def test_shared_repository_with_drop_not_attributable_still_fails(self):
        images = dict(IMAGES, radarr=pin(SONARR_REPO, "x", "5"))
        content = json.dumps({"version": 1, "pins": {"media/series": {
            "image": SONARR_NEW, "previous": SONARR_OLD, "at": "x"}}})
        r = merge(content, images, APPS, drop=True)
        self.assertEqual(len(r["errors"]), 1)
        self.assertNotIn("-e stacks_drop_host_pins=true", r["errors"][0])
        self.assertEqual(r["remove"], [])

    def test_other_apps_repository_without_drop_fails(self):
        content = json.dumps({"version": 1, "pins": {"media/radarr": {
            "image": SONARR_NEW, "previous": SONARR_OLD, "at": "x"}}})
        r = merge(content, IMAGES, APPS)
        self.assertEqual(len(r["errors"]), 1)
        self.assertIn("-e stacks_drop_host_pins=true", r["errors"][0])

    def test_other_apps_repository_with_drop_repo_wins(self):
        content = json.dumps({"version": 1, "pins": {"media/radarr": {
            "image": SONARR_NEW, "previous": SONARR_OLD, "at": "x"}}})
        self.assertDropped(merge(content, IMAGES, APPS, drop=True), "media/radarr", "radarr")

    def test_sync_plan_still_refuses_moved_repository(self):
        self.assertEqual(len(sync_plan(self.moved_record(), IMAGES, APPS)["errors"]), 1)

    def test_non_media_key_is_never_matched(self):
        cf = pin("cloudflare/cloudflared", "2026.6.0", "6")
        content = json.dumps({"version": 1, "pins": {"media/cloudflared": {
            "image": cf, "previous": IMAGES["cloudflared"], "at": "x"}}})
        r = merge(content, IMAGES, APPS)
        self.assertEqual((r["errors"], r["ignored"]), ([], ["media/cloudflared"]))
        self.assertNotIn("cloudflared", r["images"])


class Malformed(unittest.TestCase):
    def assertMalformed(self, content, fragment, b64=False):
        r = merge(content, IMAGES, APPS, b64=b64)
        self.assertTrue(r["errors"], "expected an error for %r" % (content,))
        self.assertTrue(any(fragment in e for e in r["errors"]), r["errors"])
        self.assertEqual(r["remove"], [])
        self.assertEqual(r["images"], {k: IMAGES[k] for k in APPS})
        # never silently ignored by the sync playbook either
        self.assertTrue(sync_plan(content, IMAGES, APPS, b64=b64)["errors"])

    def test_empty_file(self):
        self.assertMalformed("", "empty")
        self.assertMalformed("  \n", "empty")

    def test_not_json(self):
        self.assertMalformed("{version: 1", "not valid JSON")

    def test_not_an_object(self):
        self.assertMalformed("[]", "JSON object")

    def test_wrong_or_missing_version(self):
        self.assertMalformed(json.dumps({"version": 2, "pins": {}}), "version 2")
        self.assertMalformed(json.dumps({"version": "1", "pins": {}}), "version \"1\"")
        self.assertMalformed(json.dumps({"version": True, "pins": {}}), "version true")
        self.assertMalformed(json.dumps({"pins": {}}), "version null")

    def test_missing_pins(self):
        self.assertMalformed(json.dumps({"version": 1}), "no \"pins\"")
        self.assertMalformed(json.dumps({"version": 1, "pins": []}), "no \"pins\"")

    def test_duplicate_keys(self):
        self.assertMalformed('{"version": 1, "version": 1, "pins": {}}', "duplicate")

    def test_bad_entry_name(self):
        self.assertMalformed(json.dumps({"version": 1, "pins": {"sonarr": {}}}), "<stack>/<service>")

    def test_bad_pins_in_entry(self):
        base = {"image": SONARR_NEW, "previous": SONARR_OLD, "at": "x"}
        for field, value in (("image", "lscr.io/linuxserver/sonarr:latest"), ("previous", None),
                             ("image", SONARR_NEW[:-1]), ("image", SONARR_NEW.upper())):
            entry = dict(base, **{field: value})
            self.assertMalformed(json.dumps({"version": 1, "pins": {"media/sonarr": entry}}), field)

    def test_missing_at(self):
        entry = {"image": SONARR_NEW, "previous": SONARR_OLD}
        self.assertMalformed(json.dumps({"version": 1, "pins": {"media/sonarr": entry}}), "\"at\"")

    def test_image_and_previous_different_repositories(self):
        entry = {"image": SONARR_NEW, "previous": RADARR, "at": "x"}
        self.assertMalformed(json.dumps({"version": 1, "pins": {"media/sonarr": entry}}), "different repositories")

    def test_bad_base64_and_not_utf8(self):
        self.assertMalformed("@@@", "base64", b64=True)
        self.assertMalformed(base64.b64encode(b"\xff\xfe").decode(), "UTF-8", b64=True)


class SyncPlan(unittest.TestCase):
    def test_behind_and_conflict_are_both_adopted(self):
        content = record(sonarr=(SONARR_NEW, SONARR_OTHER), radarr=(RADARR, pin("lscr.io/linuxserver/radarr", "6.0", "8")))
        r = sync_plan(content, IMAGES, APPS)
        self.assertEqual(r["errors"], [])
        self.assertEqual([(c["key"], c["old"], c["new"]) for c in r["changes"]], [("sonarr", SONARR_OLD, SONARR_NEW)])
        self.assertIn("Sonarr", r["changes"][0]["line"])

    def test_nothing_to_sync(self):
        self.assertEqual(sync_plan(None, IMAGES, APPS), {"errors": [], "warnings": [], "changes": []})
        self.assertEqual(sync_plan(record(radarr=(RADARR, RADARR)), IMAGES, APPS)["changes"], [])


class ReplaceOnce(unittest.TestCase):
    TEXT = 'images:\n  sonarr: "%s"\n  radarr: "%s"\n' % (SONARR_OLD, RADARR)

    def test_replaces_exact_value(self):
        changes = [{"key": "sonarr", "old": SONARR_OLD, "new": SONARR_NEW}]
        r = replace_once(self.TEXT, changes)
        self.assertEqual(r["errors"], [])
        self.assertEqual(r["text"], self.TEXT.replace(SONARR_OLD, SONARR_NEW))
        self.assertTrue(r["text"].endswith("\n"))

    def test_missing_or_repeated_value_fails_and_changes_nothing(self):
        changes = [{"key": "sonarr", "old": SONARR_OLD, "new": SONARR_NEW},
                   {"key": "jellyseerr", "old": SEERR, "new": "x"}]
        r = replace_once(self.TEXT, changes)
        self.assertEqual(len(r["errors"]), 1)
        self.assertIn("0 times", r["errors"][0])
        self.assertEqual(r["text"], self.TEXT)
        r = replace_once(self.TEXT + "# old: %s\n" % SONARR_OLD, changes[:1])
        self.assertIn("2 times", r["errors"][0])


class Module(unittest.TestCase):
    def test_filters_registered(self):
        self.assertEqual(sorted(host_pins.FilterModule().filters()),
                         ["stacks_host_pins_merge", "stacks_host_pins_sync_plan", "stacks_replace_once"])


if __name__ == "__main__":
    unittest.main()
