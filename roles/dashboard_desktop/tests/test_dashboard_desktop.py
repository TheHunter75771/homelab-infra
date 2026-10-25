#!/usr/bin/env python3
"""Tests for files/homelab-dashboard-desktop: the navigation policy (which URLs load in the
window, which go to the browser, which are dropped), app.conf reading and writing, the key map
and the launcher template. No display, no GTK: the script keeps gi out of these helpers.

  python3 -m unittest discover -s roles/dashboard_desktop/tests -v
"""
import configparser
import importlib.machinery
import importlib.util
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPT = HERE.parent / "files" / "homelab-dashboard-desktop"
TEMPLATE = HERE.parent / "templates" / "homelab-dashboard.desktop.j2"
ICONS = HERE.parents[2] / "apps" / "dashboard" / "web" / "public" / "icons"


def load_script():
    loader = importlib.machinery.SourceFileLoader("homelab_dashboard_desktop", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = module  # dataclasses look the module up while it loads
    loader.exec_module(module)
    return module


app = load_script()
FQDN = "box.tailnet-name.ts.example"  # placeholder; the real name only lives in host_vars
DASH = app.https_origin(f"https://{FQDN}/")


class DashboardOrigin(unittest.TestCase):
    def test_https_host_on_443(self):
        self.assertEqual(DASH, ("https", FQDN, 443))
        for url in (f"https://{FQDN}", f"https://{FQDN}:443/", f"https://{FQDN}/drive?x=1#top", f"HTTPS://{FQDN.upper()}/"):
            with self.subTest(url=url):
                self.assertEqual(app.https_origin(url), DASH)

    def test_not_a_dashboard_url(self):
        for url in (
            "",
            None,
            f"http://{FQDN}/",
            f"https://{FQDN}:8444/",
            f"https://{FQDN}:8443/",
            f"https://{FQDN}:80/",
            f"https://{FQDN}:0/",
            "https:///path",
            f"https:{FQDN}/",
            f"https://user@{FQDN}/",
            f"https://@{FQDN}/",
            f"https://{FQDN}:99999/",
            f" https://{FQDN}/",
            f"https://{FQDN}\\",
        ):
            with self.subTest(url=url):
                self.assertIsNone(app.https_origin(url))


class Classify(unittest.TestCase):
    def assertVerdict(self, verdict, *urls):
        for url in urls:
            with self.subTest(url=url):
                self.assertEqual(app.classify(url, DASH), verdict)

    def test_same_origin_loads_in_the_window(self):
        self.assertVerdict(
            app.LOAD,
            f"https://{FQDN}/",
            f"https://{FQDN}/apps/sonarr?tab=log#x",
            f"https://{FQDN}:443/drive",
            f"https://{FQDN.upper()}/",
        )

    def test_other_ports_and_plain_http_of_the_same_host_go_to_the_browser(self):
        self.assertVerdict(
            app.EXTERNAL,
            f"https://{FQDN}:8444/sonarr/",
            f"https://{FQDN}:8443/",
            f"https://{FQDN}:8080/",
            f"http://{FQDN}/",
            f"http://{FQDN}:443/",
        )

    def test_other_sites_go_to_the_browser(self):
        self.assertVerdict(
            app.EXTERNAL,
            "https://github.com/owner/repo/blob/main/docs/runbooks/cloudflare.md",
            "http://example.org/",
            "https://[2001:db8::1]:8443/",
        )

    def test_lookalike_hosts_are_not_the_dashboard(self):
        self.assertVerdict(
            app.EXTERNAL,
            f"https://{FQDN}.evil.example/",
            f"https://evil{FQDN}/",
            f"https://evil-{FQDN}/",
            f"https://sub.{FQDN}/",
            f"https://{FQDN}./",
            f"https://{FQDN[:-1]}/",
            f"https://{FQDN}%2eevil.example/",
            f"https://{FQDN}\u3002evil.example/",
            f"https://{FQDN.replace('.', '-')}/",
        )

    def test_userinfo_tricks_are_not_the_dashboard(self):
        self.assertVerdict(
            app.EXTERNAL,
            f"https://{FQDN}@evil.example/",
            f"https://{FQDN}:443@evil.example/",
            f"https://evil.example@{FQDN}/",
            f"https://user:pass@{FQDN}/",
            f"https://{FQDN}@{FQDN}/",
        )

    def test_other_schemes_are_dropped(self):
        self.assertVerdict(
            app.DROP,
            "file:///etc/passwd",
            "FILE:///etc/passwd",
            "javascript:alert(1)",
            "JavaScript:alert(1)",
            "data:text/html,<title>x</title>",
            "DATA:text/html;base64,PHRpdGxlPg==",
            f"blob:https://{FQDN}/0f0e",
            "about:blank",
            "view-source:https://example.org/",
            "ftp://example.org/x",
            "mailto:someone@example.org",
            "steam://run/1",
            "vbscript:msgbox",
            "",
            "/relative/path",
            "//evil.example/protocol-relative",
            None,
        )

    def test_ambiguous_urls_are_dropped(self):
        self.assertVerdict(
            app.DROP,
            f"https://{FQDN}\\@evil.example/",
            f"https://evil.example\\@{FQDN}/",
            f"https://evil.example\\{FQDN}/",
            f"https://{FQDN}/ x",
            f"https://{FQDN}\t.evil.example/",
            f"https://{FQDN}\n/",
            "java\tscript:alert(1)",
            f"https://{FQDN}\x00.evil.example/",
            f"https://{FQDN}:99999/",
            f"https://{FQDN}:http/",
            "https://[::1/",
        )

    def test_without_a_dashboard_nothing_loads(self):
        self.assertEqual(app.classify(f"https://{FQDN}/", None), app.EXTERNAL)
        self.assertEqual(app.classify("file:///etc/passwd", None), app.DROP)

    def test_describe_hides_paths_queries_and_credentials(self):
        self.assertEqual(app.describe("https://user:pw@example.org/secret?token=1"), "https://example.org")
        self.assertEqual(app.describe("javascript:alert(document.cookie)"), "a javascript: URL")
        self.assertEqual(app.describe(None), "an unparsable URL")


class FakeAction:
    """The two facts WebKit gives about a navigation: was it a user gesture, and its type."""

    def __init__(self, gesture, navigation_type="LINK_CLICKED"):
        self.gesture, self.navigation_type = gesture, navigation_type

    def is_user_gesture(self):
        return self.gesture

    def get_navigation_type(self):
        return self.navigation_type


class Routing(unittest.TestCase):
    """Navigations the window does not take itself, and downloads (Reviewer finding: a script's
    a.click() is LINK_CLICKED to WebKit and must not reach the browser)."""

    EVIL = "https://evil.example/landing"

    def route(self, url, action):
        return app.route(url, DASH, gesture=app.is_user_action(action))

    def test_only_a_user_gesture_counts(self):
        self.assertFalse(app.is_user_action(FakeAction(False, "LINK_CLICKED")))
        self.assertFalse(app.is_user_action(FakeAction(False, "OTHER")))
        self.assertTrue(app.is_user_action(FakeAction(True, "LINK_CLICKED")))
        self.assertTrue(app.is_user_action(FakeAction(True, "OTHER")))

    def test_script_click_on_a_foreign_link_is_dropped(self):
        # <script>a.click()</script>: WebKit reports LINK_CLICKED, no user gesture.
        self.assertEqual(self.route(self.EVIL, FakeAction(False, "LINK_CLICKED")), app.DROP)

    def test_dispatched_mouse_event_on_a_foreign_link_is_dropped(self):
        # a.dispatchEvent(new MouseEvent('click')): the same facts as a.click().
        self.assertEqual(self.route(f"https://{FQDN}:8444/sonarr/", FakeAction(False, "LINK_CLICKED")), app.DROP)

    def test_script_clicked_foreign_download_is_dropped(self):
        # <a download href=https://evil.example/...> clicked by script: WebKit either navigates
        # (no gesture) or starts a download (no gesture information at all). Both are dropped.
        self.assertEqual(self.route(f"{self.EVIL}.zip", FakeAction(False, "LINK_CLICKED")), app.DROP)
        self.assertEqual(app.route_download(f"{self.EVIL}.zip", DASH), app.DROP)

    def test_redirects_and_script_navigations_are_dropped(self):
        self.assertEqual(self.route(self.EVIL, FakeAction(False, "OTHER")), app.DROP)

    def test_a_real_click_still_reaches_the_browser(self):
        for url in (self.EVIL, f"https://{FQDN}:8444/sonarr/", f"http://{FQDN}/", "https://github.com/o/r/blob/main/x.md"):
            with self.subTest(url=url):
                self.assertEqual(self.route(url, FakeAction(True)), app.EXTERNAL)

    def test_a_gesture_never_lets_other_schemes_out(self):
        for url in ("file:///etc/passwd", "javascript:alert(1)", "data:text/html,x", "steam://run/1", f"https://{FQDN}\\@evil.example/"):
            with self.subTest(url=url):
                self.assertEqual(self.route(url, FakeAction(True)), app.DROP)

    def test_same_origin_new_windows_stay_in_the_window(self):
        for gesture in (True, False):
            with self.subTest(gesture=gesture):
                self.assertEqual(self.route(f"https://{FQDN}/drive", FakeAction(gesture)), app.LOAD)

    def test_downloads_reach_the_browser_only_from_the_dashboard(self):
        self.assertEqual(app.route_download(f"https://{FQDN}/export.csv", DASH), app.EXTERNAL)
        for url in (
            f"https://{FQDN}:8444/sabnzbd/file.nzb",
            f"http://{FQDN}/export.csv",
            f"https://{FQDN}.evil.example/x",
            f"https://evil.example@{FQDN}/x",
            f"blob:https://{FQDN}/0f0e",
            "data:application/octet-stream,x",
            "file:///etc/passwd",
        ):
            with self.subTest(url=url):
                self.assertEqual(app.route_download(url, DASH), app.DROP)
        self.assertEqual(app.route_download(f"https://{FQDN}/x", None), app.DROP)


class Prefs(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)
        self.conf = self.dir / "homelab-dashboard" / "app.conf"

    def write(self, text):
        self.conf.parent.mkdir(parents=True, exist_ok=True)
        self.conf.write_text(text, encoding="utf-8")

    def parsed(self):
        cfg = configparser.ConfigParser(interpolation=None)
        cfg.read(self.conf, encoding="utf-8")
        return cfg

    def test_missing_file_gives_defaults(self):
        prefs = app.read_prefs(self.conf)
        self.assertEqual(prefs, app.Prefs())
        self.assertEqual((prefs.url, prefs.width, prefs.height, prefs.zoom), ("", 1280, 860, 1.0))

    def test_file_written_by_the_role_is_read(self):
        # community.general.ini_file creating the file: just the section and url.
        self.write(f"\n[app]\nurl = https://{FQDN}/\n")
        self.assertEqual(app.read_prefs(self.conf), app.Prefs(url=f"https://{FQDN}/"))

    def test_saved_values_are_read(self):
        self.write(f"[app]\nurl = https://{FQDN}/\nwidth = 1500\nheight = 900\nx = -1200\ny = 40\nmaximized = yes\nzoom = 1.3\n")
        self.assertEqual(app.read_prefs(self.conf), app.Prefs(f"https://{FQDN}/", 1500, 900, -1200, 40, True, 1.3))

    def test_junk_values_fall_back_to_defaults(self):
        self.write("[app]\nwidth = wide\nheight = -5\nx = 1e3\ny =\nmaximized = perhaps\nzoom = nan\n")
        self.assertEqual(app.read_prefs(self.conf), app.Prefs())
        for zoom, expected in (("inf", 1.0), ("12", 3.0), ("0.1", 0.5), ("-2", 0.5), ("big", 1.0)):
            with self.subTest(zoom=zoom):
                self.write(f"[app]\nzoom = {zoom}\n")
                self.assertEqual(app.read_prefs(self.conf).zoom, expected)

    def test_percent_in_the_url_is_literal(self):
        self.write(f"[app]\nurl = https://{FQDN}/search?q=a%20b%(x)s\n")
        self.assertEqual(app.read_prefs(self.conf).url, f"https://{FQDN}/search?q=a%20b%(x)s")

    def test_unparsable_file_exits_naming_it(self):
        for text in ("url = no section\n", "[app]\nurl = a\nurl = b\n"):
            with self.subTest(text=text):
                self.write(text)
                with self.assertRaises(SystemExit) as caught:
                    app.read_prefs(self.conf)
                self.assertIn(str(self.conf), str(caught.exception))
        self.conf.write_bytes(b"[app]\nurl = \xff\xfe\n")
        with self.assertRaises(SystemExit):
            app.read_prefs(self.conf)

    def test_save_round_trip_keeps_url_and_unknown_keys(self):
        self.write(f"[app]\nurl = https://{FQDN}/\nmine = keep me\n\n[other]\nk = v\n")
        prefs = app.read_prefs(self.conf)
        prefs.width, prefs.height, prefs.x, prefs.y, prefs.maximized, prefs.zoom = 1400, 950, 10, 20, False, 1.1000000000000001
        app.write_prefs(self.conf, prefs)
        self.assertEqual(app.read_prefs(self.conf), app.Prefs(f"https://{FQDN}/", 1400, 950, 10, 20, False, 1.1))
        cfg = self.parsed()
        self.assertEqual(cfg["app"]["mine"], "keep me")
        self.assertEqual(cfg["other"]["k"], "v")
        self.assertIn("zoom = 1.1\n", self.conf.read_text())

    def test_save_never_writes_the_url(self):
        # The role owns url; a one-off URL argument must not stick.
        self.write(f"[app]\nurl = https://{FQDN}/\n")
        prefs = app.read_prefs(self.conf)
        prefs.url = "https://elsewhere.example/"
        app.write_prefs(self.conf, prefs)
        self.assertEqual(app.read_prefs(self.conf).url, f"https://{FQDN}/")

    def test_save_keeps_a_url_the_role_changed_while_the_window_was_open(self):
        # Reviewer finding: closing the window must not restore the URL it started with.
        self.write("[app]\nurl = https://old-name.ts.example/\nwidth = 1000\n")
        prefs = app.read_prefs(self.conf)  # the window starts
        self.write(f"[app]\nurl = https://{FQDN}/\nwidth = 1000\nadded = by hand\n")  # the role runs
        prefs.width, prefs.zoom = 1600, 1.4  # the user resizes and zooms, then closes the window
        app.write_prefs(self.conf, prefs)
        saved = self.parsed()["app"]
        self.assertEqual((saved["url"], saved["width"], saved["zoom"], saved["added"]), (f"https://{FQDN}/", "1600", "1.4", "by hand"))

    def test_save_leaves_a_file_it_can_no_longer_parse_alone(self):
        self.write(f"[app]\nurl = https://{FQDN}/\n")
        prefs = app.read_prefs(self.conf)
        broken = "[app]\nurl = a\nurl = b\n"
        self.write(broken)
        with self.assertRaises(configparser.Error):
            app.write_prefs(self.conf, prefs)
        self.assertEqual(self.conf.read_text(), broken)

    def test_saved_url_line_matches_ini_file_format(self):
        # ini_file compares "option = value"; the app's rewrite must keep that, or every
        # playbook run after the app has saved would report a change.
        self.write(f"\n[app]\nurl = https://{FQDN}/\n")
        app.write_prefs(self.conf, app.read_prefs(self.conf))
        self.assertIn(f"\nurl = https://{FQDN}/\n", self.conf.read_text())

    def test_unknown_position_is_not_saved(self):
        self.write("[app]\nx = 5\ny = 6\n")
        prefs = app.read_prefs(self.conf)
        prefs.x = prefs.y = None
        app.write_prefs(self.conf, prefs)
        text = self.conf.read_text()
        self.assertNotIn("x =", text)
        self.assertNotIn("y =", text)

    def test_save_is_private_and_atomic(self):
        prefs = app.read_prefs(self.conf)
        stale = self.conf.parent / f".{self.conf.name}.tmp"
        self.conf.parent.mkdir(parents=True)
        stale.write_text("half-written")
        os.chmod(stale, 0o644)
        app.write_prefs(self.conf, prefs)
        self.assertEqual(stat.S_IMODE(self.conf.stat().st_mode), 0o600)
        self.assertFalse(stale.exists())
        self.assertEqual(sorted(p.name for p in self.conf.parent.iterdir()), ["app.conf"])

    def test_save_creates_a_private_directory(self):
        app.write_prefs(self.conf, app.read_prefs(self.conf))
        self.assertEqual(stat.S_IMODE(self.conf.parent.stat().st_mode), 0o700)


class PrivateDirs(unittest.TestCase):
    """Reviewer finding: existing website-data and cache directories that are too open get 0700."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)

    def mode(self, path):
        return stat.S_IMODE(path.stat().st_mode)

    def test_new_directory_is_0700(self):
        path = app.private_dir(self.dir / "a" / "homelab-dashboard")
        self.assertEqual(self.mode(path), 0o700)

    def test_existing_wide_directory_is_tightened(self):
        for wide in (0o755, 0o750, 0o777, 0o701):
            with self.subTest(mode=oct(wide)):
                path = self.dir / f"d{wide:o}"
                path.mkdir()
                path.chmod(wide)
                app.private_dir(path)
                self.assertEqual(self.mode(path), 0o700)

    def test_private_directory_is_left_alone(self):
        path = self.dir / "p"
        path.mkdir(mode=0o700)
        before = path.stat().st_ctime_ns
        app.private_dir(path)
        self.assertEqual((self.mode(path), path.stat().st_ctime_ns), (0o700, before))

    def test_someone_elses_directory_is_left_alone(self):
        path = self.dir / "theirs"
        path.mkdir(mode=0o755)
        path.chmod(0o755)
        real_getuid, logged = os.getuid, []
        app_log = app.log
        os.getuid = lambda: real_getuid() + 1
        app.log = logged.append
        try:
            app.private_dir(path)
        finally:
            os.getuid, app.log = real_getuid, app_log
        self.assertEqual(self.mode(path), 0o755)
        self.assertEqual(len(logged), 1)


class XdgDirs(unittest.TestCase):
    def test_absolute_value_is_used(self):
        self.assertEqual(app.xdg_dir("XDG_CONFIG_HOME", ".config", {"XDG_CONFIG_HOME": "/x/conf"}), Path("/x/conf/homelab-dashboard"))

    def test_relative_or_missing_value_falls_back_to_home(self):
        for env in ({"XDG_DATA_HOME": "relative/dir"}, {"XDG_DATA_HOME": ""}, {}):
            with self.subTest(env=env):
                self.assertEqual(app.xdg_dir("XDG_DATA_HOME", ".local/share", env), Path.home() / ".local/share/homelab-dashboard")


class ZoomAndKeys(unittest.TestCase):
    def test_zoom_steps_and_clamps(self):
        self.assertEqual(app.step_zoom(1.0, 1), 1.1)
        self.assertEqual(app.step_zoom(1.1, 1), 1.2)
        self.assertEqual(app.step_zoom(1.0, -1), 0.9)
        self.assertEqual(app.step_zoom(1.7, 0), 1.0)
        self.assertEqual(app.step_zoom(2.95, 1), 3.0)
        self.assertEqual(app.step_zoom(0.55, -1), 0.5)
        level = 1.0
        for _ in range(7):
            level = app.step_zoom(level, 1)
        self.assertEqual(level, 1.7)

    def test_key_map(self):
        cases = {
            ("F5", False): "reload",
            ("F5", True): "reload",
            ("r", True): "reload",
            ("R", True): "reload",
            ("q", True): "quit",
            ("Q", True): "quit",
            ("plus", True): "zoom_in",
            ("equal", True): "zoom_in",
            ("KP_Add", True): "zoom_in",
            ("minus", True): "zoom_out",
            ("KP_Subtract", True): "zoom_out",
            ("0", True): "zoom_reset",
            ("KP_0", True): "zoom_reset",
            ("r", False): None,
            ("q", False): None,
            ("0", False): None,
            ("plus", False): None,
            ("w", True): None,
            ("", True): None,
        }
        for (name, ctrl), action in cases.items():
            with self.subTest(key=name, ctrl=ctrl):
                self.assertEqual(app.key_action(name, ctrl), action)

    def test_saved_position_must_be_on_a_monitor(self):
        monitors = [(0, 0, 1920, 1080), (1920, 0, 2560, 1440)]
        self.assertTrue(app.on_screen(100, 100, monitors))
        self.assertTrue(app.on_screen(3000, 1000, monitors))
        self.assertTrue(app.on_screen(-40, 0, monitors))
        self.assertFalse(app.on_screen(5000, 100, monitors))  # an unplugged third monitor
        self.assertFalse(app.on_screen(100, 1500, monitors))
        self.assertFalse(app.on_screen(100, 100, []))


class Pages(unittest.TestCase):
    def test_error_page_escapes_its_text(self):
        page = app.error_page("<b>t</b>", 'Error resolving "<script>alert(1)</script>"')
        self.assertNotIn("<script>", page)
        self.assertIn("&lt;script&gt;", page)
        self.assertIn("default-src 'none'", page)

    def test_tls_problem_names_each_flag(self):
        self.assertEqual(app.tls_problem(8), "expired")
        self.assertEqual(app.tls_problem(1 | 2), "issuer not trusted, name does not match")
        self.assertEqual(app.tls_problem(0), "certificate rejected")


class Headless(unittest.TestCase):
    """The script must import, and refuse a bad URL, without gi installed."""

    def run_without_gi(self, *args, conf_text=None):
        with tempfile.TemporaryDirectory() as tmp:
            if conf_text is not None:
                (Path(tmp) / "homelab-dashboard").mkdir()
                (Path(tmp) / "homelab-dashboard" / "app.conf").write_text(conf_text)
            code = (
                "import runpy, sys; sys.modules['gi'] = None; sys.argv = ['homelab-dashboard-desktop'] + sys.argv[1:]; "
                f"runpy.run_path({str(SCRIPT)!r}, run_name='__main__')"
            )
            env = dict(os.environ, XDG_CONFIG_HOME=tmp, XDG_CACHE_HOME=tmp, XDG_DATA_HOME=tmp)
            return subprocess.run([sys.executable, "-c", code, *args], env=env, capture_output=True, text=True, timeout=60, check=False)

    def test_imports_without_gi(self):
        code = (
            "import importlib.machinery, importlib.util, sys; sys.modules['gi'] = None; "
            f"l = importlib.machinery.SourceFileLoader('m', {str(SCRIPT)!r}); "
            "s = importlib.util.spec_from_loader('m', l); m = importlib.util.module_from_spec(s); "
            "sys.modules['m'] = m; l.exec_module(m); print(m.Gtk)"
        )
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60, check=False)
        self.assertEqual((out.returncode, out.stdout.strip()), (0, "None"), out.stderr)

    def test_selftest_without_a_url_fails_in_one_line(self):
        out = self.run_without_gi("--selftest")
        self.assertEqual(out.returncode, 1, out.stderr)
        self.assertEqual(len(out.stdout.splitlines()), 1)
        self.assertTrue(out.stdout.startswith("fail no https:// dashboard URL"), out.stdout)

    def test_selftest_refuses_urls_off_the_dashboard_origin_rules(self):
        for url in (f"http://{FQDN}/", f"https://{FQDN}:8444/", f"https://u@{FQDN}/", "file:///etc/passwd"):
            with self.subTest(url=url):
                out = self.run_without_gi("--selftest", url, conf_text=f"[app]\nurl = https://{FQDN}/\n")
                self.assertEqual((out.returncode, out.stdout[:5]), (1, "fail "), out.stderr)

    def test_window_without_a_url_exits_1(self):
        out = self.run_without_gi(conf_text="[app]\nurl =\n")
        self.assertEqual(out.returncode, 1)
        self.assertIn("no https:// dashboard URL", out.stderr)


class Launcher(unittest.TestCase):
    BIN = "/home/someone/.local/bin/homelab-dashboard-desktop"

    def render(self):
        text = TEMPLATE.read_text()
        text = text.replace("{{ ansible_managed }}", "Ansible managed").replace("{{ dashboard_desktop_bin }}", self.BIN)
        self.assertNotIn("{{", text, "the template uses variables this test does not know about")
        return text

    def test_entry_keys(self):
        parser = configparser.ConfigParser(interpolation=None)
        parser.optionxform = str
        parser.read_string(self.render())
        entry = parser["Desktop Entry"]
        self.assertEqual(entry["Name"], "Media Server")
        self.assertEqual(entry["Comment"], "Homelab dashboard (tailnet)")
        self.assertEqual(entry["Exec"], self.BIN)
        self.assertEqual(entry["Icon"], "homelab-dashboard")
        self.assertEqual(entry["StartupWMClass"], app.APP_ID)
        self.assertTrue(entry["Categories"] and entry["Keywords"])

    @unittest.skipUnless(shutil.which("desktop-file-validate"), "desktop-file-validate not installed")
    def test_entry_validates(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "homelab-dashboard.desktop"
            path.write_text(self.render())
            out = subprocess.run(["desktop-file-validate", str(path)], capture_output=True, text=True, check=False)
            self.assertEqual((out.returncode, out.stdout + out.stderr), (0, ""))

    def test_icon_sources_exist(self):
        # The role copies the PWA's icons; a rename in apps/dashboard/web must fail here first.
        for name in ("icon-512.png", "icon.svg"):
            with self.subTest(icon=name):
                self.assertTrue((ICONS / name).is_file())


if __name__ == "__main__":
    unittest.main()
