"""The Auditor's helpers, checked against exact outputs and real PDFs.

Written to kill the mutants that survived in pipeline/auditor.py: opening a
cited page, the link check, the report text, the figure forms and the
feet-and-inch replay were each tested for the happy path only.
"""
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from pipeline import auditor
from pipeline.auditor import AuditReport, Figure, Orphan
from pipeline.schema import Claim
from tests.pdfgen import write_pdf


def claim(**kw):
    base = dict(claim_id="J-SP-001", statement="s", source_id="SW", method="clause", role="scope",
                confidence="exact", locator="p.2")
    base.update(kw)
    return Claim(**base)


class FiguresCase(unittest.TestCase):
    def test_numeric_forms(self):
        self.assertEqual(auditor._numeric_forms("2,500"), {"2,500", "2500"})
        self.assertEqual(auditor._numeric_forms("$1,200.50"), {"$1,200.50", "$1200.50", "1,200.50", "1200.50", "1200.5"})
        self.assertEqual(auditor._numeric_forms("10 %"), {"10 %", "10%", "10"})
        self.assertEqual(auditor._numeric_forms("15'-2\""), {"15'-2\"", "15'-2"})
        self.assertEqual(auditor._numeric_forms("182.0"), {"182.0", "182"})
        self.assertEqual(auditor._numeric_forms(""), set())

    def test_extract_figures_with_line_and_context(self):
        text = "Scope\nInstall 6 brackets at 2'-8\" o.c. per page 3\n1. Remove 25% of planks"
        self.assertEqual(auditor.extract_figures(text), [
            Figure("6", 2, "…Install [6] brackets at 2'-8\" o.c. per page 3…"),
            Figure("2'-8\"", 2, "…Install 6 brackets at [2'-8\"] o.c. per page 3…"),
            Figure("25%", 3, "…1. Remove [25%] of planks…"),
        ])

    def test_context_is_cut_to_48_characters_each_side(self):
        line = "a" * 60 + " 7 " + "b" * 60
        (f,) = auditor.extract_figures(line)
        self.assertEqual(f.context, "…" + "a" * 47 + " [7] " + "b" * 47 + "…")

    def test_ignore_rules(self):
        self.assertEqual(auditor.ignored_reason("see sheet 4", 10, 11), "a page, sheet or item reference")
        self.assertEqual(auditor.ignored_reason("1. Remove 25% of planks", 0, 1), "a numbered list marker")
        self.assertIsNone(auditor.ignored_reason("1. Remove 25% of planks", 10, 13))
        self.assertEqual(auditor.ignored_reason("## 3 Scope", 3, 4), "a numbered heading")
        # The window looks 32 characters back and 8 forward, no further.
        near, far = "sheet 4" + "x" * 24 + " 9", "sheet 4" + "x" * 25 + " 9"
        self.assertEqual(auditor.ignored_reason(near, 32, 33), "a page, sheet or item reference")
        self.assertIsNone(auditor.ignored_reason(far, 33, 34))
        self.assertEqual(auditor.ignored_reason("9 sheet 4", 0, 1), "a page, sheet or item reference")
        self.assertIsNone(auditor.ignored_reason("9 so sheet 4", 0, 1))
        self.assertEqual(auditor.ignored_reason("p. 9", 3, 4), "a page, sheet or item reference")

    def test_orphan_string(self):
        self.assertEqual(str(Orphan("12", 4, "…[12]…")), "line 4: 12  —  …[12]…")

    def test_ledger_figures_and_the_phrase_library(self):
        rows = [claim(value="6", value_num=6.0, statement="6 brackets"),
                claim(claim_id="J-SP-002", statement="per clause", quote="spacing 2'-8\" o.c.",
                      derivation="15'-2\" dimension string = 182 in", unit="in")]
        backed = auditor.ledger_figures(rows)
        self.assertTrue({"6", "2'-8\"", "15'-2\"", "182"} <= backed)
        self.assertEqual([o.figure for o in auditor.audit_proposal("6 and 9 and 182", rows)], ["9"])
        self.assertEqual(auditor.audit_proposal("6 and 9", rows, extra_text="warranty of 9 years"), [])


class DocumentsCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_read_document(self):
        md = self.dir / "p.md"
        md.write_text("Install 6 brackets")
        self.assertEqual(auditor.read_document(md), "Install 6 brackets")
        pdf = write_pdf(self.dir / "p.PDF", ["Install 6 brackets"])
        self.assertIn("Install 6 brackets", auditor.read_document(pdf))
        blank = write_pdf(self.dir / "blank.pdf", [""])
        with self.assertRaisesRegex(RuntimeError, "has no text layer; the orphan check needs readable text"):
            auditor.read_document(blank)
        with mock.patch("pipeline.auditor.shutil.which", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "is a PDF and pdftotext is not installed"):
                auditor.read_document(pdf)

    def test_page_text(self):
        pdf = write_pdf(self.dir / "s.pdf", ["first page", "second page", ""])
        self.assertIn("second page", auditor.page_text(pdf, 2))
        self.assertNotIn("first", auditor.page_text(pdf, 2))
        self.assertIsNone(auditor.page_text(pdf, 3))          # no text layer
        self.assertIsNone(auditor.page_text(pdf, 9))          # no such page
        self.assertIsNone(auditor.page_text(self.dir / "missing.pdf", 1))
        with mock.patch("pipeline.auditor.shutil.which", return_value=None):
            self.assertIsNone(auditor.page_text(pdf, 1))

    def register(self, *pages, status="present", file="spec.pdf"):
        if pages:
            write_pdf(self.dir / file, list(pages))
        return {"SW": {"source_id": "SW", "file": file, "status": status, "kind": "spec"}}

    def test_confirm_on_page(self):
        reg = self.register("Section A", "Coverage: 320-400 sq ft/gal\nApply 2 coats")
        v = auditor.confirm_on_page(claim(quote="Coverage: 320 - 400 sq ft/gal"), reg, self.dir)
        self.assertEqual((v.claim_id, v.verdict, v.note), ("J-SP-001", "pass", "found on SW p.2"))
        v = auditor.confirm_on_page(claim(quote="", value="APPLY 2 COATS"), reg, self.dir)
        self.assertEqual(v.verdict, "pass")
        v = auditor.confirm_on_page(claim(quote="Apply 3 coats"), reg, self.dir)
        self.assertEqual((v.verdict, v.note), ("fail", "not found on SW p.2: 'Apply 3 coats'"))
        v = auditor.confirm_on_page(claim(statement="paraphrase"), reg, self.dir)
        self.assertEqual((v.verdict, v.note), (
            "unverified", "SW p.2 opened (7 words) but the row quotes nothing; needs the yes/no support check"))

    def test_the_needle_is_the_first_120_characters(self):
        quote = "abcd " * 30
        head = auditor._normal(quote)[:120]
        reg = self.register("x", "\n".join(head[i:i + 40] for i in range(0, 120, 40)))
        v = auditor.confirm_on_page(claim(quote=quote + "TAIL"), reg, self.dir)
        self.assertEqual(v.verdict, "pass")

    def test_a_page_with_no_text_layer_is_unverified(self):
        reg = self.register("x", "")
        v = auditor.confirm_on_page(claim(quote="anything"), reg, self.dir)
        self.assertEqual((v.verdict, v.note), ("unverified", "SW p.2 has no text layer; needs the page image"))

    def test_rows_with_nothing_to_open(self):
        reg = self.register("x", "y")
        self.assertIsNone(auditor.confirm_on_page(claim(locator="Partial Foundation Plan"), reg, self.dir))
        self.assertIsNone(auditor.confirm_on_page(claim(source_id="OTHER"), reg, self.dir))
        missing = {"SW": dict(reg["SW"], status="missing")}
        self.assertIsNone(auditor.confirm_on_page(claim(quote="y"), missing, self.dir))
        photo = {"SW": dict(reg["SW"], file="IMG.jpg")}
        self.assertIsNone(auditor.confirm_on_page(claim(quote="y"), photo, self.dir))
        upper = self.register("x", "Apply 2 coats", file="SPEC.PDF")
        self.assertEqual(auditor.confirm_on_page(claim(quote="Apply 2 coats"), upper, self.dir).verdict, "pass")

    def test_page_numbers_in_locators(self):
        reg = self.register("one", "two", "three")
        for locator in ("p.3", "p 3", "P3", "spec p. 3 B"):
            self.assertEqual(auditor.confirm_on_page(claim(locator=locator, quote="three"), reg, self.dir).verdict,
                             "pass", locator)


