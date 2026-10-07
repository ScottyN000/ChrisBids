"""Exact text of the run reports and exports, and the broker's secret rule.

Written to kill surviving mutants in ReadResult.text, Comparison.text,
Ledger.register_csv / audit_json and Broker.secret: these are what Chris and
the CI log read, so a change to them should be a change someone made on purpose.
"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pipeline.broker import Broker
from pipeline.readers.compare import Comparison, compare
from pipeline.readers.run import ReadResult
from pipeline.schema import Claim, LedgerError


def c(claim_id, method="dimensioned", value="182", source="S-1", locator="Fnd", flag="", role="quantity", **kw):
    return Claim(claim_id=claim_id, statement="s", source_id=source, method=method, role=role,
                 confidence="exact", locator=locator, value=value, unit="in", flag=flag, **kw)


class ReadResultCase(unittest.TestCase):
    def test_text(self):
        r = ReadResult(reader="drawing", units=2, calls=6, discarded=["U run 1: bad"], unread=["V"],
                       rows=[c("A"), c("B", flag="conflict")], refused=["B: no"])
        self.assertEqual(r.text(), (
            "drawing: 2 units, 6 calls, 2 rows (1 flagged), 1 responses discarded, 1 units unread\n"
            "  discarded U run 1: bad\n  unread V\n  refused B: no"))
        self.assertEqual(ReadResult(reader="photo").text(),
                         "photo: 0 units, 0 calls, 0 rows (0 flagged), 0 responses discarded, 0 units unread")


class ComparisonCase(unittest.TestCase):
    def test_text_and_exactness(self):
        ok = Comparison(matched=[("dimensioned", "S-1", "Fnd", "182", "in")],
                        extra=[("observed", "IMG_1")], missing=[("scaled", "S-1", "Det", "1'-8\"", "")])
        self.assertTrue(ok.exact_ok)
        self.assertEqual(ok.text(), (
            "1 matched, 1 missing, 1 extra; dimensioned and counted reproduce exactly\n"
            "  missing: scaled | S-1 | Det | 1'-8\" |   (allowed to vary)\n"
            "  extra: observed | IMG_1  (allowed to vary)"))
        bad = Comparison(missing=[("counted", "S-1", "Fnd", "6", "each")])
        self.assertFalse(bad.exact_ok)
        self.assertEqual(bad.text(), "0 matched, 1 missing, 0 extra; dimensioned and counted DO NOT reproduce\n"
                                     "  missing: counted | S-1 | Fnd | 6 | each")
        self.assertFalse(Comparison(extra=[("dimensioned",)]).exact_ok)

    def test_scope_and_reader_rows(self):
        fixture = [c("F1"), c("F2", value="32"), c("F3", source="OTHER"), c("F4", source="S-1 + S-2"),
                   c("F5", calc="F1 - F2"), c("F6", method="clause"), c("F7", role="scope")]
        got = compare([c("G1")], fixture, {"S-1", "OTHER"})
        self.assertEqual(got.matched, [("dimensioned", "S-1", "Fnd", "182", "in")])
        self.assertEqual(got.missing, [("dimensioned", "S-1", "Fnd", "32", "in")])
        self.assertEqual(got.extra, [])
        # An explicit scope widens the comparison to sources nothing was produced for.
        got = compare([c("G1")], fixture, {"S-1", "OTHER"}, sources={"S-1", "OTHER"})
        self.assertEqual(len(got.missing), 2)
        # A source that is not in the packet is never expected.
        self.assertEqual(compare([], fixture, {"S-1"}, sources={"OTHER"}).missing, [])


class LedgerExportCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.broker = Broker.open_job(Path(self.tmp.name) / "l.db", "intake", job="NAN", run_id="r1", create=True)
        self.broker.write_register([
            {"source_id": "S-1", "title": "Plans", "file": "s1.pdf", "sha256": "ab", "pages": "1",
             "kind": "drawing", "status": "present", "notes": None},
            {"source_id": "SW", "title": "Spec, \"SW\"", "file": "", "sha256": "", "pages": "",
             "kind": "spec", "status": "missing", "notes": "not sent"},
        ])

    def tearDown(self):
        self.broker.close()
        self.tmp.cleanup()

    def test_register_csv(self):
        self.assertEqual(self.broker.ledger.register_csv(), (
            "source_id,title,file,sha256,pages,kind,status,notes\n"
            "S-1,Plans,s1.pdf,ab,1,drawing,present,\n"
            "SW,\"Spec, \"\"SW\"\"\",,,,spec,missing,not sent\n"))

    def test_audit_json(self):
        doc = json.loads(self.broker.ledger.audit_json())
        self.assertEqual(set(doc), {"job", "run_id", "schema_version", "claims", "log"})
        self.assertEqual((doc["job"], doc["run_id"], doc["claims"]), ("NAN", "r1", 0))
        self.assertTrue(doc["schema_version"])
        self.assertEqual(doc["log"], self.broker.ledger.log())
        self.assertTrue(self.broker.ledger.audit_json().endswith("}\n"))
        self.assertIn('\n  "job": "NAN"', self.broker.ledger.audit_json())


class SecretCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        Broker.open_job(Path(self.tmp.name) / "l.db", "intake", job="T", run_id="r", create=True).close()
        self.path = Path(self.tmp.name) / "l.db"

    def tearDown(self):
        self.tmp.cleanup()

    def as_(self, principal):
        return Broker.open_job(self.path, principal, job="T", run_id="r")

    def test_secrets(self):
        env = {"CHRISBIDS_RATE_BOOK": "/books/rates.csv", "CHRISBIDS_API_KEY": "k"}
        with mock.patch.dict(os.environ, env):
            for who in ("pricing", "chris"):
                b = self.as_(who)
                self.assertEqual(b.secret("CHRISBIDS_RATE_BOOK"), "/books/rates.csv")
                b.close()
            b = self.as_("drawing_reader")
            self.assertEqual(b.secret("CHRISBIDS_API_KEY"), "k")
            with self.assertRaisesRegex(LedgerError, "^drawing_reader may not read the rate book$"):
                b.secret("CHRISBIDS_RATE_BOOK")
            denied = [r for r in b.ledger.log() if r["action"] == "denied"]
            self.assertEqual([(r["subject"], r["detail"]) for r in denied],
                             [("read the rate book", "principal drawing_reader")])
            with self.assertRaisesRegex(LedgerError, r"^HOME is not a broker secret; known: \['CHRISBIDS_RATE_BOOK', "
                                                     r"'CHRISBIDS_API_KEY'\]$"):
                b.secret("HOME")
            b.close()
        with mock.patch.dict(os.environ, {}, clear=True):
            b = self.as_("chris")
            self.assertEqual(b.secret("CHRISBIDS_API_KEY"), "")
            b.close()


if __name__ == "__main__":
    unittest.main()
