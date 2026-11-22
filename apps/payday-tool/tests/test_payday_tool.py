import ast
import asyncio
import datetime
import faulthandler
import json
import os
import random
import re
import shutil
import tempfile
import unittest

from tests import owui  # installs the pydantic shim when pydantic is missing
from tests import owui_internals
from tests.fake_moneyhub import FakeMoneyHub, account, bill, ok_state
from tests.owui import extract_frontmatter, filter_model_args, load_tool, read_source, spec_properties, tool_specs

TEST_TIMEOUT_S = 30
NOAH = {"id": "noah-0001-admin", "role": "admin", "name": "Noah", "email": "noah@example.invalid"}
ALEX = {"id": "alex-0002-user", "role": "user", "name": "Alex", "email": "alex@example.invalid"}
TZ = datetime.timezone(datetime.timedelta(hours=-4))
NOW = datetime.datetime(2026, 10, 28, 9, 30, tzinfo=TZ)
PUBLIC = {"compute_split", "save_payday_run", "last_payday_run", "check_last_run"}
REQUEST = object()  # stands in for the chat's FastAPI Request (the stubs never touch it)
SPLIT_ACCOUNTS = {"Free spending": "CU Spending", "Savings A": "CU Savings A", "Savings B": "CU Savings B", "Savings C": "CU Savings C"}
# A bill with no match pattern that isn't marked paid (Lead, W4-3): never "not seen".
PHONE_NOT_MARKED = "- NOT MARKED PAID \u2014 Phone: $100.00. Not marked paid in Money Hub (no bank match pattern, so the bank wasn't checked)."
FENCE_RE = re.compile(r"\n````\n(.*)\n````\Z", re.S)


def run(coro):
    return asyncio.run(coro)


class TimedCase(unittest.TestCase):
    def setUp(self):
        faulthandler.dump_traceback_later(TEST_TIMEOUT_S, exit=True)
        self.addCleanup(faulthandler.cancel_dump_traceback_later)


SOURCE = "CU Main Holding"


def open_window(case):
    """Lift a run's own window bounds (the deposit bound, its fallback and the 12-day cap) for tests
    that are about matching. Only these module attributes change, on this test's own module copy."""
    case.mod._check_window_end = lambda run: datetime.date(2100, 1, 1)
    case.mod._deposit_bound = lambda *args: (None, None)
    case.mod._WINDOW_MAX = 31
    case.tool.valves.match_window_days = 31


class Dialog:
    """Scripted __event_call__: answers in order and records every payload. HANG never returns.
    `before` runs at call time (e.g. to prove nothing was written before the answer)."""

    HANG = object()

    def __init__(self, *answers, before=None):
        self.answers = list(answers)
        self.payloads = []
        self.before = before

    async def __call__(self, payload):
        self.payloads.append(payload)
        if self.before:
            self.before()
        answer = self.answers.pop(0) if self.answers else Dialog.HANG
        if answer is Dialog.HANG:
            await asyncio.sleep(3600)
        if isinstance(answer, BaseException):
            raise answer
        return answer


