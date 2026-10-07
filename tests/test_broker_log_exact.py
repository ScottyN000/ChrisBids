"""The broker's audit log, entry by entry, and every refusal's exact wording.

The audit log ships with the bid (architecture p.12), so what it records is
pinned here. Written to kill surviving mutants in Broker.append, _check and
set_audit.
"""
import tempfile
import unittest
from pathlib import Path

from pipeline.broker import Broker
from pipeline.schema import Claim, LedgerError

REGISTER = [
    {"source_id": "S-1", "title": "S-1", "file": "", "sha256": "", "pages": "1", "kind": "drawing",
     "status": "present"},
    {"source_id": "CT", "title": "email", "file": "", "sha256": "", "pages": "", "kind": "correspondence",
     "status": "missing"},
    {"source_id": "PRIOR", "title": "old bid", "file": "", "sha256": "", "pages": "", "kind": "prior-bid",
     "status": "present"},
]


def count(claim_id="Q-001", **kw):
    base = dict(claim_id=claim_id, statement="brackets", source_id="S-1", method="counted", role="quantity",
                confidence="exact", value="6", value_num=6.0, unit="each")
    base.update(kw)
    return Claim(**base)


class BrokerLogCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.intake = Broker.open_job(Path(self.tmp.name) / "l.db", "intake", job="T", run_id="run-7", create=True,
                                      clock=lambda: "2026-10-07T00:00:00Z")
        self.intake.write_register(REGISTER)
        self.reader = self.intake.as_principal("drawing_reader", agent_label="drawing reader", model_id="m",
                                               prompt_version="drawing@abc")

    def tearDown(self):
        self.intake.close()
        self.tmp.cleanup()

    def entries(self, since=0):
        return [(r["principal"], r["action"], r["subject"], r["detail"]) for r in self.intake.ledger.log()][since:]

    def test_an_append_is_stamped_and_logged(self):
        start = len(self.entries())
        c = self.reader.append(count())
        self.assertEqual((c.agent, c.timestamp, c.run_id, c.model_id, c.prompt_version, c.audit, c.audit_note),
                         ("drawing reader", "2026-10-07T00:00:00Z", "run-7", "m", "drawing@abc", "", ""))
        kept = self.reader.append(count("Q-002", agent="someone", timestamp="2026-01-01T00:00:00Z"))
        self.assertEqual((kept.agent, kept.timestamp), ("someone", "2026-01-01T00:00:00Z"))
        self.assertEqual(self.entries(start), [("drawing_reader", "append", "Q-001", "counted/quantity"),
                                               ("drawing_reader", "append", "Q-002", "counted/quantity")])
        row = self.intake.ledger.db.execute("SELECT principal, written_at FROM claims WHERE claim_id='Q-001'").fetchone()
        self.assertEqual(tuple(row), ("drawing_reader", "2026-10-07T00:00:00Z"))

    def test_append_takes_a_dict_too(self):
        self.assertEqual(self.reader.append(count().__dict__ | {"claim_id": "Q-009"}).claim_id, "Q-009")
        self.assertEqual([c.claim_id for c in self.reader.append_many([count("Q-010"), count("Q-011")])],
                         ["Q-010", "Q-011"])

    def test_a_rejected_row_is_logged_with_every_reason(self):
        self.reader.append(count())
        start = len(self.entries())
        with self.assertRaises(LedgerError) as e:
            self.reader.append(count(source_id="CT + NOPE"))
        self.assertEqual(str(e.exception), "Q-001: already in the ledger; a correction is a new ID with supersedes\n"
                                           "Q-001: cites CT (missing) but is not flagged unverified\n"
                                           "Q-001: source 'NOPE' is not in the Source Register")
        self.assertEqual(self.entries(start), [("drawing_reader", "rejected", "Q-001", str(e.exception).replace("\n", "; "))])

    def test_supersedes_must_name_a_row_a_page_or_a_source(self):
        self.reader.append(count())
        for target in ("Q-001", "p.4", "page 2", "PRIOR item 3"):
            self.reader.append(count(f"Q-1{len(target)}", supersedes=target))
        with self.assertRaisesRegex(LedgerError, "^Q-002: supersedes 'Q-404', which is neither a claim nor a source$"):
            self.reader.append(count("Q-002", supersedes="Q-404"))
        with self.assertRaises(LedgerError):
            self.reader.append(count("Q-003", supersedes="P.4"))

    def test_refusals_by_scope(self):
        cases = [
            ("intake", count(), "intake may not write ledger rows"),
            ("drawing_reader", count(method="observed", value="", value_num=None),
             "drawing_reader may not write a observed row (it may write ['clause', 'counted', 'dimensioned', 'scaled'])"),
            ("field_crew", count(method="dimensioned", role="scope", supersedes="Q-001"),
             "field_crew may not write a scope row"),
            ("field_crew", count(method="dimensioned"), "field_crew may not write a row that does not supersede an existing one"),
            ("drawing_reader", count(audit="pass"), "drawing_reader may not set the audit field (only the Auditor does)"),
            ("drawing_reader", count(audit_note="looks fine"), "drawing_reader may not set the audit field (only the Auditor does)"),
        ]
        for principal, claim, message in cases:
            start = len(self.entries())
            with self.assertRaises(LedgerError) as e:
                self.intake.as_principal(principal).append(claim)
            self.assertEqual(str(e.exception), message)
            self.assertEqual(self.entries(start), [(principal, "denied", message.split(" may not ", 1)[1],
                                                    f"principal {principal}")])
        # field_crew within scope is accepted.
        self.reader.append(count())
        self.intake.as_principal("field_crew").append(
            count("Q-001A", method="dimensioned", value="182", value_num=182.0, unit="in", supersedes="Q-001"))

    def test_set_audit(self):
        self.reader.append(count())
        auditor = self.intake.as_principal("auditor")
        start = len(self.entries())
        auditor.set_audit("Q-001", "pass", "found on S-1")
        auditor.set_audit("Q-001", "unverified")
        self.assertEqual(self.entries(start), [("auditor", "audit", "Q-001", "pass: found on S-1"),
                                               ("auditor", "audit", "Q-001", "unverified")])
        c = self.intake.ledger.by_id()["Q-001"]
        self.assertEqual((c.audit, c.audit_note), ("unverified", ""))
        with self.assertRaisesRegex(LedgerError, r"^audit verdict 'ok' not in \['', 'pass', 'fail', 'unverified'\]$"):
            auditor.set_audit("Q-001", "ok")
        with self.assertRaisesRegex(LedgerError, "^no claim Q-404 to audit$"):
            auditor.set_audit("Q-404", "pass")
        with self.assertRaisesRegex(LedgerError, "^drawing_reader may not set the audit field$"):
            self.reader.set_audit("Q-001", "pass")


if __name__ == "__main__":
    unittest.main()
