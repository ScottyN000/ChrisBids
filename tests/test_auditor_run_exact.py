"""One ledger with a row for every branch of auditor.run, pinned verdict by verdict.

Written to kill the surviving mutants in auditor.run: each branch was tested on
its own, so nothing noticed a wrong note, a verdict written to the wrong row,
or a branch reached in the wrong order.
"""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pipeline import auditor
from pipeline.broker import Broker
from pipeline.schema import Claim
from tests.pdfgen import write_pdf

REGISTER = [
    {"source_id": "S-1", "title": "S-1", "file": "s1.pdf", "sha256": "", "pages": "1", "kind": "drawing",
     "status": "present"},
    {"source_id": "SW", "title": "Spec", "file": "sw.pdf", "sha256": "", "pages": "2", "kind": "spec",
     "status": "present"},
    {"source_id": "CT", "title": "Chris email", "file": "", "sha256": "", "pages": "", "kind": "correspondence",
     "status": "missing"},
    {"source_id": "WEB", "title": "Code", "file": "", "sha256": "", "pages": "", "kind": "code",
     "status": "present"},
]

EXPECTED = {
    "D-001": ("unverified", "superseded by a correction row"),
    "D-001A": ("unverified", "conversion replayed (15'-2\" dimension string = 182 in); the dimension string "
                             "still needs the yes/no check against the page image"),
    "D-002": ("fail", "15'-2\" is 182 in, ledger says 184"),
    "C-001": ("pass", "arithmetic replayed: {D-001A} * 2"),
    "N-001": ("unverified", "schema and sources check out, but no cited page could be opened here; "
                            "needs the yes/no check against the page image"),
    "F-001": ("pass", "FIELD placeholder; says what to measure, carries no figure"),
    "U-001": ("unverified", "flagged unverified by the agent that wrote it"),
    "K-001": ("unverified", "two readings kept; a human resolves it"),
    "M-001": ("unverified", "cites CT, not in the packet"),
    "W-001": ("unverified", "URL stored with its retrieval date but not re-fetched this run: https://codes.example/ibc"),
    "P-001": ("pass", "found on SW p.2"),
    "P-002": ("fail", "not found on SW p.2: 'Apply 3 coats'"),
}


class RunCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.intake = Broker.open_job(self.dir / "l.db", "intake", job="T", run_id="r", create=True)
        self.intake.write_register(REGISTER)
        write_pdf(self.dir / "sw.pdf", ["cover", "Apply 2 coats"])
        dim = dict(statement="run", source_id="S-1", method="dimensioned", role="quantity", confidence="exact",
                   unit="in", locator="Fnd")
        self.add("drawing_reader", claim_id="D-001", value="180", value_num=180.0,
                 derivation="15' dimension string = 180 in", **dim)
        self.add("drawing_reader", claim_id="D-001A", value="182", value_num=182.0, supersedes="D-001",
                 derivation="15'-2\" dimension string = 182 in", **dim)
        self.add("drawing_reader", claim_id="D-002", value="184", value_num=184.0,
                 derivation="15'-2\" dimension string = 182 in", **dim)
        self.add("takeoff", claim_id="C-001", statement="two runs", source_id="S-1", method="counted",
                 role="quantity", confidence="exact", value="364", value_num=364.0, unit="in",
                 calc="{D-001A} * 2")
        self.add("drawing_reader", claim_id="N-001", statement="brackets", source_id="S-1", method="counted",
                 role="quantity", confidence="exact", value="6", value_num=6.0, unit="each", locator="Fnd")
        self.add("takeoff", claim_id="F-001", statement="measure the rail", source_id="S-1", method="FIELD",
                 role="quantity", confidence="missing")
        self.add("drawing_reader", claim_id="U-001", statement="anchors", source_id="S-1", method="counted",
                 role="quantity", confidence="exact", value="3", value_num=3.0, unit="each", flag="unverified")
        self.add("drawing_reader", claim_id="K-001", statement="anchors", source_id="S-1", method="counted",
                 role="quantity", confidence="exact", value="3 (reading A) / 4 (reading B)", unit="each",
                 flag="conflict")
        self.add("correspondence_reader", claim_id="M-001", statement="keep it simple", source_id="CT",
                 method="customer", role="note", confidence="exact", quote="keep it simple",
                 flag="unverified")
        self.add("codes", claim_id="W-001", statement="special inspection", source_id="WEB", method="fetched",
                 role="code", confidence="exact", quote="special inspection", url="https://codes.example/ibc",
                 retrieved="2026-10-07")
        self.add("spec_reader", claim_id="P-001", statement="coats", source_id="SW", method="clause", role="scope",
                 confidence="exact", locator="p.2", quote="Apply 2 coats")
        self.add("spec_reader", claim_id="P-002", statement="coats", source_id="SW", method="clause", role="scope",
                 confidence="exact", locator="p.2", quote="Apply 3 coats")

    def tearDown(self):
        self.intake.close()
        self.tmp.cleanup()

    def add(self, principal, **kw):
        self.intake.as_principal(principal).append(Claim(**kw))

    def audit(self, **kw):
        return auditor.run(self.intake.as_principal("auditor"), packet=self.dir, **kw)

    def written(self):
        return {c.claim_id: (c.audit, c.audit_note) for c in self.intake.ledger.claims()}

    def test_every_branch_writes_its_own_verdict(self):
        with mock.patch("pipeline.intake.verify", return_value=[]):
            report = self.audit()
        self.assertEqual(self.written(), EXPECTED)
        self.assertEqual(report.verdicts, {"pass": 3, "fail": 2, "unverified": 7})
        self.assertEqual(report.rows, 12)
        self.assertEqual(report.job, "T")
        self.assertEqual(report.failures, [f"{k}: {v[1]}" for k, v in EXPECTED.items() if v[0] == "fail"])
        self.assertEqual(report.unverified, [f"{k}: {v[1]}" for k, v in EXPECTED.items() if v[0] == "unverified"])
        self.assertFalse(report.ok)

    def test_a_dry_run_reports_but_writes_nothing(self):
        with mock.patch("pipeline.intake.verify", return_value=["S-1 changed"]):
            report = self.audit(write=False)
        self.assertEqual(set(self.written().values()), {("", "")})
        self.assertEqual(report.verdicts, {"pass": 3, "fail": 2, "unverified": 7})
        self.assertEqual(report.hash_problems, ["S-1 changed"])

    def test_link_checks_only_when_allowed_and_a_dead_link_fails(self):
        def audit_with(live, **kw):
            with mock.patch("pipeline.intake.verify", return_value=[]), \
                 mock.patch("pipeline.auditor.link_live", return_value=live) as m:
                self.audit(**kw)
            return m

        self.assertFalse(audit_with((False, "HTTP 404")).called)
        audit_with((False, "HTTP 404"), check_links=True).assert_called_once_with("https://codes.example/ibc")
        self.assertEqual(self.written()["W-001"],
                         ("fail", "dead link at audit time (HTTP 404): https://codes.example/ibc"))
        audit_with((True, "HTTP 200"), check_links=True)
        self.assertEqual(self.written()["W-001"], EXPECTED["W-001"])

    def test_without_a_packet_no_page_is_opened(self):
        report = auditor.run(self.intake.as_principal("auditor"))
        self.assertEqual(self.written()["P-001"], ("unverified", EXPECTED["N-001"][1]))
        self.assertEqual(report.hash_problems, [])

    def test_the_proposal_and_phrase_library(self):
        prop = self.dir / "proposal.md"
        prop.write_text("Install 6 brackets, 9 anchors and hold 10% retainage.\n")
        lib = self.dir / "library.md"
        lib.write_text("Retainage of 10% is held.")
        with mock.patch("pipeline.intake.verify", return_value=[]):
            report = self.audit(proposal=prop, phrase_library=lib, write=False)
        self.assertEqual([(o.figure, o.line) for o in report.orphans], [("9", 1)])
        with mock.patch("pipeline.intake.verify", return_value=[]):
            report = self.audit(proposal=prop, write=False)
        self.assertEqual([o.figure for o in report.orphans], ["9", "10%"])


if __name__ == "__main__":
    unittest.main()
