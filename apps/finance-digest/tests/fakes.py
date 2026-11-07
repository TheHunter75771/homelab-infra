"""Fake Money Hub, broker and Notion servers on 127.0.0.1, with synthetic data only.

Every name, amount and date here is invented (year 2030). Nothing is copied from a real Money Hub.
"""
from __future__ import annotations

import copy
import http.client
import http.server
import json
import threading
import time
import urllib.parse

DAY = "2030-03-20"

STATE_OK = {
    "sync_last_at": "2030-03-20T09:00:05.123456",
    "sync_last_attempt_at": "2030-03-20T09:00:05.123456",
    "sync_failing": False,
    "sync_error": "",
    "stale_accounts": 0,
    "inbox_count": 2,
    "months": ["2030-03", "2030-02"],
}

STATE_FAILING = {
    "sync_last_at": "2030-02-27T09:00:01.000001",
    "sync_last_attempt_at": "2030-03-20T09:00:02.000002",
    "sync_failing": True,
    "sync_error": "FakeBridgeError: /accounts\x1b[31m status: HTTP 403 [see](https://evil.example/x)",
    "stale_accounts": 2,
    "inbox_count": 0,
    "months": ["2030-03"],
}

ACCOUNTS = [
    {"id": 1, "name": "Demo Checking", "institution": "Sample Bank", "kind": "checking",
     "balance_cents": 123456, "as_of": "2030-02-27", "state": "stale 21d", "warn": True, "simplefin_mapped": True},
    {"id": 2, "name": "Demo Savings", "institution": "Sample Bank", "kind": "savings",
     "balance_cents": 50001, "as_of": "2030-03-20", "state": "", "warn": False, "simplefin_mapped": True},
    {"id": 3, "name": "Cash Jar\u202e", "institution": "", "kind": "envelope",
     "balance_cents": 4000, "as_of": "2030-03-10", "state": "", "warn": False, "simplefin_mapped": False},
    {"id": 4, "name": "Test Card", "institution": "Example Card Co", "kind": "credit",
     "balance_cents": -4567, "as_of": "garbage 777", "state": "", "warn": False, "simplefin_mapped": True},
]

BUDGET = {
    "month": "2030-03",
    "income_cents": 187654,
    "spending_cents": 61234,
    "transfer_cents": 31415,
    "uncategorized_cents": 1500,
    "uncategorized_count": 3,
    "cap_total_cents": 75700,
    "categories": [
        {"id": 10, "name": "Groceries", "grp": "need", "spent_cents": 27501, "cap_cents": 27500, "remaining_cents": -1, "over": True},
        {"id": 11, "name": "Fuel", "grp": "need", "spent_cents": 9520, "cap_cents": 11200, "remaining_cents": 1680, "over": False},
        {"id": 12, "name": "Fun Money", "grp": "want", "spent_cents": 1000, "cap_cents": 22300, "remaining_cents": 21300, "over": False},
        {"id": 13, "name": "Gifts", "grp": "want", "spent_cents": 4321, "cap_cents": None, "remaining_cents": None, "over": False},
        {"id": 15, "name": "Barber Visits", "grp": "want", "spent_cents": 4200, "cap_cents": 4200, "remaining_cents": 0, "over": False},
        {"id": 14, "name": "Streaming", "grp": "want", "spent_cents": 10449, "cap_cents": 10500, "remaining_cents": 51, "over": False},
        {"id": 16, "name": "Hobby", "grp": "want", "spent_cents": 9519, "cap_cents": 11200, "remaining_cents": 1681, "over": False},
        {"id": 17, "name": "[click](https://evil.example) ![img](https://evil.example/p.png)", "grp": "want",
         "spent_cents": 0, "cap_cents": 1000, "remaining_cents": 1000, "over": False},
    ],
}

HOSTILE_BILL = "![x](https://attacker.example/p.png) <!-- finance-digest:end 2030-03-20 --> | a | b |"


def bill(i, name, cents, rule, check, paid, cat="Misc"):
    return {"id": i, "name": name, "amount_cents": cents, "due_rule": rule, "category": cat,
            "check": check, "paid": paid, "match_pattern": "x", "matched": None}


