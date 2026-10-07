"""Intake against real PDFs built in the test, and with poppler missing.

Written to kill the mutants that survived in pipeline/intake.py: the poppler
helpers were only ever run against fake bytes, so nothing checked what they
return from a real file.
"""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pipeline import intake
from tests.pdfgen import write_pdf

LONG = "\n".join(f"General note {i}: all steel shall be hot-dip galvanized after fabrication." for i in range(6))


class PopplerCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def pdf(self, name, pages):
        return write_pdf(self.dir / name, pages)

    def test_page_count_comes_from_pdfinfo(self):
        self.assertEqual(intake.pdf_pages(self.pdf("a.pdf", ["one", "two", "three"])), "3")

    def test_text_layer_is_measured_in_non_space_characters(self):
        self.assertEqual(intake.text_layer_chars(self.pdf("a.pdf", ["06 OCT 26"])), 7)
        # Only the first two pages are read.
        self.assertEqual(intake.text_layer_chars(self.pdf("b.pdf", ["ab", "cd", "efgh"])), 4)
        self.assertEqual(intake.text_layer_chars(self.pdf("c.pdf", ["ab", "cd", "efgh"]), pages=3), 8)

    def test_a_few_stray_characters_are_not_a_text_layer(self):
        self.assertIs(intake.has_text_layer(self.pdf("seal.pdf", ["06 OCT 26"])), False)
        self.assertIs(intake.has_text_layer(self.pdf("notes.pdf", [LONG])), True)

    def test_the_threshold_is_inclusive(self):
        n = intake.TEXT_LAYER_MIN_CHARS

        def lines(k):  # k characters, 40 to a line so none runs off the page
            s = "x" * k
            return "\n".join(s[i:i + 40] for i in range(0, k, 40))

        self.assertEqual(intake.text_layer_chars(self.pdf("count.pdf", [lines(n)])), n)
        self.assertIs(intake.has_text_layer(self.pdf("edge.pdf", [lines(n)])), True)
        self.assertIs(intake.has_text_layer(self.pdf("under.pdf", [lines(n - 1)])), False)

    def test_an_unnamed_single_sheet_with_no_text_is_a_drawing(self):
        self.assertEqual(intake.classify(self.pdf("scan0001.pdf", [""]), "1"), "drawing")
        self.assertEqual(intake.classify(self.pdf("scan0002.pdf", [LONG]), "1"), "unknown")
        self.assertEqual(intake.classify(self.pdf("scan0003.pdf", ["", ""]), "2"), "unknown")

    def test_pages_render_to_images(self):
        pdf = self.pdf("two.pdf", ["first", "second"])
        names = intake.render_pages(pdf, self.dir / "img", dpi=20)
        self.assertEqual(names, ["p-1.png", "p-2.png"])
        self.assertTrue(all((self.dir / "img" / n).stat().st_size > 0 for n in names))
        self.assertEqual(intake.render_pages(pdf, self.dir / "one", dpi=20, max_pages=1), ["p-1.png"])

    def test_scan_records_pages_density_and_images(self):
        packet = self.dir / "packet"
        packet.mkdir()
        write_pdf(packet / "nantucket-condo_S-1-plans.pdf", ["06 OCT 26"])
        write_pdf(packet / "ocean-beach_paint-spec.pdf", [LONG, LONG])
        by_id = {s.source_id: s for s in intake.scan(packet, images=True, image_root=self.dir / "pages")}
        s1 = by_id["S-1"]
        self.assertEqual((s1.kind, s1.pages), ("drawing", "1"))
        self.assertEqual(s1.notes, "text layer holds 7 characters in the first pages; read from page rasters; "
                                   "1 page images rendered")
        self.assertEqual(s1.page_images, ["p-1.png"])
        spec = by_id["OBP"]
        self.assertEqual((spec.kind, spec.pages, spec.notes), ("spec", "2", "2 page images rendered"))

    def test_scan_without_images_renders_nothing(self):
        packet = self.dir / "packet"
        packet.mkdir()
        write_pdf(packet / "spec.pdf", [LONG])
        (src,) = intake.scan(packet, images=True)  # no image_root: nothing to render into
        self.assertEqual((src.page_images, src.notes), ([], ""))

    def test_a_broken_pdf_is_reported_as_unreadable_not_guessed(self):
        bad = self.dir / "broken.pdf"
        bad.write_bytes(b"%PDF-1.4 not really")
        self.assertEqual(intake.pdf_pages(bad), "")
        self.assertIsNone(intake.text_layer_chars(bad))
        self.assertIsNone(intake.has_text_layer(bad))
        self.assertEqual(intake.render_pages(bad, self.dir / "x"), [])

    def test_without_poppler_every_helper_says_so(self):
        pdf = self.pdf("a.pdf", [LONG])
        with mock.patch("pipeline.intake.shutil.which", return_value=None):
            self.assertEqual(intake.pdf_pages(pdf), "")
            self.assertIsNone(intake.text_layer_chars(pdf))
            self.assertIsNone(intake.has_text_layer(pdf))
            self.assertEqual(intake.render_pages(pdf, self.dir / "img"), [])
            self.assertEqual(intake.classify(self.dir / "scan.pdf", "1"), "unknown")


