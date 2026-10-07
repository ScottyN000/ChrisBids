"""Golden tests: the two hand-made bids are the answer key (architecture p.10).

Every fixture row is routed to the principal the access matrix says would have
written it, so a prompt or schema change that breaks the matrix fails here
rather than on a live bid. The ledger exported back out must byte-match the
committed `ledger.csv`, which is also what `tools/build_fixture.py --check`
renders the proposal from.
"""
import tempfile
import unittest
from pathlib import Path

from pipeline import auditor, fixtures
from pipeline.ledger import Ledger

ROOT = Path(__file__).resolve().parent.parent
JOBS = ("nantucket", "ocean-beach")
PACKET = Path("/mnt/project-files")


class GoldenCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def load(self, job):
        broker, data = fixtures.load(ROOT / "fixtures" / job, Path(self.tmp.name) / job / "ledger.db")
        self.addCleanup(broker.close)
        return broker, data

    def test_both_fixtures_load_through_the_broker(self):
        for job in JOBS:
            with self.subTest(job=job):
                broker, data = self.load(job)
                self.assertEqual(len(broker.ledger.claims()), len(data["rows"]))

    def test_the_export_reproduces_the_committed_ledger_byte_for_byte(self):
        for job in JOBS:
            with self.subTest(job=job):
                broker, _ = self.load(job)
                want = (ROOT / "fixtures" / job / "ledger.csv").read_text()
                self.assertEqual(broker.ledger.ledger_csv(), want)

    def test_the_audit_field_is_blank_until_the_auditor_writes_it(self):
        broker, _ = self.load("nantucket")
        self.assertEqual({c.audit for c in broker.ledger.claims()}, {""})

    def test_every_row_was_written_by_a_principal_the_matrix_allows(self):
        broker, _ = self.load("ocean-beach")
        rows = broker.ledger.db.execute("SELECT method, principal FROM claims").fetchall()
        from pipeline import roles
        for method, principal in rows:
            self.assertIn(method, roles.get(principal).methods, f"{principal} wrote {method}")

    def test_the_rendered_proposals_have_no_orphan_figures(self):
        for job in JOBS:
            with self.subTest(job=job):
                broker, _ = self.load(job)
                report = auditor.run(
                    broker.as_principal("auditor"),
                    proposal=ROOT / "fixtures" / job / "proposal.md",
                    phrase_library=fixtures.PHRASE_LIBRARY,
                    write=False,
                )
                self.assertEqual([str(o) for o in report.orphans], [])
                self.assertEqual(report.failures, [])

    def test_a_stray_yaml_key_is_a_build_failure(self):
        """An unquoted locator once read as a mapping and dropped two page cites."""
        job_dir = Path(self.tmp.name) / "broken"
        job_dir.mkdir()
        src = ROOT / "fixtures" / "nantucket"
        (job_dir / "register.csv").write_text((src / "register.csv").read_text())
        text = (src / "ledger.yaml").read_text().replace(
            "locator: title block, tag: S-1 TB", "locator: p.1, p.2, tag: S-1 TB", 1
        )
        (job_dir / "ledger.yaml").write_text(text)
        with self.assertRaises(fixtures.FixtureError):
            fixtures.read_fixture(job_dir)

    @unittest.skipUnless(PACKET.exists(), "the project's source packet is not mounted")
    def test_the_hand_made_bids_are_full_of_orphans(self):
        """The Phase 1 acceptance test (architecture p.16): feed the Auditor the
        two hand-written bids and have it find every figure with no row behind it."""
        for job, pdf in (
            ("nantucket", "nantucket-condo-plank-repair_scope-and-materials_2026-10-07.pdf"),
            ("ocean-beach", "ocean-beach-villas-repaint_scope-and-materials_2026-10-07.pdf"),
        ):
            with self.subTest(job=job):
                broker, _ = self.load(job)
                report = auditor.run(
                    broker.as_principal("auditor"), proposal=PACKET / "source" / pdf,
                    phrase_library=fixtures.PHRASE_LIBRARY, write=False,
                )
                self.assertGreater(len(report.orphans), 20)

    @unittest.skipUnless(PACKET.exists(), "the project's source packet is not mounted")
    def test_cited_pages_open_and_confirm_their_quotes(self):
        broker, _ = self.load("ocean-beach")
        report = auditor.run(broker.as_principal("auditor"), packet=PACKET, write=True)
        notes = [c.audit_note for c in broker.ledger.claims()]
        confirmed = [n for n in notes if n.startswith("found on")]
        opened = [n for n in notes if " opened (" in n]
        # Quoted rows are confirmed outright; paraphrased clause rows get their
        # page opened and wait for the Phase 2 yes/no pass. Neither is a failure.
        self.assertGreater(len(confirmed), 2)
        self.assertGreater(len(opened), 20)
        self.assertEqual(report.failures, [])
        self.assertEqual(report.hash_problems, [])


class StoreCase(unittest.TestCase):
    def test_the_ledger_file_is_openable_read_only_after_the_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            broker, _ = fixtures.load(ROOT / "fixtures" / "nantucket", Path(tmp) / "ledger.db")
            broker.close()
            with Ledger(Path(tmp) / "ledger.db") as led:
                self.assertEqual(led.meta("job"), "NAN")
                self.assertEqual(len(led.claims()), 108)
                self.assertTrue(led.register_csv().startswith("source_id,"))


if __name__ == "__main__":
    unittest.main()