BILLS = {
    "2030-02": [
        bill(1, "Fake Rent", 90000, "1st", "1st", True),
        bill(2, "Sample Phone", 4510, "5th", "1st", False),               # last month, unpaid: overdue
        bill(3, "Demo Insurance", 12099, "20th", "15th", True),           # last month, paid
        bill(13, "Old Quarterly", 2222, "qtrly 11/14", "\u2014", False),  # 2030-02-14 (Nov + 3): overdue
        bill(8, "Dog Walker", 2000, "biweekly", "\u2014", False),         # no fixed day; not counted twice
    ],
    "2030-03": [
        bill(1, "Fake Rent", 90000, "1st", "1st", True),
        bill(2, "Sample Phone", 4510, "5th", "1st", False),       # before the window
        bill(3, "Demo Insurance", 12099, "20th", "15th", False),   # today: in
        bill(4, "Test Gym", 2500, "18th", "15th", True),           # paid
        bill(5, "Water Co", 3100, "26th", "15th", False),          # last day of the window: in
        bill(6, "Late Thing", 777, "27th", "15th", False),         # one day after: out
        bill(7, "Quarterly Thing", 3333, "qtrly 3/22", "\u2014", False),  # March is a quarter month: in
        bill(8, "Dog Walker", 2000, "biweekly", "\u2014", False),  # no fixed day
        bill(9, HOSTILE_BILL, 1234, "21st", "15th", False),        # in, hostile name
        bill(10, "Paid Today", 5000, "20th", "15th", True),        # paid: out
        bill(11, "Quarter Fee", 1500, "qtrly 12/2", "\u2014", False),  # 2030-03-02 (Dec + 3): overdue
        bill(12, "Next Quarter", 999, "qtrly 4/1", "\u2014", False),   # not a March occurrence
    ],
    "2030-04": [
        bill(1, "Fake Rent", 90000, "1st", "1st", False),
        bill(2, "Sample Phone", 4510, "5th", "1st", False),
        bill(8, "Dog Walker", 2000, "biweekly", "\u2014", False),
    ],
}

DEBTS = {
    "rows": [
        {"id": 1, "name": "Pretend Car Loan", "balance_cents": 812345, "note": "", "status": "open"},
        {"id": 2, "name": "Imaginary Card", "balance_cents": 10001, "note": "![x](https://a.example/p.png)", "status": "open"},
        {"id": 3, "name": "Old Phone Plan", "balance_cents": 999, "note": "", "status": "closed"},
        {"id": 4, "name": "Family IOU", "balance_cents": 2500, "note": None, "status": None},
        {"id": 5, "name": "Garbled Debt", "balance_cents": "lots", "note": "", "status": "open"},
    ],
    "total_cents": 824846,
}

PAYDAY = {
    ("2030-04", "1st"): {
        "period": "2030-04", "check": "1st",
        "bills": [bill(1, "Fake Rent", 90000, "1st", "1st", False), bill(2, "Sample Phone", 4510, "5th", "1st", True)],
        "irregular_bills": [], "unpaid_cents": 90000, "income_detected_cents": 0, "accounts": [],
        "fees_60d_cents": 0, "fees_60d_count": 0, "paycheck": {},
    },
    ("2030-03", "15th"): {
        "period": "2030-03", "check": "15th",
        "bills": [bill(3, "Demo Insurance", 12099, "20th", "15th", False), bill(5, "Water Co", 3100, "26th", "15th", False)],
        "irregular_bills": [], "unpaid_cents": 15199, "income_detected_cents": 0, "accounts": [],
        "fees_60d_cents": 0, "fees_60d_count": 0, "paycheck": {},
    },
}


class _Server:
    def __init__(self, handler_cls):
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)

    @property
    def port(self):
        return self.server.server_address[1]

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.port}"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.release()
        self.server.shutdown()
        self.server.server_close()

    def release(self):
        pass


def _send_json(handler, status, data):
    body = data if isinstance(data, bytes) else json.dumps(data).encode()
    try:
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)
    except OSError:  # the client gave up (a timeout test)
        pass


