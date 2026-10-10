"""The access matrix and the append-only rule, exercised rather than described."""
import sqlite3
import tempfile
import unittest
from pathlib import Path

from pipeline.broker import Broker
from pipeline.schema import Claim, LedgerError

REGISTER = [
    {"source_id": "S-1", "title": "Drawing S-1", "file": "s1.pdf", "sha256": "abc", "pages": "1",
     "kind": "drawing", "status": "present"},
    {"source_id": "SW", "title": "SW spec", "file": "sw.pdf", "sha256": "def", "pages": "18",
     "kind": "spec", "status": "present"},
    {"source_id": "CT", "title": "Estimator email, Oct 6", "file": "", "sha256": "", "pages": "",
     "kind": "correspondence", "status": "missing"},
]


def claim(**kw):
    base = dict(claim_id="X-001", statement="a bracket run", source_id="S-1", method="dimensioned",
                role="quantity", confidence="exact", value="182", value_num=182.0, unit="in",
                locator="Partial Foundation Plan")
    base.update(kw)
    return Claim(**base)


class BrokerCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "ledger.db"
        self.intake = Broker.open_job(self.db, "intake", job="T", run_id="r1", create=True,
                                      clock=lambda: "2026-10-07T00:00:00Z")
        self.intake.write_register(REGISTER)

    def tearDown(self):
        self.intake.close()
        self.tmp.cleanup()

    def who(self, principal):
        return self.intake.as_principal(principal)

    # ---- the access matrix -------------------------------------------------

    def test_reader_writes_only_its_own_method(self):
        self.who("drawing_reader").append(claim())
        with self.assertRaises(LedgerError) as e:
            self.who("photo_reader").append(claim(claim_id="X-002", method="dimensioned"))
        self.assertIn("observed", str(e.exception))

    def test_photo_reader_may_not_carry_a_number(self):
        ok = claim(claim_id="P-001", method="observed", confidence="inferred", source_id="SW",
                   value="", value_num=None, unit="", statement="deck coating peeling on the walkway")
        self.who("photo_reader").append(ok)
        with self.assertRaises(LedgerError) as e:
            self.who("photo_reader").append(
                claim(claim_id="P-002", method="observed", confidence="inferred", source_id="SW")
            )
        self.assertIn("never a number", str(e.exception))

    def test_orchestrator_and_scope_writer_write_no_rows(self):
        for principal in ("orchestrator", "scope_writer", "estimator"):
            with self.assertRaises(LedgerError):
                self.who(principal).append(claim(claim_id=f"O-{principal}"))

    def test_only_intake_writes_the_register(self):
        with self.assertRaises(LedgerError):
            self.who("takeoff").write_register(REGISTER)

    def test_register_is_write_once(self):
        with self.assertRaises(LedgerError) as e:
            self.intake.write_register([dict(REGISTER[0], sha256="changed")])
        self.assertIn("write-once", str(e.exception))

    def test_only_the_auditor_sets_the_audit_field(self):
        self.who("drawing_reader").append(claim())
        with self.assertRaises(LedgerError):
            self.who("takeoff").set_audit("X-001", "pass")
        self.who("auditor").set_audit("X-001", "pass", "found on the plan")
        self.assertEqual(self.intake.ledger.by_id()["X-001"].audit, "pass")

    def test_an_agent_may_not_preset_its_own_audit_field(self):
        with self.assertRaises(LedgerError):
            self.who("drawing_reader").append(claim(audit="pass"))

    def test_field_crew_may_only_supersede(self):
        with self.assertRaises(LedgerError):
            self.who("field_crew").append(claim(claim_id="F-001"))

    def test_egress_is_codes_materials_and_the_auditor(self):
        self.assertTrue(self.who("codes").may_fetch("https://example.gov"))
        self.assertTrue(self.who("materials").may_fetch("https://example.com"))
        self.assertFalse(self.who("spec_reader").may_fetch("https://example.com"))

    def test_rate_book_is_not_readable_by_the_scope_writer(self):
        with self.assertRaises(LedgerError):
            self.who("scope_writer").secret("CHRISBIDS_RATE_BOOK")

    # ---- append-only -------------------------------------------------------

    def test_history_cannot_be_edited_or_deleted_even_outside_the_broker(self):
        self.who("drawing_reader").append(claim())
        db = sqlite3.connect(self.db)
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute("UPDATE claims SET value = '999' WHERE claim_id = 'X-001'")
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute("DELETE FROM claims WHERE claim_id = 'X-001'")
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute("DELETE FROM register WHERE source_id = 'S-1'")
        db.close()

    def test_a_correction_is_a_new_row(self):
        self.who("drawing_reader").append(claim())
        with self.assertRaises(LedgerError):
            self.who("drawing_reader").append(claim(value="183", value_num=183.0))
        self.who("drawing_reader").append(claim(claim_id="X-001a", value="183", value_num=183.0,
                                                supersedes="X-001", reason="re-read the dimension string"))
        self.assertEqual(self.intake.ledger.superseded(), {"X-001"})

    def test_every_write_is_logged_with_its_principal(self):
        self.who("drawing_reader").append(claim())
        actions = {(r["principal"], r["action"]) for r in self.intake.ledger.log()}
        self.assertIn(("drawing_reader", "append"), actions)
        self.assertIn(("intake", "register"), actions)

    def test_versions_are_stamped_by_the_broker(self):
        row = self.who("drawing_reader").append(claim())
        self.assertEqual(row.run_id, "r1")
        self.assertTrue(row.prompt_version)
        self.assertTrue(row.tool_versions)

    # ---- method rules ------------------------------------------------------

    def test_a_missing_source_must_be_flagged_unverified(self):
        with self.assertRaises(LedgerError) as e:
            self.who("customer_requirements").append(claim(
                claim_id="C-001", method="customer", source_id="CT", confidence="inferred",
                value="", value_num=None, unit="", quote="keep the base bid simple",
                statement="base bid stays six items"))
        self.assertIn("not flagged unverified", str(e.exception))
        self.who("customer_requirements").append(claim(
            claim_id="C-001", method="customer", source_id="CT", confidence="inferred",
            value="", value_num=None, unit="", flag="unverified",
            quote="keep the base bid simple", statement="base bid stays six items"))

    def test_a_scaled_value_may_not_feed_an_order_quantity(self):
        with self.assertRaises(LedgerError) as e:
            self.who("takeoff").append(claim(claim_id="M-001", method="scaled", confidence="scaled",
                                             role="material", value="40", value_num=40.0, unit="LF"))
        self.assertIn("order quantity", str(e.exception))

    def test_a_fetched_row_needs_a_url_and_a_date(self):
        with self.assertRaises(LedgerError):
            self.who("codes").append(claim(claim_id="K-001", method="fetched", source_id="SW",
                                           value="", value_num=None, unit="", quote="text"))
        self.who("codes").append(claim(claim_id="K-001", method="fetched", source_id="SW",
                                       value="", value_num=None, unit="", quote="text",
                                       url="https://example.gov/x", retrieved="2026-10-07"))

    def test_arithmetic_is_replayed_on_write(self):
        self.who("drawing_reader").append(claim(claim_id="D-001", value="6", value_num=6.0, unit="each"))
        with self.assertRaises(LedgerError) as e:
            self.who("takeoff").append(claim(claim_id="Q-001", method="counted", value="19",
                                             value_num=19.0, unit="each", calc="{D-001} * 3"))
        self.assertIn("ledger says", str(e.exception))
        self.who("takeoff").append(claim(claim_id="Q-002", method="counted", value="18",
                                         value_num=18.0, unit="each", calc="{D-001} * 3"))

    def test_a_calc_on_a_flagged_input_is_flagged_itself(self):
        self.who("drawing_reader").append(claim(claim_id="D-001", value="6", value_num=6.0, unit="each",
                                                confidence="inferred", flag="unverified"))
        with self.assertRaises(LedgerError) as e:
            self.who("takeoff").append(claim(claim_id="Q-001", method="counted", value="18",
                                             value_num=18.0, unit="each", calc="{D-001} * 3"))
        self.assertIn("calc input D-001 is flagged unverified; the row must be flagged", str(e.exception))
        # nor firmer than its input: an inferred count makes an inferred total
        self.assertIn("calc input D-001 is inferred; the row claims exact", str(e.exception))
        self.who("takeoff").append(claim(claim_id="Q-001", method="counted", value="18", value_num=18.0,
                                         unit="each", calc="{D-001} * 3", flag="unverified", confidence="inferred"))
        # a conflict flag is a flag too: the row is already not firm
        self.who("takeoff").append(claim(claim_id="Q-002", method="counted", value="18", value_num=18.0, unit="each",
                                         calc="{D-001} * 3", flag="conflict", question="OQ-1", confidence="inferred"))

    def test_calc_is_arithmetic_only(self):
        with self.assertRaises(LedgerError):
            self.who("takeoff").append(claim(claim_id="Q-003", method="counted", value="1",
                                             value_num=1.0, calc="__import__('os').getpid()"))


if __name__ == "__main__":
    unittest.main()