class Base(TimedCase):
    def setUp(self):
        super().setUp()
        self.dir = tempfile.mkdtemp(prefix="payday-runs-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.owui = owui_internals.install()
        self.owui.add_user(NOAH["id"], role="admin")
        self.owui.add_user(ALEX["id"], role="user", name="Alex")
        self.tool, _, self.mod = load_tool()
        self.tool.valves.runs_dir = self.dir
        self.tool.valves.split_accounts = json.dumps(SPLIT_ACCOUNTS)
        self.tool.valves.source_account = "CU Main Holding"
        self.tool.valves.bills_account = "CU Bills"
        self.tool.valves.timeout_s = 5
        self.mod._DIALOG_MIN_S = 0
        self.tool.valves.dialog_timeout_s = 0.3
        self.tool._now = lambda: (NOW, None)

    def save(self, dialog=None, user=NOAH, request=REQUEST, **kw):
        args = {
            "date": "2026-10-22",
            "check": "15th",
            "paycheck": "1000.00",
            "bills": [{"name": "Phone", "amount": "100"}, {"name": "Insurance", "amount": "145.50", "id": 9}],
            "card_payment": "0",
        }
        args.update(kw)
        dialog = dialog if dialog is not None else Dialog(True)
        return run(self.tool.save_payday_run(**args, __user__=user, __event_call__=dialog, __request__=request))

    def files(self):
        return sorted(os.listdir(self.dir))

    def read(self, name):
        with open(os.path.join(self.dir, name), encoding="utf-8") as f:
            return f.read()

    def kb(self):
        return [k for k in self.owui.knowledge if k.name == "Payday runs"]


# ---------------------------------------------------------------- the arithmetic


class SplitMath(Base):
    def test_largest_remainder_known_case(self):
        # 1001 cents by 15/30/30/15/10: floors 150/300/300/150/100 = 1000; the one cent left goes to
        # the largest remainder (3000, Savings A before Savings B).
        self.assertEqual(self.mod.largest_remainder(1001, [1500, 3000, 3000, 1500, 1000]), [150, 301, 300, 150, 100])
        self.assertEqual(self.mod.largest_remainder(1, [3333, 3333, 3334]), [0, 0, 1])
        self.assertEqual(self.mod.largest_remainder(2, [3333, 3333, 3334]), [1, 0, 1])
        self.assertEqual(self.mod.largest_remainder(100, [3333, 3333, 3334]), [33, 33, 34])
        self.assertEqual(self.mod.largest_remainder(0, [5000, 5000]), [0, 0])
        self.assertEqual(self.mod.largest_remainder(5, [10000, 0]), [5, 0])

    def test_largest_remainder_always_exact_and_fair(self):
        rnd = random.Random(1234)
        for _ in range(3000):
            n = rnd.randint(1, 8)
            cuts = sorted(rnd.randint(0, 10000) for _ in range(n - 1))
            weights = [b - a for a, b in zip([0, *cuts], [*cuts, 10000], strict=True)]
            total = rnd.choice([rnd.randint(0, 200), rnd.randint(0, 10**7)])
            parts = self.mod.largest_remainder(total, weights)
            self.assertEqual(sum(parts), total, (total, weights))
            for p, w in zip(parts, weights, strict=True):
                exact = total * w / 10000
                self.assertLess(abs(p - exact), 1, (total, weights, parts))
                if w == 0:
                    self.assertEqual(p, 0)

    def test_largest_remainder_refuses_bad_input(self):
        for total, weights in ((-1, [10000]), (1.5, [10000]), (True, [10000]), (5, [5000]), (5, [5000, 4000]), (5, []), (5, [-1, 10001])):
            with self.assertRaises(ValueError):
                self.mod.largest_remainder(total, weights)

    def test_default_split_of_1000_minus_bills(self):
        out = run(self.tool.compute_split("1000.00", [{"name": "Phone", "amount": "100"}, {"name": "Insurance", "amount": 145.5}], "0"))
        # 1000.00 - 245.50 = 754.50; 15% = 113.175, 30% = 226.35, 10% = 75.45: floors 113.17 / 226.35 /
        # 226.35 / 113.17 / 75.45 = 754.49; the cent goes to Free spending (remainder 0.5, first).
        self.assertIn("- Left to split (paycheck minus bills and the card payment): $754.50", out)
        self.assertIn("  - Free spending, 15%: $113.18 to CU Spending", out)
        self.assertIn("  - Savings A, 30%: $226.35 to CU Savings A", out)
        self.assertIn("  - Savings B, 30%: $226.35 to CU Savings B", out)
        self.assertIn("  - Savings C, 15%: $113.17 to CU Savings C", out)
        self.assertIn("  - Unassigned, 10%: $75.45 (stays in CU Main Holding)", out)
        self.assertIn("1. $245.50 from CU Main Holding to CU Bills (Bills)", out)
        self.assertIn("2. $113.18 from CU Main Holding to CU Spending (Free spending)", out)
        self.assertIn("= $1,000.00 = the paycheck $1,000.00.", out)
        self.assertIn("Nothing was saved", out)

    def test_custom_split_rest_unassigned(self):
        out = run(self.tool.compute_split("100", None, "10", [{"name": "Car", "percent": "12.5", "account": "CU Car"}, {"name": "Savings C", "pct": 50}]))
        self.assertIn("  - Car, 12.5%: $11.25 to CU Car", out)
        self.assertIn("  - Savings C, 50%: $45.00 to CU Savings C", out)
        self.assertIn("  - Unassigned, 37.5%: $33.75", out)
        self.assertIn("2. $11.25 from CU Main Holding to CU Car (Car)", out)
        self.assertIn("1. $10.00 paid from CU Main Holding for the card payment (Card payment)", out)

    def test_split_as_mapping_and_json_text(self):
        a = run(self.tool.compute_split("200", "", "0", {"Car": 50, "Fun": 50}))
        b = run(self.tool.compute_split("200", "[]", "0", json.dumps([{"name": "Car", "percent": 50}, {"name": "Fun", "percent": 50}])))
        self.assertEqual(a, b)
        self.assertIn("  - Unassigned, 0%: $0.00", a)

    def test_shortfall(self):
        out = run(self.tool.compute_split("100", [{"name": "Rent", "amount": "150"}], "5"))
        self.assertIn("SHORTFALL: bills and the card payment are $55.00 more than this paycheck", out)
        self.assertIn("- Left to split (paycheck minus bills and the card payment): $0.00", out)
        self.assertNotIn("Check (computed)", out)

    def test_amount_rules(self):
        ok = {"1000": 100000, "1000.5": 100050, "$1,234.56": 123456, " 12.30 ": 1230, 7: 700, 0.5: 50, "0.05": 5}
        for raw, cents in ok.items():
            self.assertEqual(self.mod._amount(raw, "x"), cents, raw)
        for raw in ("1.234", "12,34", "1e3", "-5", "abc", "", None, True, 0.1 + 0.2, float("nan"), float("inf"), "$-5", [], "100000000.01"):
            with self.assertRaises(self.mod._BadArgument, msg=repr(raw)):
                self.mod._amount(raw, "x")
        self.assertEqual(self.mod._amount("-5", "x", negative=True), -500)
        with self.assertRaises(self.mod._BadArgument):
            self.mod._amount("0", "x")
        self.assertEqual(self.mod._amount("0", "x", zero=True), 0)

    def test_bad_arguments_are_explained(self):
        cases = [
            (("abc",), "Invalid paycheck"),
            (("1000", [{"name": "", "amount": 1}]), "Invalid name of bill 1"),
            (("1000", [{"name": "X", "amount": 1, "amount_cents": 100}]), "not both"),
            (("1000", [{"name": "X"}]), "has no amount"),
            (("1000", [{"name": "X", "amount": "-4"}]), "can't be negative"),
            (("1000", [{"name": "X", "amount": 1, "id": "abc"}]), "Money Hub bill id"),
            (("1000", None, "0", [{"name": "A", "percent": 60}, {"name": "B", "percent": 41}]), "101%, more than 100%"),
            (("1000", None, "0", [{"name": "A", "percent": 0}]), "more than 0"),
            (("1000", None, "0", [{"name": "A", "percent": "10.125"}]), "Invalid percent"),
            (("1000", None, "0", [{"name": "A", "percent": 10}, {"name": "a", "percent": 10}]), "appears twice"),
            (("1000", None, "0", [{"name": "Unassigned", "percent": 10}]), "reserved"),
            (("1000", "not json"), "Invalid bills"),
            (("1000", [{"name": "X", "amount": 1}] * 41), "Too many bills"),
        ]
        for args, needle in cases:
            out = run(self.tool.compute_split(*args))
            self.assertIn(needle, out, args)
            self.assertNotIn("Left to split", out)

    def test_amount_cents_form(self):
        out = run(self.tool.compute_split("10", [{"name": "X", "amount_cents": 250}]))
        self.assertIn("  - X: $2.50", out)

    def test_bad_split_accounts_valve(self):
        self.tool.valves.split_accounts = "[1, 2]"
        out = run(self.tool.compute_split("10"))
        self.assertIn("split_accounts valve", out)

    def test_brackets_and_markup_are_neutralised(self):
        out = run(self.tool.compute_split("10", [{"name": "[a] (b) <c> !d `e` |f| @g &h *i* _j_ ~k~ \\l {m} \"n\" #o", "amount": 1}]))
        line = next(ln for ln in out.splitlines() if ln.startswith("  - ") and "a" in ln and "$1.00" in ln)
        for ch in '[]()<>!`|@&*_~\\{}"#':
            self.assertNotIn(ch, line.split(": $1.00")[0][4:], ch)
        self.assertIn("\uff3ba\uff3d", line)

    def test_split_to_the_source_account_is_not_a_transfer(self):
        out = run(self.tool.compute_split("100", None, "0", [{"name": "Keep", "percent": 50, "account": "cu main holding"}]))
        self.assertIn("- $50.00 for Keep stays in CU Main Holding", out)
        self.assertNotIn("(Keep)", out)


# ---------------------------------------------------------------- saving


class Save(Base):
    def test_confirmed_save_writes_exactly_what_the_dialog_showed(self):
        d = Dialog(True)
        out = self.save(d)
        self.assertEqual(self.files(), ["2026-10-22_15th.md"])
        self.assertEqual(len(d.payloads), 1)
        p = d.payloads[0]
        self.assertEqual(p["type"], "confirmation")
        shown = FENCE_RE.search(p["data"]["message"]).group(1)
        self.assertEqual(self.read("2026-10-22_15th.md"), shown)
        self.assertIn("Saved Payday run 2026-10-22 (15th check).", out)
        self.assertIn("- PC: saved as 50-finance/payday-runs/2026-10-22_15th.md (a new file).", out)
        self.assertIn("- Knowledge: added to your private collection Payday runs (created now).", out)
        self.assertEqual(oct(os.stat(os.path.join(self.dir, "2026-10-22_15th.md")).st_mode & 0o777), "0o640")

    def test_file_format(self):
        self.save(balances=[{"account": "CU Spending", "amount": "-12.34"}], notes="ok")
        text = self.read("2026-10-22_15th.md")
        head = text.split("\n---\n", 1)[0].splitlines()
        self.assertEqual(head[0], "---")
        self.assertEqual(head[1], 'title: "Payday run 2026-10-22 (15th check)"')
        self.assertEqual(head[2], "source: payday-tool")
        self.assertEqual(head[3], "added: 2026-10-28")
        self.assertEqual(head[4], "tags: [finance, payday]")
        self.assertTrue(head[5].startswith('summary: "15th check $1,000.00: bills $245.50, card payment $0.00, split Free spending $113.18'))
        self.assertEqual(head[6], "related: []")
        for section in ("## Paycheck", "## Deposits", "## Balances given", "## Plan (computed by the tool)", "## Notes", "## Machine-readable record"):
            self.assertIn("\n" + section + "\n", text)
        self.assertIn("- CU Spending: -$12.34", text)
        record = json.loads(text.split("```json\n", 1)[1].split("\n```", 1)[0])
        self.assertEqual(record["format"], "payday-run/1")
        self.assertEqual(record["period"], "2026-10")
        self.assertEqual(record["paycheck_cents"], 100000)
        self.assertEqual([s["amount_cents"] for s in record["split"]], [11318, 22635, 22635, 11317])
        self.assertEqual(record["unassigned"], {"bp": 1000, "amount_cents": 7545})
        self.assertEqual(record["bills"][1], {"name": "Insurance", "amount_cents": 14550, "id": 9})
        self.assertEqual([t["to"] for t in record["transfers"]], ["CU Bills", "CU Spending", "CU Savings A", "CU Savings B", "CU Savings C"])
        # The library's front-matter parser accepts it (apps/skill-library is optional here).
        try:
            import sys

            sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "skill-library"))
            from library import frontmatter, naming
        except ImportError:
            self.skipTest("apps/skill-library is not next to this app")
        meta, _ = frontmatter.parse(text)
        for key in frontmatter.REQUIRED:
            self.assertIn(key, meta)
        self.assertEqual(meta["tags"], ["finance", "payday"])
        self.assertEqual(meta["source"], "payday-tool")
        # The library's own naming rule (and its own "-n" collision suffix, naming.item_name).
        for name in ("2026-10-22_15th.md", "2026-10-22_15th-2.md", "2026-10-30_1st-12.md"):
            self.assertIsNotNone(naming.parse_item_name(name), name)
        self.assertEqual(naming.item_name("2026-10-22", "15th", "md", 2), "2026-10-22_15th-2.md")

    def test_model_figures_are_never_saved_unrecomputed(self):
        # A split amount from the model is ignored: the tool recomputes it.
        self.save(splits=[{"name": "Savings C", "percent": 50, "amount": "9999"}])
        record = json.loads(self.read("2026-10-22_15th.md").split("```json\n", 1)[1].split("\n```", 1)[0])
        self.assertEqual(record["split"], [{"name": "Savings C", "bp": 5000, "account": "CU Savings C", "amount_cents": 37725}])

    def test_nothing_is_written_before_the_answer(self):
        seen = []
        d = Dialog(True, before=lambda: seen.append((self.files(), dict(self.owui.files), list(self.owui.knowledge))))
        self.save(d)
        self.assertEqual(seen, [([], {}, [])])
        self.assertEqual(len(self.files()), 1)
        self.assertEqual(len(self.owui.files), 1)

    def test_nothing_saved_without_a_confirm(self):
        for answer in (False, None, "yes", "Confirm", 1, {"error": "Client session disconnected."}, {"confirmed": True}, RuntimeError("boom"), Dialog.HANG):
            d = Dialog(answer)
            out = self.save(d)
            self.assertTrue(out.startswith("Nothing was saved"), (answer, out))
            self.assertEqual(self.files(), [], answer)
            self.assertEqual(self.owui.files, {}, answer)
            self.assertEqual(self.kb(), [], answer)

    def test_cancel_and_timeout_are_named(self):
        self.assertIn("Noah cancelled", self.save(Dialog(False)))
        self.assertIn("no answer in time", self.save(Dialog(Dialog.HANG)))
        self.assertIn("without a confirmation", self.save(Dialog({"error": "x"})))
        self.assertIn("could not be shown or failed", self.save(Dialog(RuntimeError("socket gone"))))

    def test_no_dialog_no_save(self):
        out = run(self.tool.save_payday_run("2026-10-22", "15th", "1000", __user__=NOAH, __event_call__=None, __request__=REQUEST))
        self.assertIn("can't be shown here", out)
        self.assertEqual(self.files(), [])

    def test_non_admin_refused_before_anything(self):
        for user in (ALEX, None, {}, {"id": "x y", "role": "admin"}, {"role": "admin"}, dict(NOAH, role="pending")):
            d = Dialog(True)
            out = self.save(d, user=user)
            self.assertIn("Only Noah's admin account can save payday runs", out)
            self.assertEqual(d.payloads, [])
            self.assertEqual(self.files(), [])
            self.assertEqual(self.owui.calls, [])

    def test_bad_arguments_refused_before_the_dialog(self):
        cases = [
            {"date": "2026-10-29"},  # the future
            {"date": "28/10/2026"},
            {"date": "2026-02-30"},
            {"check": "2nd"},
            {"paycheck": "lots"},
            {"notes": "x" * 1001},
            {"balances": [{"amount": "5"}]},
            {"deposits": [{"amount": "5", "date": "2026-13-01"}]},
        ]
        for kw in cases:
            d = Dialog(True)
            out = self.save(d, **kw)
            self.assertIn("Invalid", out, kw)
            self.assertEqual(d.payloads, [], kw)
        self.assertEqual(self.files(), [])

    def test_never_overwrites(self):
        with open(os.path.join(self.dir, "2026-10-22_15th.md"), "w") as f:
            f.write("Noah's earlier file\n")
        self.save()
        self.save()
        self.assertEqual(self.files(), ["2026-10-22_15th-2.md", "2026-10-22_15th-3.md", "2026-10-22_15th.md"])
        self.assertEqual(self.read("2026-10-22_15th.md"), "Noah's earlier file\n")
        self.assertEqual(self.read("2026-10-22_15th-2.md"), self.read("2026-10-22_15th-3.md"))

    def test_symlink_at_the_name_is_not_followed(self):
        outside = tempfile.mkdtemp(prefix="outside-")
        self.addCleanup(shutil.rmtree, outside, True)
        target = os.path.join(outside, "victim.md")
        with open(target, "w") as f:
            f.write("untouched\n")
        os.symlink(target, os.path.join(self.dir, "2026-10-22_15th.md"))
        os.symlink(os.path.join(outside, "dangling.md"), os.path.join(self.dir, "2026-10-22_15th-2.md"))
        out = self.save()
        self.assertIn("2026-10-22_15th-3.md", out)
        with open(target) as f:
            self.assertEqual(f.read(), "untouched\n")
        self.assertFalse(os.path.exists(os.path.join(outside, "dangling.md")))
        self.assertTrue(os.path.islink(os.path.join(self.dir, "2026-10-22_15th.md")))

    def test_runs_dir_symlink_refused(self):
        real = tempfile.mkdtemp(prefix="real-")
        self.addCleanup(shutil.rmtree, real, True)
        link = os.path.join(self.dir, "link")
        os.symlink(real, link)
        self.tool.valves.runs_dir = link
        d = Dialog(True)
        out = self.save(d)
        self.assertIn("runs_dir valve is not an absolute path without symlinks", out)
        self.assertEqual(os.listdir(real), [])
        self.assertEqual(d.payloads, [])
        # A symlinked parent is refused too.
        os.makedirs(os.path.join(real, "sub"))
        self.tool.valves.runs_dir = os.path.join(link, "sub")
        self.assertIn("runs_dir valve", self.save(Dialog(True)))
        self.assertEqual(os.listdir(os.path.join(real, "sub")), [])

    def test_runs_dir_valve_rules(self):
        for bad in ("relative/dir", "", "/tmp/../etc", "/a\x00b"):
            self.tool.valves.runs_dir = bad
            self.assertIn("runs_dir valve", self.save(Dialog(True)), bad)
        self.tool.valves.runs_dir = os.path.join(self.dir, "missing")
        self.assertIn("not available", self.save(Dialog(True)))

    def test_not_mounted_marker_refuses_before_the_dialog(self):
        with open(os.path.join(self.dir, "PAYDAY-RUNS-NOT-MOUNTED"), "w"):
            pass
        d = Dialog(True)
        out = self.save(d)
        self.assertIn("not mounted", out)
        self.assertIn("Nothing was saved", out)
        self.assertEqual(d.payloads, [])
        self.assertEqual(self.files(), ["PAYDAY-RUNS-NOT-MOUNTED"])

    def test_marker_appearing_after_the_dialog(self):
        marker = os.path.join(self.dir, "PAYDAY-RUNS-NOT-MOUNTED")
        out = self.save(Dialog(True, before=lambda: open(marker, "w").close()))
        self.assertIn("NOTHING WAS SAVED", out)
        self.assertIn("not touched", out)
        self.assertEqual(self.files(), ["PAYDAY-RUNS-NOT-MOUNTED"])
        self.assertEqual(self.kb(), [])

    def test_write_failure_leaves_no_partial_file(self):
        real_write = os.write

        def failing(fd, data):
            raise OSError(28, "No space left on device")

        self.mod.os.write = failing
        try:
            out = self.save()
        finally:
            self.mod.os.write = real_write
        self.assertIn("NOTHING WAS SAVED", out)
        self.assertIn("No space left on device", out)
        self.assertEqual(self.files(), [])
        self.assertEqual(self.kb(), [])

    def test_dialog_and_file_are_inert(self):
        evil = "![x](https://evil.example/p.png) <img src=x> `code` **b** _i_ ~~s~~ # h | t @ & {x} \"q\" \\ \u202eabc\u200b www.evil.example"
        d = Dialog(True)
        self.save(d, bills=[{"name": evil[:60], "amount": "1"}], notes=evil, deposits=[{"amount": "1", "description": evil[:60], "account": "A"}])
        msg = d.payloads[0]["data"]["message"]
        text = self.read("2026-10-22_15th.md")
        for blob in (msg, text):
            for bad in ("![", "](", "<img", "`code`", "**b**", "_i_", "~~", "https://", "www.evil", "\u202e", "\u200b", "@", "&"):
                self.assertNotIn(bad, blob.replace("**Nothing is saved unless you click Confirm.**", "").replace("**Payday runs**", ""), bad)
        # Outside the fence, the dialog carries no data at all.
        outside = msg.split("\n````\n", 1)[0]
        self.assertNotIn("evil", outside)
        self.assertEqual(msg.count("````"), 2)
        self.assertNotIn("````", text)
        json.loads(text.split("```json\n", 1)[1].split("\n```", 1)[0])  # still valid JSON

    def test_dialogs_never_overlap(self):
        active = []
        peak = []

        async def slow(payload):
            active.append(1)
            peak.append(len(active))
            await asyncio.sleep(0.02)
            active.pop()
            return True

        async def many():
            return await asyncio.gather(
                *(self.tool.save_payday_run("2026-10-22", "15th", "1000", __user__=NOAH, __event_call__=slow, __request__=REQUEST) for _ in range(4))
            )

        self.tool.valves.dialog_timeout_s = 5
        run(many())
        self.assertEqual(max(peak), 1)
        self.assertEqual(len(self.files()), 4)

    def test_waiting_for_the_lock_is_bounded(self):
        async def go():
            await self.mod._DIALOG_LOCK.acquire()
            try:
                return await self.tool.save_payday_run("2026-10-22", "15th", "1000", __user__=NOAH, __event_call__=Dialog(True), __request__=REQUEST)
            finally:
                self.mod._DIALOG_LOCK.release()

        out = run(go())
        self.assertEqual(out, "Another save was in progress (its dialog was still open), so this dialog was never shown; nothing was saved.")
        self.assertEqual(self.files(), [])

    def test_model_cannot_pass_approval_or_context(self):
        for name in PUBLIC:
            self.assertFalse(any(p.startswith("__") for p in spec_properties(self.tool, name)))
        args = {"date": "2026-10-22", "check": "15th", "paycheck": "1000", "__event_call__": Dialog(True), "__user__": NOAH, "confirmed": True}
        kept = filter_model_args(self.tool, "save_payday_run", args)
        self.assertEqual(set(kept), {"date", "check", "paycheck"})
        with self.assertRaises(TypeError):
            run(self.tool.save_payday_run("2026-10-22", "15th", "1000", confirmed=True, __user__=NOAH, __event_call__=Dialog(True)))

    def test_check_derives_the_period(self):
        self.tool._now = lambda: (datetime.datetime(2026, 10, 31, 12, 0, tzinfo=TZ), None)
        self.save(date="2026-10-30", check="1st")
        record = json.loads(self.read("2026-10-30_1st.md").split("```json\n", 1)[1].split("\n```", 1)[0])
        self.assertEqual(record["period"], "2026-11")
        self.save(date="2026-10-01", check="1st")
        self.assertIn('"period": "2026-10"', self.read("2026-10-01_1st.md"))
        self.save(date="2026-10-14", check="15")
        self.assertIn('"period": "2026-10"', self.read("2026-10-14_15th.md"))


