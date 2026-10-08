"""The advisor's reply is checked and settled by code, not taken on its word."""
import json
import tempfile
import unittest
from pathlib import Path

from tools import advisor


def finding(**kw):
    f = {"severity": "advice", "area": "clarity", "file": "pipeline/takeoff.py", "line": 12,
         "basis": None, "finding": "Two helpers do the same thing.", "fix": "Keep one."}
    f.update(kw)
    return f


class CheckCase(unittest.TestCase):
    def test_a_blocking_finding_with_a_basis_stands(self):
        for basis in ("arch p.6 scaled never becomes an order quantity",
                      "rule: Determinism, models extract; code computes",
                      "input an empty packet gives a KeyError, should be no rows"):
            with self.subTest(basis=basis):
                [f] = advisor.check({"summary": "", "findings": [finding(severity="blocking", basis=basis)]})
                self.assertEqual(f["severity"], "blocking")
                self.assertFalse(f["demoted"])

    def test_a_blocking_finding_without_one_is_demoted(self):
        for basis in (None, "", "seems off", "architecture", "rule:", "arch p."):
            with self.subTest(basis=basis):
                [f] = advisor.check({"summary": "", "findings": [finding(severity="blocking", basis=basis)]})
                self.assertEqual(f["severity"], "advice")
                self.assertTrue(f["demoted"])

    def test_advice_is_never_promoted(self):
        [f] = advisor.check({"summary": "", "findings": [finding(basis="arch p.6")]})
        self.assertEqual(f["severity"], "advice")
        self.assertFalse(f["demoted"])

    def test_a_reply_off_the_schema_is_refused(self):
        bad = [
            {"findings": []},
            {"summary": "", "findings": [finding(severity="critical")]},
            {"summary": "", "findings": [finding(area="taste")]},
            {"summary": "", "findings": [finding(line=0)]},
            {"summary": "", "findings": [dict(finding(), verdict="pass")]},
            {"summary": "", "findings": [finding()] * 13},
        ]
        for review in bad:
            with self.subTest(review=review), self.assertRaises(ValueError):
                advisor.check(review)


class RenderCase(unittest.TestCase):
    def test_blocking_comes_first_and_the_head_counts_both(self):
        findings = advisor.check({"summary": "One real problem.", "findings": [
            finding(finding="Advice A."),
            finding(severity="blocking", area="design", basis="arch p.11", finding="Writes outside scope."),
        ]})
        text = advisor.render("One real problem.", findings)
        self.assertTrue(text.startswith(advisor.MARKER + "\n## Advisor: 1 blocking, 1 advice\n"))
        self.assertLess(text.index("Writes outside scope."), text.index("Advice A."))
        self.assertIn("**1. blocking · design** `pipeline/takeoff.py:12` (arch p.11)", text)

    def test_a_clean_review_says_nothing_blocks(self):
        text = advisor.render("Sound.", [])
        self.assertIn("## Advisor: nothing blocking, 0 advice", text)

    def test_a_demoted_finding_says_so(self):
        findings = advisor.check({"summary": "", "findings": [finding(severity="blocking", basis="vibes")]})
        self.assertIn("Demoted from blocking", advisor.render("", findings))

    def test_where_without_a_line_or_file(self):
        self.assertEqual(advisor._where(finding(line=None)), "`pipeline/takeoff.py`")
        self.assertEqual(advisor._where(finding(file=None, line=None)), "whole PR")


class MainCase(unittest.TestCase):
    def run_main(self, reply: str) -> tuple[int, str]:
        with tempfile.TemporaryDirectory() as d:
            src, out = Path(d) / "review.json", Path(d) / "comment.md"
            src.write_text(reply)
            code = advisor.main([str(src), "--out", str(out)])
            return code, out.read_text() if out.exists() else ""

    def test_exit_codes(self):
        clean = json.dumps({"summary": "Sound.", "findings": [finding()]})
        blocks = json.dumps({"summary": "", "findings": [finding(severity="blocking", basis="arch p.4")]})
        self.assertEqual(self.run_main(clean)[0], 0)
        self.assertEqual(self.run_main(blocks)[0], 1)
        self.assertEqual(self.run_main("not json"), (2, ""))
        self.assertEqual(self.run_main(json.dumps({"summary": ""})), (2, ""))


class BriefCase(unittest.TestCase):
    def test_the_brief_names_every_input_the_workflow_writes(self):
        root = Path(advisor.ROOT)
        brief = (root / "docs" / "advisor.md").read_text()
        workflow = (root / ".github" / "workflows" / "advisor.yml").read_text()
        for name in ("pr.md", "diff.patch", "files.txt"):
            self.assertIn(f".advisor/{name}", brief)
            self.assertIn(f".advisor/{name}", workflow)
        self.assertIn("(architecture.txt)", brief)
        self.assertTrue((root / "docs" / "architecture.txt").read_text().startswith("Bid Pipeline"))
