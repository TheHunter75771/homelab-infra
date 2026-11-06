"""Settings (calendar_view/config.py) and the --check-config entry point: strict keys and types, loopback
only, and no error or repr that ever holds the password."""

from __future__ import annotations

import contextlib
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from calendar_view.__main__ import main
from calendar_view.config import ConfigError, load, validate

from .helpers import APP, PASSWORD, config

GOOD = json.loads((APP / "config.example.json").read_text())


def with_(**changes) -> dict:
    raw = json.loads(json.dumps(GOOD))
    raw.update(changes)
    return raw


class Validate(unittest.TestCase):
    def test_the_example_config(self) -> None:
        cfg = validate(GOOD)
        self.assertEqual(cfg.base_url, "http://127.0.0.1:5232")
        self.assertEqual((cfg.user, cfg.timezone, cfg.days, cfg.listen_host, cfg.listen_port),
                         ("viewer", "America/New_York", 7, "127.0.0.1", 5233))
        self.assertEqual([c.path for c in cfg.calendars], ["household/shared", "household/school", "alice/routines"])
        self.assertEqual(str(cfg.tz), "America/New_York")

    def test_normalising(self) -> None:
        cfg = validate(with_(base_url="http://127.0.0.1:5232/",
                             calendars=[{"path": "household/shared", "label": "  Shared ", "color": "#AABBCC"}]))
        self.assertEqual(cfg.base_url, "http://127.0.0.1:5232")
        self.assertEqual((cfg.calendars[0].label, cfg.calendars[0].color), ("Shared", "#aabbcc"))
        self.assertEqual(validate(with_(base_url="http://[::1]:5232", listen_host="::1")).listen_host, "::1")

    def test_the_password_never_shows(self) -> None:
        cfg = config()
        self.assertNotIn(PASSWORD, repr(cfg))
        self.assertNotIn(PASSWORD, str(cfg))

    def test_bad_settings(self) -> None:
        cal = GOOD["calendars"][0]
        bad = {
            "not an object": [],
            "missing key": {k: v for k, v in GOOD.items() if k != "timezone"},
            "unknown key": with_(proxy="http://192.0.2.1:3128"),
            "https base": with_(base_url="https://127.0.0.1:5232"),
            "remote base": with_(base_url="http://192.0.2.10:5232"),
            "named base": with_(base_url="http://localhost:5232"),
            "base with path": with_(base_url="http://127.0.0.1:5232/alice/"),
            "base with credentials": with_(base_url="http://viewer:x@127.0.0.1:5232"),
            "base without port": with_(base_url="http://127.0.0.1"),
            "base bad port": with_(base_url="http://127.0.0.1:99999"),
            "user": with_(user="Viewer"),
            "user too short": with_(user="v"),
            "empty password": with_(password=""),
            "password with a newline": with_(password="abc\ndef-0123456789"),
            "password not text": with_(password=12345678901234567),
            "unknown zone": with_(timezone="Mars/Olympus_Mons"),
            "zone path": with_(timezone="../../etc/passwd"),
            "days 0": with_(days=0),
            "days 15": with_(days=15),
            "days bool": with_(days=True),
            "days text": with_(days="7"),
            "listen on all": with_(listen_host="0.0.0.0"),
            "listen on lan": with_(listen_host="192.0.2.5"),
            "listen by name": with_(listen_host="localhost"),
            "port": with_(listen_port=70000),
            "no calendars": with_(calendars=[]),
            "too many calendars": with_(calendars=[{**cal, "path": f"household/c{i}"} for i in range(13)]),
            "calendar extra key": with_(calendars=[{**cal, "url": "http://192.0.2.1/"}]),
            "calendar path traversal": with_(calendars=[{**cal, "path": "../alice/routines"}]),
            "calendar path one part": with_(calendars=[{**cal, "path": "household"}]),
            "calendar path three parts": with_(calendars=[{**cal, "path": "household/shared/x"}]),
            "calendar color name": with_(calendars=[{**cal, "color": "red"}]),
            "calendar color css": with_(calendars=[{**cal, "color": "#fff;background:url(x)"}]),
            "calendar label empty": with_(calendars=[{**cal, "label": "  "}]),
            "calendar label long": with_(calendars=[{**cal, "label": "x" * 41}]),
            "calendar label control": with_(calendars=[{**cal, "label": "Sha\x1bred"}]),
            "same path twice": with_(calendars=[cal, {**cal, "label": "Again"}]),
        }
        for what, raw in bad.items():
            with self.subTest(what), self.assertRaises(ConfigError) as caught:
                validate(raw)
            self.assertNotIn(GOOD["password"], str(caught.exception))


class LoadAndCheck(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="calendar-view-config-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def write(self, text: str) -> str:
        path = self.tmp / "config.json"
        path.write_text(text)
        return str(path)

    def test_load(self) -> None:
        self.assertEqual(load(self.write(json.dumps(GOOD))).user, "viewer")
        with self.assertRaises(ConfigError):
            load(str(self.tmp / "missing.json"))

    def test_invalid_json_never_quotes_the_file(self) -> None:
        # A broken file right next to the password: the error gives the line only.
        path = self.write('{\n  "password": "' + PASSWORD + '" "oops"\n}')
        with self.assertRaises(ConfigError) as caught:
            load(path)
        self.assertIn("line 2", str(caught.exception))
        self.assertNotIn(PASSWORD, str(caught.exception))

    def test_check_config(self) -> None:
        good, err = self.write(json.dumps(GOOD)), io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(main(["--check-config", good]), 0)
        self.assertEqual(err.getvalue(), "")
        bad = self.write(json.dumps(with_(listen_host="0.0.0.0", password=PASSWORD)))
        with contextlib.redirect_stderr(err):
            self.assertEqual(main(["--check-config", bad]), 2)
        self.assertIn("listen_host", err.getvalue())
        self.assertNotIn(PASSWORD, err.getvalue())

    def test_check_config_needs_only_the_standard_library(self) -> None:
        """The role validates the rendered file with this before writing it; it must not import icalendar."""
        good = self.write(json.dumps(GOOD))
        code = ("import sys; sys.modules['icalendar'] = None; sys.modules['recurring_ical_events'] = None; "
                "from calendar_view.__main__ import main; sys.exit(main(['--check-config', sys.argv[1]]))")
        r = subprocess.run([sys.executable, "-I", "-B", "-c", f"import sys; sys.path.insert(0, {str(APP)!r}); {code}", good],
                           capture_output=True, text=True, timeout=60, check=False)
        self.assertEqual((r.returncode, r.stderr), (0, ""))
        launcher = subprocess.run([sys.executable, "-I", "-B", str(APP / "calendar-view"), "--check-config", good],
                                  capture_output=True, text=True, timeout=60, check=False)
        self.assertEqual((launcher.returncode, launcher.stderr), (0, ""))


if __name__ == "__main__":
    unittest.main()
