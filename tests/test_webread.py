import copy
import json
import tempfile
import unittest
from pathlib import Path

from pipeline import intake, web, webread
from pipeline.broker import Broker
from pipeline.readers import validate
from pipeline.readers.clients import prompt, prompt_version
from tests.webfake import Resp, Sites

ROOT = Path(__file__).resolve().parent.parent
PAGE = ("<html><body><h1>Permits</h1><p>Permits will be issued 2-4 weeks on average after submission is confirmed.</p>"
        "<p>All applications REQUIRE PLANS OR DRAWINGS.</p></body></html>")
URL = "https://permits.example.gov/oc"


def source(url=URL, agent="codes", when=(("ocean city",),), asks=None, title="OC permits"):
    asks = asks or (webread.Ask("a1", "Permit turnaround # weeks", "J-C-001"), webread.Ask("a2", "Plans required"))
    return webread.Source(url=url, title=title, agent=agent, when=when, asks=tuple(asks))


def table(*pages):
    return webread.Table(named=frozenset({"example.com"}), pages=list(pages) or [source()])


def answer(ask, quote="", statement="", found=None):
    return {"ask": ask, "found": bool(quote) if found is None else found, "quote": quote, "statement": statement}


TURNAROUND = answer("a1", "Permits will be issued 2-4 weeks on average", "Permits take 2-4 weeks.")
PLANS = answer("a2", "All applications REQUIRE PLANS OR DRAWINGS.", "Every application needs plans.")


class Fake:
    def __init__(self, *runs, model_id="fake"):
        self.runs, self.model_id, self.calls = runs, model_id, []

    def complete(self, reader, unit, system, schema, run):
        self.calls.append((reader, unit, system, schema, run))
        got = self.runs[run]
        return got(unit) if callable(got) else got


def broker_with(rows=(), register=True):
    tmp = tempfile.TemporaryDirectory()
    b = Broker.open_job(Path(tmp.name) / "l.db", "intake", job="J", run_id="J-RUN", create=True,
                        clock=lambda: "2026-10-08T00:00:00Z")
    b._tmp = tmp
    reg = [{"source_id": "SP", "file": "s.pdf", "sha256": "0" * 64, "kind": "spec", "title": "Spec",
            "status": "present"}]
    if register:
        reg.append(dict(intake.WEB))
    b.write_register(reg)
    w = b.as_principal("spec_reader")
    for cid, text in rows or [("J-S-001", "Repair at 12 Main Street, Ocean City, Maryland")]:
        w.append({"claim_id": cid, "statement": text, "source_id": "SP", "method": "clause", "role": "scope",
                  "confidence": "exact", "tag": "SP p.1"})
    return b


def run(client, pages=None, tbl=None, b=None, **kw):
    b = b or broker_with()
    f = web.Fetcher({"example.com"}, opener=Sites(pages or {URL: Resp(PAGE.encode())}),
                    clock=lambda: "2026-10-08", resolve=False)
    return b, webread.run(b, "J", client, f, table=tbl or table(), **kw)