class LinkCase(unittest.TestCase):
    class Resp:
        def __init__(self, status):
            self.status = status

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def check(self, effect):
        with mock.patch("pipeline.auditor.urllib.request.urlopen", side_effect=effect) as m:
            out = auditor.link_live("https://example.org/a", timeout=7)
        return out, m

    def test_status_codes(self):
        (ok, why), m = self.check([self.Resp(200)])
        self.assertEqual((ok, why), (True, "HTTP 200"))
        req = m.call_args.args[0]
        self.assertEqual((req.full_url, req.get_method(), req.get_header("User-agent")),
                         ("https://example.org/a", "HEAD", "ChrisBids-Auditor/0.1"))
        self.assertEqual(m.call_args.kwargs, {"timeout": 7})
        self.assertEqual(self.check([self.Resp(399)])[0], (True, "HTTP 399"))
        self.assertEqual(self.check([self.Resp(400)])[0], (False, "HTTP 400"))
        self.assertEqual(self.check([self.Resp(199)])[0], (False, "HTTP 199"))

    def test_errors(self):
        def http(code):
            return urllib.error.HTTPError("u", code, "m", {}, None)
        self.assertEqual(self.check(http(404))[0], (False, "HTTP 404"))
        self.assertEqual(self.check(http(403))[0], (True, "HTTP 403 on HEAD, not checked further"))
        self.assertEqual(self.check(http(405))[0], (True, "HTTP 405 on HEAD, not checked further"))
        self.assertEqual(self.check(urllib.error.URLError("no route"))[0], (False, "URLError: <urlopen error no route>"))
        self.assertEqual(self.check(OSError("reset"))[0], (False, "OSError: reset"))
        self.assertEqual(self.check(ValueError("bad url"))[0], (False, "ValueError: bad url"))


class ReportCase(unittest.TestCase):
    def test_a_passing_report(self):
        r = AuditReport(job="NAN", rows=2, verdicts={"unverified": 1, "pass": 1}, unverified=["X: why"])
        self.assertTrue(r.ok)
        self.assertEqual(r.text(), "Audit of NAN: 2 rows\n  pass 1, unverified 1\n\nunverified (1):\n  - X: why\n\nPASS")

    def test_a_failing_report_lists_everything(self):
        r = AuditReport(job="J", rows=1, verdicts={"fail": 1}, failures=["A: bad"], hash_problems=["S-1 changed"],
                        orphans=[Orphan("9", 3, "…[9]…")])
        self.assertEqual(r.text(), (
            "Audit of J: 1 rows\n  fail 1\n\nsource hashes (1):\n  - S-1 changed\n\nfailures (1):\n  - A: bad\n"
            "\norphan figures (1): a figure with no ledger row behind it\n  - line 3: 9  —  …[9]…\n\nFAIL"))
        for kw in ({"failures": ["x"]}, {"orphans": [Orphan("1", 1, "")]}, {"hash_problems": ["x"]}):
            self.assertFalse(AuditReport(**kw).ok, kw)


class ConversionCase(unittest.TestCase):
    def dim(self, derivation, value_num, value="182"):
        return claim(method="dimensioned", role="quantity", derivation=derivation, value=value, value_num=value_num)

    def test_replay(self):
        self.assertEqual(auditor._replay_conversion(self.dim("15'-2\" dimension string = 182 in", 182.0)), "")
        self.assertEqual(auditor._replay_conversion(
            self.dim("15'-2\" dimension string = 182 in; seen in 2 of 3 runs", 182.0)), "")
        self.assertEqual(auditor._replay_conversion(self.dim("1/2\" dimension string = 0.5 in", 0.5, "0.5")), "")
        self.assertEqual(auditor._replay_conversion(self.dim("15'-2\" dimension string = 182 in", 184.0, "184")),
                         "15'-2\" is 182 in, ledger says 184")
        self.assertEqual(auditor._replay_conversion(self.dim("15'-2\" dimension string = 182 in", None, "")),
                         "15'-2\" is 182 in, ledger says (blank)")
        self.assertTrue(auditor._replay_conversion(self.dim("about 15 dimension string = 182 in", 182.0))
                        .startswith("derivation does not parse: "))
        self.assertIsNone(auditor._replay_conversion(self.dim("measured on site", 182.0)))
        self.assertIsNone(auditor._replay_conversion(self.dim("", 182.0)))


if __name__ == "__main__":
    unittest.main()
