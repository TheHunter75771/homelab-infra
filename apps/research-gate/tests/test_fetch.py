import gzip
import ipaddress
import os
import shutil
import ssl
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from research_gate.fetch import FetchError, RedirectRefused, SafeFetcher, SearxClient
from research_gate.netguard import AddressPolicy, AddressRefused, TablesUnreadable, address_problem

from .fakes import (HOST_V4, HOST_V6, Rebinding, Resolver, Server, gzip_bomb, make_proc, page, redirect, send,
                    slow_body, slow_headers)

UA = "test-agent/1"

REFUSED = {
    "127.0.0.1": "loopback", "127.8.9.10": "loopback", "10.1.2.3": "private", "172.16.5.4": "private",
    "172.31.255.255": "private", "192.168.1.1": "private", "169.254.169.254": "link-local",
    "100.64.0.1": "CGNAT", "100.100.100.100": "CGNAT", "100.127.255.254": "CGNAT",
    "0.0.0.0": "unspecified", "0.1.2.3": "", "224.0.0.1": "multicast", "239.255.255.250": "multicast",
    "240.0.0.1": "", "255.255.255.255": "", "192.0.2.10": "", "198.18.0.1": "", "192.0.0.8": "",
    "::1": "loopback", "::": "unspecified", "fe80::1": "link-local", "fe80::1%2": "link-local",
    "fc00::1": "ULA", "fd7a:115c:a1e0::1": "ULA", "ff02::1": "multicast", "fec0::1": "site-local",
    "2001:db8::1": "", "::ffff:127.0.0.1": "IPv4-mapped loopback", "::ffff:10.0.0.1": "IPv4-mapped private",
    "::ffff:169.254.169.254": "IPv4-mapped link-local", "::ffff:100.64.0.9": "IPv4-mapped CGNAT",
    "::ffff:0.0.0.0": "IPv4-mapped unspecified", "::ffff:192.168.0.1": "IPv4-mapped private",
    "64:ff9b::7f00:1": "NAT64 loopback", "64:ff9b::a00:1": "NAT64 private", "64:ff9b::a9fe:a9fe": "NAT64 link-local",
    "64:ff9b::6440:1": "NAT64 CGNAT", "64:ff9b:1::808:808": "local-use NAT64",
    "2002:7f00:1::1": "6to4 loopback", "2002:c0a8:101::1": "6to4 private", "2001::1": "Teredo",
    "::7f00:1": "", "100::1": "",
}
ALLOWED = ["8.8.8.8", "93.184.216.34", "1.1.1.1", "2606:4700:4700::1111", "::ffff:8.8.8.8",
           "64:ff9b::808:808", "2002:808:808::1"]


class AddressClassTests(unittest.TestCase):
    def test_each_refused_class(self):
        for addr, label in REFUSED.items():
            with self.subTest(addr=addr):
                why = address_problem(addr)
                self.assertIsNotNone(why)
                self.assertIn(label, why)

    def test_public_addresses_allowed(self):
        for addr in ALLOWED:
            with self.subTest(addr=addr):
                self.assertIsNone(address_problem(addr))

    def test_test_allowance_is_plain_ipv4_loopback_only(self):
        policy = AddressPolicy(_test_allow_loopback=True)
        self.assertIsNone(policy.problem("127.0.0.1"))
        for addr in ("::1", "::ffff:127.0.0.1", "64:ff9b::7f00:1", "10.0.0.1", "169.254.1.1", "0.0.0.0"):
            self.assertIsNotNone(policy.problem(addr), addr)
        self.assertIsNotNone(AddressPolicy().problem("127.0.0.1"))

    def test_mixed_answers_refused(self):
        with tempfile.TemporaryDirectory() as d:
            policy = AddressPolicy(proc_paths=make_proc(Path(d) / "proc"))
            policy.refresh()
            self.assertEqual(policy.check_all("x", ["8.8.8.8", "1.1.1.1"]), ["8.8.8.8", "1.1.1.1"])
            for answers in (["8.8.8.8", "10.0.0.1"], ["1.1.1.1", "::ffff:127.0.0.1"], []):
                with self.subTest(answers=answers), self.assertRaises(AddressRefused):
                    policy.check_all("x", answers)


