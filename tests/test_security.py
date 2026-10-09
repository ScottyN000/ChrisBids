"""Inputs someone else chose (file names, register paths, calcs, URLs) are data."""
import os
import sqlite3
import tempfile
import time
import unittest
from unittest import mock
from pathlib import Path

from pipeline import auditor, guard, intake
from pipeline.broker import Broker
from pipeline.schema import Claim, LedgerError, arith

from .test_broker import REGISTER, claim


class ArithCase(unittest.TestCase):
    def test_plain_arithmetic_evaluates(self):
        self.assertEqual(arith("(182 + 18) * 2 / 4 - -1"), 101.0)

    def test_power_is_refused_so_a_calc_cannot_hang_the_broker(self):
        started = time.monotonic()
        with self.assertRaises(ValueError):
            arith("9**9**9**9")
        self.assertLess(time.monotonic() - started, 1)

    def test_anything_but_numbers_and_operators_is_refused(self):
        for expr in ("__import__('os')", "1 if 1 else 2", "[1]*9", "1" * 500, "2 // 1", "()"):
            with self.subTest(expr=expr), self.assertRaises(ValueError):
                arith(expr)


class BrokerStampsCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.intake = Broker.open_job(Path(self.tmp.name) / "ledger.db", "intake", job="T", run_id="r1",
                                      create=True, clock=lambda: "2026-10-07T00:00:00Z")
        self.intake.write_register(REGISTER)

    def tearDown(self):
        self.intake.close()
        self.tmp.cleanup()

    def test_a_calc_with_power_is_refused_not_evaluated(self):
        reader = self.intake.as_principal("drawing_reader")
        reader.append(claim())
        with self.assertRaises(LedgerError) as e:
            self.intake.as_principal("takeoff").append(Claim(
                claim_id="X-002", statement="x", source_id="S-1", method="counted", role="quantity",
                confidence="exact", value="1", value_num=1.0, calc="{X-001}**9**9**9"))
        self.assertIn("calc", str(e.exception))

    def test_a_row_cannot_name_another_agent_or_backdate_itself(self):
        reader = self.intake.as_principal("photo_reader", agent_label="photo reader")
        row = reader.append(claim(claim_id="P-001", method="observed", confidence="inferred", value="",
                                  value_num=None, unit="", statement="peeling",
                                  agent="Estimator", timestamp="2020-01-01T00:00:00Z"))
        self.assertEqual(row.agent, "photo reader")
        self.assertEqual(row.timestamp, "2026-10-07T00:00:00Z")

    def test_a_direct_connection_cannot_rewrite_who_wrote_a_row_or_the_log(self):
        self.intake.as_principal("drawing_reader").append(claim())
        db = sqlite3.connect(self.intake.ledger.path)
        for sql in ("UPDATE claims SET principal = 'auditor'",
                    "UPDATE register SET status = 'present'",
                    "UPDATE audit_log SET principal = 'estimator'"):
            with self.subTest(sql=sql), self.assertRaises(sqlite3.DatabaseError):
                db.execute(sql)
        db.close()

    def test_egress_refuses_local_and_non_http_urls(self):
        codes = self.intake.as_principal("codes")
        for url in ("file:///etc/passwd", "http://169.254.169.254/latest/meta-data/",
                    "http://127.0.0.1:8080/", "http://localhost/", "https://user:pw@example.com/",
                    "http://[::1]/", "gopher://example.com/"):
            with self.subTest(url=url):
                self.assertFalse(codes.may_fetch(url))

    def test_the_api_key_is_a_broker_secret(self):
        os.environ["ANTHROPIC_API_KEY"] = "sk-test"
        try:
            self.assertEqual(self.intake.as_principal("drawing_reader").secret("ANTHROPIC_API_KEY"), "sk-test")
        finally:
            del os.environ["ANTHROPIC_API_KEY"]


class PathCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.packet = self.root / "packet"
        self.packet.mkdir()
        (self.root / "secret.txt").write_text("not part of the job")

    def tearDown(self):
        self.tmp.cleanup()

    def test_register_paths_cannot_leave_the_packet(self):
        self.assertEqual(guard.inside(self.packet, "a/b.pdf"), self.packet.resolve() / "a" / "b.pdf")
        for rel in ("../secret.txt", "/etc/passwd", "a/../../secret.txt"):
            with self.subTest(rel=rel), self.assertRaises(guard.UnsafeInput):
                guard.inside(self.packet, rel)

    def test_intake_skips_a_symlink_out_of_the_packet(self):
        (self.packet / "spec.txt").write_text("real")
        (self.packet / "email.txt").symlink_to(self.root / "secret.txt")
        files = [s.file for s in intake.scan(self.packet)]
        self.assertEqual(files, ["spec.txt"])

    def test_verify_refuses_a_register_path_outside_the_packet(self):
        b = Broker.open_job(self.root / "ledger.db", "intake", job="T", run_id="r1", create=True)
        b.write_register([{"source_id": "X", "title": "x", "file": "../secret.txt",
                           "sha256": "0", "kind": "spec", "status": "present"}])
        problems = intake.verify(b, self.packet)
        b.close()
        self.assertEqual(len(problems), 1)
        self.assertIn("outside", problems[0])

    def test_the_auditor_does_not_follow_a_link_to_a_local_address(self):
        ok, why = auditor.link_live("http://169.254.169.254/latest/meta-data/")
        self.assertFalse(ok)
        self.assertIn("not checked", why)


class PublicUrlCase(unittest.TestCase):
    def resolving(self, addr):
        return mock.patch("pipeline.guard.socket.getaddrinfo", return_value=[(2, 1, 6, "", (addr, 443))])

    def test_a_public_host_passes_and_is_returned_unchanged(self):
        with self.resolving("93.184.215.14") as m:
            self.assertEqual(guard.public_url("https://example.org:8443/a?b=1"), "https://example.org:8443/a?b=1")
        self.assertEqual(m.call_args.args, ("example.org", 8443))
        with self.resolving("93.184.215.14") as m:
            guard.public_url("http://example.org/")
        self.assertEqual(m.call_args.args, ("example.org", 80))

    def test_a_name_that_resolves_to_a_private_address_is_refused(self):
        with self.resolving("10.0.0.5"), self.assertRaises(guard.UnsafeInput) as e:
            guard.public_url("https://intranet.example.org/")
        self.assertEqual(str(e.exception), "intranet.example.org resolves to non-public address 10.0.0.5")

    def test_without_resolve_no_lookup_is_made(self):
        with mock.patch("pipeline.guard.socket.getaddrinfo") as m:
            self.assertEqual(guard.public_url("https://example.org/", resolve=False), "https://example.org/")
        m.assert_not_called()

    def test_refusal_messages(self):
        cases = {
            "ftp://example.org/": "only http(s) URLs are fetched, not 'ftp'",
            "example.org": "only http(s) URLs are fetched, not 'no scheme'",
            "https://u@example.org/": "a URL carrying credentials is not fetched",
            "https:///path": "URL has no host",
            "http://app.localhost/": "app.localhost is the local machine",
            "http://192.168.1.1/": "192.168.1.1 is a non-public address",
        }
        for url, msg in cases.items():
            with self.subTest(url=url), self.assertRaises(guard.UnsafeInput) as e:
                guard.public_url(url, resolve=False)
            self.assertEqual(str(e.exception), msg)

    def test_a_redirect_to_a_local_address_is_refused(self):
        handler = auditor._PublicRedirects()
        with self.assertRaises(guard.UnsafeInput):
            handler.redirect_request(None, None, 302, "Found", {}, "http://169.254.169.254/")

    def test_the_packet_root_itself_is_inside(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(guard.inside(d, "."), Path(d).resolve())
            self.assertEqual(guard.inside(d, ""), Path(d).resolve())


if __name__ == "__main__":
    unittest.main()