class Knowledge(Base):
    def test_created_private_and_reused(self):
        self.save()
        self.save()
        kbs = self.kb()
        self.assertEqual(len(kbs), 1)
        self.assertEqual(kbs[0].user_id, NOAH["id"])
        self.assertEqual(kbs[0].access_grants, [])
        created = [c for c in self.owui.calls if c[0] == "create_new_knowledge"]
        self.assertEqual(created, [("create_new_knowledge", NOAH["id"], "Payday runs", [])])
        names = sorted(f.filename for f in self.owui.files_in(kbs[0].id))
        self.assertEqual(names, ["2026-10-22_15th-2.md", "2026-10-22_15th.md"])
        stored = self.owui.storage[self.owui.files_in(kbs[0].id)[0].path].decode()
        self.assertIn(stored, (self.read("2026-10-22_15th.md"), self.read("2026-10-22_15th-2.md")))
        upload = [c for c in self.owui.calls if c[0] == "upload_file_handler"][0]
        self.assertEqual(upload[3:], (True, False))  # processed now, not in the background

    def test_same_bytes_as_the_pc_file(self):
        self.save()
        f = self.owui.files_in(self.kb()[0].id)[0]
        with open(os.path.join(self.dir, "2026-10-22_15th.md"), "rb") as fh:
            self.assertEqual(self.owui.storage[f.path], fh.read())
        self.assertEqual(f.meta["content_type"], "text/markdown")

    def test_oldest_of_two_is_used_and_others_ignored(self):
        old = self.owui.add_knowledge(NOAH["id"], "Payday runs", created_at=100)
        self.owui.add_knowledge(NOAH["id"], "Payday runs", created_at=200)
        self.owui.add_knowledge(ALEX["id"], "Payday runs", created_at=50)
        self.owui.add_knowledge(NOAH["id"], "payday runs", created_at=10)
        self.save()
        self.assertEqual([f.filename for f in self.owui.files_in(old.id)], ["2026-10-22_15th.md"])
        self.assertEqual(sum(len(self.owui.files_in(k.id)) for k in self.owui.knowledge), 1)

    def test_shared_collection_is_not_used(self):
        kb = self.owui.add_knowledge(NOAH["id"], "Payday runs", grants=[{"principal_type": "user", "principal_id": "*", "permission": "read"}])
        out = self.save()
        self.assertTrue(out.startswith("PARTLY SAVED"), out)
        self.assertIn("shared with others", out)
        self.assertEqual(self.files(), ["2026-10-22_15th.md"])
        self.assertEqual(self.owui.files_in(kb.id), [])
        self.assertEqual(self.owui.files, {})

    def test_external_collection_is_not_used(self):
        self.owui.add_knowledge(NOAH["id"], "Payday runs", meta={"source": "external"})
        out = self.save()
        self.assertIn("PARTLY SAVED", out)
        self.assertIn("external", out)

    def test_processing_failure_is_reported_not_hidden(self):
        self.owui.fail_index = "Tika is down"
        out = self.save()
        self.assertTrue(out.startswith("PARTLY SAVED: Payday run 2026-10-22 (15th check) is on the PC but NOT in the knowledge collection."), out)
        self.assertIn("- PC: saved as 50-finance/payday-runs/2026-10-22_15th.md", out)
        self.assertIn("NOT added: processing ended as failed (Tika is down)", out)
        self.assertIn("Do not call save again", out)
        self.assertEqual(self.files(), ["2026-10-22_15th.md"])

    def test_link_failure_is_reported(self):
        self.owui.fail_link = True
        out = self.save()
        self.assertIn("PARTLY SAVED", out)
        self.assertIn("not in the collection", out)

    def test_completed_but_not_linked_is_a_failure(self):
        # 0.12.4's auto-link gate only logs a refusal; the file still ends "completed".
        self.owui.deny_link_gate = True
        out = self.save()
        self.assertTrue(out.startswith("PARTLY SAVED"), out)
        self.assertIn("NOT added: processing ended as completed; the file is not in the collection", out)
        self.assertEqual(self.owui.files_in(self.kb()[0].id), [])

    def test_two_first_saves_create_one_collection(self):
        async def yield_a_while():
            await asyncio.sleep(0.05)

        self.owui.on_list = yield_a_while
        self.tool.valves.dialog_timeout_s = 5

        async def two():
            return await asyncio.gather(
                *(self.tool.save_payday_run("2026-10-22", "15th", "1000", __user__=NOAH, __event_call__=Dialog(True), __request__=REQUEST) for _ in range(2))
            )

        outs = run(two())
        self.assertEqual(len(self.kb()), 1, outs)
        self.assertEqual(len(self.owui.files_in(self.kb()[0].id)), 2)
        self.assertEqual(sum("(created now)" in o for o in outs), 1)

    def test_upload_refused(self):
        self.owui.allowed_extensions = ["pdf", "txt"]
        out = self.save()
        self.assertIn("PARTLY SAVED", out)
        self.assertIn("the upload was refused (File type md is not allowed)", out)
        self.owui.allowed_extensions = []
        self.owui.fail_insert_file = True
        self.assertIn("the upload was refused (Error uploading file)", self.save())

    def test_collection_create_failure(self):
        self.owui.fail_insert_knowledge = True
        out = self.save()
        self.assertIn("PARTLY SAVED", out)
        self.assertIn("creating the collection failed", out)

    def test_no_request_or_no_open_webui(self):
        out = self.save(request=None)
        self.assertIn("PARTLY SAVED", out)
        self.assertIn("no Open WebUI request context", out)
        owui_internals.uninstall_open_webui()
        try:
            out = self.save()
        finally:
            owui_internals.install()
        self.assertIn("could not be loaded", out)
        self.assertEqual(len(self.files()), 2)

    def test_user_demoted_since_the_call_started(self):
        self.owui.users[NOAH["id"]].role = "user"
        out = self.save()
        self.assertIn("PARTLY SAVED", out)
        self.assertIn("not an admin", out)

    def test_slow_indexing_is_reported_as_unknown(self):
        self.mod._KNOWLEDGE_MIN_S = 0
        self.tool.valves.knowledge_timeout_s = 0.2

        async def slow():
            await asyncio.sleep(1)

        self.owui.on_upload = slow
        out = self.save()
        self.assertIn("the knowledge step is not finished", out)
        self.assertIn("still running", out)
        self.assertNotIn("PARTLY SAVED", out)

    def test_error_after_upload_is_unknown_not_failed(self):
        async def boom(*a, **k):
            raise RuntimeError("db gone")

        original = owui_internals.Files.get_file_by_id
        owui_internals.Files.get_file_by_id = boom
        try:
            out = self.save()
        finally:
            owui_internals.Files.get_file_by_id = original
        self.assertIn("the knowledge step is not finished", out)
        self.assertIn("unknown whether", out)


# ---------------------------------------------------------------- reading back


class LastRun(Base):
    def test_newest_by_date_then_suffix(self):
        self.tool._now = lambda: (datetime.datetime(2026, 10, 31, 12, 0, tzinfo=TZ), None)
        self.save(date="2026-10-15", paycheck="1")
        self.save(date="2026-10-30", check="1st", paycheck="2")
        self.save(date="2026-10-30", check="1st", paycheck="3")
        self.save(date="2026-10-01", check="1st", paycheck="4")
        with open(os.path.join(self.dir, "notes.md"), "w") as f:
            f.write("not a run")
        out = run(self.tool.last_payday_run(__user__=NOAH))
        self.assertIn("Newest saved payday run: 50-finance/payday-runs/2026-10-30_1st-2.md", out)
        self.assertIn("- Paycheck: $3.00", out)
        self.assertIn("(bills period 2026-11)", out)

    def test_same_date_orders_by_period_then_check(self):
        # A date belongs to one check only (the save refuses the other), but a file of the other
        # check can still be put in the folder by hand. Ordering by period then check, before the -N
        # suffix, keeps the real run newest; the stray file is never picked over it.
        self.tool._now = lambda: (datetime.datetime(2026, 10, 31, 12, 0, tzinfo=TZ), None)
        # On the 30th a 1st-check run is next month's (early pay): newer than any 15th file that day.
        self.save(date="2026-10-30", check="1st", paycheck="1")
        with open(os.path.join(self.dir, "2026-10-30_15th-3.md"), "w") as f:
            f.write(self.read("2026-10-30_1st.md"))
        self.assertIn("2026-10-30_1st.md", run(self.tool.last_payday_run(__user__=NOAH)).splitlines()[0])
        for name in os.listdir(self.dir):
            os.unlink(os.path.join(self.dir, name))
        # On the 14th both are October's: the 15th is the later check, even against a 1st file's -2.
        self.save(date="2026-10-14", check="15th", paycheck="2")
        with open(os.path.join(self.dir, "2026-10-14_1st-2.md"), "w") as f:
            f.write(self.read("2026-10-14_15th.md"))
        self.assertIn("2026-10-14_15th.md", run(self.tool.last_payday_run(__user__=NOAH)).splitlines()[0])

    def test_no_runs_and_not_mounted(self):
        self.assertEqual(run(self.tool.last_payday_run(__user__=NOAH)), "No payday run has been saved yet.")
        open(os.path.join(self.dir, "PAYDAY-RUNS-NOT-MOUNTED"), "w").close()
        self.assertIn("not mounted", run(self.tool.last_payday_run(__user__=NOAH)))
        self.assertIn("not mounted", run(self.tool.check_last_run(__user__=NOAH)))

    def test_admin_only(self):
        self.save()
        for user in (ALEX, None):
            self.assertEqual(run(self.tool.last_payday_run(__user__=user)), "Only Noah's admin account can read saved payday runs.")
            self.assertEqual(run(self.tool.check_last_run(__user__=user)), "Only Noah's admin account can read saved payday runs.")

    def test_edited_or_broken_newest_is_reported_not_skipped(self):
        self.save(date="2026-10-15", check="15th")
        self.save()
        path = os.path.join(self.dir, "2026-10-22_15th.md")
        good = self.read("2026-10-22_15th.md")
        edits = {
            "figures": good.replace('"amount_cents": 11318', '"amount_cents": 21318', 1),
            "transfer": good.replace('"to": "CU Spending"', '"to": "CU Bills"', 1),
            "no json": good.replace("```json", "```"),
            "two json": good + "\n```json\n{}\n```\n",
            "bad json": good.replace('"format"', "format"),
            "unclosed": good.rsplit("```", 1)[0],
            "wrong format": good.replace("payday-run/1", "payday-run/2"),
            "big": good + "x" * 70000,
            "not utf8": None,
        }
        for label, text in edits.items():
            os.unlink(path)
            if text is None:
                with open(path, "wb") as f:
                    f.write(b"\xff\xfe")
            else:
                with open(path, "w") as f:
                    f.write(text)
            out = run(self.tool.last_payday_run(__user__=NOAH))
            self.assertIn("The newest saved run, 2026-10-22_15th.md, can't be used", out, label)
            self.assertNotIn("2026-10-15", out, label)
        os.unlink(path)
        os.symlink(os.path.join(self.dir, "2026-10-15_15th.md"), path)
        self.assertIn("can't be used", run(self.tool.last_payday_run(__user__=NOAH)))

    def test_text_from_a_file_is_inert(self):
        self.save(notes="safe")
        path = os.path.join(self.dir, "2026-10-22_15th.md")
        text = self.read("2026-10-22_15th.md").replace('"notes": "safe"', '"notes": "![x](https://evil.example/a.png) <script>"')
        with open(path, "w") as f:
            f.write(text)
        out = run(self.tool.last_payday_run(__user__=NOAH))
        self.assertNotIn("![", out)
        self.assertNotIn("<script", out)
        self.assertNotIn("https://", out)


# ---------------------------------------------------------------- checking against Money Hub


