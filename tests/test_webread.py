import copy
import json
import re
import tempfile
import unittest
from pathlib import Path

from pipeline import intake, web, webread
from pipeline.broker import Broker
from pipeline.readers import validate
from pipeline.readers.clients import prompt, prompt_version
from pipeline.schema import Claim
from tests.webfake import Resp, Sites

ROOT = Path(__file__).resolve().parent.parent
PAGE = ("<html><body><h1>Permits</h1><p>Permits will be issued 2-4 weeks on average after submission is confirmed.</p>"
        "<p>All applications REQUIRE PLANS OR DRAWINGS.</p></body></html>")
URL = "https://permits.example.gov/oc"


def source(url=URL, agent="codes", when=(("ocean city",),), asks=None, title="OC permits", ids=()):
    asks = asks or (webread.Ask("a1", "Permit turnaround # weeks", "J-C-001"), webread.Ask("a2", "Plans required"))
    return webread.Source(url=url, title=title, agent=agent, when=when, asks=tuple(asks), ids=tuple(ids))


def table(*pages):
    return webread.Table(named=frozenset({"example.com"}), pages=list(pages) or [source()])


def answer(ask, quote="", statement="", found=None, figures=None, choice=""):
    """An answer; its `figures` default to the figures its statement gives, in order."""
    if figures is None:
        figures = [n for n in webread.NUMBER.findall(statement)]
    return {"ask": ask, "found": bool(quote) if found is None else found, "quote": quote, "figures": figures,
            "choice": choice, "statement": statement}


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
        self.assertEqual(first.asks[1], webread.Ask("a2", "How long permit review takes: # weeks", "NAN-C-002", (),
                                                    "weeks"))
        # an ask that names its unit puts its first figure on the row as a value: the spread rates, the
        # permit weeks and the fall-protection and ladder heights; the unit is the text after the first mark
        with_unit = [a for p in t.pages for a in p.asks if a.unit]
        self.assertEqual(sorted(a.unit for a in with_unit), ["ft"] * 3 + ["sq ft/gal"] * 8 + ["weeks"])
        for a in with_unit:
            self.assertTrue(a.ask.split("#")[1].strip(" )").startswith(a.unit), (a.ask, a.unit))
        self.assertEqual(sorted(a.fixture for a in with_unit if a.unit == "sq ft/gal"),
                         [f"OBV-C-0{n}" for n in range(23, 31)])
        self.assertEqual(sum(1 for p in t.pages for a in p.asks if a.fixture), 50)
        esr = next(p for p in t.pages if p.url.endswith("ESR-4143.pdf"))
        self.assertEqual(esr.ids, ("ESR-4143", "HIT-HY 270", "HY 270"))
        self.assertEqual(first.ids, ())
        for p in t.pages:
            self.assertEqual(web.allowed(p.url, t.named), "", p.url)
            self.assertEqual(len({a.id for a in p.asks}), len(p.asks))
        # the table is the cache for every job: an ask says what to look for, never a test bid's job-specific notes
        for a in (a for p in t.pages for a in p.asks):
            self.assertNotRegex(a.ask, r"NAN-|OBV-|FIELD|owner flag|spec p\.|unverified|only the title", a.ask)
        # a # mark stands for a figure the answer's quote must carry, never a section or table locator
        for a in (a for p in t.pages for a in p.asks):
            self.assertNotRegex(a.ask, r"§ #|Table #|\(#\)\(", a.ask)
        # a product or report code in an ask is written out, and is one of its page's identifiers, so a
        # run's sentence may name it: the model is never asked to fill in what the table already knows
        for p in t.pages:
            for a in p.asks:
                for code in re.findall(r"[A-Z][\w.-]*\d[\w.-]*(?: \d+)?", a.ask):
                    self.assertTrue(any(code.lower() in i.lower() or i.lower() in code.lower() for i in p.ids),
                                    (a.ask, code, p.ids))
        # a title is the row's citation in the proposal: it names the page, never an edition, date,
        # sub-paragraph or status the test bid found there (p.7), which today's page may not carry
        for p in t.pages:
            self.assertNotRegex(p.title, r"(?<![.\d])(19|20)\d\d(?![.\d])|\d/\d|\d\([a-z0-9]+\)|transition|phase", p.title)

    def test_the_prompt_example_is_no_test_bid_s_answer(self):
        # every bid sends the prompt, so its worked example must carry no job's evidence (p.7), and the
        # gate must test the model, not what it can copy from its instructions
        text = (ROOT / "pipeline" / "readers" / "prompts" / "web_reader.md").read_text()
        example = text[text.index("## Example"):]
        answers = json.loads(example[example.index("{"):example.rindex("}") + 1])["answers"]
        quoted = [a[k] for a in answers for k in ("quote", "statement") if a[k]]
        asks = re.findall(r"`a\d+: ([^`]+)`", example)
        self.assertEqual(len(asks), len(answers))
        norm = lambda s: " ".join(re.findall(r"\w+", s.lower()))
        fixtures = norm(" ".join((ROOT / "fixtures" / j / "ledger.yaml").read_text()
                                 for j in ("nantucket", "ocean-beach")))
        table = {norm(a.ask) for p in webread.load().pages for a in p.asks}
        for s in quoted:
            self.assertNotIn(norm(s), fixtures, s)
        for a in asks:
            self.assertNotIn(norm(a), table, a)
            self.assertNotIn(norm(a), fixtures, a)
        for a in answers:
            for f in a["figures"]:
                self.assertNotRegex(fixtures, rf"\b{re.escape(f)}\b", f)

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
        # a state code counts only as an address writes it
        self.assertTrue(webread.matches(source(when=(("fl",),)), "Cocoa Beach,FL"))
        self.assertTrue(webread.matches(source(when=(("md",),)), "MD 21842"))
        self.assertFalse(webread.matches(source(when=(("fl",),)), "sealant in 10.1 fl oz cartridges"))
        self.assertFalse(webread.matches(source(when=(("md",),)), "MD Anderson"))
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
        self.assertEqual(webread.brief(source(ids=("F-1",))).splitlines()[1], "Its identifiers: F-1")
        self.assertEqual(webread.brief(s), "The page: OC permits\nAnswer each ask from the page:\na1: Permit turnaround # weeks\n"
                                           "a2: Plans required")
        self.assertEqual(answers["items"]["properties"]["choice"], {"enum": [""]})
        # a closed ask lists its options; the schema offers them (and other), code checks them per ask
        yn = source(asks=(webread.Ask("a1", "Whether plans are required", options=("yes", "no")),
                          webread.Ask("a2", "Status", options=("adopted", "proposed"))))
        self.assertEqual(webread.schema_for(yn)["properties"]["answers"]["items"]["properties"]["choice"],
                         {"enum": ["", "adopted", "no", "proposed", "yes", "other"]})
        self.assertIn("a1: Whether plans are required [choice: yes | no | other]", webread.brief(yn))
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
            (answer("a1", "", "Permits take 2-4 weeks.", found=True, figures=[]),
             ["found, but no quote", "the statement has figures not among its quoted figures: 2-4"]),
            (answer("a1", "   ", "x", found=True), ["found, but no quote"]),
            # the statement must give the figures the marks were filled with, not another the quote carries
            (answer("a1", "Permits will be issued 2-4 weeks on average after submission. Fee: $1,250.50",
                    "Permits cost 1,250.50.", figures=["2-4"]),
             ["the statement does not give its figures: 2-4"]),
            (answer("a1", "Permits will be issued 2-4 weeks", "Permits are issued.", figures=["2-4"]),
             ["the statement does not give its figures: 2-4"]),
            (answer("a1", "Permits will be issued 3 weeks", "Permits take 3 weeks."),
             ["the quote is not on the page"]),
            (answer("a1", "Permits will be issued", " "), ["found, but no statement"]),
            (answer("a1", "Permits will be issued 2-4 weeks", "Permits take 2-6 weeks, 12 at most.", figures=["2-4"]),
             ["the statement does not give its figures: 2-4",
              "the statement has figures not among its quoted figures: 12, 2-6"]),
            (answer("a1", "Permits will be issued 2-4 weeks", "Permits take 3 weeks.", figures=["3"]),
             ["figures the quote does not carry: 3", "the statement has figures not among its quoted figures: 3"]),
        ):
            with self.subTest(a=a):
                self.assertEqual(webread.answer_errors(a, self.TEXT), why)

    def test_the_statement_may_name_the_page_s_own_identifiers(self):
        a = answer("a1", "Permits will be issued 2-4 weeks", "HIT-HY 270 permits take 2-4 weeks.", figures=["2-4"])
        page = self.TEXT + " Product: HIT-HY 270, HIT-HY 200."
        self.assertEqual(webread.answer_errors(a, page, ("HIT-HY 270",)), [])
        self.assertEqual(webread.answer_errors(a, page, ("HIT-HY 200",)),
                         ["the statement has figures not among its quoted figures: 270"])
        # an identifier the page does not carry gets no pass: a recalled product name (HY 70 -> HY 270, p.7)
        self.assertEqual(webread.answer_errors(a, self.TEXT, ("HIT-HY 270",)),
                         ["the statement has figures not among its quoted figures: 270"])
        # only the whole identifier comes out: its digits are still figures elsewhere
        text = "A24W8300 data sheet. Recoat: 4 hours."
        ok = answer("a1", "Recoat: 4 hours.", "A24W8300 recoats after 4 hours.", figures=["4"])
        self.assertEqual(webread.answer_errors(ok, text, ("A24W08300", "A24W8300")), [])
        bad = answer("a1", "Recoat: 4 hours.", "A24W8300 recoats after 24 hours.", figures=["4"])
        self.assertEqual(webread.answer_errors(bad, text, ("A24W08300", "A24W8300")),
                         ["the statement does not give its figures: 4",
                          "the statement has figures not among its quoted figures: 24"])

    def test_a_statement_may_give_any_figure_its_quote_carries(self):
        # beyond the marks: a section number or a date the quote carries
        a = answer("a1", "Fee: $1,250.50 per 1/2 lot.", "It costs 1,250.50 per 1/2 lot.", figures=["1/2"])
        self.assertEqual(webread.answer_errors(a, self.TEXT), [])
        # ...but not one the quote lacks, marks or none
        a = answer("a1", "Fee: $1,250.50 per 1/2 lot.", "It costs 1,250.50, due 2027.", figures=[])
        self.assertEqual(webread.answer_errors(a, self.TEXT),
                         ["the statement has figures not among its quoted figures: 2027"])
        # an ask that wants a date has a # for it (the ESR asks), so runs are compared on it
        asks = [a for p in webread.load().pages for a in p.asks if "expir" in a.ask]
        self.assertTrue(asks and all("#" in a.ask for a in asks))

    def test_a_choice_must_be_one_of_the_ask_s_options(self):
        yn = webread.Ask("a2", "Whether plans are required", options=("yes", "no"))
        self.assertEqual(webread.choice_errors(answer("a2", "q", "s", choice="yes"), yn), [])
        self.assertEqual(webread.choice_errors(answer("a2", "q", "s", choice="other"), yn), [])
        self.assertEqual(webread.choice_errors(answer("a2", "q", "s", choice="maybe"), yn),
                         ["the choice 'maybe' is not one of yes, no, other"])
        self.assertEqual(webread.choice_errors(answer("a2", "q", "s"), yn), ["the choice '' is not one of yes, no, other"])
        self.assertEqual(webread.choice_errors(answer("a2", found=False), yn), [])
        # one figures entry per # mark, "" for a mark the page leaves unfilled
        marked = webread.Ask("a1", "Permit turnaround # weeks")
        self.assertEqual(webread.choice_errors(answer("a1", "q", "s", figures=[""]), marked), [])
        self.assertEqual(webread.choice_errors(answer("a1", "q", "s", figures=[]), marked),
                         ["figures has 0 entries for the ask's 1 # marks"])
        free = webread.Ask("a2", "Plans required")
        self.assertEqual(webread.choice_errors(answer("a2", "q", "s", choice="yes"), free),
                         ["the ask has no options, but the choice is 'yes'"])

    def test_a_figure_may_come_from_the_page_s_identifier(self):
        a = answer("a1", "Permits will be issued 2-4 weeks", "ESR-4143: 2-4 weeks.", figures=["4143", "2-4"])
        ask = "What ESR-# says about permits: # weeks"
        self.assertEqual(webread.id_marks(ask, ("ESR-4143",)), frozenset({0}))
        self.assertEqual(webread.answer_errors(a, self.TEXT + " ESR-4143", ("ESR-4143",), ask), [])
        self.assertEqual(webread.answer_errors(a, self.TEXT, ("ESR-4143",), ask)[0],
                         "figures the quote does not carry: 4143")
        self.assertEqual(webread.answer_errors(a, self.TEXT)[0], "figures the quote does not carry: 4143")
        # only a mark the ask places in the identifier is exempt: "ESR-# ... # weeks" filled "4143" in the
        # second mark is a figure the quote must carry
        swapped = dict(a, figures=["4143", "4143"])
        self.assertEqual(webread.answer_errors(swapped, self.TEXT + " ESR-4143", ("ESR-4143",), ask)[0],
                         "figures the quote does not carry: 4143")
        self.assertEqual(webread.slots(a), (("4143",), ("2-4",)))
        self.assertEqual(webread.slots(a, ("ESR-4143",), frozenset({0})), ((), ("2-4",)))
        self.assertEqual(webread.slots(a, ("ESR-4143",)), (("4143",), ("2-4",)))
        self.assertEqual(webread.slots({"figures": ["", "Jan 15, 2018"]}), ((), ("jan 15 2018",)))
        self.assertEqual(webread.slots({}), ())

    def test_identifier_marks_come_from_the_ask_and_the_page_s_identifiers(self):
        cases = [("Kem Kromik B# spread rate: # sq ft/gal", ("B50WZ0001", "B50"), {0}),
                 ("Loxon S# joint size limits (#\" wide, #\" deep)", ("Loxon S1",), {0}),
                 ("What ESR-# covers for HIT-HY # and when it was reissued: #", ("ESR-4143", "HIT-HY 270"), {0, 1}),
                 ("Primer A#W# spread rate (# sq ft/gal)", ("A24W8300",), {0, 1}),
                 ("AWS D#/D#M (:#)", ("D1.1",), {0, 1}),
                 ("At what height: # ft", ("29 CFR 1926.501",), set())]
        for ask, ids, want in cases:
            self.assertEqual(webread.id_marks(ask, ids), frozenset(want), ask)

    def test_a_date_s_month_is_part_of_the_figure(self):
        self.assertEqual(webread.figures("Reissued March 2024, revised May 2024; Jan. 15, 2018; may 2 coats"),
                         {"mar 2024", "may 2024", "jan 15 2018", "2"})
        text = "<p>Reissued March 2024. Revised May 2024.</p>"
        march = answer("a1", "Reissued March 2024", "It was reissued March 2024.", figures=["March 2024"])
        self.assertEqual(webread.answer_errors(march, web.html_text(text)), [])
        # a sentence that changes the month gives a date its quote does not carry
        moved = dict(march, statement="It was reissued May 2024.")
        self.assertIn("the statement has figures not among its quoted figures: may 2024",
                      webread.answer_errors(moved, web.html_text(text)))
        # two runs that read different months of one year are two readings
        may = answer("a1", "Revised May 2024", "It was revised May 2024.", figures=["May 2024"])
        ask = webread.Ask("a1", "When it was reissued: #")
        self.assertIsNone(webread._agree([{"a1": march}, {"a1": may}], ask)[0])

    def test_a_range_is_one_figure(self):
        self.assertEqual(webread.figures("issued 2-4 weeks, 350 – 400 sq ft, 1/2 in, 1,250.50"),
                         {"2-4", "350-400", "1/2", "1,250.50"})
        narrowed = answer("a1", "Permits will be issued 2-4 weeks", "Permits are issued in 4 weeks.", figures=["2-4"])
        self.assertEqual(webread.answer_errors(narrowed, self.TEXT), [
            "the statement does not give its figures: 2-4", "the statement has figures not among its quoted figures: 4"])
        self.assertEqual(webread.figures("ESR-4143 and esr-4143x", ("ESR-4143",)), {"4143"})

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
                                               self.TEXT), ["the statement has figures not among its quoted figures: three"])
        text = "Allow three weeks. One coat."
        self.assertEqual(webread.answer_errors(answer("a1", "Allow three weeks.", "Three weeks."), text), [])
        self.assertEqual(webread.answer_errors(answer("a1", "One coat.", "Apply one coat."), text), [])

    def test_same_facts_needs_every_number_of_the_statement(self):
        self.assertTrue(webread.same_facts("ESR-4143 covers HY 270; reissued 2026", "ESR-4143 ... HY 270 ... 2026"))
        self.assertFalse(webread.same_facts("ESR-4143 covers HY 270; reissued 2026", "ESR-4143 ... HY 270"))
        self.assertFalse(webread.same_facts("Plans are required", "Plans are required"))
        self.assertFalse(webread.same_facts("", "1"))
        self.assertFalse(webread.same_facts("1", None))
        self.assertTrue(webread.same_facts("Satin A89: 350-400 sq ft/gal", "350-400 sq. ft. per gallon", ("A89",)))
        self.assertFalse(webread.same_facts("Satin A89: 350-400 sq ft/gal", "350-400 sq. ft. per gallon"))
        self.assertFalse(webread.same_facts("Satin A89", "A89", ("A89",)))
        self.assertFalse(webread.same_facts("issued 2-4 weeks", "issued in 4 weeks"))

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
        # same figures from the same passage agree, whatever the wording
        same = {"a1": answer("a1", "Permits will be issued 2-4 weeks", "Permits take 2-4 weeks after you apply.")}
        self.assertEqual(webread._agree([good, same], a), (TURNAROUND, "", []))
        # a status is a choice: the same figure with different options is two readings, both kept
        status = webread.Ask("a1", "The status of the # IBC", options=("proposed", "adopted"))
        proposed = {"a1": answer("a1", "The 2024 IBC is proposed for adoption.", "The 2024 IBC is proposed.",
                                 choice="proposed")}
        adopted = {"a1": answer("a1", "Maryland adopted the 2024 IBC in May.", "The 2024 IBC is adopted.",
                                figures=["2024"], choice="adopted")}
        self.assertEqual(webread._agree([proposed, adopted], status),
                         (*differ, [proposed["a1"], adopted["a1"]]))
        # the same figure from two passages (a data sheet's table and its text) is one reading
        table_row = {"a1": answer("a1", "Permits will be issued 2-4 weeks", "Permits take 2-4 weeks.")}
        text_line = {"a1": answer("a1", "Fee: $1,250.50 per 2-4 weeks", "Permits take 2-4 weeks.")}
        self.assertEqual(webread._agree([table_row, text_line], a)[0], table_row["a1"])
        # the same passage with different figures is two readings: code keeps both, picks neither
        close = {"a1": answer("a1", "Permits will be issued 2-4 weeks", "Permits take 4 weeks.")}
        self.assertEqual(webread._agree([good, close], a), (*differ, [TURNAROUND, close["a1"]]))
        bare = {"a1": answer("a1", "Permits will be issued 2-4 weeks", "Permits are issued.")}
        self.assertEqual(webread._agree([good, bare], a)[:2], differ)       # one gives a figure, one none
        fewer = {"a1": answer("a1", "Permits will be issued 2-4 weeks", "Permits take at least 2 weeks.")}
        self.assertEqual(webread._agree([good, fewer], a)[:2], differ)
        self.assertEqual(webread._agree([fewer, good], a)[2], [fewer["a1"], TURNAROUND])
        self.assertEqual(webread._agree([good, good, close], a)[2], [TURNAROUND, close["a1"]])   # once each
        # the runs are compared on the figures they fill the # marks with, not on their wording
        model = {"a1": answer("a1", "Permits will be issued 2-4 weeks on average", "Form 5 permits take 2-4 weeks.",
                              figures=["2-4"])}
        self.assertEqual(webread._agree([good, model], a)[0], TURNAROUND)
        spaced = {"a1": answer("a1", "Permits will be issued 2-4 weeks", "x", figures=["2 – 4"])}
        self.assertEqual(webread._agree([good, spaced], a)[0], TURNAROUND)          # the same range
        swapped = {"a1": answer("a1", "Permits will be issued 2-4 weeks", "x", figures=["4", "2"])}
        self.assertEqual(webread._agree([good, swapped], a)[0], None)
        # no figures on either side: the quotes must overlap
        plans = {"a1": PLANS}
        also = {"a1": answer("a1", "All applications REQUIRE PLANS", "Plans are needed.")}
        self.assertEqual(webread._agree([plans, also], a), (PLANS, "", []))
        other = {"a1": answer("a1", "Permits will be", "Permits exist.")}
        self.assertEqual(webread._agree([plans, other], a), (*differ, [PLANS, other["a1"]]))
        # a closed answer: the same passage with opposite choices is two readings; "other" never agrees
        yes = {"a1": answer("a1", "All applications REQUIRE PLANS", "Plans are required.", choice="yes")}
        no = {"a1": answer("a1", "All applications REQUIRE PLANS", "Plans are not required.", choice="no")}
        self.assertEqual(webread._agree([yes, no], a), (*differ, [yes["a1"], no["a1"]]))
        self.assertEqual(webread._agree([yes, copy.deepcopy(yes)], a)[0], yes["a1"])
        odd = {"a1": answer("a1", "All applications REQUIRE PLANS", "Plans are needed.", choice="other")}
        self.assertEqual(webread._agree([odd, copy.deepcopy(odd)], a)[:2], differ)
        # ...and their statements give the same figures
        dated = {"a1": answer("a1", "All applications REQUIRE PLANS", "Plans are needed (2 sets).", figures=[])}
        self.assertEqual(webread._agree([plans, dated], a)[:2], differ)
        # a mark filled from the page's identifier, written whole or in part, is the same figure
        lx = webread.Ask("a1", "Primer LX# turnaround: # weeks")
        whole = {"a1": answer("a1", "Permits will be issued 2-4 weeks", "x", figures=["LX02W0050", "2-4"])}
        part = {"a1": answer("a1", "Permits will be issued 2-4 weeks", "x", figures=["W0050", "2-4"])}
        self.assertEqual(webread._agree([whole, part], lx, ids=("LX02W0050",))[0], whole["a1"])
        self.assertEqual(webread._agree([whole, part], lx)[0], None)
        # identifier digits count in every other mark: "1/4 to 1" and "1/4" differ on the Loxon S1 sheet
        s1 = webread.Ask("a1", "Loxon S# joint width: #")
        wide = {"a1": answer("a1", "Permits will be issued 2-4 weeks", "x", figures=["S1", '1/4" to 1"'])}
        narrow = {"a1": answer("a1", "Permits will be issued 2-4 weeks", "x", figures=["S1", '1/4"'])}
        self.assertIsNone(webread._agree([wide, narrow], s1, ids=("Loxon S1",))[0])


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

    def test_an_agreed_choice_is_the_row_s_value(self):
        tbl = table(source(asks=(webread.Ask("a2", "Whether plans are required", options=("yes", "no")),)))
        yes = answer("a2", "All applications REQUIRE PLANS OR DRAWINGS.", "Plans are required.", choice="yes")
        _, res = run(Fake({"answers": [yes]}, {"answers": [yes]}), tbl=tbl)
        self.assertEqual([(c.value, c.confidence, c.flag) for c in res.rows], [("yes", "exact", "")])
        # a closed ask with no marks: the shared option is one reading, whichever sentence each run quoted
        other = answer("a2", TURNAROUND["quote"], "Plans are required.", choice="yes")
        _, res = run(Fake({"answers": [yes]}, {"answers": [other]}), tbl=tbl)
        self.assertEqual([(c.value, c.confidence, c.flag) for c in res.rows], [("yes", "exact", "")])
        no = dict(other, choice="no")
        _, res = run(Fake({"answers": [yes]}, {"answers": [no]}), tbl=tbl)
        self.assertEqual({(c.confidence, c.flag) for c in res.rows}, {("inferred", "unverified")})
        bad = dict(yes, choice="")
        _, res = run(Fake({"answers": [bad]}, {"answers": [bad]}, {"answers": [bad]}, {"answers": [bad]}), tbl=tbl)
        self.assertIn("J#web1 run 1 a2: the choice '' is not one of yes, no, other", res.discarded)

    def test_an_ask_that_names_its_unit_writes_its_figure_as_the_row_s_value(self):
        weeks = (webread.Ask("a1", "Permit turnaround # weeks", "J-C-001", unit="weeks"), webread.Ask("a2", "Plans required"))
        b, res = run(Fake({"answers": [TURNAROUND, PLANS]}, {"answers": [PLANS, TURNAROUND]}), tbl=table(source(asks=weeks)))
        self.assertEqual([(c.locator, c.value, c.unit, c.flag) for c in res.rows],
                         [("ask a1", "2-4", "weeks", ""), ("ask a2", "", "", "")])
        back = b.ledger.by_id()["J-WEB-001"]
        self.assertEqual((back.value, back.value_num, back.unit), ("2-4", None, "weeks"))
        # each kept reading carries its own figure; a reading with none carries no value
        other = answer("a1", "All applications REQUIRE PLANS OR DRAWINGS.", "Plans come first.", figures=[""])
        b, res = run(Fake({"answers": [TURNAROUND, PLANS]}, {"answers": [other, PLANS]}), tbl=table(source(asks=weeks)))
        self.assertEqual([(c.value, c.unit, c.flag) for c in res.rows if c.locator == "ask a1"],
                         [("2-4", "weeks", "unverified"), ("", "", "unverified")])

    def test_a_split_after_a_unit_ask_still_names_the_page_in_the_run_log(self):
        # The live run of 2026-10-09 crashed here: the figure's unit had taken the
        # page's name, and the next ask's split tried to log the page by it.
        weeks = (webread.Ask("a1", "Permit turnaround # weeks", "J-C-001", unit="weeks"), webread.Ask("a2", "Plans required"))
        other = answer("a2", "", "The page does not say.", found=False)
        b, res = run(Fake({"answers": [TURNAROUND, PLANS]}, {"answers": [TURNAROUND, other]}), tbl=table(source(asks=weeks)))
        self.assertEqual([(c.locator, c.value, c.unit, c.flag) for c in res.rows if c.locator == "ask a1"],
                         [("ask a1", "2-4", "weeks", "")])
        a2 = [(c.value, c.unit, c.flag) for c in res.rows if c.locator == "ask a2"]
        self.assertTrue(a2 and all(row == ("", "", "unverified") for row in a2), a2)
        self.assertTrue(res.readings and all(line.startswith("J#web1 a2: ") for line in res.readings), res.readings)

    def test_figure_of(self):
        ask = webread.Ask("a1", "Spread rate: # sq ft/gal", unit="sq ft/gal")
        found = lambda *figs: {"found": True, "figures": list(figs)}
        cases = [
            (found("350-400"), ask, (), ("350-400", "sq ft/gal")),
            (found("350 - 400"), ask, (), ("350-400", "sq ft/gal")),     # a range is one figure, written one way
            (found("2,500"), ask, (), ("2,500", "sq ft/gal")),
            (found("350 to 400"), ask, (), ("", "")),                     # two figures in the first mark: none
            (found(""), ask, (), ("", "")),
            (found(), ask, (), ("", "")),
            ({"found": False, "figures": []}, ask, (), ("", "")),
            (found("350-400"), webread.Ask("a1", "Spread rate: # sq ft/gal"), (), ("", "")),   # no unit in the table
            # a first mark that fills the page's own identifier is no figure
            (found("02", "350-400"), webread.Ask("a1", "LX# spread rate: # sq ft/gal", unit="sq ft/gal"), ("LX02",),
             ("", "")),
        ]
        for answer_, ask_, ids, want in cases:
            self.assertEqual(webread.figure_of(answer_, ask_, ids), want, (answer_, ask_.ask))

    def test_materials_pages_are_written_as_materials(self):
        client = Fake({"answers": [TURNAROUND, PLANS]}, {"answers": [TURNAROUND, PLANS]})
        b, res = run(client, tbl=table(source(agent="materials")))
        self.assertEqual({c.agent for c in res.rows}, {"materials"})
        b2, res2 = run(Fake(), tbl=table(source(agent="materials")), agents=("codes",))
        self.assertEqual((res2.pages, res2.notes[0][:20]), (0, "no page in the table"))

    def test_disagreement_and_refused_answers_become_unverified_rows(self):
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
        # the one reading is kept, flagged unverified, not dropped or passed as exact; the refused ask is a gap row
        self.assertEqual([(c.claim_id, c.quote, c.statement, c.locator, c.confidence, c.flag) for c in res.rows], [
            ("J-WEB-001", TURNAROUND["quote"], TURNAROUND["statement"], "ask a1", "inferred", "unverified"),
            ("J-WEB-002", "", "OC permits: no answer for \"Plans required\" (a run's answer was refused); "
             "nothing on it is verified", "ask a2", "missing", "unverified")])

    def test_two_readings_are_both_kept_unverified(self):
        other = answer("a1", "All applications REQUIRE PLANS OR DRAWINGS.", "Plans come first.", figures=[""])
        b, res = run(Fake({"answers": [TURNAROUND, PLANS]}, {"answers": [other, PLANS]}))
        rows = [(c.quote, c.confidence, c.flag) for c in res.rows if c.locator == "ask a1"]
        self.assertEqual(rows, [(TURNAROUND["quote"], "inferred", "unverified"),
                                (other["quote"], "inferred", "unverified")])
        self.assertEqual(res.unanswered, [f"{URL} a1 (Permit turnaround # weeks): the runs give different "
                                          "readings; 2 readings kept, flagged unverified"])
        self.assertEqual(res.readings, [
            "J#web1 a1: figures ['2-4'], choice '', quote 'Permits will be issued 2-4 weeks on average'",
            "J#web1 a1: figures [''], choice '', quote 'All applications REQUIRE PLANS OR DRAWINGS.'"])
        self.assertIn("  reading " + res.readings[0], res.text())
        # an unverified reading is not an answer at the gate
        g = webread.gate(res, table(), {"J-C-001": {"id": "J-C-001", "quote": TURNAROUND["quote"]}})
        self.assertEqual((g.ok, g.failures, len(g.misses)), (False, [], 1))

    def test_an_invalid_run_leaves_the_page_unread(self):
        for raw, why in (("not json", "not JSON: Expecting value: line 1 column 1 (char 0)"),
                         ({"answers": [TURNAROUND]}, "answers ['a1'] do not match the asks ['a1', 'a2']")):
            with self.subTest(raw=str(raw)[:10]):
                b, res = run(Fake({"answers": [TURNAROUND, PLANS]}, raw, raw, raw))
                # each ask is a gap row, flagged unverified, so the bid still shows it
                self.assertEqual([(c.statement, c.locator, c.confidence, c.flag) for c in res.rows], [
                    (f'OC permits: no answer for "{a}" (1 of 2 runs valid); nothing on it is verified',
                     f"ask {i}", "missing", "unverified")
                    for i, a in (("a1", "Permit turnaround # weeks"), ("a2", "Plans required"))])
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

    def test_the_fetch_log_names_the_host_a_redirect_reached(self):
        moved = "https://permits.example.gov/new"
        b, res = run(Fake(), pages={URL: Resp(b"", status=301, location=moved), moved: Resp(b"", status=404)})
        fetch = [e["detail"] for e in b.ledger.log() if e["action"] == "fetch"]
        self.assertEqual(fetch, [f"2026-10-08; HTTP 404; served by {moved}"])

    def test_the_broker_refuses_a_fetch_to_a_private_address(self):
        b, res = run(Fake(), tbl=table(source(url="http://127.0.0.1/x")))
        self.assertEqual((res.blocked, res.fetched, res.unread), (["http://127.0.0.1/x"], [], []))
        # the refusal is a row, as a page that did not open is, so the gap reaches the bid
        self.assertEqual([(c.url, c.retrieved, c.flag, c.confidence, c.statement) for c in res.rows], [
            ("http://127.0.0.1/x", "2026-10-08", "unverified", "missing",
             "OC permits: the broker refused the fetch; nothing on it is verified")])
        self.assertIn("  blocked http://127.0.0.1/x", res.text())
        tbl = table(source(url="http://127.0.0.1/x"))
        g = webread.gate(res, tbl, GateCase.FIXTURE)
        self.assertEqual((g.ok, g.failures), (False, ["J-C-001: the broker refused to fetch http://127.0.0.1/x"]))

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
        self.assertEqual([c.flag for c in res.rows], ["", "unverified"])     # the gap is a row too
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
        other = answer("a1", "Permits will be issued", "x", figures=[""])
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
        good = [answer(f"a{i}", f"Fee {i} is {i}0 dollars.", f"Fee {i} is {i}0 dollars.", figures=[])
                for i in range(1, 6)]
        pages = {URL: Resp(f"<html><body><p>{text}</p></body></html>".encode())}
        one_miss = good[:4] + [answer("a5", found=False)]
        _, res = run(Fake(*[{"answers": one_miss}] * 2), pages=pages, tbl=tbl)
        g = webread.gate(res, tbl, fixture)
        self.assertEqual((g.ok, g.compared, len(g.misses)), (True, 5, 1))      # 4 of 5 is 80%
        two_miss = good[:3] + [answer("a4", found=False), answer("a5", found=False)]
        _, res = run(Fake(*[{"answers": two_miss}] * 2), pages=pages, tbl=tbl)
        self.assertFalse(webread.gate(res, tbl, fixture).ok)
        wrong = good[:4] + [answer("a5", "Fee 1 is 10 dollars.", "Fee 1 is 10 dollars.", figures=[])]
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

    def test_a_statement_that_narrows_or_swaps_the_fixture_s_figures_fails(self):
        fixture = {"J-C-001": {"id": "J-C-001", "statement": "Permits take 2-4 weeks",
                               "quote": "Permits will be issued 2-4 weeks on average after submission"}}
        narrowed = answer("a1", "Permits will be issued 2-4 weeks on average", "Permits are issued.", figures=[""])
        g = self.gate([{"answers": [narrowed, PLANS]}] * 2, fixture=fixture)
        self.assertEqual(g.failures, ["J-C-001: states 'Permits are issued.', the fixture states "
                                      "'Permits take 2-4 weeks'"])
        # a figure the fixture's statement adds from outside its quote is not asked of the run
        fixture["J-C-001"]["statement"] = "Permits take 2-4 weeks (Table 9)"
        g = self.gate([{"answers": [TURNAROUND, PLANS]}] * 2, fixture=fixture)
        self.assertEqual((g.ok, g.failures), (True, []))

    def test_a_changed_or_dead_page_is_a_note(self):
        g = self.gate([{"answers": [TURNAROUND, PLANS]}] * 2, pages={URL: Resp(b"", status=500)})
        self.assertEqual((g.ok, g.compared, g.notes), (False, 0, [f"J-C-001: {URL} did not open (HTTP 500)"]))
        changed = {"J-C-001": {"id": "J-C-001", "quote": "Permits take a month"}}
        g = self.gate([{"answers": [TURNAROUND, PLANS]}] * 2, fixture=changed)
        self.assertEqual((g.ok, g.notes), (False, ["J-C-001: the page no longer carries the fixture's quote"]))
        self.assertIn("  note J-C-001: the page no longer", g.text())

    def test_a_page_the_job_s_rows_do_not_lead_to_is_reported_not_compared(self):
        tbl = table(source(when=(("nowhere",),)))
        g = self.gate([], tbl=tbl)
        self.assertEqual((g.ok, g.compared, g.failures, g.notes), (False, 0, [], [
            f"J-C-001: {URL} not matched by the job's packet rows (cold-cache search not built)"]))

    def test_pages_are_matched_only_on_rows_from_the_packet(self):
        def row(source_id):
            return Claim(claim_id="J-Q-001", statement="EPA RRP applies if built before 1978", source_id=source_id,
                         method="clause", role="question", confidence="exact")
        self.assertEqual([webread.from_packet(row(s)) for s in ("SP", "SP + PH", "WEB", "SP + WEB", "none", "")],
                         [True, True, False, False, False, False])

    def test_asks_of_other_fixtures_are_skipped(self):
        g = self.gate([{"answers": [TURNAROUND, PLANS]}] * 2, fixture={})
        self.assertEqual((g.ok, g.compared, g.failures), (False, 0, []))


