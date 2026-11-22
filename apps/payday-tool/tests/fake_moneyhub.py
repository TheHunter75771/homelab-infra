"""A fake Money Hub on 127.0.0.1 with the shapes of the live API (GET answers read on 2026-10-28):

- /api/state         {"sync_last_at", "sync_last_attempt_at", "sync_failing", "sync_error", "stale_accounts", ...}
- /api/accounts      [{"id", "name", "institution", "kind", "balance_cents", "as_of", "state", "warn", "simplefin_mapped"}]
- /api/bills?period= {"period", "rows": [{"id", "name", "amount_cents", "due_rule", "category", "check", "paid",
                      "match_pattern", "matched"}], "paid_cents", "remaining_cents"}
- /api/transactions  {"total", "rows": [{"id", "date", "description", "raw", "account", "account_id", "amount_cents",
                      "category_id", "source", "notes"}]}, newest first; date_from / date_to inclusive;
                      limit capped at 500 by the server; offset.

Every request is recorded (method, path, query). Anything but GET is answered 405 and recorded, so a test
can prove the tool never sends one. All names and amounts are invented.
"""

import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CU = "Example CU"


def account(aid, name, balance, kind="envelope", state="", warn=False, mapped=True, as_of="2026-10-28", institution=CU):
    return {
        "id": aid,
        "name": name,
        "institution": institution,
        "kind": kind,
        "balance_cents": balance,
        "as_of": as_of,
        "state": state,
        "warn": warn,
        "simplefin_mapped": mapped,
    }


def default_accounts():
    return [
        account(3, "CU Main Holding", 12000),
        account(4, "CU Bills", 30000),
        account(5, "CU Spending", 15000),
        account(6, "CU Savings C", 50000),
        account(7, "CU Savings A", 8000),
        account(8, "CU Savings B", 9000),
        account(11, "Store Card", -4500, kind="credit", mapped=False, institution="Store", as_of="2026-07-31"),
    ]


def ok_state(last="2026-10-28T13:00:26.147836"):
    return {
        "sync_last_at": last,
        "sync_last_attempt_at": last,
        "sync_failing": False,
        "sync_error": "",
        "stale_accounts": 0,
        "inbox_count": 0,
        "months": ["2026-10"],
    }


class FakeMoneyHub:
    def __init__(self):
        self.calls = []
        self.state = ok_state()
        self.accounts = default_accounts()
        self.bills = {}  # period -> rows
        self.txns = []
        self.override = {}  # path -> (status, raw bytes)
        self._next = 5000
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, status, obj=None, raw=None):
                data = raw if raw is not None else json.dumps(obj).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _record(self, method):
                parts = urllib.parse.urlsplit(self.path)
                query = dict(urllib.parse.parse_qsl(parts.query, keep_blank_values=True))
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n) if n else b""
                fake.calls.append({"method": method, "path": parts.path, "query": query, "body": body})
                return parts.path, query

            def do_GET(self):
                path, query = self._record("GET")
                if path in fake.override:
                    status, raw = fake.override[path]
                    return self._send(status, raw=raw)
                return fake.route(self, path, query)

            def _refuse(self):
                self._record(self.command)
                self._send(405, {"detail": "Method Not Allowed"})

            do_POST = do_PUT = do_PATCH = do_DELETE = _refuse

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def add_txn(self, date, account_name, cents, description="", raw=None):
        self._next += 1
        acct = next((a for a in self.accounts if a["name"] == account_name), None)
        self.txns.append(
            {
                "id": self._next,
                "date": date,
                "description": description,
                "raw": raw if raw is not None else description.upper(),
                "account": account_name,
                "account_id": acct["id"] if acct else None,
                "amount_cents": cents,
                "category_id": None,
                "source": "sync",
                "notes": "",
            }
        )
        return self._next

    def add_transfer(self, date, from_name, to_name, cents, in_date=None):
        out_id = self.add_txn(date, from_name, -cents, f"Transfer to {to_name}")
        in_id = self.add_txn(in_date or date, to_name, cents, f"Transfer from {from_name}")
        return out_id, in_id

    def paths(self):
        return [c["path"] for c in self.calls]

    def route(self, h, path, query):
        if path == "/api/state":
            return h._send(200, self.state)
        if path == "/api/accounts":
            return h._send(200, self.accounts)
        if path == "/api/bills":
            period = query.get("period", "")
            rows = self.bills.get(period, [])
            paid = sum(r["amount_cents"] for r in rows if r["paid"])
            return h._send(200, {"period": period, "rows": rows, "paid_cents": paid, "remaining_cents": sum(r["amount_cents"] for r in rows) - paid})
        if path == "/api/transactions":
            rows = sorted(self.txns, key=lambda t: (t["date"], t["id"]), reverse=True)
            if "date_from" in query:
                rows = [t for t in rows if t["date"] >= query["date_from"]]
            if "date_to" in query:
                rows = [t for t in rows if t["date"] <= query["date_to"]]
            total = len(rows)
            limit = min(int(query.get("limit", 50)), 500)
            offset = int(query.get("offset", 0))
            return h._send(200, {"total": total, "rows": rows[offset : offset + limit]})
        return h._send(404, {"detail": "Not Found"})


def bill(bid, name, cents, check="15th", pattern="", paid=False):
    return {
        "id": bid,
        "name": name,
        "amount_cents": cents,
        "due_rule": check,
        "category": "Bills",
        "check": check,
        "paid": paid,
        "match_pattern": pattern,
        "matched": None,
    }