class LocalNetworkTests(unittest.TestCase):
    """The host's own addresses and on-link networks (Reviewer E round 1, finding 1)."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def policy(self, **tables):
        p = AddressPolicy(proc_paths=make_proc(self.dir / "proc", **tables))
        p.refresh()
        return p

    def test_own_address_and_same_prefix_refused_other_global_allowed(self):
        p = self.policy()
        self.assertIn("local network", p.problem(HOST_V6))
        self.assertIn("2600:1700:aaaa:bbbb::/64", p.problem("2600:1700:aaaa:bbbb::99"))  # noprefixroute /64
        self.assertIsNone(p.problem("2600:1700:aaaa:cccc::1"))
        self.assertIsNone(p.problem("2606:4700:4700::1111"))

    def test_on_link_route_refused_gateway_route_allowed(self):
        p = self.policy()
        self.assertIn("2600:1700:dddd::/48", p.problem("2600:1700:dddd:1::5"))
        self.assertIsNone(p.problem("2a00:1450::1"))  # 2a00::/16 is via a gateway, not on-link

    def test_local_ipv4_address_and_on_link_prefix_refused(self):
        p = self.policy()
        self.assertIn("44.1.2.3/32", p.problem(HOST_V4))
        self.assertIn("44.1.2.0/24", p.problem("44.1.2.77"))
        self.assertIn("local network", p.problem("::ffff:44.1.2.3"))
        self.assertIsNone(p.problem("8.8.8.8"))

    def test_a_128_address_is_refused_as_an_address(self):
        p = self.policy(if_inet6=f"{int(ipaddress.IPv6Address('2600:9::7')):032x} 03 80 00 80 tailscale0\n")
        self.assertIn("2600:9::7/128", p.problem("2600:9::7"))
        self.assertIsNone(p.problem("2600:9::8"))

    def test_unreadable_tables_raise_not_refuse(self):
        p = AddressPolicy(proc_paths={"if_inet6": str(self.dir / "missing"),
                                      **{k: v for k, v in make_proc(self.dir / "p").items() if k != "if_inet6"}})
        p.refresh()
        with self.assertRaises(TablesUnreadable) as ctx:
            p.problem("2600:1700:aaaa:cccc::1")
        self.assertIn("IPv6 interface tables could not be read", str(ctx.exception))
        self.assertIsNone(p.problem("8.8.8.8"))
        self.assertIn("IPv6", p.local.unreadable())
        self.assertIsNone(p.local.unreadable(need_v6=False))
        p = AddressPolicy(proc_paths={"fib_trie": str(self.dir / "missing"),
                                      **{k: v for k, v in make_proc(self.dir / "p").items() if k != "fib_trie"}})
        p.refresh()
        with self.assertRaises(TablesUnreadable):
            p.problem("8.8.8.8")
        with self.assertRaises(TablesUnreadable):
            p.problem("::ffff:8.8.8.8")
        self.assertIn("IPv4", p.local.unreadable(need_v6=False))

    def test_embedded_ipv4_forms_of_local_addresses_refused(self):
        p = self.policy()
        local4 = ipaddress.IPv4Address(HOST_V4)          # 44.1.2.3, a local address
        onlink4 = ipaddress.IPv4Address("44.1.2.77")      # in the local on-link /24
        for v4 in (local4, onlink4):
            n = int(v4)
            for form in (f"::ffff:{v4}", f"64:ff9b::{n >> 16:x}:{n & 0xffff:x}",
                         f"64:ff9b:1::{n >> 16:x}:{n & 0xffff:x}", f"2002:{n >> 16:x}:{n & 0xffff:x}::1",
                         f"::{n >> 16:x}:{n & 0xffff:x}"):
                with self.subTest(form=form):
                    why = p.local.problem(ipaddress.ip_address(form))
                    self.assertIsNotNone(why)
                    self.assertIn("local network", why)
                    self.assertIn("embedded in", why)
        self.assertIsNone(p.local.problem(ipaddress.ip_address("64:ff9b::808:808")))
        self.assertIsNone(p.local.problem(ipaddress.ip_address("2002:808:808::1")))

    def test_this_hosts_real_tables_parse(self):
        p = AddressPolicy()
        p.refresh()  # reads only local /proc files; no network
        if os.path.exists("/proc/self/net/fib_trie"):
            self.assertTrue(p.local.v4_ok)


class FetcherCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.proc = make_proc(Path(tmp.name) / "proc")
        self.web = Server()
        self.addCleanup(self.web.close)
        self.resolver = Resolver({"pub.example.test": ["127.0.0.1"], "two.example.test": ["127.0.0.1"]})

    def fetcher(self, *, max_bytes=64 * 1024, timeout=2.0, max_redirects=3, loopback=True, resolver=None, **kw):
        kw.setdefault("proc_paths", self.proc)
        return SafeFetcher(max_bytes=max_bytes, timeout=timeout, max_redirects=max_redirects, user_agent=UA,
                           resolver=resolver or self.resolver, _test_allow_loopback=loopback, **kw)

    def url(self, path, host="pub.example.test"):
        return f"http://{host}:{self.web.port}{path}"


class SsrfTests(FetcherCase):
    def test_every_refused_class_is_never_contacted(self):
        self.web.routes["/x"] = page("<p>x</p>")
        for addr in REFUSED:
            with self.subTest(addr=addr):
                resolver = Resolver({"target.example.test": [addr]})
                with self.assertRaises(FetchError) as ctx:
                    self.fetcher(resolver=resolver, loopback=False).fetch(
                        self.url("/x", "target.example.test"), lambda u: True)
                self.assertTrue(ctx.exception.terminal)
                self.assertIn("refused address", str(ctx.exception))
        self.assertEqual(self.web.requests, [])

    def test_production_refuses_loopback(self):
        self.web.routes["/x"] = page("<p>x</p>")
        prod = self.fetcher(loopback=False)
        for url in (self.url("/x"), f"http://127.0.0.1:{self.web.port}/x", f"http://[::1]:{self.web.port}/x"):
            with self.subTest(url=url), self.assertRaises(FetchError) as ctx:
                prod.fetch(url, lambda u: True)
            self.assertIn("loopback", str(ctx.exception))
        self.assertEqual(self.web.requests, [])

    def test_ip_literal_urls_are_checked_without_dns(self):
        with self.assertRaises(FetchError):
            self.fetcher().fetch(f"http://10.0.0.1:{self.web.port}/", lambda u: True)
        with self.assertRaises(FetchError):
            self.fetcher().fetch("http://[::ffff:127.0.0.1]/", lambda u: True)
        self.assertEqual(self.resolver.calls, [])

    def test_non_canonical_urls_refused(self):
        for bad in ("ftp://pub.example.test/", "http://u:p@pub.example.test/", "http://0x7f.1/",
                    "http://pub.example.test/#f", "file:///etc/passwd", "http://2130706433/"):
            with self.subTest(url=bad), self.assertRaises(FetchError) as ctx:
                self.fetcher().fetch(bad, lambda u: True)
            self.assertTrue(ctx.exception.terminal)
        self.assertEqual(self.resolver.calls, [])

    def test_mixed_dns_answer_never_contacted(self):
        self.web.routes["/x"] = page("<p>x</p>")
        self.resolver.table["mixed.example.test"] = ["127.0.0.1", "10.0.0.1"]
        with self.assertRaises(FetchError) as ctx:
            self.fetcher().fetch(self.url("/x", "mixed.example.test"), lambda u: True)
        self.assertIn("10.0.0.1", str(ctx.exception))
        self.assertEqual(self.web.requests, [])

    def test_dns_rebinding_only_first_answer_used(self):
        self.web.routes["/x"] = page("<p>x</p>")
        rebind = Rebinding(["127.0.0.1"], ["169.254.169.254"])
        resolver = Resolver({"rebind.example.test": rebind})
        resp = self.fetcher(resolver=resolver).fetch(self.url("/x", "rebind.example.test"), lambda u: True)
        self.assertEqual(resp.status, 200)
        self.assertEqual(resolver.calls, ["rebind.example.test"])
        self.assertEqual(rebind.n, 1)
        self.assertEqual(self.web.requests[0][1]["host"], f"rebind.example.test:{self.web.port}")

    def test_dns_rebinding_refused_first_answer_is_final(self):
        self.web.routes["/x"] = page("<p>x</p>")
        rebind = Rebinding(["10.0.0.1"], ["127.0.0.1"])
        resolver = Resolver({"rebind.example.test": rebind})
        with self.assertRaises(FetchError):
            self.fetcher(resolver=resolver).fetch(self.url("/x", "rebind.example.test"), lambda u: True)
        self.assertEqual(rebind.n, 1)
        self.assertEqual(self.web.requests, [])

    def test_redirect_into_private_space_refused_after_re_resolve(self):
        self.resolver.table["internal.example.test"] = ["192.168.0.10"]
        self.web.routes["/r"] = redirect(f"http://internal.example.test:{self.web.port}/admin")
        with self.assertRaises(FetchError) as ctx:
            self.fetcher().fetch(self.url("/r"), lambda u: True)
        self.assertIn("192.168.0.10", str(ctx.exception))
        self.assertEqual(self.web.paths(), ["/r"])

    def test_own_global_ipv6_refused_and_tables_re_read_each_hop(self):
        self.resolver.table["own.example.test"] = [HOST_V6]
        self.resolver.table["neighbour.example.test"] = ["2600:1700:aaaa:bbbb::42"]
        self.resolver.table["newnet.example.test"] = ["2600:1700:eeee::5"]
        for name in ("own.example.test", "neighbour.example.test"):
            with self.subTest(name=name), self.assertRaises(FetchError) as ctx:
                self.fetcher().fetch(self.url("/x", name), lambda u: True)
            self.assertIn("local network", str(ctx.exception))

        def hop1(req):
            # The host gains a new on-link prefix while the first hop is answered.
            z = "0" * 32
            Path(self.proc["ipv6_route"]).write_text(
                f"{int(ipaddress.IPv6Address('2600:1700:eeee::')):032x} 30 {z} 00 {z} 00000100 00000001 00000000 00000001 eth0\n")
            send(req, 302, b"", "text/plain", {"Location": self.url("/y", "newnet.example.test")})
        self.web.routes["/hop"] = hop1
        with self.assertRaises(FetchError) as ctx:
            self.fetcher().fetch(self.url("/hop"), lambda u: True)
        self.assertIn("2600:1700:eeee::/48", str(ctx.exception))
        self.assertEqual(self.web.paths(), ["/hop"])

    def test_unreadable_tables_are_a_retryable_not_counted_error(self):
        Path(self.proc["if_inet6"]).unlink()
        self.resolver.table["v6.example.test"] = ["2606:4700::1111"]
        with self.assertRaises(FetchError) as ctx:
            self.fetcher().fetch(self.url("/x", "v6.example.test"), lambda u: True)
        self.assertFalse(ctx.exception.terminal)
        self.assertIn("not counted: the host's IPv6 interface tables could not be read", str(ctx.exception))
        self.assertIn("IPv6", self.fetcher().tables_problem())
        self.assertIsNone(self.fetcher(ipv4_only=True).tables_problem())

    def test_ipv4_only_uses_the_ipv4_answer(self):
        import socket
        self.web.routes["/x"] = page("<p>x</p>")
        # A dual-stack answer whose IPv6 half is refused (the host's own address): with ipv4_only
        # the IPv6 answer is never considered, so the fetch goes to the IPv4 address.
        self.resolver.table["dual.example.test"] = [HOST_V6, "127.0.0.1"]
        resp = self.fetcher(ipv4_only=True).fetch(self.url("/x", "dual.example.test"), lambda u: True)
        self.assertEqual(resp.status, 200)
        self.assertEqual(self.resolver.families, [socket.AF_INET])
        with self.assertRaises(FetchError) as ctx:
            self.fetcher().fetch(self.url("/x", "dual.example.test"), lambda u: True)
        self.assertIn("local network", str(ctx.exception))
        self.assertEqual(self.resolver.families[-1], socket.AF_UNSPEC)
        self.assertEqual(self.web.paths(), ["/x"])

    def test_ipv4_only_refuses_v6_only_answers_and_literals(self):
        self.resolver.table["v6only.example.test"] = ["2606:4700::1111"]
        with self.assertRaises(FetchError) as ctx:
            self.fetcher(ipv4_only=True).fetch(self.url("/x", "v6only.example.test"), lambda u: True)
        self.assertIn("no IPv4 address", str(ctx.exception))
        self.assertTrue(ctx.exception.terminal)
        with self.assertRaises(FetchError) as ctx:
            self.fetcher(ipv4_only=True).fetch("http://[2606:4700::1111]/", lambda u: True)
        self.assertIn("no IPv4 address", str(ctx.exception))
        self.assertEqual(self.web.requests, [])

    def test_redirect_to_literal_loopback_refused(self):
        self.web.routes["/r"] = redirect(f"http://[::ffff:127.0.0.1]:{self.web.port}/admin")
        with self.assertRaises(FetchError):
            self.fetcher().fetch(self.url("/r"), lambda u: True)
        self.assertEqual(self.web.paths(), ["/r"])

    def test_redirect_needs_approval(self):
        self.web.routes["/r"] = redirect(self.url("/t"))
        self.web.routes["/t"] = page("<p>t</p>")
        asked = []
        with self.assertRaises(RedirectRefused) as ctx:
            self.fetcher().fetch(self.url("/r"), lambda u: asked.append(u) or False)
        self.assertEqual(asked, [self.url("/t")])
        self.assertEqual(ctx.exception.target, self.url("/t"))
        self.assertEqual(self.web.paths(), ["/r"])
        resp = self.fetcher().fetch(self.url("/r"), lambda u: True)
        self.assertEqual((resp.url, resp.redirects), (self.url("/t"), [self.url("/t")]))

    def test_redirect_to_bad_scheme_refused(self):
        self.web.routes["/r"] = redirect("file:///etc/passwd")
        with self.assertRaises(FetchError) as ctx:
            self.fetcher().fetch(self.url("/r"), lambda u: True)
        self.assertTrue(ctx.exception.terminal)

    def test_at_most_max_redirects_hops(self):
        for i in range(6):
            self.web.routes[f"/r{i}"] = redirect(f"/r{i + 1}")
        with self.assertRaises(RedirectRefused) as ctx:
            self.fetcher(max_redirects=3).fetch(self.url("/r0"), lambda u: True)
        self.assertEqual(self.web.paths(), ["/r0", "/r1", "/r2", "/r3"])
        self.assertEqual(ctx.exception.target, self.url("/r4"))
        self.web.requests.clear()
        self.web.routes["/r3"] = page("<p>end</p>")
        resp = self.fetcher(max_redirects=3).fetch(self.url("/r0"), lambda u: True)
        self.assertEqual(len(resp.redirects), 3)

    def test_max_redirects_zero(self):
        self.web.routes["/r"] = redirect(self.url("/t"))
        with self.assertRaises(RedirectRefused):
            self.fetcher(max_redirects=0).fetch(self.url("/r"), lambda u: True)

    def test_no_proxy_from_environment(self):
        proxy = Server()
        self.addCleanup(proxy.close)
        proxy.routes["/x"] = page("<p>proxy</p>")
        self.web.routes["/x"] = page("<p>direct</p>")
        names = ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY", "all_proxy", "ALL_PROXY")
        old = {n: os.environ.get(n) for n in names}
        try:
            for n in names:
                os.environ[n] = f"http://127.0.0.1:{proxy.port}"
            resp = self.fetcher().fetch(self.url("/x"), lambda u: True)
        finally:
            for n, v in old.items():
                if v is None:
                    os.environ.pop(n, None)
                else:
                    os.environ[n] = v
        self.assertEqual(resp.body, b"<p>direct</p>")
        self.assertEqual(proxy.requests, [])

    def test_exact_request_target(self):
        self.web.routes["/A%20b/"] = page("<p>x</p>")
        self.web.routes["/"] = page("<p>root</p>")
        self.fetcher().fetch(self.url("/A%20b/?q=1&Z=%2F"), lambda u: True)
        self.fetcher().fetch(f"http://pub.example.test:{self.web.port}", lambda u: True)
        self.assertEqual(self.web.paths(), ["/A%20b/?q=1&Z=%2F", "/"])


class LimitTests(FetcherCase):
    def test_size_cap_by_content_length(self):
        self.web.routes["/big"] = page(b"x" * 5000, "text/plain")
        with self.assertRaises(FetchError) as ctx:
            self.fetcher(max_bytes=4096).fetch(self.url("/big"), lambda u: True)
        self.assertIn("Content-Length 5000 is over the 4096-byte cap", str(ctx.exception))
        self.assertTrue(ctx.exception.terminal)

    def test_size_cap_while_reading_without_length(self):
        def chunked(req):
            req.send_response(200)
            req.send_header("Content-Type", "text/plain")
            req.send_header("Transfer-Encoding", "chunked")
            req.end_headers()
            for _ in range(100):
                req.wfile.write(b"400\r\n" + b"y" * 1024 + b"\r\n")
            req.wfile.write(b"0\r\n\r\n")
        self.web.routes["/chunked"] = chunked
        with self.assertRaises(FetchError) as ctx:
            self.fetcher(max_bytes=8192).fetch(self.url("/chunked"), lambda u: True)
        self.assertIn("cap", str(ctx.exception))

    def test_wire_cap_applies_to_compressed_bytes(self):
        body = gzip.compress(os.urandom(4000))  # incompressible: more bytes on the wire than decoded
        self.assertGreater(len(body), 4000)

        def chunked(req):
            req.send_response(200)
            req.send_header("Content-Type", "text/plain")
            req.send_header("Content-Encoding", "gzip")
            req.send_header("Transfer-Encoding", "chunked")
            req.end_headers()
            req.wfile.write(b"%x\r\n" % len(body) + body + b"\r\n0\r\n\r\n")
        self.web.routes["/gz"] = chunked
        with self.assertRaises(FetchError) as ctx:
            self.fetcher(max_bytes=4010).fetch(self.url("/gz"), lambda u: True)
        self.assertIn("larger than the 4010-byte cap", str(ctx.exception))
        self.assertNotIn("after decoding", str(ctx.exception))

    def test_lying_content_length_is_still_capped(self):
        def liar(req):
            req.send_response(200)
            req.send_header("Content-Type", "text/plain")
            req.send_header("Content-Length", "10")
            req.end_headers()
            req.wfile.write(b"z" * 50_000)
        self.web.routes["/liar"] = liar
        resp = self.fetcher(max_bytes=4096).fetch(self.url("/liar"), lambda u: True)
        self.assertEqual(resp.body, b"z" * 10)  # http.client stops at the declared length

    def test_gzip_bomb_stops_at_the_cap(self):
        bomb = gzip_bomb(50 * 1024 * 1024)
        self.assertLess(len(bomb), 64 * 1024)
        self.web.routes["/bomb"] = page(bomb, "text/html", headers={"Content-Encoding": "gzip"})
        t0 = time.monotonic()
        with self.assertRaises(FetchError) as ctx:
            self.fetcher(max_bytes=64 * 1024).fetch(self.url("/bomb"), lambda u: True)
        self.assertIn("after decoding", str(ctx.exception))
        self.assertLess(time.monotonic() - t0, 2)

    def test_deflate_bomb_and_raw_deflate(self):
        import zlib
        self.web.routes["/raw"] = page(zlib.compress(b"hello", 9)[2:-4], "text/plain",
                                       headers={"Content-Encoding": "deflate"})
        self.assertEqual(self.fetcher().fetch(self.url("/raw"), lambda u: True).body, b"hello")
        self.web.routes["/zb"] = page(zlib.compress(b"\0" * (20 * 1024 * 1024), 9), "text/plain",
                                      headers={"Content-Encoding": "deflate"})
        with self.assertRaises(FetchError):
            self.fetcher(max_bytes=64 * 1024).fetch(self.url("/zb"), lambda u: True)

    def test_unknown_encoding_and_bad_gzip(self):
        self.web.routes["/br"] = page(b"xx", "text/plain", headers={"Content-Encoding": "br"})
        self.web.routes["/badgz"] = page(b"not gzip at all", "text/plain", headers={"Content-Encoding": "gzip"})
        for p in ("/br", "/badgz"):
            with self.subTest(p=p), self.assertRaises(FetchError):
                self.fetcher().fetch(self.url(p), lambda u: True)

    def test_slowloris_body_hits_the_total_deadline(self):
        self.web.routes["/slow"] = slow_body(self.web, every=0.05)
        t0 = time.monotonic()
        with self.assertRaises(FetchError) as ctx:
            self.fetcher(timeout=1.0).fetch(self.url("/slow"), lambda u: True)
        elapsed = time.monotonic() - t0
        self.assertIn("time limit", str(ctx.exception))
        self.assertFalse(ctx.exception.terminal)
        self.assertLess(elapsed, 2.0)
        self.assertGreater(elapsed, 0.9)

    def test_slowloris_headers_hit_the_total_deadline(self):
        self.web.routes["/slowh"] = slow_headers(self.web, every=0.05)
        t0 = time.monotonic()
        with self.assertRaises(FetchError) as ctx:
            self.fetcher(timeout=1.0).fetch(self.url("/slowh"), lambda u: True)
        self.assertIn("time limit", str(ctx.exception))
        self.assertLess(time.monotonic() - t0, 2.0)

    def test_deadline_spans_redirect_hops(self):
        def slow_redirect(req):
            time.sleep(0.6)
            send(req, 302, b"", "text/plain", {"Location": self.url("/r")})
        self.web.routes["/r"] = slow_redirect
        t0 = time.monotonic()
        with self.assertRaises(FetchError):
            self.fetcher(timeout=1.0, max_redirects=3).fetch(self.url("/r"), lambda u: True)
        self.assertLess(time.monotonic() - t0, 1.8)

    def test_truncated_body_is_an_error(self):
        def short(req):
            req.send_response(200)
            req.send_header("Content-Type", "text/plain")
            req.send_header("Content-Length", "1000")
            req.end_headers()
            req.wfile.write(b"only a bit")
            req.wfile.flush()
            req.close_connection = True
        self.web.routes["/short"] = short
        with self.assertRaises(FetchError) as ctx:
            self.fetcher().fetch(self.url("/short"), lambda u: True)
        self.assertFalse(ctx.exception.terminal)

    def test_user_agent_and_headers(self):
        self.web.routes["/x"] = page("<p>x</p>")
        self.fetcher().fetch(self.url("/x"), lambda u: True)
        headers = self.web.requests[0][1]
        self.assertEqual(headers["user-agent"], UA)
        self.assertEqual(headers["accept-encoding"], "gzip, deflate")
        for h in ("cookie", "authorization", "proxy-authorization"):
            self.assertNotIn(h, headers)


@unittest.skipUnless(shutil.which("openssl"), "openssl is needed to make a test certificate")
class TlsTests(unittest.TestCase):
    """https: connect to the resolved IP but validate the certificate (and send SNI) for the name."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        d = Path(tmp.name)
        self.cert, self.key = d / "c.pem", d / "k.pem"
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                        "-subj", "/CN=secure.example.test", "-addext", "subjectAltName=DNS:secure.example.test",
                        "-keyout", str(self.key), "-out", str(self.cert)],
                       check=True, capture_output=True)
        self.server = Server()
        self.addCleanup(self.server.close)
        self.sni: list[str | None] = []
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(self.cert, self.key)
        ctx.sni_callback = lambda sock, name, c: self.sni.append(name)
        self.server.httpd.socket = ctx.wrap_socket(self.server.httpd.socket, server_side=True)
        self.server.routes["/s"] = page("<p>secure</p>")
        self.client_ctx = ssl.create_default_context(cafile=str(self.cert))

    def fetcher(self, names):
        return SafeFetcher(max_bytes=65536, timeout=3, max_redirects=0, user_agent=UA,
                           resolver=Resolver({n: ["127.0.0.1"] for n in names}),
                           proc_paths=make_proc(Path(self.cert).parent / "proc"),
                           ssl_context=self.client_ctx, _test_allow_loopback=True)

    def test_sni_and_certificate_for_the_name(self):
        resp = self.fetcher(["secure.example.test"]).fetch(
            f"https://secure.example.test:{self.server.port}/s", lambda u: True)
        self.assertEqual(resp.body, b"<p>secure</p>")
        self.assertEqual(self.sni, ["secure.example.test"])
        self.assertEqual(self.server.requests[0][1]["host"], f"secure.example.test:{self.server.port}")

    def test_wrong_name_fails_certificate_check(self):
        with self.assertRaises(FetchError) as ctx:
            self.fetcher(["wrong.example.test"]).fetch(f"https://wrong.example.test:{self.server.port}/s",
                                                       lambda u: True)
        self.assertIn("CERTIFICATE", str(ctx.exception).upper())
        self.assertEqual(self.server.requests, [])