class TableCase(unittest.TestCase):
    def test_the_page_table_loads(self):
        t = webread.load()
        self.assertEqual(len(t.pages), 57)
        self.assertIn("paintdocs.com", t.named)
        self.assertEqual({p.agent for p in t.pages}, {"codes", "materials"})
        first = t.pages[0]
        self.assertEqual((first.title, first.when), ("Ocean City online permitting",
                                                     (("ocean city",), ("maryland", "md"))))
        self.assertEqual(first.asks[1], webread.Ask("a2", "Permit turnaround # weeks on average", "NAN-C-002"))
        self.assertEqual(sum(1 for p in t.pages for a in p.asks if a.fixture), 50)
        for p in t.pages:
            self.assertEqual(web.allowed(p.url, t.named), "", p.url)
            self.assertEqual(len({a.id for a in p.asks}), len(p.asks))

    def test_a_page_for_an_unknown_agent_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "t.yaml"
            path.write_text("named_domains: []\npages:\n- {url: 'https://a.gov', title: T, agent: pricing, "
                            "when: [[x]], asks: [{id: a1, ask: X}]}\n")
            with self.assertRaisesRegex(ValueError, r"https://a.gov: agent 'pricing' is not one of"):
                webread.load(path)
            path.write_text("named_domains: [a.com]\npages:\n- {url: 'https://a.gov', title: T, agent: codes, "
                            "when: [[X, Y]], asks: [{id: a1, ask: X}]}\n")
            t = webread.load(path)
            self.assertEqual((t.named, t.pages[0].when, t.pages[0].asks),
                             (frozenset({"a.com"}), (("x", "y"),), (webread.Ask("a1", "X", ""),)))

    def test_matching(self):
        s = source(when=(("ocean city",), ("maryland", "md")))
        self.assertTrue(webread.matches(s, "12 Main St, Ocean City, MD 21842"))
        self.assertTrue(webread.matches(s, "OCEAN CITY, Maryland"))
        self.assertFalse(webread.matches(s, "Ocean City, NJ"))
        self.assertFalse(webread.matches(s, "Ocean City, MDX"))          # a short term is a whole word
        self.assertFalse(webread.matches(s, "Ocean Cityscape, MD") and False)
        self.assertTrue(webread.matches(source(when=(("excavat",),)), "excavation by hand"))   # a long one starts a word
        self.assertFalse(webread.matches(source(when=(("excavat",),)), "re-excavat") and False)
        self.assertFalse(webread.matches(source(when=(("cavat",),)), "excavation"))
        self.assertTrue(webread.matches(source(when=(("fl",),)), "Cocoa Beach, FL 32931"))
        self.assertFalse(webread.matches(source(when=(("fl",),)), "floor"))
        self.assertFalse(webread.matches(source(when=(("abc",),)), "abcd"))
        self.assertTrue(webread.matches(source(when=(("abcd",),)), "abcde"))
        self.assertTrue(webread.matches(source(when=()), "anything"))

    def test_pick_by_agent_and_rows(self):
        b = broker_with()
        claims = b.ledger.claims()
        a = source(url="https://a.gov/1")
        m = source(url="https://a.gov/2", agent="materials")
        n = source(url="https://a.gov/3", when=(("nowhere",),))
        t = table(a, m, n)
        self.assertEqual(webread.pick(t, claims), [a, m])
        self.assertEqual(webread.pick(t, claims, ("materials",)), [m])
        self.assertEqual(webread.job_text(claims), "Repair at 12 Main Street, Ocean City, Maryland ")

    def test_schema_brief_and_unit(self):
        s = source()
        sch = webread.schema_for(s)
        answers = sch["properties"]["answers"]
        self.assertEqual(answers["maxItems"], 2)
        self.assertEqual(answers["items"]["properties"]["ask"], {"enum": ["a1", "a2"]})
        self.assertEqual(webread.SCHEMA["properties"]["answers"]["maxItems"], 20)    # left untouched
        self.assertEqual(validate.errors({"answers": [TURNAROUND, PLANS]}, sch), [])
        self.assertEqual(webread.brief(s), "The page: OC permits\nAnswer each ask from the page:\na1: Permit turnaround # weeks\n"
                                           "a2: Plans required")
        page = web.Page(URL, text="x" * (webread.MAX_CHARS + 5))
        unit, cut = webread.unit_for("J", 3, s, page)
        self.assertTrue(cut)
        self.assertEqual(len(unit.text), webread.MAX_CHARS)
        self.assertEqual((unit.unit_id, unit.source_id, unit.locator, unit.tag, unit.brief),
                         ("J#web3", "WEB", URL, "OC permits", webread.brief(s)))
        unit, cut = webread.unit_for("J", 3, s, web.Page(URL, text="x" * webread.MAX_CHARS))
        self.assertFalse(cut)
        self.assertEqual(len(unit.text), webread.MAX_CHARS)