class Check(Base):
    def setUp(self):
        super().setUp()
        self.mh = FakeMoneyHub()
        self.addCleanup(self.mh.close)
        self.tool.valves.base_url = self.mh.url
        # These tests are about matching, not the window, so the run's own window bounds are lifted
        # here (test-only knobs, like _DIALOG_MIN_S): the window ends where Money Hub's data does.
        # The Window class tests the real bounds, which no knob touches.
        open_window(self)
        self.mh.bills["2026-10"] = [
            bill(9, "Insurance", 14550, pattern="acme ins"),
            bill(12, "Phone", 10000, pattern=""),
        ]
        # The run under test: 2026-10-15, 15th check, 1000.00 - 245.50 = 754.50 split by default.
        self.tool._now = lambda: (datetime.datetime(2026, 10, 15, 9, 0, tzinfo=TZ), None)
        self.save(date="2026-10-15", balances=[{"account": "CU Spending", "amount": "50"}, {"account": "Ghost", "amount": "1"}])
        self.tool._now = lambda: (NOW, None)
        self.mh.calls.clear()

    def check(self):
        return run(self.tool.check_last_run(__user__=NOAH))

    def line(self, out, needle):
        hits = [ln for ln in out.splitlines() if needle in ln]
        self.assertEqual(len(hits), 1, (needle, out))
        return hits[0]

    def all_transfers_done(self):
        for to, cents in (("CU Bills", 24550), ("CU Spending", 11318), ("CU Savings A", 22635), ("CU Savings B", 22635), ("CU Savings C", 11317)):
            self.mh.add_transfer("2026-10-15", "CU Main Holding", to, cents)

    def test_everything_done(self):
        self.all_transfers_done()
        self.mh.add_txn("2026-10-17", "CU Bills", -14550, "ACME INS 123")
        out = self.check()
        self.assertTrue(out.startswith("Data freshness: last good sync 2026-10-28 13:00"), out)
        self.assertIn("- DONE \u2014 Bills: $245.50 from CU Main Holding to CU Bills. Seen 2026-10-15.", out)
        self.assertIn("- DONE \u2014 Savings C: $113.17 from CU Main Holding to CU Savings C. Seen 2026-10-15.", out)
        self.assertIn(
            "- DONE \u2014 Insurance: $145.50. A matching bank payment was seen 2026-10-17 in CU Bills; not marked paid in Money Hub.", out
        )
        self.assertIn(
            PHONE_NOT_MARKED,
            out,
        )
        self.assertIn("Summary (computed): done 6, amount differs 0, not seen 0, not marked paid 1, can't verify 0, can't match 0.", out)
        self.assertIn("- CU Spending: $50.00 at the run; Money Hub now $150.00 (as of 2026-10-28, current)", out)
        self.assertIn("- Ghost: $1.00 at the run; not in Money Hub", out)
        self.assertTrue(out.rstrip().endswith("Noah moves every dollar himself."))

    def test_amount_differs_and_not_seen(self):
        self.mh.add_transfer("2026-10-16", "CU Main Holding", "CU Bills", 24000)
        self.mh.add_transfer("2026-10-15", "CU Main Holding", "CU Savings C", 11317)
        self.mh.add_txn("2026-10-20", "CU Bills", -15000, "Acme Ins")
        out = self.check()
        self.assertIn("- AMOUNT DIFFERS \u2014 Bills: $245.50 from CU Main Holding to CU Bills. Planned $245.50, seen $240.00 on 2026-10-16.", out)
        self.assertIn("- DONE \u2014 Savings C", out)
        self.assertIn(
            "- NOT SEEN \u2014 Savings A: $226.35 from CU Main Holding to CU Savings A. Not seen as of last sync 2026-10-28 09:00: no "
            "transfer between these accounts from 2026-10-15 to 2026-10-28.",
            out,
        )
        self.assertIn("- AMOUNT DIFFERS \u2014 Insurance: $145.50. Planned $145.50, seen $150.00 on 2026-10-20 in CU Bills", out)
        self.assertIn("done 1, amount differs 2, not seen 3, not marked paid 1, can't verify 0", out)

    def test_marked_paid_counts_as_done(self):
        self.mh.bills["2026-10"][0]["paid"] = True  # Insurance, with a pattern, no bank payment
        self.mh.bills["2026-10"][1]["paid"] = True  # Phone, no pattern
        out = self.check()
        self.assertIn(
            "- DONE \u2014 Insurance: $145.50. Marked paid in Money Hub; no matching bank payment from 2026-10-15 to 2026-10-28 "
            "(as of last sync 2026-10-28 09:00).",
            out,
        )
        self.assertIn(
            "- DONE \u2014 Phone: $100.00. Marked paid in Money Hub; Money Hub has no match pattern for this bill, so there is no bank "
            "evidence.",
            out,
        )

    def test_marked_paid_shows_bank_evidence_alongside(self):
        self.mh.bills["2026-10"][0]["paid"] = True
        self.mh.add_txn("2026-10-18", "CU Bills", -14550, "acme ins autopay")
        self.assertIn(
            "- DONE \u2014 Insurance: $145.50. Marked paid in Money Hub; a matching bank payment was seen 2026-10-18 in CU Bills.",
            self.check(),
        )
        self.mh.txns.clear()
        self.mh.add_txn("2026-10-18", "CU Bills", -14900, "ACME INS")
        self.assertIn(
            "- DONE \u2014 Insurance: $145.50. Marked paid in Money Hub; a matching bank payment of $149.00 was seen 2026-10-18 in "
            "CU Bills (planned $145.50).",
            self.check(),
        )

    def test_only_a_true_paid_flag_counts(self):
        for flag in ("true", 1, "yes", None, False):
            self.mh.bills["2026-10"][1]["paid"] = flag
            self.assertIn(PHONE_NOT_MARKED, self.check(), flag)

    def test_paid_flag_of_the_runs_period_only(self):
        self.mh.bills["2026-11"] = [bill(12, "Phone", 10000, paid=True)]
        self.assertIn(PHONE_NOT_MARKED, self.check())
        q = [c["query"] for c in self.mh.calls if c["path"] == "/api/bills"]
        self.assertEqual(q, [{"period": "2026-10"}])

    def test_marked_paid_is_done_while_sync_is_failing(self):
        self.all_transfers_done()
        self.mh.bills["2026-10"][0]["paid"] = True
        self.mh.state = dict(ok_state("2026-10-05T13:00:00"), sync_failing=True)
        out = self.check()
        self.assertIn(
            "- DONE \u2014 Insurance: $145.50. Marked paid in Money Hub (bank not verified: bank sync stale since 2026-10-05).", out
        )
        self.assertIn("- CAN'T VERIFY \u2014 Phone: $100.00. Bank sync stale since 2026-10-05.", out)
        self.assertIn("- CAN'T VERIFY \u2014 Bills: $245.50", out)  # transfers stay unverified
        self.assertEqual([ln for ln in out.splitlines() if "- DONE" in ln], [ln for ln in out.splitlines() if "Insurance" in ln])
        self.assertIn("done 1, amount differs 0, not seen 0, not marked paid 0, can't verify 6, can't match 0.", out)
        self.assertNotIn("/api/transactions", self.mh.paths())

    def test_bill_found_by_id_when_renamed(self):
        self.mh.bills["2026-10"][0]["name"] = "Acme (renamed)"
        self.mh.add_txn("2026-10-16", "CU Bills", -14550, "acme ins")
        self.assertIn("- DONE \u2014 Insurance", self.check())

    def test_bill_missing_in_money_hub(self):
        self.mh.bills["2026-10"] = []
        out = self.check()
        self.assertIn("- CAN'T MATCH \u2014 Insurance: $145.50. No bill by this name in Money Hub for 2026-10.", out)

    def test_legs_must_be_close_in_time_and_inside_the_window(self):
        self.mh.add_txn("2026-10-15", "CU Main Holding", -24550, "x")
        self.mh.add_txn("2026-10-20", "CU Bills", 24550, "x")  # 5 days apart: not one transfer
        self.mh.add_transfer("2026-10-14", "CU Main Holding", "CU Savings C", 11317)  # before the run
        out = self.check()
        self.assertIn("- NOT SEEN \u2014 Bills", out)
        self.assertIn("- NOT SEEN \u2014 Savings C", out)
        q = [c["query"] for c in self.mh.calls if c["path"] == "/api/transactions"][0]
        # The query starts at C - 12 (2026-10-03) to find the run's own pay deposit; only rows from the
        # run date on are evidence.
        self.assertEqual((q["date_from"], q["date_to"]), ("2026-10-03", "2026-10-28"))

    def test_window_ends_at_the_valve(self):
        self.tool.valves.match_window_days = 3
        self.mh.add_transfer("2026-10-19", "CU Main Holding", "CU Savings C", 11317)
        self.mh.add_transfer("2026-10-17", "CU Main Holding", "CU Bills", 24550)
        out = self.check()
        self.assertIn(
            "Compared with Money Hub from 2026-10-15 to 2026-10-18 (last good sync 2026-10-28 09:00); the window closed after "
            "match_window_days (2026-10-18), so nothing is reported as not seen.",
            out,
        )
        self.assertIn("- DONE \u2014 Bills", out)  # inside the window: still counts
        self.assertIn(
            "- CAN'T VERIFY \u2014 Savings C: $113.17 from CU Main Holding to CU Savings C. The window closed after match_window_days "
            "(2026-10-18).",
            out,
        )
        # Only Phone (no pattern, so judged by Money Hub's current paid flag alone) may say not seen.
        self.assertEqual([ln for ln in out.splitlines() if "NOT SEEN" in ln and "Phone" not in ln], [])

    def test_each_transaction_counts_once(self):
        self.tool._now = lambda: (datetime.datetime(2026, 10, 28, 9, 0, tzinfo=TZ), None)
        self.save(splits=[{"name": "A", "percent": 50, "account": "CU Savings C"}, {"name": "B", "percent": 50, "account": "CU Savings C"}], bills=[])
        self.mh.add_transfer("2026-10-28", "CU Main Holding", "CU Savings C", 50000)
        out = self.check()
        self.assertIn("- DONE \u2014 A: $500.00", out)
        self.assertIn("- NOT SEEN \u2014 B: $500.00", out)

    def test_sync_failing_verifies_nothing(self):
        self.all_transfers_done()
        self.mh.add_txn("2026-10-17", "CU Bills", -14550, "ACME INS")
        self.mh.state = dict(ok_state("2026-10-05T13:00:26.147836"), sync_failing=True, sync_error="SimpleFIN 403", stale_accounts=8)
        out = self.check()
        self.assertTrue(out.startswith("\u26a0 BANK SYNC IS FAILING. Last good sync: 2026-10-05"), out)
        for label in ("Bills: $245.50", "Savings C: $113.17", "Insurance: $145.50", "Phone: $100.00"):
            self.assertIn(f"- CAN'T VERIFY \u2014 {label}", out)
        self.assertIn("Bank sync stale since 2026-10-05.", out)
        self.assertNotIn("DONE", out)
        self.assertNotIn("NOT SEEN", out)
        self.assertIn("done 0, amount differs 0, not seen 0, not marked paid 0, can't verify 7, can't match 0.", out)
        self.assertIn("Nothing was verified against the bank: can't verify (bank sync stale since 2026-10-05).", out)
        self.assertNotIn("/api/transactions", self.mh.paths())

    def test_sync_status_unknown_or_old_verifies_nothing(self):
        self.all_transfers_done()
        for state, reason in (
            (dict(ok_state(), sync_failing=None), "Money Hub's sync status is unknown; last good sync 2026-10-28 09:00"),
            ({k: v for k, v in ok_state().items() if k != "sync_failing"}, "Money Hub's sync status is unknown"),
            (dict(ok_state(), sync_failing="false"), "Money Hub's sync status is unknown"),
            (dict(ok_state(), sync_failing=0), "Money Hub's sync status is unknown"),
            (dict(ok_state(), sync_last_at=None), "no successful bank sync on record"),
            (ok_state("2026-10-14T23:00:00"), "no bank sync since the run was saved; last good sync 2026-10-14 19:00"),
            ([], "sync status is unavailable"),
        ):
            self.mh.state = state
            out = self.check()
            self.assertNotIn("- DONE", out, state)
            if "unknown" in reason:
                self.assertNotIn("sync ok", out, state)
                self.assertIn("sync UNKNOWN", out, state)
            self.assertIn(reason, out.lower() if reason.islower() else out, state)

    def test_sync_on_the_run_day_before_the_save_is_cant_verify(self):
        # Reviewer W, reproduction A: the run was saved 2026-10-15 09:00 EDT (13:00 UTC); a sync that
        # morning, before it, can't show money moved after it, even though the dates match.
        self.mh.state = ok_state("2026-10-15T06:00:00")
        out = self.check()
        self.assertNotIn("NOT SEEN", out)
        self.assertNotIn("- DONE", out)
        self.assertIn("- CAN'T VERIFY \u2014 Bills: $245.50 from CU Main Holding to CU Bills. No bank sync since the run was saved; "
                      "last good sync 2026-10-15 02:00.", out)
        self.assertNotIn("/api/transactions", self.mh.paths())
        # Exactly at the save is not after it.
        self.mh.state = ok_state("2026-10-15T13:00:00")
        self.assertIn("No bank sync since the run was saved", self.check())
        # The same day, after the save: checked, up to that day only.
        self.mh.state = ok_state("2026-10-15T14:00:00")
        out = self.check()
        self.assertIn("- NOT SEEN \u2014 Bills: $245.50 from CU Main Holding to CU Bills. Not seen as of last sync 2026-10-15 10:00: "
                      "no transfer between these accounts from 2026-10-15 to 2026-10-15.", out)

    def test_window_ends_at_the_last_sync(self):
        # Reviewer W, reproduction B: the sync stopped 3 days after the run; the days after it have no
        # data, so they are not searched and the negatives say "as of last sync".
        self.mh.state = ok_state("2026-10-18T10:00:00")
        out = self.check()
        q = [c["query"] for c in self.mh.calls if c["path"] == "/api/transactions"][0]
        self.assertEqual((q["date_from"], q["date_to"]), ("2026-10-03", "2026-10-18"))
        self.assertIn("Compared with Money Hub from 2026-10-15 to 2026-10-18 (last good sync 2026-10-18 06:00).", out)
        self.assertIn("- NOT SEEN \u2014 Savings C: $113.17 from CU Main Holding to CU Savings C. Not seen as of last sync 2026-10-18 06:00: "
                      "no transfer between these accounts from 2026-10-15 to 2026-10-18.", out)
        for ln in out.splitlines():
            if "NOT SEEN" in ln:
                self.assertIn("Not seen as of last sync 2026-10-18 06:00", ln)

    def test_sync_time_after_now_is_cant_verify(self):
        # Now is 2026-10-28 09:30 EDT = 13:30 UTC. A sync stamped later is impossible: Money Hub's clock
        # or the moneyhub_timezone valve is wrong, so it can't be ordered against the run.
        self.mh.state = ok_state("2026-10-28T13:40:00")
        out = self.check()
        self.assertIn("Money Hub's last sync time 2026-10-28 09:40 is later than now", out)
        self.assertNotIn("NOT SEEN", out)
        self.mh.state = ok_state("2026-10-28T13:34:00")  # within the 5-minute slack
        self.assertNotIn("is later than now", self.check())

    def test_window_never_passes_today(self):
        # Now 19:58 EDT on the 28th = 23:58 UTC, still the 28th in both zones. A sync stamped 3 minutes
        # ahead (inside the slack) is the 29th in UTC, but the window can't end after today.
        self.tool._now = lambda: (datetime.datetime(2026, 10, 28, 19, 58, tzinfo=TZ), None)
        self.mh.state = ok_state("2026-10-29T00:01:00")
        self.check()
        q = [c["query"] for c in self.mh.calls if c["path"] == "/api/transactions"][0]
        self.assertEqual(q["date_to"], "2026-10-28")

    def test_evening_transfer_dated_the_next_day_in_utc_is_found(self):
        # Reviewer W2-1: 21:00 EDT on the 28th is the 29th in UTC. The sync (20:30 EDT = 00:30 UTC on
        # the 29th) carries a transfer the bank dated the 29th; the window ends at the LATER of the
        # sync's date in the two zones, so it is searched.
        self.tool._now = lambda: (datetime.datetime(2026, 10, 28, 21, 0, tzinfo=TZ), None)
        self.mh.state = ok_state("2026-10-29T00:30:00")
        self.mh.add_transfer("2026-10-29", "CU Main Holding", "CU Savings C", 11317)
        out = self.check()
        q = [c["query"] for c in self.mh.calls if c["path"] == "/api/transactions"][0]
        self.assertEqual(q["date_to"], "2026-10-29")
        self.assertIn("- DONE \u2014 Savings C: $113.17 from CU Main Holding to CU Savings C. Seen 2026-10-29.", out)
        self.assertIn("Compared with Money Hub from 2026-10-15 to 2026-10-29 (last good sync 2026-10-28 20:30).", out)

    def test_bad_moneyhub_timezone_is_noted_and_read_as_utc(self):
        # Reviewer W2-2 (T7): "2026-10-15T12:00:00" read as UTC is 08:00 EDT, before the 09:00 save.
        # A fallback to any western zone would put it after the save and wrongly verify.
        self.tool.valves.moneyhub_timezone = "Not/AZone"
        self.mh.state = ok_state("2026-10-15T12:00:00")
        out = self.check()
        self.assertIn("Note: the moneyhub_timezone valve could not be loaded, so Money Hub's own times are read as UTC.", out)
        self.assertIn("No bank sync since the run was saved; last good sync 2026-10-15 08:00", out)
        self.tool.valves.moneyhub_timezone = "UTC"
        self.assertNotIn("moneyhub_timezone valve could not be loaded", self.check())

    def test_a_window_never_ends_before_it_starts(self):
        # Reviewer W2-4: a hand-edited saved_at before the run date, and a sync before the run date.
        path = os.path.join(self.dir, "2026-10-15_15th.md")
        text = re.sub(r'"saved_at": "[^"]*"', '"saved_at": "2026-10-10T00:00:00+00:00"', self.read("2026-10-15_15th.md"))
        with open(path, "w") as f:
            f.write(text)
        self.mh.state = ok_state("2026-10-12T00:00:00")
        out = self.check()
        self.assertIn("Money Hub has no bank data since the run (last good sync 2026-10-11 20:00).", out)
        self.assertNotIn("Compared with Money Hub from", out)
        self.assertNotIn("NOT SEEN", out)
        self.assertNotIn("- DONE", out)
        self.assertNotIn("/api/transactions", self.mh.paths())

    def test_moneyhub_timezone_valve(self):
        # A naive 12:00 is 08:00 EDT in UTC (before the 09:00 save) but 15:00 EDT in Los Angeles time.
        self.mh.state = ok_state("2026-10-15T12:00:00")
        self.assertIn("No bank sync since the run was saved", self.check())
        self.tool.valves.moneyhub_timezone = "America/Los_Angeles"
        out = self.check()
        self.assertNotIn("No bank sync since the run was saved", out)
        self.assertIn("as of last sync 2026-10-15 15:00", out)

    def test_run_without_a_save_time_uses_the_end_of_its_day(self):
        path = os.path.join(self.dir, "2026-10-15_15th.md")
        text = self.read("2026-10-15_15th.md")
        text = re.sub(r'"saved_at": "[^"]*"', '"saved_at": ""', text)
        with open(path, "w") as f:
            f.write(text)
        self.mh.state = ok_state("2026-10-15T20:00:00")  # 16:00 EDT on the run day
        self.assertIn("No bank sync since the run was saved", self.check())
        self.mh.state = ok_state("2026-10-16T05:00:00")  # 01:00 EDT the next day
        self.assertNotIn("No bank sync since the run was saved", self.check())

    def test_unreadable_rows_make_negatives_unverifiable(self):
        self.mh.add_transfer("2026-10-15", "CU Main Holding", "CU Savings C", 11317)
        self.mh.add_txn("2026-10-16", "CU Spending", -4500.0, "float amount")
        self.mh.add_txn("2026-10-16", "CU Spending", "-4500", "text amount")
        self.mh.txns.append({"id": 99999, "date": "2026-10-1x", "account": "CU Bills", "amount_cents": -1, "description": "bad date", "raw": ""})
        out = self.check()
        self.assertIn("\u26a0 3 Money Hub transaction row(s) in the window could not be read", out)
        self.assertIn("- DONE \u2014 Savings C", out)  # positives still count
        self.assertIn("- CAN'T VERIFY \u2014 Savings A: $226.35 from CU Main Holding to CU Savings A. 3 Money Hub transaction row(s) "
                      "in the window could not be read.", out)
        self.assertIn("- CAN'T VERIFY \u2014 Insurance: $145.50. 3 Money Hub transaction row(s) in the window could not be read; not marked "
                      "paid in Money Hub.", out)
        self.assertEqual([ln for ln in out.splitlines() if "NOT SEEN" in ln and "Phone" not in ln], [])

    def test_stale_or_manual_accounts(self):
        self.mh.accounts[4] = account(7, "CU Savings A", 8000, state="stale 24d", warn=True, as_of="2026-10-04")
        self.tool._now = lambda: (datetime.datetime(2026, 10, 28, 9, 0, tzinfo=TZ), None)
        self.save(card_payment="20", bills=[{"name": "Insurance", "amount": "145.50", "id": 9}, {"name": "Phone", "amount": "100"}])
        self.mh.bills["2026-10"][1]["paid"] = True
        out = self.check()
        self.assertIn("- CAN'T VERIFY \u2014 Savings A: ", out)
        self.assertIn("CU Savings A's bank data is stale (as of 2026-10-04)", out)
        self.assertIn(
            "- CAN'T VERIFY \u2014 Insurance: $145.50. Not marked paid in Money Hub and not seen, but 1 account(s) are stale (CU Savings A).",
            out,
        )
        self.assertIn("- DONE \u2014 Phone: $100.00. Marked paid in Money Hub", out)  # the flag doesn't depend on bank data
        self.tool.valves.source_account = "Store Card"
        self.save(card_payment="20", bills=[])
        out = self.check()
        self.assertIn("- CAN'T VERIFY \u2014 Card payment: $20.00 paid from Store Card. Store Card is a manual account", out)
        self.mh.accounts[0] = account(3, "Store Card", 1, state="stale 9d", warn=True)
        self.assertIn("- CAN'T VERIFY \u2014 Card payment: $20.00 paid from Store Card. Store Card's bank data is stale", self.check())

    def test_unknown_account(self):
        self.mh.accounts = [a for a in self.mh.accounts if a["name"] != "CU Savings B"]
        self.assertIn("- CAN'T MATCH \u2014 Savings B: $226.35 from CU Main Holding to CU Savings B. Money Hub has no account named CU Savings B.", self.check())

    def test_only_allowlisted_gets(self):
        self.all_transfers_done()
        self.check()
        self.assertTrue(self.mh.calls)
        self.assertEqual({c["method"] for c in self.mh.calls}, {"GET"})
        self.assertTrue(set(self.mh.paths()) <= {"/api/state", "/api/accounts", "/api/bills", "/api/transactions"})
        self.assertTrue(all(c["body"] == b"" for c in self.mh.calls))
        self.assertEqual(self.mh.calls[0]["path"], "/api/state")

    def test_paging(self):
        for i in range(1100):
            self.mh.add_txn("2026-10-20", "CU Spending", -100 - i, "coffee")
        self.mh.add_transfer("2026-10-15", "CU Main Holding", "CU Savings C", 11317)
        out = self.check()
        self.assertIn("- DONE \u2014 Savings C", out)
        offsets = [c["query"]["offset"] for c in self.mh.calls if c["path"] == "/api/transactions"]
        self.assertEqual(offsets, ["0", "500", "1000"])

    def test_too_many_transactions_is_cant_verify(self):
        for _ in range(2001):
            self.mh.add_txn("2026-10-20", "CU Spending", -1, "coffee")
        out = self.check()
        self.assertIn("- CAN'T VERIFY \u2014 Bills", out)
        self.assertIn("Too many transactions since the run to check them all.", out)
        self.assertIn("- CAN'T VERIFY \u2014 Insurance", out)
        self.assertEqual([ln for ln in out.splitlines() if "NOT SEEN" in ln], [])
        self.assertIn(PHONE_NOT_MARKED, out)

    def test_money_hub_text_is_inert(self):
        self.mh.state = dict(self.mh.state, sync_failing=True, sync_error="![x](https://evil.example/a.png)")
        self.mh.accounts.append(account(20, "CU Spending", 1, state="<img src=x>"))
        out = self.check()
        for bad in ("![", "](", "https://", "<img"):
            self.assertNotIn(bad, out)

    def test_money_hub_failures(self):
        self.mh.override["/api/accounts"] = (500, b"{}")
        self.assertIn("Money Hub answered HTTP 500 for /api/accounts. Nothing was checked", self.check())
        self.mh.override = {"/api/state": (200, b"not json")}
        self.assertIn("not JSON", self.check())
        self.mh.override = {"/api/transactions": (500, b"{}")}
        out = self.check()
        self.assertIn("transactions could not be read", out)
        self.assertNotIn("- DONE", out)
        self.mh.override = {"/api/bills": (500, b"{}")}
        out = self.check()
        self.assertIn("- CAN'T VERIFY \u2014 Insurance: $145.50. Money Hub's bills could not be read.", out)
        self.mh.override = {}
        self.tool.valves.base_url = "http://127.0.0.1:1"
        self.assertIn("unreachable", self.check())

    def test_base_url_valve(self):
        for bad in ("ftp://x:1", "http://user:pw@x:1", "http://x:1/api", "http://x:1?q=1", "x:1", "http://x:abc", ""):
            self.tool.valves.base_url = bad
            out = self.check()
            self.assertIn("base_url valve is not a plain", out, bad)
        self.assertEqual(self.mh.calls, [])

    def test_redirect_refused(self):
        self.mh.override["/api/state"] = (302, b"")
        out = self.check()
        self.assertNotIn("Data freshness", out)


