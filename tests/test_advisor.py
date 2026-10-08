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
        for basis in (None, "", "seems off", "architecture", "rule:", "arch p.", "rule: I just think so",
                      "arch p.0", "arch p.18", "arch p.99", "input the diff gives concern"):
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


class BasisCase(unittest.TestCase):
    def test_every_rule_name_is_a_rule_in_the_brief(self):
        brief = (Path(advisor.ROOT) / "docs" / "advisor.md").read_text()
        for name in advisor.RULES:
            self.assertIn(f"**{name}.**", brief)

    def test_the_page_range_comes_from_the_architecture_text(self):
        self.assertEqual(advisor.PAGES, 17)
        self.assertTrue(advisor.has_basis("arch p.1"))
        self.assertTrue(advisor.has_basis("arch p.17 build plan"))
        self.assertTrue(advisor.has_basis("Rule: golden fixtures, both ledgers"))


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
        for reply in ("not json", json.dumps({"summary": ""})):
            code, comment = self.run_main(reply)
            self.assertEqual(code, 2)
            self.assertIn("## Advisor: no review", comment)


class BriefCase(unittest.TestCase):
    def test_the_brief_names_every_input_the_workflow_writes(self):
        root = Path(advisor.ROOT)
        brief = (root / "docs" / "advisor.md").read_text()
        workflow = (root / ".github" / "workflows" / "advisor.yml").read_text()
        self.assertIn(f'startswith("{advisor.MARKER}")', workflow)
        for name in ("pr.md", "diff.patch", "files.txt"):
            self.assertIn(f".advisor/{name}", brief)
            self.assertIn(f".advisor/{name}", workflow)
        self.assertIn("(architecture.txt)", brief)
        self.assertTrue((root / "docs" / "architecture.txt").read_text().startswith("Bid Pipeline"))


class UsageCase(unittest.TestCase):
    RESULT = {"type": "result", "num_turns": 9, "total_cost_usd": 1.234,
              "usage": {"input_tokens": 10, "output_tokens": 20, "cache_creation_input_tokens": 30,
                        "cache_read_input_tokens": None}}

    def test_the_last_result_message_gives_the_totals(self):
        early = dict(self.RESULT, num_turns=1)
        for text in (json.dumps([{"type": "system"}, early, self.RESULT]),
                     "\n".join(json.dumps(m) for m in ({"type": "system"}, early, self.RESULT))):
            with self.subTest(text=text[:20]):
                self.assertEqual(advisor.usage_line(text),
                                 "usage: 9 turns, 10 input_tokens, 20 output_tokens, 30 cache_creation_input_tokens, "
                                 "0 cache_read_input_tokens, $1.23 as Claude Code reports it")

    def test_missing_figures_are_said_to_be_missing(self):
        self.assertEqual(advisor.usage_line("[]"), "usage: not reported (no result message)")
        self.assertEqual(advisor.usage_line("{broken"), "usage: not reported (execution output unreadable)")
        self.assertEqual(advisor.usage_line(json.dumps([{"type": "result"}])),
                         "usage: ? turns, 0 input_tokens, 0 output_tokens, 0 cache_creation_input_tokens, "
                         "0 cache_read_input_tokens")

    def test_main_puts_the_usage_at_the_foot_of_the_comment(self):
        with tempfile.TemporaryDirectory() as d:
            src, ex, out = Path(d) / "r.json", Path(d) / "ex.json", Path(d) / "c.md"
            src.write_text(json.dumps({"summary": "Sound.", "findings": []}))
            ex.write_text(json.dumps([self.RESULT]))
            self.assertEqual(advisor.main([str(src), "--usage", str(ex), "--out", str(out)]), 0)
            self.assertIn("<sub>usage: 9 turns,", out.read_text())
            self.assertEqual(advisor.main([str(src), "--usage", str(Path(d) / "none"), "--out", str(out)]), 0)
            self.assertIn("<sub>usage: not reported (no execution output)</sub>", out.read_text())


class FailureCase(unittest.TestCase):
    def test_a_failed_run_says_why(self):
        failed = {"type": "result", "is_error": True, "num_turns": 1, "total_cost_usd": 0, "result": "Invalid API key"}
        self.assertTrue(advisor.usage_line(json.dumps([failed])).startswith("review failed: Invalid API key; usage: 1 turns"))

    def test_no_reply_still_writes_a_comment_with_the_reason(self):
        with tempfile.TemporaryDirectory() as d:
            src, ex, out = Path(d) / "r.json", Path(d) / "ex.json", Path(d) / "c.md"
            src.write_text("")
            ex.write_text(json.dumps([{"type": "result", "is_error": True, "result": "Credit balance is too low"}]))
            self.assertEqual(advisor.main([str(src), "--usage", str(ex), "--out", str(out)]), 2)
            text = out.read_text()
            self.assertTrue(text.startswith(advisor.MARKER + "\n## Advisor: no review\n"))
            self.assertIn("review failed: Credit balance is too low", text)