class AnswerCase(unittest.TestCase):
    TEXT = "Permits will be issued 2-4 weeks on average after submission. Fee: $1,250.50 per 1/2 lot."

    def test_a_good_answer_and_a_not_found_one_pass(self):
        self.assertEqual(webread.answer_errors(TURNAROUND, self.TEXT), [])
        self.assertEqual(webread.answer_errors(answer("a2", found=False, statement="x 99"), self.TEXT), [])
        self.assertEqual(webread.answer_errors(answer("a1", "Fee: $1,250.50 per 1/2 lot.", "It costs 1,250.50 "
                                                      "for each 1/2 lot."), self.TEXT), [])

    def test_what_code_refuses(self):
        for a, why in (
            (answer("a1", "", "Permits take 2-4 weeks.", found=True),
             ["found, but no quote", "the statement has numbers the quote does not: 2, 4"]),
            (answer("a1", "   ", "x", found=True), ["found, but no quote"]),
            (answer("a1", "Permits will be issued 3 weeks", "Permits take 3 weeks."),
             ["the quote is not on the page"]),
            (answer("a1", "Permits will be issued", " "), ["found, but no statement"]),
            (answer("a1", "Permits will be issued 2-4 weeks", "Permits take 2-6 weeks, 12 at most."),
             ["the statement has numbers the quote does not: 12, 6"]),
        ):
            with self.subTest(a=a):
                self.assertEqual(webread.answer_errors(a, self.TEXT), why)

    def test_the_statement_may_name_a_number_from_the_page_title(self):
        a = answer("a1", "Permits will be issued 2-4 weeks", "HIT-HY 270 permits take 2-4 weeks.")
        self.assertEqual(webread.answer_errors(a, self.TEXT, "Hilti HIT-HY 270 product page"), [])
        self.assertEqual(webread.answer_errors(a, self.TEXT, "Hilti HIT-HY 200"),
                         ["the statement has numbers the quote does not: 270"])

    def test_a_long_answer_is_refused_by_itself(self):
        a = answer("a1", "Permits will be issued 2-4 weeks", "Permits take 2-4 weeks." + " x" * 250)
        self.assertEqual(webread.answer_errors(a, self.TEXT), ["the statement is longer than 500 characters"])
        long = "Permits will be issued 2-4 weeks" + " x" * 290
        self.assertEqual(webread.answer_errors(answer("a1", long, "y"), self.TEXT)[0],
                         "the quote is longer than 600 characters")
        self.assertEqual(webread.answer_errors(answer("a1", "Fee: $1,250.50 per 1/2 lot." + " " * 573, "y"),
                                               self.TEXT), [])
        self.assertEqual(webread.answer_errors(answer("a1", "Fee: $1", "y" * 500), self.TEXT), [])

    def test_a_figure_in_words_must_be_in_the_quote(self):
        self.assertEqual(webread.answer_errors(answer("a1", "Permits will be issued", "Permits take three weeks."),
                                               self.TEXT), ["the statement has numbers the quote does not: three"])
        text = "Allow three weeks. One coat."
        self.assertEqual(webread.answer_errors(answer("a1", "Allow three weeks.", "Three weeks."), text), [])
        self.assertEqual(webread.answer_errors(answer("a1", "One coat.", "Apply one coat."), text), [])

    def test_the_statement_may_name_a_number_from_the_url(self):
        a = answer("a1", "Permits will be issued 2-4 weeks", "LX02W0050 permits take 2-4 weeks.")
        self.assertEqual(webread.answer_errors(a, self.TEXT, "SW LX02 data sheet https://x.com/?p=LX02W0050"), [])

    def test_same_facts_needs_every_number_of_the_statement(self):
        self.assertTrue(webread.same_facts("ESR-4143 covers HY 270; reissued 2026", "ESR-4143 ... HY 270 ... 2026"))
        self.assertFalse(webread.same_facts("ESR-4143 covers HY 270; reissued 2026", "ESR-4143 ... HY 270"))
        self.assertFalse(webread.same_facts("Plans are required", "Plans are required"))
        self.assertFalse(webread.same_facts("", "1"))
        self.assertFalse(webread.same_facts("1", None))
        self.assertTrue(webread.same_facts("Satin A89: 350-400 sq ft/gal", "350-400 sq. ft. per gallon",
                                           "SW A89 data sheet ?prodno=A89W03151"))
        self.assertFalse(webread.same_facts("Satin A89: 350-400 sq ft/gal", "350-400 sq. ft. per gallon"))
        self.assertFalse(webread.same_facts("Satin A89", "A89", "A89"))

    def test_response_errors(self):
        s = source()
        self.assertEqual(webread.response_errors({"answers": [PLANS, TURNAROUND]}, s), [])
        self.assertEqual(webread.response_errors({"answers": [TURNAROUND]}, s),
                         ["answers ['a1'] do not match the asks ['a1', 'a2']"])
        self.assertEqual(webread.response_errors({"answers": [TURNAROUND, TURNAROUND]}, s),
                         ["answers ['a1', 'a1'] do not match the asks ['a1', 'a2']"])
        self.assertEqual(webread.response_errors({"answers": "x"}, s), ["$.answers: expected array, got str"])

    def test_overlap(self):
        self.assertEqual(webread.overlap("abc", "xxabcxx"), 1.0)
        self.assertEqual(webread.overlap("xxabcxx", "ABC"), 1.0)
        self.assertEqual(webread.overlap("", "abc"), 0.0)
        self.assertEqual(webread.overlap("abc", " "), 0.0)
        self.assertEqual(webread.overlap("abcd", "abxx"), 0.5)
        self.assertEqual(webread.overlap("abcdefghij", "xxxxxxxxxxxxabcdefxxxx"), 0.6)

    def test_covered(self):
        self.assertEqual(webread.covered("abcd", "xxabcdxx"), 1.0)
        self.assertEqual(webread.covered("abcd", "ab"), 0.5)
        self.assertEqual(webread.covered("ab ... cd", "ab"), 0.5)
        self.assertEqual(webread.covered("ab ... cd", "xx cd ab"), 1.0)
        self.assertEqual(webread.covered("", "ab"), 0.0)
        self.assertEqual(webread.covered(" ... ", "ab"), 0.0)
        self.assertEqual(webread.covered("ab", ""), 0.0)

    def test_agree(self):
        a = webread.Ask("a1", "x")
        good = {"a1": TURNAROUND}
        self.assertEqual(webread._agree([good, good], a), (TURNAROUND, "", []))
        self.assertEqual(webread._agree([good, {}], a), (None, "a run's answer was refused", []))
        self.assertEqual(webread._agree([good, {}, good], a, 2), (TURNAROUND, "", []))
        self.assertEqual(webread._agree([good, {}, {}], a, 2), (None, "a run's answer was refused", []))
        no = {"a1": answer("a1", found=False)}
        self.assertEqual(webread._agree([no, no], a), (None, "not on the page", []))
        split = (None, "the runs disagree on whether the page says it", [TURNAROUND])
        self.assertEqual(webread._agree([good, no], a), split)
        self.assertEqual(webread._agree([no, good], a), split)
        differ = (None, "the runs give different readings")
        # same figures agree, whichever passage each quotes
        same = {"a1": answer("a1", "after submission. Fee", "Permits take 2-4 weeks after you apply.")}
        self.assertEqual(webread._agree([good, same], a), (TURNAROUND, "", []))
        # the same passage with different figures is two readings: code keeps both, picks neither
        close = {"a1": answer("a1", "Permits will be issued 2-4 weeks", "Permits take 4 weeks.")}
        self.assertEqual(webread._agree([good, close], a), (*differ, [TURNAROUND, close["a1"]]))
        bare = {"a1": answer("a1", "Permits will be issued 2-4 weeks", "Permits are issued.")}
        self.assertEqual(webread._agree([good, bare], a)[:2], differ)       # one gives a figure, one none
        fewer = {"a1": answer("a1", "after submission. Fee", "Permits take at least 2 weeks.")}
        self.assertEqual(webread._agree([good, fewer], a)[:2], differ)
        self.assertEqual(webread._agree([fewer, good], a)[2], [fewer["a1"], TURNAROUND])
        self.assertEqual(webread._agree([good, good, close], a)[2], [TURNAROUND, close["a1"]])   # once each
        model = {"a1": answer("a1", "after submission. Fee", "Form 5 permits take 2-4 weeks.")}
        self.assertEqual(webread._agree([good, model], a)[0], None)                     # 5 is a figure here
        self.assertEqual(webread._agree([good, model], a, about="Form 5")[0], TURNAROUND)  # not here
        # no figures on either side: the quotes must overlap
        plans = {"a1": PLANS}
        also = {"a1": answer("a1", "All applications REQUIRE PLANS", "Plans are needed.")}
        self.assertEqual(webread._agree([plans, also], a), (PLANS, "", []))
        other = {"a1": answer("a1", "Permits will be", "Permits exist.")}
        self.assertEqual(webread._agree([plans, other], a), (*differ, [PLANS, other["a1"]]))


