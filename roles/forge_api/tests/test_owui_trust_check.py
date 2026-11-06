"""Unit tests for files/owui_trust_check.py (stdlib only; run by tests/run.sh, or `python3 -I -B <this file>`).
The playbook test covers the checker through verify.yml; these cover the edges: usage, unreadable databases (a
symlink, a FIFO, a directory, a wrong layout, bad JSON), Open WebUI's own truthiness (Python bool() for
permissions, JavaScript for the iframe setting), the S-2 checks, the read-only open, the timeout, and that the
output never carries settings beyond the checks."""
import importlib.util
import io
import json
import os
import signal
import sqlite3
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
SPEC = importlib.util.spec_from_file_location("owui_trust_check", os.path.join(HERE, "..", "files", "owui_trust_check.py"))
mod = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mod)

NOAH = "11111111-2222-3333-4444-555555555555"
NAMES = ["admins", "default-permissions", "group-permissions", "tool-write-grants", "tool-owners", "iframe-same-origin"]


def make_db(path, users, rows, groups=(), tools=(), grants=(), tables=("user", "config", "group", "tool", "access_grant")):
    c = sqlite3.connect(path)
    if "user" in tables:
        c.execute("CREATE TABLE user (id TEXT, role TEXT, settings JSON, email TEXT)")
        c.executemany("INSERT INTO user VALUES (?, ?, ?, 'x@example.invalid')",
                      [(u, r, json.dumps(s) if s is not None else None) for u, r, s in users])
    if "config" in tables:
        c.execute("CREATE TABLE config (key TEXT, value JSON)")
        c.executemany("INSERT INTO config VALUES (?, ?)", [(k, json.dumps(v)) for k, v in rows.items()])
    if "group" in tables:
        c.execute('CREATE TABLE "group" (id TEXT, name TEXT, permissions JSON, parent_group_id TEXT)')
        c.executemany('INSERT INTO "group" VALUES (?, ?, ?, NULL)', [(g, n, json.dumps(p)) for g, n, p in groups])
    if "tool" in tables:
        c.execute("CREATE TABLE tool (id TEXT, user_id TEXT, content TEXT)")
        c.executemany("INSERT INTO tool VALUES (?, ?, 'code')", list(tools))
    if "access_grant" in tables:
        c.execute("CREATE TABLE access_grant (id TEXT, resource_type TEXT, resource_id TEXT, principal_type TEXT, "
                  "principal_id TEXT, permission TEXT)")
        c.executemany("INSERT INTO access_grant VALUES (?, ?, ?, ?, ?, ?)",
                      [(str(i), *g) for i, g in enumerate(grants)])
    c.commit()
    c.close()


def run(db, approver=NOAH):
    out = io.StringIO()
    with redirect_stdout(out):
        rc = mod.main(["owui_trust_check.py", db, approver])
    return rc, json.loads(out.getvalue())


