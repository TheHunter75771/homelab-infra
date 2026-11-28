"""Ansible-level tests for role stacks' host image pins (docs/CONTRACTS.md §11.7.4), offline.

Runs the role's real task files (host_pins.yml, host_pins_cleanup.yml through host_pins_play.yml) and the real
playbooks/sync-pins.yml on localhost against fixture files in a temp dir, as root inside an unprivileged user
namespace (`unshare -r`), so the role's `owner: root` works without touching this machine. Covers the merge rules
(synced / repo behind / conflict / drop / malformed), check mode writing nothing, and a second run reporting
changed=0.

    python3 -m unittest discover -s roles/stacks/tests -v

Needs ansible-playbook (the repo's .venv, $ANSIBLE_PLAYBOOK or PATH) and `unshare -r`. Without them the tests are
skipped locally and fail in CI ($CI set).
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROLE = os.path.dirname(HERE)
REPO = os.path.dirname(os.path.dirname(ROLE))
PLAY = os.path.join(HERE, "host_pins_play.yml")
SYNC_PLAY = os.path.join(REPO, "playbooks", "sync-pins.yml")


def _find_ansible():
    for cand in (os.environ.get("ANSIBLE_PLAYBOOK"), os.path.join(REPO, ".venv", "bin", "ansible-playbook"),
                 shutil.which("ansible-playbook")):
        if cand and os.access(cand, os.X_OK):
            return cand
    return None


def _unshare_ok():
    try:
        return subprocess.run(["unshare", "-r", "true"], capture_output=True, timeout=20).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


ANSIBLE = _find_ansible()
MISSING = None if ANSIBLE and _unshare_ok() else "needs ansible-playbook and `unshare -r`"


def pin(repo, tag, digit):
    return "%s:%s@sha256:%s" % (repo, tag, digit * 64)


SONARR_REPO = "lscr.io/linuxserver/sonarr"
SONARR_OLD = pin(SONARR_REPO, "4.0.17.2952-ls312", "a")
SONARR_NEW = pin(SONARR_REPO, "4.0.20.3014-ls7", "b")
SONARR_OTHER = pin(SONARR_REPO, "4.0.19.3000-ls5", "c")
RADARR_REPO = "lscr.io/linuxserver/radarr"
RADARR = pin(RADARR_REPO, "6.1.1.10360-ls303", "d")
RADARR_OLD = pin(RADARR_REPO, "6.0.0.1-ls1", "8")
IMAGES = {"sonarr": SONARR_OLD, "radarr": RADARR}
APPS = {"sonarr": "Sonarr", "radarr": "Radarr"}


def record(**entries):
    return json.dumps({"version": 1, "pins": {
        "media/" + name: {"image": image, "previous": previous, "at": "2026-11-28T12:00:00Z"}
        for name, (image, previous) in entries.items()}}, indent=1) + "\n"


@unittest.skipIf(MISSING and not os.environ.get("CI"), MISSING or "")
class PlayTests(unittest.TestCase):
    def setUp(self):
        if MISSING:
            self.fail("CI: " + MISSING)
        self.tmp = tempfile.mkdtemp(prefix="stacks-pins-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.pins = os.path.join(self.tmp, "state", "pins.json")
        os.makedirs(os.path.dirname(self.pins))
        self.result = os.path.join(self.tmp, "result.json")
        self.cfg = os.path.join(self.tmp, "ansible.cfg")
        with open(self.cfg, "w") as fh:
            fh.write("[defaults]\nroles_path = %s\nretry_files_enabled = False\n" % os.path.join(REPO, "roles"))

    def run_play(self, playbook, extra, check=False, inventory="localhost,"):
        varfile = os.path.join(self.tmp, "vars.json")
        with open(varfile, "w") as fh:
            json.dump(extra, fh)
        env = dict(os.environ, ANSIBLE_CONFIG=self.cfg, ANSIBLE_NOCOLOR="1", ANSIBLE_FORCE_COLOR="0",
                   ANSIBLE_LOCAL_TEMP=os.path.join(self.tmp, "ansible-local"),
                   ANSIBLE_REMOTE_TEMP=os.path.join(self.tmp, "ansible-remote"),
                   ANSIBLE_STDOUT_CALLBACK="default", PYTHONDONTWRITEBYTECODE="1")
        cmd = ["unshare", "-r", ANSIBLE, "-i", inventory, "-c", "local", playbook, "-e", "@" + varfile]
        if check:
            cmd += ["--check", "--diff"]
        proc = subprocess.run(cmd, cwd=self.tmp, env=env, stdin=subprocess.DEVNULL, capture_output=True,
                              text=True, timeout=300)
        out = proc.stdout + proc.stderr
        recap = re.findall(r"^\S+\s*:\s*ok=(\d+)\s+changed=(\d+)\s+unreachable=(\d+)\s+failed=(\d+)", out, re.M)
        changed = sum(int(r[1]) for r in recap)
        return proc.returncode, out, changed

    def merge(self, content=None, check=False, drop=False):
        if content is not None and not os.path.exists(self.pins):
            with open(self.pins, "w") as fh:
                fh.write(content)
        extra = {"stacks_host_pins_file": self.pins, "images": IMAGES, "stacks_media_apps": APPS,
                 "stacks_drop_host_pins": drop, "test_result_file": self.result,
                 "ansible_python_interpreter": "{{ ansible_playbook_python }}"}
        if os.path.exists(self.result):
            os.unlink(self.result)
        rc, out, changed = self.run_play(PLAY, extra, check=check)
        result = None
        if os.path.exists(self.result):
            with open(self.result) as fh:
                result = json.load(fh)
        return rc, out, changed, result

    def read_pins(self):
        if not os.path.exists(self.pins):
            return None
        with open(self.pins) as fh:
            return fh.read()

    # --- merge rules ------------------------------------------------------------------------------------------
    def test_no_record(self):
        rc, out, changed, result = self.merge()
        self.assertEqual(rc, 0, out)
        self.assertEqual(changed, 0, out)
        self.assertEqual(result["images"], IMAGES)

    def test_synced_entry_removed_then_idempotent(self):
        rc, out, changed, result = self.merge(record(sonarr=(SONARR_OLD, SONARR_OTHER)))
        self.assertEqual(rc, 0, out)
        self.assertEqual(result["images"]["sonarr"], SONARR_OLD)
        self.assertIsNone(self.read_pins(), "a record with no entries left must be deleted")
        self.assertEqual(changed, 1, out)
        rc, out, changed, _ = self.merge()
        self.assertEqual((rc, changed), (0, 0), out)

    def test_check_mode_writes_nothing(self):
        content = record(sonarr=(SONARR_OLD, SONARR_OTHER), radarr=(RADARR, RADARR_OLD))
        rc, out, changed, result = self.merge(content, check=True)
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.read_pins(), content)
        self.assertEqual(sorted(result["remove"]), ["media/radarr", "media/sonarr"])

    def test_repo_behind_renders_host_pin_and_keeps_entry(self):
        content = record(sonarr=(SONARR_NEW, SONARR_OLD))
        rc, out, changed, result = self.merge(content)
        self.assertEqual(rc, 0, out)
        self.assertEqual(changed, 0, out)
        self.assertEqual(result["images"]["sonarr"], SONARR_NEW)
        self.assertEqual(self.read_pins(), content)
        self.assertIn("WARNING - host image pins", out)
        self.assertIn("ansible-playbook playbooks/sync-pins.yml", out)

    def test_mixed_rewrites_record_then_idempotent(self):
        rc, out, changed, result = self.merge(record(sonarr=(SONARR_NEW, SONARR_OLD), radarr=(RADARR, RADARR_OLD)))
        self.assertEqual(rc, 0, out)
        self.assertEqual(changed, 1, out)
        doc = json.loads(self.read_pins())
        self.assertEqual(list(doc["pins"]), ["media/sonarr"])
        self.assertEqual(oct(os.stat(self.pins).st_mode & 0o7777), "0o644")
        rc, out, changed, result = self.merge()
        self.assertEqual((rc, changed), (0, 0), out)
        self.assertEqual(result["images"]["sonarr"], SONARR_NEW)

    def test_conflict_fails_and_touches_nothing(self):
        content = record(sonarr=(SONARR_NEW, SONARR_OTHER))
        rc, out, changed, result = self.merge(content)
        self.assertNotEqual(rc, 0, out)
        self.assertIsNone(result, "the play must stop before anything after the merge")
        for part in ("Sonarr", SONARR_OLD, SONARR_NEW, "playbooks/sync-pins.yml", "-e stacks_drop_host_pins=true"):
            self.assertIn(part, out)
        self.assertEqual(self.read_pins(), content)

    def test_drop_repo_wins(self):
        rc, out, changed, result = self.merge(record(sonarr=(SONARR_NEW, SONARR_OTHER)), drop=True)
        self.assertEqual(rc, 0, out)
        self.assertEqual(result["images"]["sonarr"], SONARR_OLD)
        self.assertIsNone(self.read_pins())

    def test_moved_repository_fails_then_drop_repo_wins(self):
        moved = pin("ghcr.io/example/sonarr", "5.0", "3")
        content = record(sonarr=(moved, pin("ghcr.io/example/sonarr", "4.0", "4")))
        rc, out, changed, result = self.merge(content)
        self.assertNotEqual(rc, 0, out)
        self.assertIn("-e stacks_drop_host_pins=true", out)
        self.assertEqual(self.read_pins(), content)
        rc, out, changed, result = self.merge(drop=True)
        self.assertEqual(rc, 0, out)
        self.assertEqual(result["images"]["sonarr"], SONARR_OLD)
        self.assertIsNone(self.read_pins())
        self.assertIn("dropping the host pin", out)

    def test_malformed_fails(self):
        for content in ("", "{not json", json.dumps({"version": 2, "pins": {}})):
            with self.subTest(content=content):
                if os.path.exists(self.pins):
                    os.unlink(self.pins)
                rc, out, changed, result = self.merge(content)
                self.assertNotEqual(rc, 0, out)
                self.assertIsNone(result)
                self.assertEqual(self.read_pins(), content)

    def test_symlink_record_fails(self):
        target = os.path.join(self.tmp, "elsewhere.json")
        with open(target, "w") as fh:
            fh.write(record(sonarr=(SONARR_OLD, SONARR_OTHER)))
        os.symlink(target, self.pins)
        rc, out, changed, result = self.merge()
        self.assertNotEqual(rc, 0, out)
        self.assertIn("not a regular file", out)

    # --- playbooks/sync-pins.yml --------------------------------------------------------------------------------
    def sync(self, check=False):
        inv = os.path.join(self.tmp, "inventory.ini")
        with open(inv, "w") as fh:
            fh.write("[media_servers]\nfake-media ansible_connection=local\n")
        versions = os.path.join(self.tmp, "versions.yml")
        extra = {"stacks_host_pins_file": self.pins, "images": IMAGES, "stacks_media_apps": APPS,
                 "stacks_sync_pins_versions_file": versions, "ansible_become": False,
                 "ansible_python_interpreter": "{{ ansible_playbook_python }}"}
        return self.run_play(SYNC_PLAY, extra, check=check, inventory=inv)

    def write_versions(self):
        text = '---\nimages:\n  sonarr: "%s"\n  radarr: "%s"\n' % (SONARR_OLD, RADARR)
        with open(os.path.join(self.tmp, "versions.yml"), "w") as fh:
            fh.write(text)
        return text

    def read_versions(self):
        with open(os.path.join(self.tmp, "versions.yml")) as fh:
            return fh.read()

    def test_sync_adopts_host_pin(self):
        text = self.write_versions()
        content = record(sonarr=(SONARR_NEW, SONARR_OLD), radarr=(RADARR, RADARR_OLD))
        with open(self.pins, "w") as fh:
            fh.write(content)
        rc, out, changed = self.sync(check=True)
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.read_versions(), text, "check mode must not write versions.yml")
        rc, out, changed = self.sync()
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.read_versions(), text.replace(SONARR_OLD, SONARR_NEW))
        self.assertIn("Sonarr (images.sonarr)", out)
        self.assertEqual(self.read_pins(), content, "sync-pins never writes the host record")
        # with versions.yml caught up, a site.yml run sees the entry as synced (mirrored here with IMAGES)

    def test_sync_nothing(self):
        text = self.write_versions()
        rc, out, changed = self.sync()
        self.assertEqual(rc, 0, out)
        self.assertIn("nothing to sync", out)
        self.assertEqual(changed, 0, out)
        self.assertEqual(self.read_versions(), text)

    def test_sync_old_value_not_found_fails(self):
        with open(os.path.join(self.tmp, "versions.yml"), "w") as fh:
            fh.write("images: {}\n")
        with open(self.pins, "w") as fh:
            fh.write(record(sonarr=(SONARR_NEW, SONARR_OLD)))
        rc, out, changed = self.sync()
        self.assertNotEqual(rc, 0, out)
        self.assertIn("0 times", out)
        self.assertEqual(self.read_versions(), "images: {}\n")


if __name__ == "__main__":
    unittest.main()