class CardPayment(Base):
    """The card payment is an outbound payment: a debit from the source account whose text contains
    card_payment_pattern. The pattern's default is empty (Noah's real value lives in his valve), so these
    tests set a generic one."""

    def setUp(self):
        super().setUp()
        self.mh = FakeMoneyHub()
        self.addCleanup(self.mh.close)
        self.tool.valves.base_url = self.mh.url
        self.tool.valves.card_payment_pattern = "card pmt"
        open_window(self)  # about card-payment matching, not the window (see Check.setUp)
        self.tool._now = lambda: (datetime.datetime(2026, 10, 15, 9, 0, tzinfo=TZ), None)
        self.save(date="2026-10-15", bills=[], card_payment="50", splits=[{"name": "Savings C", "percent": 100}])
        self.tool._now = lambda: (NOW, None)

    def check(self):
        return run(self.tool.check_last_run(__user__=NOAH))

    def test_plan_line_and_record(self):
        text = self.read("2026-10-15_15th.md")
        record = json.loads(text.split("```json\n", 1)[1].split("\n```", 1)[0])
        self.assertEqual(
            record["transfers"][0],
            {"n": 1, "label": "Card payment", "kind": "card_payment", "from": "CU Main Holding", "to": "", "amount_cents": 5000},
        )
        self.assertEqual(record["card_payment_cents"], 5000)
        self.assertEqual(record["accounts"], {"source": "CU Main Holding", "bills": "CU Bills"})
        self.assertIn("1. $50.00 paid from CU Main Holding for the card payment (Card payment)", text)
        self.assertIn("- Card payment: $50.00", text)

    def test_done_any_case(self):
        self.mh.add_txn("2026-10-16", "CU Main Holding", -5000, "Card", raw="CARD PMT*CASH 8812")
        self.assertIn("- DONE \u2014 Card payment: $50.00 paid from CU Main Holding. Seen 2026-10-16.", self.check())

    def test_amount_differs_and_closest_wins(self):
        self.mh.add_txn("2026-10-16", "CU Main Holding", -9000, "Card Pmt")
        self.mh.add_txn("2026-10-17", "CU Main Holding", -4500, "card pmt transfer")
        self.assertIn(
            "- AMOUNT DIFFERS \u2014 Card payment: $50.00 paid from CU Main Holding. Planned $50.00, seen $45.00 on 2026-10-17.", self.check()
        )

    def test_only_debits_from_the_source_with_the_pattern(self):
        self.mh.add_txn("2026-10-16", "CU Spending", -5000, "Card Pmt")  # another account
        self.mh.add_txn("2026-10-16", "CU Main Holding", 5000, "Card Pmt refund")  # a credit
        self.mh.add_txn("2026-10-16", "CU Main Holding", -5000, "Grocery store")  # no pattern
        self.mh.add_txn("2026-10-14", "CU Main Holding", -5000, "Card Pmt")  # before the run
        self.assertIn(
            "- NOT SEEN \u2014 Card payment: $50.00 paid from CU Main Holding. Not seen as of last sync 2026-10-28 09:00: no matching "
            "card-payment debit from 2026-10-15 to 2026-10-28.",
            self.check(),
        )

    def test_custom_pattern_and_empty_pattern(self):
        self.mh.add_txn("2026-10-16", "CU Main Holding", -5000, "OP CASH")
        self.assertIn("- NOT SEEN \u2014 Card payment", self.check())
        self.tool.valves.card_payment_pattern = "op cash"
        self.assertIn("- DONE \u2014 Card payment", self.check())
        for empty in ("  ", ""):
            self.tool.valves.card_payment_pattern = empty
            self.assertIn(
                "- CAN'T MATCH \u2014 Card payment: $50.00 paid from CU Main Holding. The card_payment_pattern valve is empty.", self.check()
            )

    def test_the_pattern_default_is_empty_so_it_cant_match(self):
        tool, _, _ = load_tool()
        self.assertEqual(tool.valves.card_payment_pattern, "")
        self.tool.valves.card_payment_pattern = tool.valves.card_payment_pattern
        self.mh.add_txn("2026-10-16", "CU Main Holding", -5000, "Card Pmt")
        self.assertIn("- CAN'T MATCH \u2014 Card payment", self.check())

    def test_a_card_payment_debit_is_never_a_transfer_leg(self):
        # A card-payment debit and an unrelated Savings C deposit of the same amount must not pair up.
        self.tool._now = lambda: (datetime.datetime(2026, 10, 28, 6, 0, tzinfo=TZ), None)  # saved 10:00 UTC
        self.save(bills=[], card_payment="0", paycheck="50", splits=[{"name": "Savings C", "percent": 100}])
        self.tool._now = lambda: (NOW, None)  # checked 13:30 UTC
        self.mh.state = ok_state("2026-10-28T12:00:00")  # between the two
        self.mh.add_txn("2026-10-28", "CU Main Holding", -5000, "CARD PMT")
        self.mh.add_txn("2026-10-28", "CU Savings C", 5000, "interest")
        self.assertIn("- NOT SEEN \u2014 Savings C: $50.00", self.check())

    def test_each_debit_counts_once(self):
        self.mh.add_txn("2026-10-16", "CU Main Holding", -5000, "CARD PMT")
        self.mh.add_transfer("2026-10-16", "CU Main Holding", "CU Savings C", 95000)
        out = self.check()
        self.assertIn("- DONE \u2014 Card payment", out)
        self.assertIn("- DONE \u2014 Savings C: $950.00", out)

    def test_stale_sync(self):
        self.mh.add_txn("2026-10-16", "CU Main Holding", -5000, "CARD PMT")
        self.mh.state = dict(ok_state("2026-10-05T13:00:00"), sync_failing=True)
        self.assertIn("- CAN'T VERIFY \u2014 Card payment: $50.00 paid from CU Main Holding. Bank sync stale since 2026-10-05.", self.check())