class SearxClientTests(unittest.TestCase):
    def setUp(self):
        self.web = Server()
        self.addCleanup(self.web.close)

    def client(self, **kw):
        args = dict(max_bytes=4096, timeout=1.0, user_agent=UA)
        args.update(kw)
        return SearxClient(f"http://127.0.0.1:{self.web.port}", **args)

    def test_query_encoding_and_results(self):
        self.web.routes["/search"] = page(b'{"results": [{"url": "https://a/", "title": "A"}, 5]}', "application/json")
        results, nbytes, status = self.client().search("a b&c=d/e")
        self.assertEqual(results, [{"url": "https://a/", "title": "A"}])
        self.assertEqual(status, 200)
        self.assertEqual(self.web.paths(), ["/search?q=a+b%26c%3Dd%2Fe&format=json"])

    def test_size_cap_timeout_and_garbage(self):
        self.web.routes["/search"] = page(b'{"results": []}' + b" " * 10_000, "application/json")
        with self.assertRaises(FetchError):
            self.client().search("q")
        self.web.routes["/search"] = slow_body(self.web, "application/json", every=0.05)
        with self.assertRaises(FetchError):
            self.client().search("q")
        self.web.routes["/search"] = page(b"<html>", "text/html")
        with self.assertRaises(FetchError):
            self.client().search("q")
        self.web.routes["/search"] = redirect("http://elsewhere.example/")
        with self.assertRaises(FetchError):
            self.client().search("q")


if __name__ == "__main__":
    unittest.main()
