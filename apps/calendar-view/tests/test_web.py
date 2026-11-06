"""The page's three files (CONTRACTS §14.2): no external requests, nothing the CSP would block (no inline
script or style, no handlers in markup), text only through textContent, and the 60 s poll. Static checks;
`node --check` on app.js when node is installed."""

from __future__ import annotations

import re
import shutil
import subprocess
import unittest
from html.parser import HTMLParser

from .helpers import APP

WEB = APP / "web"


class Tags(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.tags: list[tuple[str, dict]] = []
        self.inline_script = False
        self._in_script = False

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))
        self._in_script = tag == "script"

    def handle_endtag(self, tag):
        self._in_script = False

    def handle_data(self, data):
        if self._in_script and data.strip():
            self.inline_script = True


class Page(unittest.TestCase):
    def setUp(self) -> None:
        self.html = (WEB / "index.html").read_text()
        self.js = (WEB / "app.js").read_text()
        self.css = (WEB / "app.css").read_text()
        self.parsed = Tags()
        self.parsed.feed(self.html)

    def test_one_html_one_css_one_js(self) -> None:
        refs = [(t, a.get("src") or a.get("href")) for t, a in self.parsed.tags if t in ("script", "link", "img", "iframe")]
        self.assertEqual(refs, [("link", "/static/app.css"), ("script", "/static/app.js")])

    def test_nothing_the_csp_blocks(self) -> None:
        self.assertFalse(self.parsed.inline_script, "inline script")
        self.assertNotIn("<style", self.html)
        for tag, attrs in self.parsed.tags:
            with self.subTest(tag=tag):
                self.assertNotIn("style", attrs, "inline style attribute")
                self.assertFalse([a for a in attrs if a.startswith("on")], "inline event handler")

    def test_no_external_requests_or_web_fonts(self) -> None:
        for name, text in (("index.html", self.html), ("app.js", self.js), ("app.css", self.css)):
            with self.subTest(name):
                self.assertNotRegex(text, r"(?i)\b(https?:)?//[a-z0-9.-]+\.[a-z]{2,}")
                self.assertNotRegex(text, r"(?i)@import|@font-face|url\(")

    def test_text_only_through_text_content(self) -> None:
        for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function",
                     "setAttribute(", "srcdoc", "DOMParser", "createContextualFragment"):
            with self.subTest(sink):
                self.assertNotIn(sink, self.js)
        self.assertIn("textContent", self.js)
        # Colors reach the DOM only through the CSSOM, and only when they look like #rrggbb.
        self.assertEqual(re.findall(r"\.style\.(\w+)", self.js), ["backgroundColor", "backgroundColor"])
        self.assertIn("const COLOR = /^#[0-9a-f]{6}$/i;", self.js)

    def test_polls_the_api_every_minute(self) -> None:
        self.assertIn("const POLL_MS = 60000;", self.js)
        self.assertIn('fetch("/api/agenda"', self.js)
        self.assertIn("setInterval(load, POLL_MS)", self.js)

    def test_no_date_logic(self) -> None:
        """The clock is the only date the page reads; every agenda label comes from the server."""
        for api in ("getDate", "getDay", "getHours", "getMinutes", "getMonth", "getFullYear", "setDate", "Date.parse",
                    "toLocaleDateString", "getTimezoneOffset"):
            with self.subTest(api):
                self.assertNotIn(api, self.js)
        self.assertEqual(self.js.count("new Date("), 1)

    def test_dark_and_one_column_on_a_phone(self) -> None:
        self.assertIn("color-scheme: dark", self.css)
        self.assertIn("@media (max-width: 999px)", self.css)
        self.assertIn('<meta name="viewport" content="width=device-width, initial-scale=1">', self.html)

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_app_js_parses(self) -> None:
        r = subprocess.run(["node", "--check", str(WEB / "app.js")], capture_output=True, text=True, timeout=60,
                           check=False)
        self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == "__main__":
    unittest.main()
