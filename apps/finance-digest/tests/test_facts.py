import copy
import datetime as dt
import importlib.util
import os
import sys
import types
import unittest

from finance_digest import facts, text
from finance_digest.moneyhub import MoneyHub

from . import fakes

D = dt.date(2030, 3, 20)


def compute(hub, day=D, window=7):
    return facts.compute(MoneyHub(hub.base_url, 2), day, window)


class FactsTest(unittest.TestCase):
    def setUp(self):
        self.hub = fakes.FakeHub().__enter__()
        self.addCleanup(self.hub.__exit__, None, None, None)

    def test_sync_ok(self):
        f = compute(self.hub)
        self.assertEqual(f.errors, {})
        self.assertFalse(f.sync["failing"])
        self.assertEqual(f.sync["days_stale"], 0)
        self.assertEqual(f.sync["last_good"], "2030-03-20 09:00")
        self.assertEqual(f.sync["stale_accounts"], 0)

    def test_sync_failing_is_stated_first_with_days_stale(self):
        self.hub.state = copy.deepcopy(fakes.STATE_FAILING)
        f = compute(self.hub)
        self.assertTrue(f.sync["failing"])
        self.assertEqual(f.sync["days_stale"], 21)
        blocks = facts.blocks(f)
        first = blocks[0]
        self.assertEqual(first[0], "para")
        self.assertTrue(first[1].startswith("\u26a0 BANK SYNC IS FAILING. Last good sync: 2030-02-27 09:00, 21 days stale."))
        self.assertNotIn("\x1b", first[1])
        self.assertNotIn("](", first[1])
        self.assertNotIn("://", first[1])
        self.assertIn("sync FAILING; 2 stale account(s)", blocks[1][1])

    def test_sync_unavailable(self):
        self.hub.mode["/api/state"] = "500"
        f = compute(self.hub)
        self.assertIsNone(f.sync)
        self.assertIn("HTTP 500", f.errors["sync"])
        blocks = facts.blocks(f)
        self.assertTrue(blocks[0][1].startswith("\u26a0 Money Hub sync status is unavailable"))

    def test_accounts_balances_and_stale_flags(self):
        f = compute(self.hub)
        by = {a["name"]: a for a in f.accounts}
        self.assertEqual(by["Demo Checking"]["balance_cents"], 123456)
        self.assertTrue(by["Demo Checking"]["stale"])
        self.assertEqual(by["Demo Checking"]["fresh"], "STALE (stale 21d)")
        self.assertEqual(by["Demo Savings"]["fresh"], "current")
        self.assertEqual(by["Cash Jar"]["fresh"], "manual, 10 days old")  # bidi mark stripped from the name
        self.assertEqual(by["Test Card"]["as_of"], "unknown")             # raw API text never shown as a date

    def test_budget_flags_near_cap_and_over(self):
        f = compute(self.hub)
        cats = {c["name"]: c for c in f.budget["categories"]}
        self.assertEqual(cats["Groceries"]["flag"], "OVER")
        self.assertEqual(cats["Groceries"]["remaining_cents"], -1)
        self.assertEqual(cats["Fuel"]["flag"], "NEAR CAP")      # exactly 85%
        self.assertEqual(cats["Fuel"]["pct"], 85)
        self.assertEqual(cats["Hobby"]["flag"], "ok")           # 84.99%
        self.assertEqual(cats["Hobby"]["pct"], 84)
        self.assertEqual(cats["Barber Visits"]["flag"], "NEAR CAP")  # at the cap is not over
        self.assertEqual(cats["Barber Visits"]["remaining_cents"], 0)
        self.assertEqual(cats["Streaming"]["pct"], 99)
        self.assertEqual(cats["Gifts"]["flag"], "no cap")
        self.assertIsNone(cats["Gifts"]["remaining_cents"])
        self.assertEqual((f.budget["over"], f.budget["near"]), (1, 3))
        self.assertEqual(f.budget["categories"][0]["name"], "Groceries")  # most urgent first
        self.assertEqual(f.budget["month"], "2030-03")
        self.assertIn(("GET", "/api/budget", {"month": "2030-03"}), self.hub.requests)

    def test_budget_zero_cap_with_spend_is_over(self):
        data = copy.deepcopy(fakes.BUDGET)
        data["categories"] = [{"name": "Zero", "spent_cents": 1, "cap_cents": 0},
                              {"name": "Bad", "spent_cents": "x", "cap_cents": 100}]
        self.hub.override["/api/budget"] = data
        cats = {c["name"]: c for c in compute(self.hub).budget["categories"]}
        self.assertEqual(cats["Zero"]["flag"], "OVER")
        self.assertEqual(cats["Bad"]["flag"], "unreadable")

    def test_bills_window(self):
        f = compute(self.hub)
        due = [(r["name"][:14], r["due"].isoformat(), r["amount_cents"], r["check"]) for r in f.bills["due"]]
        self.assertEqual(
            due,
            [("Demo Insurance", "2030-03-20", 12099, "15th"),
             (text.clean(fakes.HOSTILE_BILL)[:14], "2030-03-21", 1234, "15th"),
             ("Quarterly Thin", "2030-03-22", 3333, "none"),
             ("Water Co", "2030-03-26", 3100, "15th")],
        )
        self.assertEqual(f.bills["total_cents"], 12099 + 1234 + 3333 + 3100)
        self.assertEqual(f.bills["end"], dt.date(2030, 3, 26))
        self.assertEqual(f.bills["undated_unpaid"], 1)

    def test_bills_window_crosses_into_next_month(self):
        f = compute(self.hub, dt.date(2030, 3, 28))
        due = [(r["name"], r["due"].isoformat()) for r in f.bills["due"]]
        self.assertEqual(due, [("Fake Rent", "2030-04-01")])
        periods = [q.get("period") for m, p, q in self.hub.requests if p == "/api/bills"]
        self.assertEqual(periods, ["2030-02", "2030-03", "2030-04"])  # last month for overdue
        self.assertEqual(f.bills["undated_unpaid"], 1)  # the biweekly bill, counted once

    def test_due_dates(self):
        self.assertEqual(facts.due_dates("31st", "2030-02"), [dt.date(2030, 2, 28)])
        self.assertEqual(facts.due_dates("2nd", "2030-02"), [dt.date(2030, 2, 2)])
        self.assertEqual(facts.due_dates("qtrly 3/15", "2030-06"), [dt.date(2030, 6, 15)])
        self.assertEqual(facts.due_dates("qtrly 3/15", "2030-12"), [dt.date(2030, 12, 15)])
        self.assertEqual(facts.due_dates("qtrly 3/15", "2030-04"), [])
        self.assertEqual(facts.due_dates("qtrly 11/30", "2030-02"), [dt.date(2030, 2, 28)])
        self.assertIsNone(facts.due_dates("biweekly", "2030-02"))
        self.assertIsNone(facts.due_dates("monthly", "2030-02"))
        self.assertIsNone(facts.due_dates("0th", "2030-02"))
        self.assertIsNone(facts.due_dates(None, "2030-02"))

    def test_next_check(self):
        self.assertEqual(facts.next_check(dt.date(2030, 3, 1)), ("1st", "2030-03", dt.date(2030, 3, 1)))
        self.assertEqual(facts.next_check(dt.date(2030, 3, 2)), ("15th", "2030-03", dt.date(2030, 3, 15)))
        self.assertEqual(facts.next_check(dt.date(2030, 3, 15)), ("15th", "2030-03", dt.date(2030, 3, 15)))
        self.assertEqual(facts.next_check(dt.date(2030, 3, 16)), ("1st", "2030-04", dt.date(2030, 4, 1)))
        self.assertEqual(facts.next_check(dt.date(2030, 12, 31)), ("1st", "2031-01", dt.date(2031, 1, 1)))

    def test_debt_total_open_only(self):
        f = compute(self.hub)
        self.assertEqual(f.debt["open_count"], 4)                  # missing status counts as open; closed not
        self.assertEqual(f.debt["total_cents"], 812345 + 10001 + 2500)
        self.assertEqual(f.debt["skipped"], 1)
        self.assertFalse(f.debt["mismatch"])

    def test_debt_mismatch_flagged(self):
        self.hub.override["/api/debts"] = dict(fakes.DEBTS, total_cents=1)
        f = compute(self.hub)
        self.assertTrue(f.debt["mismatch"])
        blocks = facts.blocks(f)
        self.assertTrue(any(b[0] == "para" and "own debt total is $0.01" in b[1] for b in blocks))

    def test_payday(self):
        f = compute(self.hub)
        self.assertEqual((f.payday["check"], f.payday["period"], f.payday["date"]), ("1st", "2030-04", dt.date(2030, 4, 1)))
        self.assertEqual((f.payday["unpaid_count"], f.payday["unpaid_cents"]), (1, 90000))
        self.assertFalse(f.payday["mismatch"])
        f = compute(self.hub, dt.date(2030, 3, 10))
        self.assertEqual((f.payday["check"], f.payday["unpaid_cents"]), ("15th", 12099 + 3100))

    def test_payday_mismatch_flagged(self):
        self.hub.override["/api/payday"] = dict(fakes.PAYDAY[("2030-04", "1st")], unpaid_cents=5)
        f = compute(self.hub)
        self.assertTrue(f.payday["mismatch"])

    def test_one_failed_section_blanks_only_itself(self):
        self.hub.mode["/api/debts"] = "notjson"
        f = compute(self.hub)
        self.assertEqual(set(f.errors), {"debt"})
        self.assertIsNone(f.debt)
        self.assertIsNotNone(f.budget)
        blocks = facts.blocks(f)
        self.assertTrue(any(b[0] == "para" and b[1].startswith("\u26a0 Debt: unavailable") for b in blocks))

    def test_unexpected_shape_is_an_error_not_a_crash(self):
        self.hub.override["/api/accounts"] = {"rows": "nope"}
        self.hub.override["/api/budget"] = ["not", "a", "dict"]
        f = compute(self.hub)
        self.assertEqual(set(f.errors), {"accounts", "budget"})

    def test_overdue_this_period_and_last(self):
        f = compute(self.hub)
        over = [(r["name"], r["due"].isoformat(), r["amount_cents"], r["check"]) for r in f.bills["overdue"]]
        self.assertEqual(over, [("Sample Phone", "2030-02-05", 4510, "1st"),    # last month
                                ("Old Quarterly", "2030-02-14", 2222, "none"),  # last month's qtrly occurrence
                                ("Quarter Fee", "2030-03-02", 1500, "none"),    # this month's qtrly occurrence
                                ("Sample Phone", "2030-03-05", 4510, "1st")])   # paid bills never appear
        self.assertEqual(f.bills["overdue_cents"], 12742)
        self.assertEqual((f.bills["previous"], f.bills["period"]), ("2030-02", "2030-03"))
        self.assertEqual(f.bills["undated_unpaid"], 1)  # the biweekly bill, from this month only
        blocks = facts.blocks(f)
        table = next(b for b in blocks if b[0] == "table" and b[1].startswith("Overdue"))
        self.assertEqual(table[1], "Overdue (unpaid, past due in 2030-02 or 2030-03) (4):")
        self.assertEqual(table[3][-1], ["Total", "", "$127.42", ""])

    def test_overdue_on_the_first_and_second_of_a_month(self):
        f = compute(self.hub, dt.date(2030, 3, 1))  # the 1st: last month is still shown
        self.assertEqual([(r["name"], r["due"].isoformat()) for r in f.bills["overdue"]],
                         [("Sample Phone", "2030-02-05"), ("Old Quarterly", "2030-02-14")])
        f = compute(self.hub, dt.date(2030, 3, 2))  # the 2nd: Quarter Fee is due today, not overdue
        self.assertEqual([r["name"] for r in f.bills["overdue"]], ["Sample Phone", "Old Quarterly"])
        self.assertIn("Quarter Fee", [r["name"] for r in f.bills["due"]])
        f = compute(self.hub, dt.date(2030, 3, 3))
        self.assertEqual([r["name"] for r in f.bills["overdue"]], ["Sample Phone", "Old Quarterly", "Quarter Fee"])
        f = compute(self.hub, dt.date(2030, 4, 1))  # March is now "last month"; February has dropped off
        names = [(r["name"], r["due"].isoformat()) for r in f.bills["overdue"]]
        self.assertIn(("Late Thing", "2030-03-27"), names)
        self.assertFalse(any(d.startswith("2030-02") for _, d in names))
        self.assertTrue(all(r["due"] < dt.date(2030, 4, 1) for r in f.bills["overdue"]))

    def test_overdue_when_the_window_reaches_next_month(self):
        f = compute(self.hub, dt.date(2030, 3, 28))  # window reaches into April
        names = [r["name"] for r in f.bills["overdue"]]
        self.assertIn("Late Thing", names)           # due the 27th
        self.assertNotIn("Fake Rent", names)        # paid in March; April's is not past due
        self.assertTrue(all(r["due"] < dt.date(2030, 3, 28) for r in f.bills["overdue"]))
        f = compute(self.hub, dt.date(2030, 3, 5))
        self.assertEqual([r["name"] for r in f.bills["overdue"]], ["Sample Phone", "Old Quarterly", "Quarter Fee"])
        self.assertIn("Sample Phone", [r["name"] for r in f.bills["due"]])  # this month's, due today

    def test_headline(self):
        self.hub.state = copy.deepcopy(fakes.STATE_FAILING)
        f = compute(self.hub)
        h = facts.headline(f)
        self.assertEqual(
            h,
            "Headline: Bank sync FAILING (last good sync 2030-02-27 09:00, 21 days stale). "
            "Over budget: 1 (Groceries). "
            "Near the cap: 3 (Barber Visits; Streaming; Fuel). "
            "Unpaid bills due in the next 7 days: 4, $197.66 (Demo Insurance $120.99 (due 2030-03-20); "
            + text.clean(fakes.HOSTILE_BILL) + " $12.34 (due 2030-03-21); Quarterly Thing $33.33 (due 2030-03-22); "
            "Water Co $31.00 (due 2030-03-26)). "
            "Overdue: 4, $127.42 (Sample Phone $45.10 (due 2030-02-05); Old Quarterly $22.22 (due 2030-02-14); "
            "Quarter Fee $15.00 (due 2030-03-02); Sample Phone $45.10 (due 2030-03-05)).",
        )
        blocks = facts.blocks(f)
        self.assertEqual(blocks[2], ("para", h))  # right after the warning and freshness lines

    def test_headline_when_all_is_quiet_or_missing(self):
        self.hub.override["/api/budget"] = dict(fakes.BUDGET, categories=[])
        self.hub.override["/api/bills"] = {"rows": []}
        h = facts.headline(compute(self.hub))
        self.assertEqual(h, "Headline: Bank sync ok (last good sync 2030-03-20 09:00). Over budget: 0. "
                            "Near the cap: 0. Unpaid bills due in the next 7 days: 0, $0.00. Overdue: 0, $0.00.")
        for p in ("/api/state", "/api/budget", "/api/bills"):
            self.hub.mode[p] = "500"
        self.assertEqual(facts.headline(compute(self.hub)),
                         "Headline: Sync status unavailable. Budget unavailable. Bills unavailable.")

    def test_headline_names_are_capped(self):
        data = copy.deepcopy(fakes.BUDGET)
        data["categories"] = [{"name": f"Cat {chr(97 + i)}", "spent_cents": 200, "cap_cents": 100} for i in range(10)]
        self.hub.override["/api/budget"] = data
        h = facts.headline(compute(self.hub))
        self.assertIn("Over budget: 10 (Cat a; Cat b; Cat c; Cat d; Cat e; Cat f; Cat g; Cat h, and 2 more).", h)