class NamingCase(unittest.TestCase):
    def test_ids_and_kinds(self):
        taken = set()
        self.assertEqual(intake.source_id_for(Path("0_IMG_8324.jpg"), "photo", taken), "IMG_8324")
        self.assertEqual(intake.source_id_for(Path("img-8343.JPG"), "photo", taken), "IMG_8343")
        self.assertEqual(intake.source_id_for(Path("IMG_8324.jpg"), "photo", taken), "IMG_8324-2")
        self.assertEqual(intake.source_id_for(Path("photo.jpg"), "photo", taken), "P")
        self.assertEqual(intake.source_id_for(Path("2024-plan_A-101.pdf"), "drawing", taken), "A-101")
        self.assertEqual(intake.source_id_for(Path("copy of S-2 rev.pdf"), "drawing", taken), "S-2")
        self.assertEqual(intake.source_id_for(Path("2026-10-06.pdf"), "unknown", taken), "SRC")

    def test_unique_gives_up_rather_than_overwriting(self):
        taken = {"X"} | {f"X-{n}" for n in range(2, 100)}
        with self.assertRaises(RuntimeError):
            intake._unique("X", taken)

    def test_classify_by_extension_and_name(self):
        cases = {
            "a.heic": "photo", "b.xlsx": "spreadsheet", "c.tsv": "spreadsheet",
            "sheet-plans.pdf": "drawing", "addendum-2.pdf": "spec",
            "proposal-format-example.pdf": "proposal-template", "past-bid.pdf": "prior-bid",
            "pm-notes.pdf": "correspondence", "architecture.pdf": "design",
            "message.eml": "correspondence", "readme.docx": "unknown",
        }
        for name, kind in cases.items():
            self.assertEqual(intake.classify(Path(name)), kind, name)

    def test_register_row_carries_exactly_the_register_fields(self):
        s = intake.Source(source_id="S-1", title="t", file="f", sha256="h", kind="drawing",
                          pages="1", notes="n", duplicate_of="", page_images=["p-1.png"])
        self.assertEqual(s.as_register_row(), {
            "source_id": "S-1", "title": "t", "file": "f", "sha256": "h", "pages": "1",
            "kind": "drawing", "status": "present", "notes": "n", "duplicate_of": ""})

    def test_sha256_reads_in_chunks(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "f"
            p.write_bytes(b"abc" * 1000)
            import hashlib
            want = hashlib.sha256(b"abc" * 1000).hexdigest()
            self.assertEqual(intake.sha256_file(p, chunk=7), want)
            self.assertEqual(intake.sha256_file(p), want)


if __name__ == "__main__":
    unittest.main()