class Window(Base):
    """Lead decision, round 5: a run's window ends the day before the NEXT pay deposit appears in
    Money Hub (a pay-pattern credit in the source account, after this run's own deposit). Until it
    appears, the day before its assumed earliest landing (the nominal date moved to the prior
    business day, then 5 business days earlier). And at match_window_days (default 10, at most 12)."""

    def setUp(self):
        super().setUp()
        self.mh = FakeMoneyHub()
        self.addCleanup(self.mh.close)
        self.tool.valves.base_url = self.mh.url
        self.tool.valves.split_accounts = json.dumps({"Savings A": "CU Savings A"})

    def run_at(self, saved, date, check, now, sync, window=None, bills=None):
        if window is not None:
            self.tool.valves.match_window_days = window
        for name in os.listdir(self.dir):
            os.unlink(os.path.join(self.dir, name))
        self.mh.txns.clear()
        self.mh.calls.clear()
        self.tool._now = lambda: (saved, None)
        out = self.save(date=date, check=check, paycheck="1000", bills=bills or [], splits=[{"name": "Savings A", "percent": 50}])
        self.tool._now = lambda: (now, None)
        self.mh.state = ok_state(sync)
        return out

    def at(self, y, m, d, h=12):
        return datetime.datetime(y, m, d, h, 0, tzinfo=TZ)

    def pair(self, day):
        self.mh.add_transfer(day, SOURCE, "CU Savings A", 50000)

    def deposit(self, day, description="Employer Payroll", raw="EMPLOYER PAYROLL 4411  -   DFAS PAY", account=SOURCE):
        self.mh.add_txn(day, account, 100000, description, raw=raw)

    def check(self):
        return run(self.tool.check_last_run(__user__=NOAH))

    def window_line(self, out):
        return next(ln for ln in out.splitlines() if ln.startswith("Last payday run"))

    def assert_not_done(self, out, closed):
        self.assertNotIn("- DONE", out)
        self.assertNotIn("NOT SEEN", out)
        self.assertIn(f"- CAN'T VERIFY \u2014 Savings A: $500.00 from CU Main Holding to CU Savings A. The window closed {closed}.", out)

    # ------------------------------------------------------------ the model and the calendar

    def test_federal_holidays(self):
        h = self.mod._federal_holidays(2026)
        for d in ("2026-01-01", "2026-01-19", "2026-02-16", "2026-05-25", "2026-06-19", "2026-07-03", "2026-09-07",
                  "2026-10-12", "2026-11-11", "2026-11-26", "2026-12-25"):
            self.assertIn(datetime.date.fromisoformat(d), h, d)
        self.assertNotIn(datetime.date(2026, 7, 4), h)  # a Saturday: observed Friday 7-3
        self.assertIn(datetime.date(2027, 12, 31), self.mod._federal_holidays(2027))  # Jan 1 2028 is a Saturday

    def test_fallback_dates(self):
        cases = (
            ("15th", "2026-10", "2026-10-23"),  # next: Sun Nov 1 -> Fri Oct 30, 5 business days earlier
            ("1st", "2026-11", "2026-11-05"),  # next: Sun Nov 15 -> Fri Nov 13; Veterans Day skipped
            ("15th", "2026-11", "2026-11-23"),  # next: Tue Dec 1; Thanksgiving skipped
            ("15th", "2026-12", "2026-12-23"),  # next: Fri Jan 1 (holiday) -> Thu Dec 31; Christmas skipped
            ("1st", "2027-02", "2027-02-05"),  # next: Presidents' Day Mon Feb 15 -> Fri Feb 12
            ("15th", "2028-12", "2028-12-21"),  # next: Mon Jan 1 2029 (holiday) -> Fri Dec 29
        )
        for check, period, want in cases:
            run_ = {"check": check, "period": period}
            self.assertEqual(self.mod._fallback_deposit(run_).isoformat(), want, (check, period))
            self.assertEqual(self.mod._check_window_end(run_), datetime.date.fromisoformat(want) - datetime.timedelta(days=1))
        self.assertIsNone(self.mod._check_window_end({"check": None, "period": "2026-10"}))

    def test_fallback_never_overlaps_a_three_or_four_business_day_landing_2026_to_2050(self):
        # Pay lands 3-4 business days early after the weekend/holiday shift (the model; measured on
        # the operator's own data). The fallback assumes 5, so the window always ends before either.
        for year in range(2026, 2051):
            for month in range(1, 13):
                for check in ("1st", "15th"):
                    run_ = {"check": check, "period": f"{year:04d}-{month:02d}"}
                    nxt = self.mod._next_nominal(run_["period"], check)
                    for early in (3, 4):
                        landing = self.mod._business_days_before(nxt, early)
                        self.assertLess(self.mod._check_window_end(run_), landing, (run_, early))
                        self.assertLessEqual(self.mod._fallback_deposit(run_), landing, (run_, early))

    # ------------------------------------------------------------ the deposit bound

    def test_rd1_next_checks_money_on_11_06_is_never_this_runs_done(self):
        # Reviewer W round 5, RD1: the Nov 1st-check run (10-27) never moved Savings A; the Nov 15 check landed
        # Fri 11-06 (4 business days early) and its $500 moved that day. With the deposit visible, and without.
        for visible in (True, False):
            self.run_at(self.at(2026, 10, 27), "2026-10-27", "1st", self.at(2026, 11, 7), "2026-11-07T13:00:00", 12)
            if visible:
                self.deposit("2026-10-27")
                self.deposit("2026-11-06")
            self.pair("2026-11-06")
            out = self.check()
            # A visible deposit can only shorten the window (W6-2): the fallback (11-04) ends it either way.
            self.assert_not_done(out, "before the next check's earliest expected deposit (2026-11-05) (2026-11-04)")

    def test_a_visible_deposit_earlier_than_the_fallback_ends_the_window(self):
        # A next deposit seen on 11-04 (earlier than the fallback's assumed 11-05) ends the window on 11-03.
        self.run_at(self.at(2026, 10, 27), "2026-10-27", "1st", self.at(2026, 11, 7), "2026-11-07T13:00:00", 12)
        self.deposit("2026-10-27")
        self.deposit("2026-11-04")
        self.pair("2026-11-03")
        out = self.check()
        self.assertIn("- DONE \u2014 Savings A: $500.00 from CU Main Holding to CU Savings A. Seen 2026-11-03.", out)
        self.assertIn("Pay deposits in CU Main Holding: this run's 2026-10-27; the next 2026-11-04.", out)
        self.assertIn("Compared with Money Hub from 2026-10-27 to 2026-11-03", self.window_line(out))
        self.mh.txns.clear()
        self.deposit("2026-10-27")
        self.deposit("2026-11-04")
        self.pair("2026-11-04")
        self.assert_not_done(self.check(), "the day before the next pay deposit (seen 2026-11-04) (2026-11-03)")

    def test_a_visible_deposit_later_than_the_fallback_never_extends_the_window(self):
        # Lead, W6-2 (reversed): money can leave before the deposit row appears, so a deposit seen on
        # 11-09 doesn't extend the window past the fallback (11-04). An 11-07 transfer can't be verified.
        self.run_at(self.at(2026, 10, 27), "2026-10-27", "1st", self.at(2026, 11, 10), "2026-11-10T13:00:00", 12)
        self.deposit("2026-10-27")
        self.deposit("2026-11-09")
        self.pair("2026-11-07")
        self.assert_not_done(self.check(), "before the next check's earliest expected deposit (2026-11-05) (2026-11-04)")

    def test_a1_own_deposit_missing(self):
        # Reviewer W6-1, A1: the run's own (Nov 1) deposit isn't in Money Hub; the Nov 15 deposit (11-06) and
        # the Dec 1 one (11-24) are. A deposit after the run's pay date is never its own.
        self.run_at(self.at(2026, 10, 27), "2026-10-27", "1st", self.at(2026, 11, 26), "2026-11-26T13:00:00", 12)
        self.deposit("2026-11-06")
        self.deposit("2026-11-24")
        self.pair("2026-11-06")
        out = self.check()
        self.assertNotIn("- DONE", out)
        self.assertIn("this run's own deposit not seen (none from 2026-10-20 to its pay date); the next 2026-11-06.", out)
        # With a next deposit early enough to bound the window by itself: taking it as "own" would move the
        # bound to the fallback's 11-04, and the 11-03 transfer would read done.
        self.mh.txns.clear()
        self.deposit("2026-11-03")
        self.deposit("2026-11-24")
        self.pair("2026-11-03")
        self.assert_not_done(self.check(), "the day before the next pay deposit (seen 2026-11-03) (2026-11-02)")

    def test_deposit_bound_unit(self):
        run_ = {"check": "1st", "period": "2026-11"}  # C = 11-01, own range 10-20 .. 11-01

        def rows(*dates, amount=100000, text="Employer Payroll"):
            return [{"id": i + 1, "date": d, "account": SOURCE, "amount_cents": amount, "description": text, "raw": ""} for i, d in enumerate(dates)]

        pats = self.mod._pay_patterns("EMPLOYER PAYROLL, DFAS PAY")
        bound = self.mod._deposit_bound
        d = datetime.date.fromisoformat
        # X1: the previous check's deposit (a late 10-14) is before C - 12: never this run's own.
        self.assertEqual(bound(run_, rows("2026-10-14", "2026-10-27", "2026-11-06"), SOURCE, pats), (d("2026-10-27"), d("2026-11-06")))
        self.assertEqual(bound(run_, rows("2026-10-20", "2026-11-06"), SOURCE, pats), (d("2026-10-20"), d("2026-11-06")))  # C - 12 is in
        self.assertEqual(bound(run_, rows("2026-11-01", "2026-11-06"), SOURCE, pats), (d("2026-11-01"), d("2026-11-06")))  # C is in
        self.assertEqual(bound(run_, rows("2026-11-02", "2026-11-24"), SOURCE, pats), (None, d("2026-11-02")))  # after C: never own
        self.assertEqual(bound(run_, rows("2026-10-27", "2026-10-27"), SOURCE, pats), (d("2026-10-27"), None))  # same day: not next
        # X5: a debit carrying the pay text is not a deposit.
        self.assertEqual(bound(run_, rows("2026-10-27") + rows("2026-11-02", amount=-5000), SOURCE, pats), (d("2026-10-27"), None))
        self.assertEqual(bound(run_, rows("2026-10-27"), "Another account", pats), (None, None))
        self.assertEqual(bound(run_, rows("2026-10-27"), SOURCE, []), (None, None))

    def test_x5_a_debit_with_the_pay_text_is_not_the_next_deposit(self):
        self.run_at(self.at(2026, 10, 27), "2026-10-27", "1st", self.at(2026, 11, 5, 18), "2026-11-05T13:00:00", 12)
        self.deposit("2026-10-27")
        self.mh.add_txn("2026-11-02", SOURCE, -5000, "Employer Payroll", raw="DFAS PAY RECOUPMENT")
        self.pair("2026-11-03")
        out = self.check()
        self.assertIn("- DONE \u2014 Savings A: $500.00 from CU Main Holding to CU Savings A. Seen 2026-11-03.", out)
        self.assertIn("the next not seen yet", out)

    def test_sync_too_stale_to_see_the_next_deposit_uses_the_fallback(self):
        self.run_at(self.at(2026, 10, 27), "2026-10-27", "1st", self.at(2026, 11, 5, 18), "2026-11-05T13:00:00", 12)
        self.deposit("2026-10-27")
        self.pair("2026-11-05")
        out = self.check()
        self.assertIn("the next not seen yet (window bounded by the fallback)", out)
        self.assert_not_done(out, "before the next check's earliest expected deposit (2026-11-05) (2026-11-04)")

    def test_only_pay_credits_in_the_source_account_count(self):
        # Invented data: a credit to another account carries "Employer Payroll" too, and a benefit credit
        # shares the payroll line's code "4411": neither is the next deposit. A statement-style row says
        # only "PAYROLL". The 1st check of April 2027 (Thu Apr 1) lands Mon Mar 29; the next (Thu Apr 15)
        # lands Fri Apr 9.
        self.run_at(self.at(2027, 3, 29), "2027-03-29", "1st", self.at(2027, 4, 10), "2027-04-10T13:00:00", 12)
        self.deposit("2027-03-29", description="External Deposit 4411 - PAYROLL", raw="External Deposit 4411 - PAYROLL")
        self.deposit("2027-03-31", description="Employer Payroll Check #1234567", account="Example Bank Savings")
        self.deposit("2027-03-30", description="Benefit Plan Payment", raw="EMPLOYER BENEFIT 4411  -   FUND DIST")
        self.deposit("2027-04-09")
        self.pair("2027-04-02")
        out = self.check()
        self.assertIn("Pay deposits in CU Main Holding: this run's 2027-03-29; the next 2027-04-09.", out)
        self.assertIn("- DONE \u2014 Savings A: $500.00 from CU Main Holding to CU Savings A. Seen 2027-04-02.", out)

    def test_pattern_valve(self):
        self.run_at(self.at(2026, 10, 27), "2026-10-27", "1st", self.at(2026, 11, 7), "2026-11-07T13:00:00", 12)
        self.deposit("2026-10-27", description="DFAS-CLEVELAND", raw="DFAS CLEVELAND PAY")
        self.deposit("2026-11-06", description="DFAS-CLEVELAND", raw="DFAS CLEVELAND PAY")
        self.pair("2026-11-04")
        out = self.check()
        self.assertIn("this run's 2026-10-27; the next 2026-11-06", out)  # the default includes DFAS
        self.assertIn("- DONE \u2014 Savings A", out)
        self.tool.valves.pay_deposit_pattern = "  "
        out = self.check()
        self.assertIn("the next not seen yet (window bounded by the fallback)", out)
        self.assertEqual(self.mod._pay_patterns(" Employer Payroll ,dfas,, "), ["employer payroll", "dfas"])

    def test_late_run_after_the_next_deposit(self):
        # A 15th-check run logged late on 10-28: the next (Nov 1) deposit landed 10-26, so the window had
        # closed before the run. Nothing can be claimed.
        self.run_at(self.at(2026, 10, 28), "2026-10-28", "15th", self.at(2026, 10, 30), "2026-10-30T13:00:00", 12)
        self.deposit("2026-10-09")
        self.deposit("2026-10-26")
        self.pair("2026-10-28")
        out = self.check()
        self.assertIn(
            "Its window had already closed: the window closed before the next check's earliest expected deposit (2026-10-23) (2026-10-22).", out
        )
        self.assertNotIn("- DONE", out)
        self.assertIn(
            "\u26a0 Nothing was verified against the bank: can't verify (the window closed before the next check's earliest expected "
            "deposit (2026-10-23) (2026-10-22)).",
            out,
        )

    # ------------------------------------------------------------ the reviewer's earlier cases (now via the fallback)

    def test_o1_next_checks_transfer_is_never_this_runs_done(self):
        self.run_at(self.at(2026, 10, 15), "2026-10-15", "15th", self.at(2026, 10, 31), "2026-10-31T14:00:00", 12)
        self.pair("2026-10-30")
        self.assert_not_done(self.check(), "before the next check's earliest expected deposit (2026-10-23) (2026-10-22)")
        self.deposit("2026-10-09")
        self.deposit("2026-10-26")
        self.assert_not_done(self.check(), "before the next check's earliest expected deposit (2026-10-23) (2026-10-22)")

    def test_f1_f2_presidents_day(self):
        for window in (None, 31):
            self.run_at(self.at(2027, 2, 2), "2027-02-02", "1st", self.at(2027, 2, 13), "2027-02-13T13:00:00", window)
            self.pair("2027-02-12")
            self.assert_not_done(self.check(), "before the next check's earliest expected deposit (2027-02-05) (2027-02-04)")

    def test_d1_december_run_and_an_early_new_year_check(self):
        self.run_at(self.at(2028, 12, 15), "2028-12-15", "15th", self.at(2028, 12, 30), "2028-12-30T13:00:00", 31)
        self.pair("2028-12-29")
        self.assert_not_done(self.check(), "before the next check's earliest expected deposit (2028-12-21) (2028-12-20)")

    def test_default_ten_days(self):
        self.run_at(self.at(2026, 10, 3), "2026-10-03", "15th", self.at(2026, 10, 30), "2026-10-30T13:00:00")
        self.pair("2026-10-20")
        self.assert_not_done(self.check(), "after match_window_days (2026-10-13)")

    def test_match_window_days_is_capped_at_twelve(self):
        for value, want in ((31, 12), (12, 12), (10, 10), (0, 1), ("x", 10)):
            self.tool.valves.match_window_days = value
            self.assertEqual(self.tool._window(), want, value)
        self.assertEqual(load_tool()[0].valves.match_window_days, 10)

    def test_open_window_still_says_not_seen(self):
        self.run_at(self.at(2026, 10, 15), "2026-10-15", "15th", self.at(2026, 10, 20), "2026-10-20T14:00:00")
        out = self.check()
        self.assertIn("- NOT SEEN \u2014 Savings A: $500.00 from CU Main Holding to CU Savings A. Not seen as of last sync", out)
        self.assertNotIn("window closed", out)

    # ------------------------------------------------------------ the save rule: C - 12 to C + 13

    def test_save_range_edges(self):
        self.tool._now = lambda: (self.at(2026, 11, 30), None)
        accepted = (("2026-10-03", "15th"), ("2026-10-28", "15th"), ("2026-10-20", "1st"), ("2026-11-14", "1st"), ("2026-09-19", "1st"), ("2026-10-14", "1st"))
        for date, check in accepted:
            self.assertIn("Saved", self.save(date=date, check=check), (date, check))
        refused = (
            ("2026-10-02", "15th", "Oct 2", "the 1st check"),
            ("2026-10-29", "15th", "Oct 29", "the 1st check"),
            ("2026-10-19", "1st", "Oct 19", "the 15th check"),
            ("2026-11-15", "1st", "Nov 15", "the 15th check"),
            ("2026-10-16", "1st", "Oct 16", "the 15th check"),  # C1: a "1st" on 10-16 is the 15th check
        )
        for date, check, shown, right in refused:
            d = Dialog(True)
            out = self.save(d, date=date, check=check)
            self.assertEqual(out, f"This date ({shown}) belongs to {right}'s payday, not the {check}'s. Save it as {right}. Nothing was saved.")
            self.assertEqual(d.payloads, [])
        for day in range(0, 800):
            date = datetime.date(2026, 1, 1) + datetime.timedelta(days=day)
            self.assertTrue(self.mod._checks_for(date), date)  # every date belongs to at least one check

    def test_saving_past_its_own_bound_warns(self):
        # Reviewer W6-4: a run saved after its own fallback bound is still saved, with a note.
        note = "- Note: This run's checklist can't be verified later: the next check's pay may already have landed."
        self.tool._now = lambda: (self.at(2026, 11, 7), None)
        out = self.save(date="2026-11-07", check="1st")  # the Nov 1st check's bound is 11-04
        self.assertIn("Saved", out)
        self.assertIn(note, out)
        out = self.save(date="2026-11-04", check="1st")
        self.assertIn("Saved", out)
        self.assertNotIn(note, out)
        self.assertEqual(len(self.files()), 2)

    def test_runs_on_model_landing_days_are_accepted(self):
        # A run dated the day its pay lands (3 or 4 business days early, the model) is always in range.
        for year in (2026, 2027, 2028):
            for month in range(1, 13):
                for dom, check in ((1, "1st"), (15, "15th")):
                    for early in (3, 4):
                        day = self.mod._business_days_before(datetime.date(year, month, dom), early)
                        first, last = self.mod._run_range(day, check)
                        self.assertTrue(first <= day <= last, (day, check))
        day = self.mod._business_days_before(datetime.date(2027, 4, 1), 3)  # a Thursday Apr 1 -> Mon Mar 29
        self.tool._now = lambda: (datetime.datetime.combine(day, datetime.time(12), tzinfo=TZ), None)
        self.assertIn("Saved", self.save(date=day.isoformat(), check="1st"))

    def test_e3_e4_hand_edited_check_or_period_is_refused(self):
        self.run_at(self.at(2026, 10, 15), "2026-10-15", "15th", self.at(2026, 11, 12), "2026-11-12T13:00:00", 12)
        path = os.path.join(self.dir, "2026-10-15_15th.md")
        original = self.read("2026-10-15_15th.md")
        edits = {
            "E4": (original.replace('"period": "2026-10"', '"period": "2027-06"'), "its period 2027-06 is not the 15th check's period for 2026-10-15"),
            "E3": (original.replace('"check": "15th"', '"check": "1st"'), "its check (1st) is not the one in its file name (15th)"),
            "date": (original.replace('"date": "2026-10-15"', '"date": "2026-10-30"'), "a 15th-check run can't be dated 2026-10-30"),
        }
        for label, (text, why) in edits.items():
            with open(path, "w") as f:
                f.write(text)
            out = self.check()
            self.assertIn(f"The newest saved run, 2026-10-15_15th.md, can't be used: {why}.", out, label)

    def test_b1_bill_without_a_pattern_is_not_marked_paid_never_not_seen(self):
        self.mh.bills["2026-10"] = [bill(1, "Phone", 4500)]
        self.run_at(self.at(2026, 10, 15), "2026-10-15", "15th", self.at(2026, 10, 20), "2026-10-20T13:00:00", bills=[{"name": "Phone", "amount": "45", "id": 1}])
        out = self.check()
        self.assertIn(
            "- NOT MARKED PAID \u2014 Phone: $45.00. Not marked paid in Money Hub (no bank match pattern, so the bank wasn't checked).", out
        )

    def test_missing_check_field_is_cant_verify(self):
        self.run_at(self.at(2026, 10, 15), "2026-10-15", "15th", self.at(2026, 10, 20), "2026-10-20T14:00:00")
        self.pair("2026-10-16")
        path = os.path.join(self.dir, "2026-10-15_15th.md")
        original = self.read("2026-10-15_15th.md")
        for text in (original.replace('"check": "15th"', '"check": null'), original.replace('  "check": "15th",\n', "")):
            with open(path, "w") as f:
                f.write(text)
            out = self.check()
            self.assertIn("(unknown check)", out)
            self.assertIn("- CAN'T VERIFY \u2014 Savings A: $500.00 from CU Main Holding to CU Savings A. The saved run has no check "
                          "recorded, so its window can't be bounded by the next check.", out)
            self.assertNotIn("/api/transactions", self.mh.paths())
            self.mh.calls.clear()
        with open(path, "w") as f:
            f.write(original.replace('"check": "15th"', '"check": "2nd"'))
        self.assertIn("can't be used: check is not 1st or 15th", self.check())