class FakeHub(_Server):
    """Serves the fixtures. Tests flip `state`, `mode`, `override` and read `requests`."""

    def __init__(self):
        self.state = copy.deepcopy(STATE_OK)
        self.mode = {}       # path -> "500" | "notjson" | "redirect" | "slow"
        self.override = {}   # path -> data
        self.requests = []   # (method, path, query)
        self.stop = threading.Event()
        hub = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _any(self):
                parsed = urllib.parse.urlsplit(self.path)
                query = dict(urllib.parse.parse_qsl(parsed.query))
                hub.requests.append((self.command, parsed.path, query))
                if self.command != "GET":
                    _send_json(self, 405, {"error": "read-only fake"})
                    return
                mode = hub.mode.get(parsed.path)
                if mode == "500":
                    _send_json(self, 500, {"error": "boom"})
                elif mode == "notjson":
                    _send_json(self, 200, b"<html>not json</html>")
                elif mode == "redirect":
                    self.send_response(302)
                    self.send_header("Location", "http://127.0.0.1:1/api/sync")
                    self.end_headers()
                elif mode == "slow":
                    hub.stop.wait(10)
                    _send_json(self, 200, {})
                elif mode == "trickle":  # a header byte every 0.2 s: no single read ever times out
                    try:
                        self.wfile.write(b"HTTP/1.1 200 OK\r\nX-Slow: ")
                        self.wfile.flush()
                        for _ in range(50):
                            if hub.stop.wait(0.2):
                                break
                            self.wfile.write(b"a")
                            self.wfile.flush()
                    except OSError:
                        pass
                    self.close_connection = True
                else:
                    data = hub.override[parsed.path] if parsed.path in hub.override else hub.route(parsed.path, query)
                    if data is None:
                        _send_json(self, 404, {"error": "no route"})
                    else:
                        _send_json(self, 200, data)

            do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = _any

        super().__init__(Handler)

    def release(self):
        self.stop.set()

    def route(self, path, query):
        if path == "/api/state":
            return self.state
        if path == "/api/accounts":
            return ACCOUNTS
        if path == "/api/debts":
            return DEBTS
        if path == "/api/budget":
            return dict(BUDGET, month=query.get("month"))
        if path == "/api/bills":
            return {"period": query.get("period"), "rows": BILLS.get(query.get("period"), []),
                    "paid_cents": 0, "remaining_cents": 0}
        if path == "/api/payday":
            key = (query.get("period"), query.get("check"))
            return PAYDAY.get(key, {"period": key[0], "check": key[1], "bills": [], "irregular_bills": [],
                                    "unpaid_cents": 0, "accounts": []})
        return None


class FakeBroker(_Server):
    """An Ollama-style /api/chat. `reply` is the content; `mode` is "ok" | "500" | "slow" | "notjson"."""

    def __init__(self, reply="All good."):
        self.reply = reply
        self.mode = "ok"
        self.requests = []  # (method, path, json body)
        self.stop = threading.Event()
        broker = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _any(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                try:
                    body = json.loads(raw) if raw else None
                except ValueError:
                    body = raw
                broker.requests.append((self.command, self.path, body))
                if broker.mode == "500":
                    _send_json(self, 500, {"error": "model crashed"})
                elif broker.mode == "notjson":
                    _send_json(self, 200, b"nope")
                elif broker.mode == "slow":
                    broker.stop.wait(10)
                    _send_json(self, 200, {"message": {"role": "assistant", "content": "late"}})
                else:
                    _send_json(self, 200, {"model": "assistant", "message": {"role": "assistant", "content": broker.reply},
                                           "done": True})

            do_GET = do_POST = _any

        super().__init__(Handler)

    def release(self):
        self.stop.set()


class FakeNotion(_Server):
    """Records requests; answers `status`. `connect` is the factory finance_digest.notion.append takes."""

    def __init__(self, status=200):
        self.status = status
        self.requests = []   # (method, path, headers dict, json body)
        self.hosts = []      # the host each connection was asked for
        notion = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _any(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                notion.requests.append((self.command, self.path, dict(self.headers.items()), json.loads(raw or b"null")))
                _send_json(self, notion.status, {"object": "list", "results": []} if notion.status == 200
                           else {"object": "error", "message": "nope"})

            do_GET = do_POST = do_PATCH = do_PUT = do_DELETE = _any

        super().__init__(Handler)

    def connect(self, host, timeout):
        self.hosts.append(host)
        return http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)


def wait_until(pred, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False