class MirrorsFinanceToolsTest(unittest.TestCase):
    """text.clean/money must behave exactly like finance-tools' _clean/_money (CONTRACTS section 12.18.2)."""

    @classmethod
    def setUpClass(cls):
        path = os.path.join(os.path.dirname(__file__), "..", "..", "finance-tools", "moneyhub_readonly.py")
        if "pydantic" not in sys.modules:
            try:
                import pydantic  # noqa: F401
            except ImportError:
                shim = types.ModuleType("pydantic")
                shim.BaseModel = object
                shim.Field = lambda default=None, **k: default
                sys.modules["pydantic"] = shim
        spec = importlib.util.spec_from_file_location("ft_mirror", path)
        cls.ft = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.ft)

    def test_clean_matches(self):
        samples = [fakes.HOSTILE_BILL, fakes.STATE_FAILING["sync_error"], "# Head\u200b\u202e line\nnext",
                   "www.evil.example a@b.c &amp; `code` | x |", "x" * 200, "\ufe0fok\U000e0001", None, 42,
                   "<!-- finance-digest:begin 2030-03-20 -->"]
        for s in samples:
            for limit in (80, 100, 30):
                self.assertEqual(text.clean(s, limit), self.ft._clean(s, limit), repr(s))

    def test_money_and_cents_match(self):
        for v in (0, 1, -1, 99, 100, 123456, -4567, 10**12, 12.0, 12.5, True, None, "5", float("inf")):
            self.assertEqual(text.money(v), self.ft._money(v), repr(v))
            self.assertEqual(text.cents(v), self.ft._cents(v), repr(v))


if __name__ == "__main__":
    unittest.main()