class Renamed(TimedCase):
    def test_call_arguments_and_valve_names(self):
        tool, _, _ = load_tool()
        self.assertEqual(spec_properties(tool, "compute_split"), ["paycheck", "bills", "card_payment", "splits"])
        self.assertIn("card_payment", spec_properties(tool, "save_payday_run"))
        self.assertEqual(tool.valves.card_payment_pattern, "")
        self.assertEqual(
            sorted(vars(tool.valves)),
            sorted(
                [
                    "base_url", "timeout_s", "timezone", "moneyhub_timezone", "runs_dir", "source_account", "bills_account",
                    "pay_deposit_pattern", "card_payment_pattern", "default_splits", "split_accounts", "match_window_days",
                    "dialog_timeout_s", "knowledge_timeout_s",
                ]
            ),
        )


class DefaultSplits(Base):
    """The default_splits valve: Noah's own split, used when no splits are given."""

    def test_the_valve_default_is_the_generic_split(self):
        tool, _, _ = load_tool()
        self.assertEqual(
            json.loads(tool.valves.default_splits),
            [{"name": "Free spending", "percent": 15}, {"name": "Savings A", "percent": 30},
             {"name": "Savings B", "percent": 30}, {"name": "Savings C", "percent": 15}],
        )

    def test_the_valve_is_used_and_mapped_to_accounts(self):
        self.tool.valves.default_splits = json.dumps([{"name": "House fund", "percent": 40}, {"name": "Fun", "percent": 12.5}])
        self.tool.valves.split_accounts = json.dumps({"House fund": "CU House"})
        out = run(self.tool.compute_split("100"))
        self.assertIn("  - House fund, 40%: $40.00 to CU House", out)
        self.assertIn("  - Fun, 12.5%: $12.50 to (no Money Hub account set)", out)
        self.assertIn("  - Unassigned, 47.5%: $47.50", out)
        self.assertNotIn("Savings A", out)
        self.save(paycheck="100", bills=[])
        record = json.loads(self.read("2026-10-22_15th.md").split("```json\n", 1)[1].split("\n```", 1)[0])
        self.assertEqual([(s["name"], s["bp"], s["account"]) for s in record["split"]], [("House fund", 4000, "CU House"), ("Fun", 1250, "")])

    def test_an_explicit_split_overrides_the_valve(self):
        self.tool.valves.default_splits = "not json at all"
        out = run(self.tool.compute_split("100", None, "0", [{"name": "Car", "percent": 50}]))
        self.assertIn("  - Car, 50%: $50.00", out)

    def test_an_empty_valve_uses_the_generic_split(self):
        self.tool.valves.default_splits = "  "
        self.assertIn("  - Savings A, 30%", run(self.tool.compute_split("100")))

    def test_the_valve_is_validated(self):
        cases = {
            "not json": "it is not JSON",
            '{"name": "A", "percent": 10}': "it is not a non-empty list",
            "[]": "it is not a non-empty list",
            '["A"]': 'item 1 is not exactly {"name", "percent"}',
            '[{"name": "A"}]': 'item 1 is not exactly {"name", "percent"}',
            '[{"name": "A", "percent": 10, "account": "X"}]': 'item 1 is not exactly {"name", "percent"}',
            '[{"name": "A", "percent": 60}, {"name": "B", "percent": 41}]': "101%, more than 100%",
            '[{"name": "A", "percent": 10}, {"name": "a", "percent": 10}]': "appears twice",
            '[{"name": "Bills", "percent": 10}]': "that name is reserved",
            '[{"name": "Card payment", "percent": 10}]': "that name is reserved",
            '[{"name": "Unassigned", "percent": 10}]': "that name is reserved",
            '[{"name": "card_payment", "percent": 10}]': "that name is reserved",
            '[{"name": "CARD PAYMENT", "percent": 10}]': "that name is reserved",
            '[{"name": "' + "x" * 61 + '", "percent": 10}]': "keep it under 60 characters",
            '[{"name": "", "percent": 10}]': "it is empty",
            '[{"name": "A", "percent": 0}]': "more than 0",
            '[{"name": "A", "percent": "ten"}]': "Invalid percent",
            '[{"name": "A", "percent": 10.125}]': "Invalid percent",
            json.dumps([{"name": f"S{i}", "percent": 1} for i in range(13)]): "Too many splits",
        }
        for raw, why in cases.items():
            self.tool.valves.default_splits = raw
            out = run(self.tool.compute_split("100"))
            self.assertTrue(out.startswith("The default_splits valve is invalid ("), (raw, out))
            self.assertIn(why, out, raw)
            self.assertNotIn("Left to split", out, raw)
            d = Dialog(True)
            self.assertIn("The default_splits valve is invalid", self.save(d, bills=[]), raw)
            self.assertEqual(d.payloads, [], raw)
        self.assertEqual(self.files(), [])

    def test_model_splits_cant_take_reserved_names_either(self):
        for name in ("card_payment", "Card payment", "bills", "UNASSIGNED"):
            out = run(self.tool.compute_split("100", None, "0", [{"name": name, "percent": 10}]))
            self.assertIn("that name is reserved", out, name)

    def test_split_accounts_keys_differing_only_by_case_are_refused(self):
        for raw in ('{"Savings A": "CU A", "savings a": "CU B"}', '{"Savings A": "CU A", "Savings A": "CU B"}'):
            self.tool.valves.split_accounts = raw
            out = run(self.tool.compute_split("100"))
            self.assertIn("The split_accounts valve names", out, raw)
            self.assertIn("twice", out, raw)
            self.assertNotIn("Left to split", out, raw)
        self.tool.valves.split_accounts = '{"Savings A": "CU A", "Savings B": "CU B"}'
        self.assertIn("  - Savings A, 30%: $22.50 to CU A", run(self.tool.compute_split("100", None, "25")))
        for bad in ('{"Savings A": {"x": "y"}}', '["Savings A"]', '{"Savings A": 1}', "not json"):
            self.tool.valves.split_accounts = bad
            self.assertIn("The split_accounts valve is not a JSON object", run(self.tool.compute_split("100")), bad)

    def test_twelve_items_and_exactly_one_hundred_are_fine(self):
        self.tool.valves.default_splits = json.dumps([{"name": f"S{i}", "percent": 8} for i in range(11)] + [{"name": "S11", "percent": 12}])
        out = run(self.tool.compute_split("100"))
        self.assertIn("  - S11, 12%: $12.00", out)
        self.assertIn("  - Unassigned, 0%: $0.00", out)


