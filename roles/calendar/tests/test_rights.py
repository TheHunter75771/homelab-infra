"""(b) The rendered rights file (templates/rights.j2) against Radicale's own rights classes
(docs/CONTRACTS.md §14.1). Skipped when radicale isn't importable.

  - Accounts and anonymous requests get exactly what owner_only gave them, path for path, with the
    placeholder viewer and with the default (no viewers).
  - The viewer gets r on its listed calendars and nothing anywhere else: not the root, not a home (its
    own included), no writes, and no calendar it isn't given (lookalike paths included).
  - A login that is neither an account nor a viewer gets nothing (owner_only would have given it a home).
  - The rendered file is what the role's validate step accepts.
"""

from __future__ import annotations

import configparser
import importlib.util
import pathlib
import re
import tempfile
import unittest

import role_render as rr

HAVE_RADICALE = importlib.util.find_spec("radicale") is not None
ACCOUNTS = ["alice", "bob", "household"]
LISTED = ["/household/shared/", "/household/school/", "/alice/routines/"]
PATHS = [
    "/", "/alice/", "/bob/", "/household/", "/viewer/", "/mallory/",
    "/alice/routines/", "/alice/personal/", "/bob/personal/", "/household/shared/", "/household/school/",
    "/household/notes/", "/viewer/mine/", "/alice/routines/x.ics", "/household/shared/e.ics",
    "/alice/a/b/", "/household/shared/sub/", "/.well-known/caldav",
    # lookalikes of the listed paths: the viewer pattern is anchored and escaped
    "/household/sharedx/", "/xhousehold/shared/", "/household/shared2/", "/household/sh.red/",
    # sanitized first, as Radicale does for every request: "/alice/personal/", "/household/shared/"
    "/alice/routines/../personal/", "/household//shared/",
]


def sane(path: str) -> str:
    """What Radicale hands the rights backend: the sanitized request path."""
    from radicale import pathutils

    return pathutils.sanitize_path(path)


@unittest.skipUnless(HAVE_RADICALE, "radicale not installed (pip install radicale==<versions.yml pin> bcrypt)")
class Rights(unittest.TestCase):
    def rights(self, viewers: list | None = None):
        """from_file over the rendered rights.j2, and owner_only, both as Radicale builds them."""
        from radicale import config
        from radicale.rights import from_file, owner_only

        rr.require_tools()
        overrides = {} if viewers is None else {"calendar_viewers": viewers}
        text = rr.render("rights.j2", rr.load_vars(overrides))
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = pathlib.Path(tmp.name, "rights")
        path.write_text(text)

        def cfg(kind: str):
            c = config.load([])
            c.update({"auth": {"type": "htpasswd"}, "rights": {"type": kind, "file": str(path)}}, "test", privileged=True)
            return c

        return from_file.Rights(cfg("from_file")), owner_only.Rights(cfg("owner_only")), text

    def assert_accounts_as_owner_only(self, ff, oo) -> None:
        for user in [*ACCOUNTS, ""]:
            for p in PATHS:
                with self.subTest(user=user or "anonymous", path=p):
                    self.assertEqual(ff.authorization(user, sane(p)), oo.authorization(user, sane(p)))

    def test_accounts_get_exactly_owner_only(self) -> None:
        ff, oo, _ = self.rights()
        self.assert_accounts_as_owner_only(ff, oo)
        # What that is, spelled out (owner_only gives the home RW, not R).
        self.assertEqual([ff.authorization("alice", p) for p in ("/", "/alice/", "/alice/routines/", "/bob/")],
                         ["R", "RW", "rw", ""])

    def test_the_viewer_reads_its_calendars_and_nothing_else(self) -> None:
        ff, _, _ = self.rights()
        for p in PATHS:
            with self.subTest(path=p):
                self.assertEqual(ff.authorization("viewer", sane(p)), "r" if sane(p) in LISTED else "")

    def test_a_login_that_is_neither_gets_nothing(self) -> None:
        ff, _, _ = self.rights()
        for p in PATHS:
            with self.subTest(path=p):
                self.assertEqual(ff.authorization("mallory", sane(p)), "")

    def test_no_viewers_leaves_only_the_owner_rules(self) -> None:
        ff, oo, text = self.rights(viewers=[])
        parser = configparser.ConfigParser()
        parser.read_string(text)
        self.assertEqual(parser.sections(), ["owner-root", "owner-home", "owner-calendars"])
        self.assert_accounts_as_owner_only(ff, oo)
        for p in PATHS:
            with self.subTest(path=p):
                self.assertEqual(ff.authorization("viewer", sane(p)), "")

    def test_each_viewer_gets_one_rule_with_exactly_its_paths(self) -> None:
        viewers = [{"user": "viewer", "read": ["household/shared"]},
                   {"user": "kiosk-2", "read": ["alice/routines", "household/school"]}]
        ff, oo, text = self.rights(viewers)
        parser = configparser.ConfigParser()
        parser.read_string(text)
        self.assertEqual(parser.sections(), ["owner-root", "owner-home", "owner-calendars", "viewer-viewer", "viewer-kiosk-2"])
        self.assertEqual(dict(parser["viewer-kiosk-2"]),
                         {"user": "kiosk\\-2", "collection": "(?:alice/routines|household/school)", "permissions": "r"})
        self.assert_accounts_as_owner_only(ff, oo)
        self.assertEqual(ff.authorization("viewer", "/household/shared/"), "r")
        self.assertEqual(ff.authorization("viewer", "/household/school/"), "")
        self.assertEqual(ff.authorization("kiosk-2", "/household/school/"), "r")
        self.assertEqual(ff.authorization("kiosk-2", "/household/shared/"), "")

    def test_the_roles_validate_step_accepts_it(self) -> None:
        """The same checks as the template task's validate: ConfigParser, format(), compile, permissions."""
        _, _, text = self.rights()
        parser = configparser.ConfigParser()
        parser.read_string(text)
        for section in parser.sections():
            with self.subTest(section=section):
                re.compile(parser.get(section, "user").format())
                re.compile(parser.get(section, "collection").format(user="u"))
                self.assertIn(parser.get(section, "permissions"), ("R", "RW", "rw", "r"))


if __name__ == "__main__":
    unittest.main()
