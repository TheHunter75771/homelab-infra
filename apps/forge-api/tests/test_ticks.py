"""The K3 adapter: against a fake forge (every answer shape, every protocol error) and against
the real forge in a temp library."""
import types
import unittest

from forge import Forge, TickItem, TickResult
from forge_api.service import Service
from forge_api.ticks import ForgeTicks, TickProtocolError, TickUnavailable, normalise

from .helpers import APPROVER, Env, Served


def result(*items):
    return TickResult("search", [TickItem(i, i, o, r) for i, o, r in items])


class FakeForge:
    """Records calls and answers whatever ``answer`` says (a TickResult or a bad shape)."""

    def __init__(self, answer):
        self.answer = answer
        self.calls = []

    def tick_searches(self, rid, queries, *, by):
        self.calls.append(("searches", rid, list(queries), by))
        return self.answer(queries) if callable(self.answer) else self.answer

    def tick_sources(self, rid, urls, *, by):
        self.calls.append(("sources", rid, list(urls), by))
        return self.answer(urls) if callable(self.answer) else self.answer


class NormaliseTests(unittest.TestCase):
    def test_in_input_order_with_reasons(self):
        raw = result(("b", "already", ""), ("a", "ticked", ""), ("c", "not-found", "not listed in searches.md"))
        self.assertEqual(normalise(["a", "b", "c"], raw),
                         [("a", "ticked", ""), ("b", "already", ""), ("c", "not-found", "not listed in searches.md")])

    def test_protocol_errors(self):
        bad = {
            "not a result": ["a"],
            "dict": {"a": "ticked"},
            "none": None,
            "missing": result(("a", "ticked", "")),
            "extra": result(("a", "ticked", ""), ("b", "ticked", ""), ("z", "ticked", "")),
            "duplicate": result(("a", "ticked", ""), ("a", "already", ""), ("b", "ticked", "")),
            "unknown outcome": result(("a", "approved", ""), ("b", "ticked", "")),
            "outcome case": result(("a", "TICKED", ""), ("b", "ticked", "")),
            "item not str": TickResult("search", [types.SimpleNamespace(item=1, outcome="ticked", reason="")]),
        }
        for why, raw in bad.items():
            with self.subTest(why=why), self.assertRaises(TickProtocolError):
                normalise(["a", "b"], raw)

    def test_reason_not_text_is_dropped(self):
        raw = TickResult("search", [types.SimpleNamespace(item="a", outcome="not-found", reason=None)])
        self.assertEqual(normalise(["a"], raw), [("a", "not-found", "")])


class ForgeTicksTests(unittest.TestCase):
    def test_passes_by_and_items(self):
        fake = FakeForge(lambda items: result(*[(i, "ticked", "") for i in items]))
        out = ForgeTicks(fake).tick_sources("2026-10-27_x", ["https://e.com/a"], APPROVER)
        self.assertEqual(out, [("https://e.com/a", "ticked", "")])
        self.assertEqual(fake.calls, [("sources", "2026-10-27_x", ["https://e.com/a"], APPROVER)])

    def test_old_forge(self):
        with self.assertRaises(TickUnavailable):
            ForgeTicks(types.SimpleNamespace()).tick_searches("2026-10-27_x", ["a"], APPROVER)
        with self.assertRaises(TickUnavailable):
            ForgeTicks(types.SimpleNamespace(tick_searches="not callable")).tick_searches("r", ["a"], APPROVER)

    def test_real_forge_has_the_interface(self):
        env = Env(self)
        rid = env.new_request()
        out = ForgeTicks(Forge(env.lib)).tick_searches(rid, ["apa 7 citation rules", "nope"], APPROVER)
        self.assertEqual(out, [("apa 7 citation rules", "ticked", ""), ("nope", "not-found", "not listed in searches.md")])
        self.assertIn(f"approve-chat by={APPROVER} search", env.read(rid, "state.md"))


class OverHttpTests(unittest.TestCase):
    """The service with a fake forge behind the adapter: what comes back is exactly what the
    forge reported, and a bad answer is a 500 that claims nothing."""

    def setUp(self):
        self.env = Env(self)
        self.rid = self.env.new_request()
        self.answer = None
        self.fakes = []

        def factory(forge):
            fake = FakeForge(self.answer)
            self.fakes.append(fake)
            return ForgeTicks(fake)

        self.env.service = Service(self.env.cfg, ticker_factory=factory, library_kwargs={"clock": self.env.clock})
        self.srv = Served(self, self.env)

    def post(self, body):
        return self.srv.call("POST", f"/requests/{self.rid}/tick-searches", body)

    def test_fake_answers_pass_through(self):
        self.answer = lambda items: result(*[(i, "already", "") for i in items])
        code, out, _ = self.post({"queries": ["q one", "q two", "q one"], "user": APPROVER})
        self.assertEqual(code, 200, out)
        self.assertEqual(out["results"], [{"query": "q one", "result": "already"}, {"query": "q two", "result": "already"}])
        self.assertEqual(self.fakes[-1].calls, [("searches", self.rid, ["q one", "q two"], APPROVER)])

    def test_bad_answer_is_500_without_claims(self):
        self.answer = lambda items: result(*[(i, "ticked", "") for i in items[:-1]])
        code, out, _ = self.post({"queries": ["q one", "q two"], "user": APPROVER})
        self.assertEqual((code, out), (500, {"error": "internal error"}))

    def test_forge_refusal_is_409(self):
        from forge import ForgeError

        def refuse(items):
            raise ForgeError("searches.md changed while the forge was working")
        self.answer = refuse
        code, out, _ = self.post({"queries": ["q one"], "user": APPROVER})
        self.assertEqual(code, 409)
        self.assertIn("changed while the forge was working", out["error"])

    def test_wrong_user_never_reaches_the_forge(self):
        self.answer = lambda items: result(*[(i, "ticked", "") for i in items])
        code, _, _ = self.post({"queries": ["q one"], "user": "someone-else"})
        self.assertEqual(code, 403)
        self.assertEqual(self.fakes, [])


if __name__ == "__main__":
    unittest.main()