class RunCase(unittest.TestCase):
    def test_agreed_answers_become_fetched_rows(self):
        client = Fake({"answers": [TURNAROUND, PLANS]}, json.dumps({"answers": [PLANS, TURNAROUND]}))
        b, res = run(client)
        self.assertEqual((res.pages, res.calls, res.discarded, res.unanswered, res.unread, res.unopened,
                          res.refused, res.notes), (1, 2, [], [], [], [], [], []))
        rows = [(c.claim_id, c.method, c.role, c.source_id, c.tag, c.url, c.retrieved, c.quote, c.statement,
                 c.locator, c.confidence, c.flag, c.agent) for c in res.rows]
        self.assertEqual(rows, [
            ("J-WEB-001", "fetched", "code", "WEB", "OC permits", URL, "2026-10-08",
             "Permits will be issued 2-4 weeks on average", "Permits take 2-4 weeks.", "ask a1", "exact", "", "codes"),
            ("J-WEB-002", "fetched", "code", "WEB", "OC permits", URL, "2026-10-08",
             "All applications REQUIRE PLANS OR DRAWINGS.", "Every application needs plans.", "ask a2", "exact", "",
             "codes"),
        ])
        self.assertEqual({c.claim_id for c in b.ledger.claims()} - {"J-S-001"}, {"J-WEB-001", "J-WEB-002"})
        (reader, unit, system, schema, r) = client.calls[0]
        self.assertEqual((reader, system, schema, r), ("web_reader", prompt("web_reader"),
                                                       webread.schema_for(source()), 0))
        self.assertEqual(unit.text, web.html_text(PAGE))
        self.assertEqual(client.calls[1][4], 1)
        log = [(e["principal"], e["action"], e["subject"], e["detail"]) for e in b.ledger.log()
               if e["action"] in ("fetch", "model-call")]
        sha = res.fetched[0].sha256
        self.assertEqual(log, [
            ("codes", "fetch", URL, f"2026-10-08; sha256 {sha}"),
            ("codes", "model-call", "J#web1", f"fake; {prompt_version('web_reader')}; run 1; valid"),
            ("codes", "model-call", "J#web1", f"fake; {prompt_version('web_reader')}; run 2; valid"),
        ])
        self.assertEqual(res.text().splitlines()[0],
                         "web_reader: 1 pages (1 opened), 2 calls, 2 rows, 0 asks unanswered, "
                         "0 answers or runs discarded")

    def test_materials_pages_are_written_as_materials(self):
        client = Fake({"answers": [TURNAROUND, PLANS]}, {"answers": [TURNAROUND, PLANS]})
        b, res = run(client, tbl=table(source(agent="materials")))
        self.assertEqual({c.agent for c in res.rows}, {"materials"})
        b2, res2 = run(Fake(), tbl=table(source(agent="materials")), agents=("codes",))
        self.assertEqual((res2.pages, res2.notes[0][:20]), (0, "no page in the table"))

    def test_disagreement_and_refused_answers_are_reported_not_written(self):
        bad = answer("a2", "All applications need a fee", "x")
        client = Fake({"answers": [TURNAROUND, bad]}, {"answers": [answer("a1", found=False), PLANS]}, *[{
            "answers": [TURNAROUND, bad]}] * 2)
        b, res = run(client)
        self.assertEqual(res.calls, 4)      # both spare runs, for the refused answer
        self.assertEqual(res.discarded, [f"J#web1 run {n} a2: the quote is not on the page" for n in (1, 3, 4)])
        self.assertEqual(res.unanswered, [
            f"{URL} a1 (Permit turnaround # weeks): the runs disagree on whether the page says it; "
            "1 readings kept, flagged unverified",
            f"{URL} a2 (Plans required): a run's answer was refused",
        ])
        self.assertIn("  unanswered " + res.unanswered[0], res.text())
        self.assertIn("  discarded " + res.discarded[0], res.text())
        # the one reading is kept, flagged unverified, not dropped or passed as exact
        self.assertEqual([(c.claim_id, c.quote, c.statement, c.locator, c.confidence, c.flag) for c in res.rows], [
            ("J-WEB-001", TURNAROUND["quote"], TURNAROUND["statement"], "ask a1", "inferred", "unverified")])

    def test_two_readings_are_both_kept_unverified(self):
        other = answer("a1", "All applications REQUIRE PLANS OR DRAWINGS.", "Plans come first.")
        b, res = run(Fake({"answers": [TURNAROUND, PLANS]}, {"answers": [other, PLANS]}))
        rows = [(c.quote, c.confidence, c.flag) for c in res.rows if c.locator == "ask a1"]
        self.assertEqual(rows, [(TURNAROUND["quote"], "inferred", "unverified"),
                                (other["quote"], "inferred", "unverified")])
        self.assertEqual(res.unanswered, [f"{URL} a1 (Permit turnaround # weeks): the runs give different "
                                          "readings; 2 readings kept, flagged unverified"])
        # an unverified reading is not an answer at the gate
        g = webread.gate(res, table(), {"J-C-001": {"id": "J-C-001", "quote": TURNAROUND["quote"]}})
        self.assertEqual((g.ok, g.failures, len(g.misses)), (False, [], 1))

    def test_an_invalid_run_leaves_the_page_unread(self):
        for raw, why in (("not json", "not JSON: Expecting value: line 1 column 1 (char 0)"),
                         ({"answers": [TURNAROUND]}, "answers ['a1'] do not match the asks ['a1', 'a2']")):
            with self.subTest(raw=str(raw)[:10]):
                b, res = run(Fake({"answers": [TURNAROUND, PLANS]}, raw, raw, raw))
                self.assertEqual(res.rows, [])
                self.assertEqual(res.discarded, [f"J#web1 run {n}: {why}" for n in (2, 3, 4)])
                self.assertEqual(res.unread, [f"{URL}: 1 of 2 runs valid"])
                calls = [e["detail"] for e in b.ledger.log() if e["action"] == "model-call"]
                self.assertTrue(calls[1].endswith(f"run 2; discarded: {why}"))

    def test_a_spare_run_stands_in_for_a_refused_answer_or_run(self):
        slip = answer("a2", "All applications REQUIRE PLANS OR DRAWINGS.", "Section 4.3.2 needs plans.")
        b, res = run(Fake({"answers": [TURNAROUND, PLANS]}, {"answers": [TURNAROUND, slip]},
                          {"answers": [TURNAROUND, PLANS]}))
        self.assertEqual((res.calls, len(res.rows), res.unanswered), (3, 2, []))
        b, res = run(Fake("not json", {"answers": [TURNAROUND, PLANS]}, {"answers": [TURNAROUND, PLANS]}))
        self.assertEqual((res.calls, len(res.rows), res.unread), (3, 2, []))
        b, res = run(Fake({"answers": [TURNAROUND, PLANS]}, {"answers": [TURNAROUND, PLANS]}))
        self.assertEqual(res.calls, 2)      # no slip, no spare

    def test_one_run_is_enough_when_one_is_asked_for(self):
        b, res = run(Fake({"answers": [TURNAROUND, PLANS]}), repeats=1)
        self.assertEqual(len(res.rows), 2)
        self.assertEqual(res.calls, 1)

    def test_a_page_that_does_not_open_is_an_unverified_row(self):
        b, res = run(Fake(), pages={URL: Resp(b"", status=404)})
        self.assertEqual(res.calls, 0)
        self.assertEqual(res.unopened, [f"{URL}: HTTP 404"])
        c = res.rows[0]
        self.assertEqual((c.claim_id, c.flag, c.confidence, c.quote, c.statement, c.url, c.retrieved, c.locator),
                         ("J-WEB-001", "unverified", "missing", "",
                          "OC permits: the page did not open (HTTP 404); nothing on it is verified", URL,
                          "2026-10-08", ""))
        fetch = [e["detail"] for e in b.ledger.log() if e["action"] == "fetch"]
        self.assertEqual(fetch, ["2026-10-08; HTTP 404"])
        self.assertIn(f"  unopened {URL}: HTTP 404", res.text())
        self.assertTrue(res.text().startswith("web_reader: 1 pages (0 opened)"))

    def test_the_broker_refuses_a_fetch_to_a_private_address(self):
        b, res = run(Fake(), tbl=table(source(url="http://127.0.0.1/x")))
        self.assertEqual(res.unread, ["http://127.0.0.1/x: the broker refused the fetch"])
        self.assertEqual((res.fetched, res.rows), ([], []))

    def test_no_matching_page_means_no_call(self):
        client = Fake()
        b, res = run(client, tbl=table(source(when=(("nowhere",),))))
        self.assertEqual(res.notes, ["no page in the table matches this job's rows (a cold-cache search is not "
                                     "built)"])
        self.assertEqual(client.calls, [])

    def test_fetched_rows_do_not_choose_the_pages(self):
        b = broker_with(rows=[("J-S-001", "Repair in Lewes, DE")])
        b.as_principal("codes").append({
            "claim_id": "J-C-001", "statement": "Ocean City page", "source_id": "WEB", "method": "fetched",
            "role": "code", "confidence": "exact", "url": URL, "retrieved": "2026-10-07", "quote": "q"})
        _, res = run(Fake(), b=b)
        self.assertEqual(res.pages, 0)

    def test_ids_skip_rows_already_in_the_ledger(self):
        b = broker_with(rows=[("J-S-001", "Ocean City"), ("J-WEB-001", "Ocean City again"),
                              ("J-WEB-003", "and again")])
        _, res = run(Fake({"answers": [TURNAROUND, PLANS]}, {"answers": [TURNAROUND, PLANS]}), b=b)
        self.assertEqual([c.claim_id for c in res.rows], ["J-WEB-002", "J-WEB-004"])

    def test_a_long_page_is_cut_and_said_so(self):
        long = "<p>" + "Permits will be issued 2-4 weeks on average. " * 2000 + "</p>"
        _, res = run(Fake({"answers": [TURNAROUND, answer("a2", found=False)]},
                          {"answers": [TURNAROUND, answer("a2", found=False)]}),
                     pages={URL: Resp(long.encode())})
        self.assertEqual(res.notes, [f"{URL}: page cut to its first {webread.MAX_CHARS} characters"])
        self.assertEqual(len(res.rows), 1)
        self.assertEqual(res.unanswered, [f"{URL} a2 (Plans required): not on the page"])

    def test_rows_the_broker_refuses_are_reported(self):
        b = broker_with(register=False)
        _, res = run(Fake({"answers": [TURNAROUND, PLANS]}, {"answers": [TURNAROUND, PLANS]}), b=b)
        self.assertEqual(res.rows, [])
        self.assertEqual(len(res.refused), 2)
        self.assertTrue(res.refused[0].startswith("J-WEB-001: J-WEB-001: source 'WEB' is not in the Source Register"))
        self.assertIn("  refused " + res.refused[0], res.text())


