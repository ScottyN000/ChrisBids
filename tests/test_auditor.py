"""The Auditor: figure extraction, orphans, and verdicts per row."""
import tempfile
import unittest
from pathlib import Path

from pipeline import auditor
from pipeline.broker import Broker
from pipeline.schema import Claim

REGISTER = [
    {"source_id": "S-1", "title": "Drawing S-1", "file": "s1.pdf", "sha256": "", "pages": "1",
     "kind": "drawing", "status": "present"},
    {"source_id": "CT", "title": "Estimator email, Oct 6", "file": "", "sha256": "", "pages": "",
     "kind": "correspondence", "status": "missing"},
]


class FiguresCase(unittest.TestCase):
    def test_a_figure_is_a_number_that_could_price_work(self):
        text = "6 bracket supports at 2'-8\" on centre, 18 anchors, 10% retainage"
        self.assertEqual([f.text for f in auditor.extract_figures(text)],
                         ["6", "2'-8\"", "18", "10%"])

    def test_page_cites_claim_ids_codes_and_dates_are_not_figures(self):
        for text in ("see p.17 for the system", "NAN-Q-003 carries the count",
                     "IBC 2021 Chapter 17 special inspection", "rev 10/06/26 For Construction",
                     "Berlin MD 21811", "555-555-0102", "SuperPaint A89 one coat",
                     "F.S. 489.105 definitions", "### 3.1 Plank Underside Rebuild",
                     "1. Remove the loose coating"):
            self.assertEqual(auditor.extract_figures(text), [], text)

    def test_an_orphan_is_a_figure_with_no_row_behind_it(self):
        claims = [Claim(claim_id="Q-002", statement="6 bracket supports", source_id="S-1",
                        method="counted", role="quantity", confidence="exact", value="6", value_num=6.0,
                        unit="each")]
        orphans = auditor.audit_proposal("Install 6 bracket supports and 18 anchors.", claims)
        self.assertEqual([o.figure for o in orphans], ["18"])

    def test_a_figure_inside_a_quoted_source_is_not_an_orphan(self):
        claims = [Claim(claim_id="C-001", statement="minimum compressive strength", source_id="S-1",
                        method="clause", role="code", confidence="exact",
                        quote="concrete shall attain 3,000 psi at 28 days")]
        self.assertEqual(auditor.audit_proposal("Repair mortar at 3000 psi.", claims), [])

    def test_a_library_paragraph_traces_to_the_format_example(self):
        self.assertEqual(
            auditor.audit_proposal("Retainage of 10% is held until final acceptance.", [],
                                   extra_text="text: 'Retainage of 10% is held until final acceptance.'"),
            [],
        )

    def test_commas_and_decimals_are_the_same_figure(self):
        claims = [Claim(claim_id="C-1", statement="s", source_id="S-1", method="clause", role="code",
                        confidence="exact", value="2500", value_num=2500.0)]
        self.assertEqual(auditor.audit_proposal("Carry 2,500 psi.", claims), [])


class VerdictCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "ledger.db"
        self.intake = Broker.open_job(self.db, "intake", job="T", run_id="r", create=True)
        self.intake.write_register(REGISTER)

    def tearDown(self):
        self.intake.close()
        self.tmp.cleanup()

    def add(self, **kw):
        base = dict(claim_id="X-001", statement="a run", source_id="S-1", method="counted",
                    role="quantity", confidence="exact", value="6", value_num=6.0, unit="each")
        base.update(kw)
        principal = base.pop("principal", "drawing_reader")
        return self.intake.as_principal(principal).append(Claim(**base))

    def audit(self, **kw):
        return auditor.run(self.intake.as_principal("auditor"), **kw)

    def test_a_flagged_row_is_unverified_not_passed(self):
        self.add(claim_id="U-001", source_id="CT", flag="unverified", method="customer",
                 quote="keep the base bid simple", value="", value_num=None, unit="",
                 confidence="inferred", principal="customer_requirements")
        report = self.audit()
        self.assertEqual(report.verdicts.get("unverified"), 1)
        self.assertEqual(self.intake.ledger.by_id()["U-001"].audit, "unverified")

    def test_a_replayed_derivation_passes_and_a_bare_count_does_not(self):
        self.add(claim_id="D-001")
        self.add(claim_id="Q-001", principal="takeoff", value="18", value_num=18.0,
                 calc="{D-001} * 3", statement="3 per bracket x 6")
        report = self.audit()
        verdicts = {c.claim_id: c.audit for c in self.intake.ledger.claims()}
        self.assertEqual(verdicts["Q-001"], "pass")
        self.assertEqual(verdicts["D-001"], "unverified")
        self.assertFalse(report.failures)

    def test_a_superseded_row_stops_counting(self):
        self.add(claim_id="D-001")
        self.add(claim_id="D-001a", value="7", value_num=7.0, supersedes="D-001",
                 reason="re-counted the symbols")
        self.audit()
        self.assertEqual(self.intake.ledger.by_id()["D-001"].audit, "unverified")

    def test_a_field_row_passes_because_it_carries_no_figure(self):
        self.add(claim_id="F-001", principal="takeoff", method="FIELD", confidence="missing",
                 value="", value_num=None, unit="", source_id="S-1",
                 statement="measure the plank underside repair depth")
        self.audit()
        self.assertEqual(self.intake.ledger.by_id()["F-001"].audit, "pass")

    def test_a_dangling_open_question_fails(self):
        self.add(claim_id="D-001", question="OQ-99")
        report = self.audit()
        self.assertTrue(any("OQ-99" in f for f in report.failures))


if __name__ == "__main__":
    unittest.main()