class TrustCheck(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "webui.db")

    def tearDown(self):
        self.tmp.cleanup()

    def failed(self, result):
        return [c["name"] for c in result["checks"] if not c["ok"]]

    def fresh(self, *args, **kwargs):
        if os.path.exists(self.db):
            os.remove(self.db)
        make_db(self.db, *args, **kwargs)
        return run(self.db)[1]

    def test_usage(self):
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(mod.main(["x", self.db]), 2)
            self.assertEqual(mod.main(["x", self.db, ""]), 2)

    def test_missing_database_is_unreadable_and_not_created(self):
        rc, r = run(self.db)
        self.assertEqual(rc, 2)
        self.assertFalse(r["ok"])
        self.assertFalse(os.path.exists(self.db))

    def test_symlink_fifo_and_directory_are_refused(self):
        real = os.path.join(self.tmp.name, "real.db")
        make_db(real, [(NOAH, "admin", None)], {})
        os.symlink(real, self.db)
        rc, r = run(self.db)
        self.assertEqual(rc, 2)
        self.assertIn("cannot open", r["error"])  # O_NOFOLLOW: ELOOP
        os.remove(self.db)
        os.mkfifo(self.db)
        start = time.monotonic()
        rc, r = run(self.db)  # O_NONBLOCK: no writer, no hang
        self.assertLess(time.monotonic() - start, 5)
        self.assertEqual((rc, "not a regular file" in r["error"]), (2, True))
        os.remove(self.db)
        os.mkdir(self.db)
        self.assertEqual(run(self.db)[0], 2)

    def test_wrong_layout_is_unreadable(self):
        for missing in ("user", "group", "tool", "access_grant"):
            with self.subTest(missing=missing):
                if os.path.exists(self.db):
                    os.remove(self.db)
                make_db(self.db, [(NOAH, "admin", None)], {},
                        tables=tuple(t for t in ("user", "config", "group", "tool", "access_grant") if t != missing))
                self.assertEqual(run(self.db)[0], 2)

    def test_bad_json_is_unreadable(self):
        make_db(self.db, [(NOAH, "admin", None)], {})
        c = sqlite3.connect(self.db)
        c.execute("INSERT INTO config VALUES ('user.permissions', '{not json')")
        c.commit()
        c.close()
        self.assertEqual(run(self.db)[0], 2)

    def test_timeout(self):
        old = mod.RUN_TIMEOUT
        mod.RUN_TIMEOUT = 1
        real_read = mod.read
        mod.read = lambda path: time.sleep(5)
        try:
            start = time.monotonic()
            rc, r = run(self.db)
            self.assertEqual(rc, 2)
            self.assertIn("timed out", r["error"])
            self.assertLess(time.monotonic() - start, 4)
            self.assertEqual(signal.alarm(0), 0)  # the alarm is cleared afterwards
        finally:
            mod.RUN_TIMEOUT = old
            mod.read = real_read

    def test_defaults_pass(self):
        make_db(self.db, [(NOAH, "admin", None), ("u", "user", {"ui": {}})], {})
        rc, r = run(self.db)
        self.assertEqual((rc, r["ok"], self.failed(r)), (0, True, []))
        self.assertEqual([c["name"] for c in r["checks"]], NAMES)
        self.assertEqual(list(mod.CHECKS), NAMES)

    def test_admins(self):
        r = self.fresh([(NOAH, "admin", None), ("eve", "admin", None)], {})
        self.assertEqual(self.failed(r), ["admins"])
        self.assertIn("eve", r["checks"][0]["detail"])
        self.assertEqual(self.failed(run(self.db, "someone-else")[1]), ["admins"])

    def test_permissions_use_python_bool(self):
        # S-3: has_permission returns bool(value), so a non-empty string is on, even "false".
        for value, on in ((True, True), ("true", True), ("false", True), ("0", True), (1, True), ([0], True),
                          (False, False), (0, False), ("", False), (None, False), ([], False), ({}, False)):
            with self.subTest(value=value):
                r = self.fresh([(NOAH, "admin", None)], {"user.permissions": {"workspace": {"tools": value}}})
                self.assertEqual(self.failed(r), ["default-permissions"] if on else [])

    def test_tools_export_and_functions(self):
        r = self.fresh([(NOAH, "admin", None)],
                       {"user.permissions": {"workspace": {"tools_export": True, "functions_import": True}}})
        self.assertEqual(self.failed(r), ["default-permissions"])
        self.assertIn("functions_import", r["checks"][1]["detail"])
        self.assertIn("tools_export", r["checks"][1]["detail"])

    def test_other_permissions_are_not_this_check(self):
        r = self.fresh([(NOAH, "admin", None)],
                       {"user.permissions": {"workspace": {"models": True, "knowledge": True}, "chat": {"controls": True}}},
                       groups=[("g1", "readers", {"workspace": {"knowledge": True}, "sharing": {"tools": True}})])
        self.assertEqual(self.failed(r), [])

    def test_group_permissions(self):
        # S-2: any group granting tool permissions fails; the default group is named in the detail.
        for key in ("tools", "tools_import", "tools_export", "functions"):
            with self.subTest(key=key):
                r = self.fresh([(NOAH, "admin", None)], {"ui.default_group_id": "g2"},
                               groups=[("g1", "plain", {}), ("g2", "everyone", {"workspace": {key: "yes"}})])
                self.assertEqual(self.failed(r), ["group-permissions"])
                self.assertIn("default group", r["checks"][2]["detail"])
                self.assertIn(key, r["checks"][2]["detail"])

    def test_tool_write_grants(self):
        # S-2: write on a tool, for a user, a group or everyone ("*"), fails; read grants and other resources don't.
        for principal in (("user", "eve"), ("group", "g1"), ("user", "*"), ("user", NOAH)):
            with self.subTest(principal=principal):
                r = self.fresh([(NOAH, "admin", None)], {}, tools=[("t1", NOAH)],
                               grants=[("tool", "t1", *principal, "write")])
                self.assertEqual(self.failed(r), ["tool-write-grants"])
        r = self.fresh([(NOAH, "admin", None)], {}, tools=[("t1", NOAH)],
                       grants=[("tool", "t1", "group", "g1", "read"), ("tool", "t1", "anyone", "*", "read"),
                               ("knowledge", "k1", "group", "g1", "write"), ("model", "m1", "user", "*", "write")])
        self.assertEqual(self.failed(r), [])

    def test_tool_owners(self):
        r = self.fresh([(NOAH, "admin", None), ("eve", "user", None)], {}, tools=[("t1", NOAH), ("t2", "eve")])
        self.assertEqual(self.failed(r), ["tool-owners"])
        self.assertIn("t2 (owner eve)", r["checks"][4]["detail"])

    def test_iframe_uses_javascript_truthiness(self):
        # S-3: the frontend reads `value ?? false`, then JS truthiness.
        for value, on in ((True, True), ("false", True), ("0", True), ([], True), ({}, True), (1, True),
                          (False, False), (0, False), (0.0, False), ("", False), (None, False)):
            with self.subTest(value=value):
                r = self.fresh([(NOAH, "admin", {"ui": {"iframeSandboxAllowSameOrigin": value}})], {})
                self.assertEqual(self.failed(r), ["iframe-same-origin"] if on else [])
                r = self.fresh([(NOAH, "admin", None)], {"ui.default_interface_settings": {"iframeSandboxAllowSameOrigin": value}})
                self.assertEqual(self.failed(r), ["iframe-same-origin"] if on else [])

    def test_read_only(self):
        make_db(self.db, [(NOAH, "admin", None)], {"user.permissions": {"workspace": {}}})

        def data():
            with open(self.db, "rb") as fh:
                return fh.read()
        before = data()
        os.chmod(self.db, 0o444)
        self.assertEqual(run(self.db)[0], 0)
        self.assertEqual(data(), before)

    def test_output_holds_no_emails(self):
        make_db(self.db, [(NOAH, "admin", None)], {})
        out = io.StringIO()
        with redirect_stdout(out):
            mod.main(["x", self.db, NOAH])
        self.assertNotIn("example.invalid", out.getvalue())


if __name__ == "__main__":
    result = unittest.main(exit=False, verbosity=1).result
    sys.exit(0 if result.wasSuccessful() else 1)