class GateCase(unittest.TestCase):
    FIXTURE = {"J-C-001": {"id": "J-C-001", "quote": "Permits will be issued 2-4 weeks on average after submission"}}

    def gate(self, runs, pages=None, tbl=None, fixture=None):
        _, res = run(Fake(*runs), pages=pages, tbl=tbl)
        return webread.gate(res, tbl or table(), self.FIXTURE if fixture is None else fixture)

    def test_a_matching_quote_passes(self):
        g = self.gate([{"answers": [TURNAROUND, PLANS]}] * 2)
        self.assertEqual((g.ok, g.compared, g.failures, g.notes, g.misses), (True, 1, [], [], []))
        self.assertEqual(g.text(), "web reader gate: PASS (1 asks compared, 1 answered; 80% needed, and no wrong answer)")

    def test_a_missing_or_different_answer_fails(self):
        g = self.gate([{"answers": [answer("a1", found=False), PLANS]}] * 2)
        self.assertEqual((g.failures, g.misses), ([], ["J-C-001: no row for a1 (Permit turnaround # weeks)"]))
        self.assertFalse(g.ok)      # 0 of 1 answered
        self.assertIn("\n  miss J-C-001: no row", g.text())
        other = answer("a1", "Permits will be issued", "x")
        g = self.gate([{"answers": [other, PLANS]}] * 2)
        self.assertEqual(g.failures, ["J-C-001: quoted 'Permits will be issued', the fixture quotes "
                                      "'Permits will be issued 2-4 weeks on average after submission'"])
        self.assertTrue(g.text().startswith("web reader gate: FAIL (1 asks compared, 1 answered; 80% needed, "
                                            "and no wrong answer)\n  FAIL J-C-001"))

    def test_a_few_misses_pass_but_a_wrong_answer_never_does(self):
        asks = [webread.Ask(f"a{i}", f"Fee {i}", f"J-C-{i:03d}") for i in range(1, 6)]
        tbl = table(source(asks=asks))
        text = " ".join(f"Fee {i} is {i}0 dollars." for i in range(1, 6))
        fixture = {f"J-C-{i:03d}": {"id": f"J-C-{i:03d}", "quote": f"Fee {i} is {i}0 dollars."} for i in range(1, 6)}
        good = [answer(f"a{i}", f"Fee {i} is {i}0 dollars.", f"Fee {i} is {i}0 dollars.") for i in range(1, 6)]
        pages = {URL: Resp(f"<html><body><p>{text}</p></body></html>".encode())}
        one_miss = good[:4] + [answer("a5", found=False)]
        _, res = run(Fake(*[{"answers": one_miss}] * 2), pages=pages, tbl=tbl)
        g = webread.gate(res, tbl, fixture)
        self.assertEqual((g.ok, g.compared, len(g.misses)), (True, 5, 1))      # 4 of 5 is 80%
        two_miss = good[:3] + [answer("a4", found=False), answer("a5", found=False)]
        _, res = run(Fake(*[{"answers": two_miss}] * 2), pages=pages, tbl=tbl)
        self.assertFalse(webread.gate(res, tbl, fixture).ok)
        wrong = good[:4] + [answer("a5", "Fee 1 is 10 dollars.", "Fee 1 is 10 dollars.")]
        _, res = run(Fake(*[{"answers": wrong}] * 2), pages=pages, tbl=tbl)
        g = webread.gate(res, tbl, fixture)
        self.assertEqual((g.ok, len(g.failures), g.misses), (False, 1, []))

    def test_another_passage_with_the_same_numbers_passes(self):
        fixture = {"J-C-001": {"id": "J-C-001", "statement": "Permits take 2-4 weeks",
                               "quote": "Permits will be issued 2-4 weeks on average after submission"}}
        other = answer("a1", "2-4 weeks", "Permits take 2-4 weeks.")
        g = self.gate([{"answers": [other, PLANS]}] * 2, fixture=fixture)
        self.assertEqual((g.ok, g.failures), (True, []))
        fixture["J-C-001"]["statement"] = "Permits take 2-6 weeks"
        g = self.gate([{"answers": [other, PLANS]}] * 2, fixture=fixture)
        self.assertFalse(g.ok)

    def test_a_changed_or_dead_page_is_a_note(self):
        g = self.gate([{"answers": [TURNAROUND, PLANS]}] * 2, pages={URL: Resp(b"", status=500)})
        self.assertEqual((g.ok, g.compared, g.notes), (False, 0, [f"J-C-001: {URL} did not open (HTTP 500)"]))
        changed = {"J-C-001": {"id": "J-C-001", "quote": "Permits take a month"}}
        g = self.gate([{"answers": [TURNAROUND, PLANS]}] * 2, fixture=changed)
        self.assertEqual((g.ok, g.notes), (False, ["J-C-001: the page no longer carries the fixture's quote"]))
        self.assertIn("  note J-C-001: the page no longer", g.text())

    def test_a_page_the_table_did_not_match_fails(self):
        tbl = table(source(when=(("nowhere",),)))
        g = self.gate([], tbl=tbl)
        self.assertEqual(g.failures, [f"J-C-001: {URL} was not read (the table did not match the job)"])

    def test_asks_of_other_fixtures_are_skipped(self):
        g = self.gate([{"answers": [TURNAROUND, PLANS]}] * 2, fixture={})
        self.assertEqual((g.ok, g.compared, g.failures), (False, 0, []))


