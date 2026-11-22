"""Parity with helper X's reference redaction (Lead: one rule set), linear time, and Y's extras."""

import importlib.util
import json
import os
import sys
import time
import unittest

from ai_host_agent import redact
from unittest import mock

from ai_host_agent.redact import MASK, Redactor

from .fakes import TOKEN

# X owns the cases; they are read at test time (never copied), so the two can't drift.
CASES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "..", "..", "helper", "tests", "redact_cases.json")
with open(CASES_PATH, encoding="utf-8") as _f:
    CASES = json.load(_f)
HELPER_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "..", "..", "helper", "aictl_helper.py")


def load_helper():
    """X's module, imported read-only from its file (no bytecode written into X's folder), or
    None when it isn't next to us (an install without the helper)."""
    if not os.path.exists(HELPER_PATH):
        return None
    old = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec = importlib.util.spec_from_file_location("aictl_helper_ref", HELPER_PATH)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod  # its dataclasses look themselves up there
        spec.loader.exec_module(mod)
        return mod
    finally:
        sys.modules.pop("aictl_helper_ref", None)
        sys.dont_write_bytecode = old

# Inputs that make a backtracking rule quadratic (or worse) if it isn't bounded. 64 KiB each.
N = 65536
PATHOLOGICAL = {
    "alnum": "aB3" * (N // 3),
    "word-chars": "a_b.c-" * (N // 6),
    "keyword-repeat": "token" * (N // 5),
    "keyword-then-spaces": "password" + " " * N,
    "keyword-colon-repeat": "password:" * (N // 9),
    "authorization-spaces": "authorization:" + " " * N,
    "authorization-repeat": "authorization: bearer " * (N // 22),
    "bearer-spaces": "bearer" + " " * N,
    "bearer-repeat": "bearer " * (N // 7),
    "jwt-prefixes": "eyJ-" * (N // 4),
    "jwt-no-dots": "eyJ" + "a" * N,
    "jwt-dots": "eyJaaaaaaaa." * (N // 12),
    "sk-repeat": "sk-" * (N // 3),
    "tskey-repeat": "tskey-" * (N // 6),
    "ghp-repeat": "ghp_" * (N // 4),
    "github-pat-repeat": "github_pat_" * (N // 11),
    "url-schemes": "a://" * (N // 4),
    "url-user-no-colon": "://" + "u" * N,
    "url-creds-no-at": "://u:" + "p" * N,
    "curl-repeat": "curl " * (N // 5),
    "curl-u-no-colon": "curl -u " + "a" * N,
    "mysql-repeat": "mysql " * (N // 6),
    "ansible-repeat": "ansible_" * (N // 8),
    "vault-hex": "$ANSIBLE_VAULT;1.1;AES256" + " ab" * (N // 3) + " zz",
    "pem-begins": "-----BEGIN RSA PRIVATE KEY-----" * (N // 31),
    "quotes": 'password="' + "x" * N,
    "flags": "--password" + " " * N,
    "flags-repeat": "--token " * (N // 8),
    "colons": "=:" * (N // 2),
    "spaces": " " * N,
    "dashes": "-" * N,
    "escapes": "\x1b[" * (N // 2),
    "osc-unterminated": "\x1b]" + "a" * N,
    "token-value-repeat": TOKEN[:20] * (N // 20),
    # YAML block scalars (the opener's lookahead and the block tracking)
    "yaml-pipe-hash": "password:|#" * (N // 11),
    "yaml-pipe-hash-words": "password: |# " * (N // 13),
    "yaml-indent": " " * N + "password: |",
    "yaml-long-name": "a" * N + ": |",
    "yaml-block-lines": "\n".join(["password: |"] + ["  x"] * (N // 4)),
    "yaml-openers": "\n".join(["- password: |"] * (N // 14)),
}


# X's tests/test_hardening.py DENSE_UNITS (copied: X's tests aren't importable from here).
# One dense unit per rule: 1000 lines of 8 KiB each, made of it, is that rule's worst reply.
DENSE_UNITS = ["Authorization: ", "bearer ", "token: | ", "password=", "--password ", "curl -u ",
               "mysql -u x ", "sshpass ", "docker login ", "redis-cli -h ", "cookie: ", "://a:",
               "$ANSIBLE_VAULT;1.1;AES256 ", "tskey-", "sk-", "gh", "github_pat_", "glpat-", "hf_",
               "xox", "AKIA", "eyJ-", "[REDACTED]", "-----BEGIN RSA PRIVATE KEY-----", "key=",
               'password: "\\', "token:|#", " ",
               # and one line that triggers every rule at once
               "Authorization: bearer token: | password= --password curl -u mysql -p sshpass "
               "docker login redis-cli cookie: ://a: $ANSIBLE_VAULT;1.1;AES256 tskey- sk- gh_ "
               "github_pat_ glpat- hf_ xoxb- AKIA eyJ [REDACTED] "]


class ParityTest(unittest.TestCase):
    def test_every_reference_case(self):
        self.assertGreaterEqual(len(CASES), 50)
        r = Redactor([TOKEN])
        for case in CASES:
            with self.subTest(case=case["in"][:60]):
                self.assertEqual(set(case), {"in", "out"})
                self.assertEqual(redact.redact(case["in"]), case["out"])
                if "\n" not in case["in"]:
                    # the single-line paths the agent uses agree (sudo commands, journal lines)
                    self.assertEqual(redact.redact_line(case["in"]), case["out"])
                    self.assertEqual(r.line(case["in"]), case["out"][:redact.LINE_MAX])
                    self.assertEqual(r.lines([case["in"]]), [case["out"][:redact.LINE_MAX]])
                else:
                    self.assertEqual("\n".join(r.lines(case["in"].split("\n"))), case["out"])

    def test_every_rule_has_a_reference_case(self):
        for pattern, _ in redact.REDACT_RULES:
            with self.subTest(rule=pattern.pattern[:60]):
                self.assertTrue(any(pattern.search(c["in"]) for c in CASES))
        self.assertTrue(any(redact.PEM_BEGIN_RE.search(c["in"]) for c in CASES))
        self.assertTrue(any(redact.VAULT_HEADER_RE.search(line)
                            for c in CASES for line in c["in"].split("\n")))


def x_dense_units():
    """DENSE_UNITS from X's tests/test_hardening.py, read as a literal (never imported)."""
    import ast
    path = os.path.join(os.path.dirname(HELPER_PATH), "tests", "test_hardening.py")
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read())
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "DENSE_UNITS":
            return ast.literal_eval(node.value)
    return None


class SameRegexesTest(unittest.TestCase):
    """Stronger than the cases: every pattern is X's, character for character, in X's order."""

    def test_patterns_identical_to_x(self):
        x = load_helper()
        if x is None:
            self.skipTest("helper X not present")
        self.assertEqual([(p.pattern, p.flags) for p, _ in redact.REDACT_RULES],
                         [(p.pattern, p.flags) for p, _ in x.REDACT_RULES])
        self.assertEqual(redact.RULE_TRIGGERS, x.RULE_TRIGGERS)
        self.assertEqual(DENSE_UNITS, x_dense_units())  # the perf test uses X's inputs
        self.assertEqual(redact.FOLDED_RULES, x.FOLDED_RULES)
        self.assertEqual(redact.SCOPED_RULES, x.SCOPED_RULES)
        self.assertEqual({i: (p.pattern, p.flags) for i, p in redact._FOLDED_PATTERNS.items()},
                         {i: (p.pattern, p.flags) for i, p in x._FOLDED_PATTERNS.items()})
        self.assertEqual(redact._ASCII_FOLD, x._ASCII_FOLD)
        for name in ("PEM_BEGIN_RE", "PEM_END_RE", "VAULT_HEADER_RE", "VAULT_BODY_RE",
                     "YAML_BLOCK_RE", "SECRET_NAME_END_RE", "ANSI_RE", "CONTROL_RE"):
            with self.subTest(name=name):
                self.assertEqual(getattr(redact, name).pattern, getattr(x, name).pattern)
        self.assertEqual((redact.MASK, redact.REDACT_LINE_CAP, redact.LINE_MAX),
                         (x.REDACTED, x.REDACT_LINE_CAP, x.LINE_CAP))
        texts = [c["in"] for c in CASES] + [(u * (8192 // len(u) + 1))[:8192] for u in DENSE_UNITS]
        for text in texts:  # and the same answers, through X's own code
            self.assertEqual(redact.redact(text), x.redact(text))
            self.assertEqual(redact.redact_line(text[:8192]), x.redact_line(text[:8192]))


class PrefilterAndFoldTest(unittest.TestCase):
    """As X: the substring prefilter and the ASCII folding are speed-ups that never change a
    result."""

    TEXTS = ([c["in"] for c in CASES] + [c["in"].upper() for c in CASES]
             + [c["in"].title() for c in CASES] + [c["in"].swapcase() for c in CASES])

    def texts(self):
        return (self.TEXTS + [t[:8192] for t in PATHOLOGICAL.values()]
                + [(u * (8192 // len(u) + 1))[:8192] for u in DENSE_UNITS])

    def test_the_prefilter_never_changes_a_result(self):
        texts = self.texts()
        with_triggers = [redact.redact(t) for t in texts]
        # As X: a scoped rule's trigger is its scope (a condition), so it stays; every other
        # trigger is a pure speed-up and is switched off.
        speed_only_off = [t if i in redact.SCOPED_RULES else ()
                          for i, t in enumerate(redact.RULE_TRIGGERS)]
        with mock.patch.object(redact, "RULE_TRIGGERS", speed_only_off):
            without = [redact.redact(t) for t in texts]
        for text, a, b in zip(texts, with_triggers, without):
            with self.subTest(text=text[:60]):
                self.assertEqual(a, b)

    def test_folded_matching_equals_ignorecase_matching(self):
        texts = self.texts()
        folded = [redact.redact(t) for t in texts]
        with mock.patch.object(redact, "FOLDED_RULES", frozenset()):
            ignorecase = [redact.redact(t) for t in texts]
        for text, a, b in zip(texts, folded, ignorecase):
            with self.subTest(text=text[:60]):
                self.assertEqual(a, b)


class LinearTest(unittest.TestCase):
    def assert_fast(self, fn, text, what):
        t0 = time.perf_counter()
        fn(text)
        took = time.perf_counter() - t0
        self.assertLess(took, 1.0, "%s took %.2f s" % (what, took))

    def test_pathological_64k_lines(self):
        r = Redactor([TOKEN])
        for name, text in PATHOLOGICAL.items():
            with self.subTest(name=name):
                self.assertGreaterEqual(len(text), N - 64)
                self.assert_fast(redact.redact, text, name + " redact")
                self.assert_fast(r.line, text, name + " line")
                self.assert_fast(lambda t: r.lines([t] * 4), text, name + " 4 lines")
                # and with no line cap at all, every rule alone is still linear
                for i, (pattern, repl) in enumerate(redact.REDACT_RULES):
                    self.assert_fast(lambda t: pattern.sub(repl, t), text, "%s rule %d" % (name, i))


    def test_block_trackers_on_pathological_lines(self):
        for name, text in PATHOLOGICAL.items():
            with self.subTest(name=name):
                self.assert_fast(redact._redact_pem, text, name + " pem")
                self.assert_fast(redact.yaml_secret_block, text, name + " yaml block start")

    def test_dense_keywords_at_512k_stay_linear(self):
        """As X: a rescan to the line end at every keyword (the pre-fix block-indicator
        lookahead) is quadratic but fast in C at 64 KiB (~0.08 s); at 512 KiB it takes
        seconds. Linear rules take milliseconds. No line cap here: each rule alone."""
        big = 512 * 1024
        for name, unit in (("indicator-comment", "token:|#"), ("indicator-space", "token: | #"),
                           ("indicator-redacted", "token: | [REDACTED]"), ("assign", "password="),
                           ("quote", 'secret:"'), ("vault-tag", "token: !vault ")):
            text = unit * (big // len(unit))
            for i, (pattern, repl) in enumerate(redact.REDACT_RULES):
                with self.subTest(rule=i, input=name):
                    t0 = time.perf_counter()
                    pattern.sub(repl, text)
                    took = time.perf_counter() - t0
                    self.assertLess(took, 1.0, "rule %d on %s took %.2f s on 512 KiB"
                                    % (i, name, took))


    def test_a_dense_maximum_size_reply_within_two_seconds(self):
        """As X: 1000 lines x 8 KiB (the most one reply can carry), worst case for each rule in
        turn; through X-style redact() and through the agent's own path (cleaning, the token,
        then the rules)."""
        r = Redactor([TOKEN])
        for unit in DENSE_UNITS:
            lines = [(unit * (8192 // len(unit) + 1))[:8192]] * 1000
            for what, fn in (("redact", lambda: redact.redact("\n".join(lines))),
                             ("Redactor.lines", lambda: r.lines(lines))):
                with self.subTest(unit=unit[:40], path=what):
                    t0 = time.perf_counter()
                    fn()
                    took = time.perf_counter() - t0
                    self.assertLess(took, 2.0, "%r via %s: %.2f s" % (unit[:40], what, took))


class ExtrasTest(unittest.TestCase):
    def test_known_token_anywhere_and_before_the_caps(self):
        r = Redactor([TOKEN])
        self.assertEqual(r.line("plain " + TOKEN + " here"), "plain %s here" % MASK)
        # X's last rule: a [REDACTED] never shields text glued to it, so that is absorbed too
        self.assertEqual(r.line("glued" + TOKEN + "glued"), "glued%s" % MASK)
        # split by an escape sequence: stripped first, so it's whole again for the match
        self.assertEqual(r.line(TOKEN[:10] + "\x1b[0m" + TOKEN[10:]), MASK)
        out = r.line("x" * 1995 + TOKEN)  # straddles the 2000 response cap
        self.assertEqual(out, ("x" * 1995 + MASK)[:2000])
        self.assertNotIn(TOKEN[:5], r.line("y " * 4094 + TOKEN))  # straddles the 8 KiB cap

    def test_redacted_before_the_response_cut(self):
        """A token shape straddling the 2000-character cut: cut first, its fragment is too short
        for the rule (ghp_ needs 20) and would show. Both the single-line and stream paths."""
        r = Redactor()
        line = "x " * 995 + "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1"  # ghp_ starts at 1990
        for out in (r.line(line), r.lines([line])[0]):
            self.assertEqual(len(out), 2000)
            self.assertNotIn("ghp_", out)
            self.assertNotIn("A1b2", out)

    def test_token_shapes_start_at_the_start_of_a_run(self):
        # (X's _TOK lookbehind) the shape inside a longer word is not a token
        r = Redactor()
        for text in ("task-0123456789abcdefABCDEF", "xghp_FAKEfakeFAKEfakeFAKEfake0123",
                     "my_hf_FAKEfakeFAKEfake01234567",
                     "zeyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIx.c2lnbmF0dXJl"):
            self.assertEqual(r.line(text), text)
        self.assertEqual(r.line("a sk-0123456789abcdefABCDEF b"), "a [REDACTED] b")

    def test_cap_drops_partial_word(self):
        self.assertEqual(redact.cap_line("ab cd", 4), ("ab ", True))
        self.assertEqual(redact.cap_line("abcdef", 4), ("", True))
        self.assertEqual(redact.cap_line("abc", 4), ("abc", False))
        line = "w " * 4094 + "sk-SYNTHETICSYNTHETIC0123"  # the key starts at 8188 and is cut
        out = Redactor().line(line, limit=100000)
        self.assertTrue(out.endswith("w "))
        self.assertNotIn("sk-", out)

    def test_cleaning(self):
        r = Redactor()
        self.assertEqual(r.line("d \x1b[31mred\x1b[0m\x07 ctrl"), "d red ctrl")
        self.assertEqual(r.line(b"\xffbad utf8"), "\ufffdbad utf8")
        self.assertEqual(r.line("a\nb\r\nc"), "a b c")

    def test_scrub(self):
        r = Redactor([TOKEN])
        self.assertEqual(r.scrub({TOKEN: [TOKEN + "x", 1, None, {"k": "a" + TOKEN}]}),
                         {MASK: [MASK + "x", 1, None, {"k": "a" + MASK}]})


if __name__ == "__main__":
    unittest.main()
