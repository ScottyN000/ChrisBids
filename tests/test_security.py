"""Inputs someone else chose (file names, register paths, calcs, URLs) are data."""
import os
import sqlite3
import tempfile
import time
import unittest
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
                                  agent="Chris Turner", timestamp="2020-01-01T00:00:00Z"))
        self.assertEqual(row.agent, "photo reader")
        self.assertEqual(row.timestamp, "2026-10-07T00:00:00Z")

    def test_a_direct_connection_cannot_rewrite_who_wrote_a_row_or_the_log(self):
        self.intake.as_principal("drawing_reader").append(claim())
        db = sqlite3.connect(self.intake.ledger.path)
        for sql in ("UPDATE claims SET principal = 'auditor'",
                    "UPDATE register SET status = 'present'",
                    "UPDATE audit_log SET principal = 'chris'"):
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


if __name__ == "__main__":
    unittest.main()