# ---------------------------------------------------------------- the client allowlist


class Client(Base):
    def test_get_allowlist(self):
        for path, params in (
            ("/api/sync", None),
            ("/api/bills/1/paid", None),
            ("/api/transactions", {"q": "x"}),
            ("/api/transactions", {"limit": 501}),
            ("/api/transactions", {"limit": True}),
            ("/api/transactions", {"offset": 2000}),
            ("/api/transactions", {"date_from": "2026-13-01"}),
            ("/api/bills", {"period": "2026-1"}),
            ("/api/state", {"x": 1}),
        ):
            with self.assertRaises(ValueError, msg=(path, params)):
                self.tool._get(path, params)


# ---------------------------------------------------------------- the source file


class SourceFile(TimedCase):
    def test_frontmatter_ascii_imports(self):
        src = read_source()
        self.assertTrue(src.isascii())
        fm = extract_frontmatter(src)
        self.assertEqual(fm["title"], "Payday (calculator + log)")
        tree = ast.parse(src)
        top, inner = set(), set()
        for node in tree.body:
            if isinstance(node, ast.Import):
                top.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                top.add(node.module)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module not in top:
                inner.add(node.module)
        self.assertEqual(
            top,
            {"asyncio", "datetime", "io", "json", "math", "os", "re", "stat", "time", "types", "unicodedata",
             "urllib.error", "urllib.parse", "urllib.request", "zoneinfo", "pydantic"},
        )
        self.assertEqual(
            inner,
            {"fastapi", "open_webui.models.files", "open_webui.models.knowledge", "open_webui.models.users",
             "open_webui.routers.files", "open_webui.routers.knowledge", "starlette.datastructures"},
        )
        for banned in ("subprocess", "os.system", "eval(", "exec(", "pickle", "O_TRUNC", "O_APPEND", "shutil", "rename(", "replace("):
            if banned == "replace(":
                continue  # str.replace is fine; os.replace is checked below
            self.assertNotIn(banned, src)
        names = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)} | {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        for banned in ("ProxyHandler", "build_opener", "urlopen", "FileHandler", "FTPHandler", "DataHandler", "rmdir", "removedirs", "chmod", "chown", "symlink", "link"):
            self.assertNotIn(banned, names)
        # The file names no write method at all; GET is the only one.
        for word in ("POST", "PUT", "PATCH", "DELETE"):
            self.assertNotIn(word, src)
        # The only os.unlink removes the file this call just created after a failed write.
        self.assertEqual(src.count("os.unlink("), 1)

    def test_network_only_in_get(self):
        tree = ast.parse(read_source())
        owners = []
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Attribute) and sub.attr in ("OpenerDirector", "Request"):
                        owners.append(node.name)
        self.assertEqual(sorted(set(owners)), ["_get"])

    def test_public_functions(self):
        tool, _, _ = load_tool()
        specs = tool_specs(tool)
        self.assertEqual({s["name"] for s in specs}, PUBLIC)
        for spec in specs:
            self.assertTrue(spec["is_async"], spec["name"])
            self.assertTrue(spec["description"].strip(), spec["name"])
            for name, p in spec["parameters"].items():
                if not name.startswith("__"):
                    self.assertTrue(p["description"], (spec["name"], name))
        self.assertEqual(spec_properties(tool, "last_payday_run"), [])
        self.assertEqual(spec_properties(tool, "check_last_run"), [])

    def test_valve_defaults(self):
        tool, _, _ = load_tool()
        self.assertEqual(tool.valves.runs_dir, "/payday-runs")
        self.assertEqual(tool.valves.base_url, "http://moneyhub.invalid:8710")
        self.assertEqual((tool.valves.source_account, tool.valves.bills_account), ("", ""))
        self.assertEqual(tool.valves.card_payment_pattern, "")
        self.assertEqual(tool.valves.split_accounts, "{}")


@unittest.skipUnless(owui.HAVE_LANGCHAIN, "pydantic and langchain_core are not installed")
class OpenAISpecs(TimedCase):
    def test_model_sees_no_reserved_parameter(self):
        tool, _, _ = load_tool()
        specs = {s["name"]: s for s in owui.openai_tool_specs(tool)}
        self.assertEqual(set(specs), PUBLIC)
        for name, spec in specs.items():
            props = {k for k in spec["parameters"].get("properties", {}) if not k.startswith("__")}
            self.assertEqual(sorted(props), sorted(spec_properties(tool, name)))


if __name__ == "__main__":
    unittest.main()