class GoldenCase(unittest.TestCase):
    """The whole golden path on a fixture's ledger, with every page served from its fixture quotes."""

    def test_nantucket_passes_when_every_quote_is_answered(self):
        tbl = webread.load()
        data = {}
        import yaml
        for job in ("nantucket", "ocean-beach"):
            for r in yaml.safe_load((ROOT / "fixtures" / job / "ledger.yaml").read_text())["rows"]:
                data[r["id"]] = r
        pages, answers = {}, {}
        for s in tbl.pages:
            quotes = [data[a.fixture]["quote"].replace(" ... ", " ") for a in s.asks if a.fixture]
            pages[s.url] = Resp(("<p>" + "</p><p>".join(quotes or ["Nothing here."]) + "</p>").encode())
            answers[s.url] = {"answers": [
                answer(a.id, data[a.fixture]["quote"], "The page says so.") if a.fixture else answer(a.id, found=False)
                for a in s.asks]}
        client = Fake(lambda unit: answers[unit.locator], lambda unit: copy.deepcopy(answers[unit.locator]))
        fetcher = web.Fetcher(tbl.named, opener=Sites(pages), clock=lambda: "2026-10-08", resolve=False)
        with tempfile.TemporaryDirectory() as d:
            broker, res, g = webread.golden(ROOT / "fixtures" / "nantucket", Path(d) / "l.db", client, fetcher,
                                            table=tbl)
            self.assertTrue(g.ok, g.text() + "\n" + res.text())
            self.assertEqual(g.compared, 23)
            self.assertEqual(res.refused, [])
            broker.close()

    def test_golden_binds_a_live_client_and_closes_on_failure(self):
        class Refuses(Fake):
            def bind(self, broker):
                raise RuntimeError("no key")

        with tempfile.TemporaryDirectory() as d:
            ledger = Path(d) / "l.db"
            ledger.write_text("junk")
            f = web.Fetcher(set(), opener=Sites({}), resolve=False)
            with self.assertRaisesRegex(RuntimeError, "no key"):
                webread.golden(ROOT / "fixtures" / "nantucket", ledger, Refuses(), f)


if __name__ == "__main__":
    unittest.main()