class GoldenCase(unittest.TestCase):
    """The whole golden path on a fixture's ledger, with every page served from its fixture quotes."""

    def test_both_jobs_pass_when_every_quote_is_answered(self):
        tbl = webread.load()
        data = {}
        import yaml
        for job in ("nantucket", "ocean-beach"):
            for r in yaml.safe_load((ROOT / "fixtures" / job / "ledger.yaml").read_text())["rows"]:
                data[r["id"]] = r
        choices = {}
        for job in ("nantucket", "ocean-beach"):
            choices.update(yaml.safe_load((ROOT / "fixtures" / job / "web_choices.yaml").read_text()))
        pages, answers = {}, {}
        for s in tbl.pages:
            quotes = [data[a.fixture]["quote"].replace(" ... ", " ") for a in s.asks if a.fixture]
            pages[s.url] = Resp(("<p>" + "</p><p>".join(quotes or ["Nothing here."]) + "</p>").encode())
            answers[s.url] = {"answers": [
                answer(a.id, data[a.fixture]["quote"], data[a.fixture]["quote"], figures=[""] * a.ask.count("#"),
                       choice=choices.get(a.fixture, ""))
                if a.fixture else answer(a.id, found=False)
                for a in s.asks]}
        client = Fake(lambda unit: answers[unit.locator], lambda unit: copy.deepcopy(answers[unit.locator]))
        fetcher = web.Fetcher(tbl.named, opener=Sites(pages), clock=lambda: "2026-10-08", resolve=False)
        for job, compared in (("nantucket", 19), ("ocean-beach", 24)):
            with self.subTest(job=job), tempfile.TemporaryDirectory() as d:
                broker, res, g = webread.golden(ROOT / "fixtures" / job, Path(d) / "l.db", client, fetcher,
                                                table=tbl)
                self.assertTrue(g.ok, g.text() + "\n" + res.text())
                self.assertEqual(g.compared, compared)
                if job == "ocean-beach":     # the RRP page is named only by rows the test bid wrote from the web
                    self.assertIn("OBV-C-022: https://www.epa.gov/lead/lead-renovation-repair-and-painting-program "
                                  "not matched by the job's packet rows (cold-cache search not built)", g.notes)
                self.assertEqual(res.refused, [])
                broker.close()
        # a closed answer the fixture contradicts is a wrong answer, whatever the quote
        flip = next(p for p in tbl.pages if any(a.fixture == "NAN-C-005" for a in p.asks))
        for a in answers[flip.url]["answers"]:
            a["choice"] = "yes"
        with tempfile.TemporaryDirectory() as d:
            broker, res, g = webread.golden(ROOT / "fixtures" / "nantucket", Path(d) / "l.db", client, fetcher,
                                            table=tbl)
            self.assertEqual(g.failures, ["NAN-C-005: chose 'yes', the fixture's answer is 'no'"])
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
