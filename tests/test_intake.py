"""Intake: hashes, duplicate collapsing, IDs and classification."""
import tempfile
import unittest
from pathlib import Path

from pipeline import intake
from pipeline.broker import Broker


class IntakeCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.packet = self.root / "packet"
        (self.packet / "photos").mkdir(parents=True)
        # The Ocean Beach case: every photo arrived twice, as IMG_ and 0_IMG_.
        (self.packet / "photos" / "IMG_8316.jpg").write_bytes(b"\xff\xd8photo-one")
        (self.packet / "photos" / "0_IMG_8316.jpg").write_bytes(b"\xff\xd8photo-one")
        (self.packet / "photos" / "IMG_8324.jpg").write_bytes(b"\xff\xd8photo-two")
        (self.packet / "nantucket-condo_S-1-plans-and-detail.pdf").write_bytes(b"%PDF-1.7 fake")
        (self.packet / "estimator-email-2026-10-06.txt").write_text("keep the base bid simple")
        (self.packet / "takeoff.csv").write_text("a,b\n1,2\n")
        (self.packet / "SHA256SUMS").write_text("ignored\n")

    def tearDown(self):
        self.tmp.cleanup()

    def test_duplicates_are_collapsed_before_anyone_reads_them(self):
        sources = intake.scan(self.packet)
        dups = [s for s in sources if s.status == "duplicate"]
        self.assertEqual(len(dups), 1)
        self.assertEqual(dups[0].duplicate_of, "IMG_8316")
        kept = [s for s in sources if s.status == "present" and s.kind == "photo"]
        self.assertEqual({s.source_id for s in kept}, {"IMG_8316", "IMG_8324"})

    def test_photo_ids_come_from_the_filename(self):
        ids = {s.source_id for s in intake.scan(self.packet)}
        self.assertIn("IMG_8324", ids)

    def test_kinds_are_classified_from_the_filename_and_extension(self):
        kinds = {s.source_id: s.kind for s in intake.scan(self.packet)}
        self.assertEqual(kinds["S-1"], "drawing")
        self.assertEqual(kinds["IMG_8316"], "photo")
        self.assertEqual(kinds["EE"], "correspondence")
        self.assertEqual(kinds["T"], "spreadsheet")

    def test_hashes_are_written_and_a_change_fails_the_run(self):
        db = self.root / "ledger.db"
        broker = Broker.open_job(db, "intake", job="T", run_id="r", create=True)
        intake.run(self.packet, broker)
        self.assertEqual(intake.verify(broker, self.packet), [])
        (self.packet / "photos" / "IMG_8324.jpg").write_bytes(b"\xff\xd8tampered")
        problems = intake.verify(broker, self.packet)
        self.assertEqual(len(problems), 1)
        self.assertIn("hash changed", problems[0])
        broker.close()

    def test_a_missing_file_is_reported_not_skipped(self):
        db = self.root / "ledger.db"
        broker = Broker.open_job(db, "intake", job="T", run_id="r", create=True)
        intake.run(self.packet, broker)
        (self.packet / "photos" / "IMG_8324.jpg").unlink()
        self.assertTrue(any("not in the packet" in p for p in intake.verify(broker, self.packet)))
        broker.close()


if __name__ == "__main__":
    unittest.main()
