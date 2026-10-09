"""Scope Writer: a layout the model selects, checked and rendered by code."""
import copy
import json
import tempfile
import unittest
from pathlib import Path

import yaml

from pipeline import fixtures, proposal, scope_writer as sw
from pipeline.auditor import Orphan
from pipeline.broker import Broker
from pipeline.readers import validate
from pipeline.readers.clients import prompt, prompt_version
from pipeline.schema import Claim

ROOT = Path(__file__).resolve().parent.parent
JOBS = {"nantucket": "NAN", "ocean-beach": "OBV"}
PHRASES = sw.load_phrases(fixtures.PHRASE_LIBRARY)


def row(cid, role, division="", part="", method="clause", statement="", value="", value_num=None, unit=""):
    return Claim(claim_id=cid, statement=statement or f"statement of {cid}", source_id="S-1", method=method,
                 role=role, confidence="missing" if method == "FIELD" else "exact", division=division, part=part, value=value,
                 value_num=value_num, unit=unit, tag="S-1 X")


CLAIMS = [
    row("J-H-001", "header", statement="Deck repair, Harbor House"),
    row("J-H-002", "header", statement="Bay Road, Lewes DE"),
    row("J-F-001", "header", method="FIELD", statement="Client: confirm"),
    row("J-S-001", "scope", "07"),
    row("J-S-002", "scope", "07"),
    row("J-S-003", "scope", "09", "alternate"),
    row("J-S-004", "scope", "09"),
    row("J-S-005", "scope", "09", "base"),
    row("J-A-001", "allowance", "07", "base", method="FIELD"),
    row("J-A-002", "allowance", "09", "alternate", method="FIELD"),
    row("J-Q-001", "quantity", method="counted"),
    row("J-X-001", "exclusion"),
    row("J-M-001", "material"),
]
BY_ID = {c.claim_id: c for c in CLAIMS}


def item(ref, kind="row"):
    return {"kind": kind, "ref": ref}


def task(items, allowance=(), close="", title="Work"):
    return {"title": title, "items": items, "allowance": list(allowance), "close": close}


def layout(sections=None, exclusions=("excl_mold",), terms=sw.TERMS_PHRASES, header=None):
    return {
        "header": header or {"project": "J-H-001", "address": "J-H-002", "client": "J-F-001"},
        "sections": sections if sections is not None else [
            {"division": "07", "tasks": [task([item("J-S-001"), item("seal_tool", "phrase"), item("J-S-002")],
                                              ["J-A-001"], "concealed_preparation", "Deck Joint Sealants")]},
            {"division": "09", "tasks": [task([item("J-S-004"), item("J-S-005"), item("J-Q-001")])]},
            {"division": "ALT", "tasks": [task([item("J-S-003")], ["J-A-002"], title="Stair Recoat")]},
        ],
        "exclusion_phrases": list(exclusions),
        "terms": list(terms),
    }


class Fake:
    def __init__(self, answers, model_id="fake-model"):
        self.answers, self.model_id, self.calls = answers, model_id, []

    def complete(self, reader, unit, system, schema, run):
        self.calls.append((reader, unit, system, schema, run))
        return self.answers[run]


class FixtureCase(unittest.TestCase):
    """Both golden ledgers, loaded once."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.loaded = {}
        for job in JOBS:
            broker, data = fixtures.load(ROOT / "fixtures" / job, Path(cls.tmp.name) / job / "l.db")
            cls.loaded[job] = (broker, data, sw.current(broker))

    @classmethod
    def tearDownClass(cls):
        for broker, _, _ in cls.loaded.values():
            broker.close()
        cls.tmp.cleanup()


class RenderCase(FixtureCase):
    def test_the_fixture_layout_renders_the_committed_proposal_from_the_ledger(self):
        # The Scope Writer and tools/build_fixture.py share one renderer: a ledger
        # loaded through the broker gives the same bytes as the hand-made YAML.
        for job, (broker, data, claims) in self.loaded.items():
            with self.subTest(job=job):
                text, xref = sw.render(sw.from_fixture_layout(data["proposal"]), claims, PHRASES, broker)
                self.assertEqual(text, (ROOT / "fixtures" / job / "proposal.md").read_text())
                self.assertEqual(proposal.xref_csv(xref), (ROOT / "fixtures" / job / "xref.csv").read_text())

    def test_the_fixture_layout_round_trips_through_the_model_shape(self):
        for job, (_, data, claims) in self.loaded.items():
            with self.subTest(job=job):
                model = sw.from_fixture_layout(data["proposal"])
                self.assertEqual(sw.to_fixture_layout(model), data["proposal"])
                self.assertEqual(validate.errors(model, sw.SCHEMA), [])
                self.assertEqual(sw.layout_errors(model, {c.claim_id: c for c in claims}, PHRASES), [])
                self.assertEqual(sw.unplaced(model, claims), [])

    def test_a_missing_status_line_gets_the_default(self):
        broker, data, claims = self.loaded["nantucket"]
        saved = broker.ledger.meta("status_line")
        broker.ledger.set_meta(status_line="")
        try:
            text, _ = sw.render(sw.from_fixture_layout(data["proposal"]), claims, PHRASES, broker)
        finally:
            broker.ledger.set_meta(status_line=saved)
        self.assertIn("\n_Draft rendered from the claim ledger. Not priced. Not released: only the estimator releases a bid._\n",
                      text)

    def test_row_of_maps_every_field_the_renderer_reads(self):
        c = Claim(claim_id="X-1", statement="s", source_id="S-1", method="counted", role="quantity",
                  confidence="exact", value="1200", value_num=1200.0, unit="SF", locator="p.2", tag="T",
                  flag="unverified", question="X-OQ-1", url="https://x", retrieved="2026-10-08", quote="q",
                  division="09", part="base")
        self.assertEqual(proposal.row_of(c), {
            "id": "X-1", "statement": "s", "source": "S-1", "locator": "p.2", "tag": "T", "method": "counted",
            "role": "quantity", "flag": "unverified", "question": "X-OQ-1", "value": 1200, "unit": "SF",
            "url": "https://x", "retrieved": "2026-10-08", "quote": "q", "division": "09", "part": "base",
        })
        self.assertEqual(proposal.fmt_value(proposal.row_of(c)["value"]), "1,200")

    def test_row_of_keeps_a_fraction_and_maps_no_value_to_none(self):
        frac = row("X-2", "quantity", value="2.5", value_num=2.5)
        self.assertEqual(proposal.row_of(frac)["value"], "2.5")
        self.assertIsNone(proposal.row_of(row("X-3", "scope"))["value"])
        odd = row("X-4", "quantity", value="about 6")
        self.assertEqual(proposal.row_of(odd)["value"], "about 6")


class UnitCase(unittest.TestCase):
    def test_the_unit_lists_rows_without_figures_and_the_selectable_phrases(self):
        claims = [row("J-S-001", "scope", "07", "base", statement="Remove   failed\nsealant"),
                  row("J-Q-001", "quantity", method="counted", statement="Spaces", value="5", value_num=5.0)]
        phrases = {"greeting": {"text": "Hello"}, "seal_tool": {"text": "Tool it."}, "allowance": {"text": "A"}}
        unit = sw.unit_for("JOB", claims, phrases)
        self.assertEqual(unit.unit_id, "JOB#scope")
        self.assertEqual(unit.locator, "ledger rows and phrase library")
        self.assertEqual(unit.source_id, "")
        self.assertEqual(unit.text,
                         "ROWS (ID | role | division | part | method | statement)\n"
                         "J-S-001 | scope | 07 | base | clause | Remove failed sealant\n"
                         "J-Q-001 | quantity | - | - | counted | Spaces\n\n"
                         "PHRASES (key | text)\n"
                         "seal_tool | Tool it.")

    def test_long_text_is_cut_at_160_characters(self):
        self.assertEqual(sw._short("a" * 160), "a" * 160)
        self.assertEqual(sw._short("a" * 161), "a" * 157 + "...")
        self.assertEqual(sw._short("ab cd", 4), "a...")

    def test_reserved_phrases_are_not_offered(self):
        offered = sw.selectable_phrases(PHRASES)
        for key in sw.RESERVED_PHRASES:
            self.assertNotIn(key, offered)
        self.assertIn("gc_site", offered)
        self.assertEqual(len(offered), len(PHRASES) - len(sw.RESERVED_PHRASES))


class LayoutErrorsCase(unittest.TestCase):
    def errs(self, lay):
        return sw.layout_errors(lay, BY_ID, PHRASES)

    def test_a_good_layout_has_no_errors(self):
        self.assertEqual(self.errs(layout()), [])
        self.assertEqual(validate.errors(layout(), sw.SCHEMA), [])

    def test_header_slots_must_be_header_rows(self):
        lay = layout(header={"project": "J-S-001", "address": "nope", "client": "J-F-001"})
        self.assertEqual(self.errs(lay), ["header project 'J-S-001' is not a header row",
                                          "header address 'nope' is not a header row"])

    def test_a_section_may_appear_once(self):
        lay = layout(sections=[{"division": "07", "tasks": [task([item("J-S-001")])]},
                               {"division": "07", "tasks": [task([item("J-S-002")], ["J-A-001"])]},
                               {"division": "09", "tasks": [task([item("J-S-004"), item("J-S-005")])]},
                               {"division": "ALT", "tasks": [task([item("J-S-003")], ["J-A-002"])]}])
        self.assertEqual(self.errs(lay), ["section 07 appears twice"])

    def test_an_item_may_appear_once_in_a_task(self):
        lay = layout()
        lay["sections"][0]["tasks"][0]["items"].append(item("J-S-001"))
        self.assertEqual(self.errs(lay), ["07 task 1 lists the same item twice"])

    def test_the_same_row_may_sit_in_two_sections(self):
        lay = layout()
        lay["sections"][2]["tasks"][0]["items"].append(item("J-S-004"))
        self.assertEqual(self.errs(lay), [])

    def test_only_scope_phrases_go_in_a_task(self):
        for key in ("nope", "greeting", "allowance", "warranty", "excl_mold", "concealed_preparation"):
            with self.subTest(key=key):
                lay = layout()
                lay["sections"][0]["tasks"][0]["items"].append(item(key, "phrase"))
                self.assertEqual(self.errs(lay), [f"07 task 1: {key!r} is not a scope phrase in the library"])

    def test_items_must_be_ledger_rows_of_an_item_role(self):
        lay = layout()
        lay["sections"][1]["tasks"][0]["items"] += [item("J-NOPE"), item("J-X-001"), item("J-M-001"),
                                                     item("J-A-001")]
        self.assertEqual(self.errs(lay), [
            "09 task 1: 'J-NOPE' is not a ledger row",
            "09 task 1: J-X-001 is a exclusion row, which has its own section",
            "09 task 1: J-M-001 is a material row, which has its own section",
            "09 task 1: J-A-001 is a allowance row, which has its own section",
        ])

    def test_a_row_goes_in_its_own_division(self):
        lay = layout()
        lay["sections"][1]["tasks"][0]["items"].append(item("J-S-001"))
        self.assertEqual(self.errs(lay), ["09 task 1: J-S-001 belongs to division 07"])

    def test_a_row_with_no_division_goes_anywhere(self):
        lay = layout()
        for sec in lay["sections"]:
            sec["tasks"][0]["items"].append(item("J-Q-001")) if sec["division"] != "09" else None
        self.assertEqual(self.errs(lay), [])

    def test_alternate_work_goes_under_alternates_only(self):
        lay = layout()
        lay["sections"][1]["tasks"][0]["items"].append(item("J-S-003"))
        lay["sections"][1]["tasks"][0]["allowance"].append("J-A-002")
        self.assertEqual(self.errs(lay), [
            "09 task 1: J-S-003 is alternate work and goes under Alternates",
            "09 task 1: J-A-002 is alternate work and goes under Alternates",
        ])

    def test_base_work_never_goes_under_alternates(self):
        lay = layout()
        lay["sections"][2]["tasks"][0]["items"].append(item("J-S-005"))
        lay["sections"][2]["tasks"][0]["allowance"].append("J-A-001")
        self.assertEqual(self.errs(lay), [
            "ALT task 1: J-S-005 is base-bid work, not an alternate",
            "ALT task 1: J-A-001 is base-bid work, not an alternate",
        ])

    def test_a_row_with_no_part_may_go_under_alternates_from_any_division(self):
        lay = layout()
        lay["sections"][2]["tasks"][0]["items"].append(item("J-S-001"))
        self.assertEqual(self.errs(lay), [])

    def test_allowances_must_be_allowance_rows_once_each_in_their_division(self):
        lay = layout()
        lay["sections"][0]["tasks"][0]["allowance"] += ["J-S-001", "J-NOPE", "J-A-001"]
        self.assertEqual(self.errs(lay), [
            "07 task 1: 'J-S-001' is not an allowance row",
            "07 task 1: 'J-NOPE' is not an allowance row",
            "07 task 1 lists the same allowance twice",
        ])
        lay = layout()
        lay["sections"][1]["tasks"][0]["allowance"].append("J-A-001")
        self.assertEqual(self.errs(lay), ["09 task 1: J-A-001 belongs to division 07"])

    def test_a_close_must_be_a_concealed_conditions_phrase(self):
        for close in ("excl_mold", "concealed_nope"):
            with self.subTest(close=close):
                lay = layout()
                lay["sections"][0]["tasks"][0]["close"] = close
                self.assertEqual(self.errs(lay),
                                 [f"07 task 1: close {close!r} is not a concealed-conditions phrase"])

    def test_an_empty_task_is_refused_but_an_allowance_alone_is_a_task(self):
        lay = layout()
        lay["sections"][1]["tasks"].append(task([]))
        self.assertEqual(self.errs(lay), ["09 task 2 is empty"])
        lay = layout()
        lay["sections"][0]["tasks"][0]["items"] = []
        lay["sections"][1]["tasks"][0]["items"] += [item("J-S-001")] * 0
        errs = self.errs(lay)
        self.assertNotIn("07 task 1 is empty", errs)

    def test_exclusions_and_terms_come_from_their_own_phrases_once_each(self):
        lay = layout(exclusions=("excl_mold", "gc_site", "excl_nope", "excl_mold"),
                     terms=("warranty", "excl_mold", "warranty"))
        self.assertEqual(self.errs(lay), [
            "exclusion 'gc_site' is not an exclusion phrase in the library",
            "exclusion 'excl_nope' is not an exclusion phrase in the library",
            "terms 'excl_mold' is not a terms phrase",
            "an exclusion phrase is listed twice",
            "an terms phrase is listed twice",
        ])

    def test_the_schema_refuses_a_title_with_a_figure(self):
        lay = layout()
        lay["sections"][0]["tasks"][0]["title"] = "Seal 40 joints"
        self.assertEqual(validate.errors(lay, sw.SCHEMA), [
            "$.sections[0].tasks[0].title: 'Seal 40 joints' does not match " + sw.TITLE_PATTERN])

    def test_the_schema_refuses_an_unknown_section(self):
        lay = layout()
        lay["sections"][0]["division"] = "08"
        self.assertEqual(len(validate.errors(lay, sw.SCHEMA)), 1)


class ShapeCase(unittest.TestCase):
    def test_to_fixture_layout_orders_numbers_and_decorates_sections(self):
        lay = layout(sections=[
            {"division": "ALT", "tasks": [task([item("J-S-003")], ["J-A-002"], title=" Stair Recoat "),
                                          task([item("J-S-001")], title="Joints")]},
            {"division": "09", "tasks": [task([item("J-S-004")], title="")]},
            {"division": "01", "tasks": [task([item("gc_site", "phrase")], title="")]},
            {"division": "07", "tasks": [task([item("J-S-001")]), task([item("J-S-002")], ["J-A-001"],
                                                                        "concealed_preparation", "Sealants")]},
        ])
        self.assertEqual(sw.to_fixture_layout(lay), {
            "header": {"project": "J-H-001", "address": "J-H-002", "client": "J-F-001"},
            "sections": [
                {"title": "Division 01 General Conditions", "numbered": True,
                 "tasks": [{"items": [{"phrase": "gc_site"}]}]},
                {"title": "Division 07 Thermal & Moisture Protection",
                 "tasks": [{"title": "7.1 Work", "items": [{"row": "J-S-001"}]},
                           {"title": "7.2 Sealants", "items": [{"row": "J-S-002"}], "allowance": ["J-A-001"],
                            "close": "concealed_preparation"}]},
                {"title": "Division 09 Finishes", "tasks": [{"items": [{"row": "J-S-004"}]}]},
                {"title": "Alternates", "intro_phrase": "alternates_intro",
                 "tasks": [{"title": "A.1 Stair Recoat", "items": [{"row": "J-S-003"}], "allowance": ["J-A-002"]},
                           {"title": "A.2 Joints", "items": [{"row": "J-S-001"}]}]},
            ],
            "exclusion_phrases": ["excl_mold"],
            "terms": list(sw.TERMS_PHRASES),
        })

    def test_from_fixture_layout_strips_task_numbers_and_fills_defaults(self):
        fixture = {"header": {"project": "P", "address": "A", "client": "C"},
                   "sections": [{"title": "Alternates", "intro_phrase": "alternates_intro",
                                 "tasks": [{"title": "A.12 Doors", "items": [{"row": "R"}, {"phrase": "k"}]}]},
                                {"title": "Division 02 Site Construction",
                                 "tasks": [{"title": "2.1 Wash", "items": [], "allowance": ["X"], "close": "c"}]},
                                {"title": "Division 01 General Conditions"}]}
        self.assertEqual(sw.from_fixture_layout(fixture), {
            "header": {"project": "P", "address": "A", "client": "C"},
            "sections": [
                {"division": "ALT", "tasks": [{"title": "Doors", "items": [{"kind": "row", "ref": "R"},
                                                                           {"kind": "phrase", "ref": "k"}],
                                               "allowance": [], "close": ""}]},
                {"division": "02", "tasks": [{"title": "Wash", "items": [], "allowance": ["X"], "close": "c"}]},
                {"division": "01", "tasks": []},
            ],
            "exclusion_phrases": [], "terms": [],
        })

    def test_canonical_ignores_order_and_titles(self):
        a = layout()
        b = copy.deepcopy(a)
        b["sections"].reverse()
        b["sections"][0]["tasks"][0]["title"] = "Other"
        b["sections"][2]["tasks"][0]["items"].reverse()
        b["exclusion_phrases"].reverse()
        self.assertEqual(sw.canonical(a), sw.canonical(b))
        self.assertEqual(sw.agree([a, b]), (a, []))

    def test_canonical_holds_every_placement(self):
        self.assertEqual(sw.canonical(layout()), (
            ("J-H-001", "J-H-002", "J-F-001"),
            frozenset({("07", "row", "J-S-001"), ("07", "phrase", "seal_tool"), ("07", "row", "J-S-002"),
                       ("09", "row", "J-S-004"), ("09", "row", "J-S-005"), ("09", "row", "J-Q-001"),
                       ("ALT", "row", "J-S-003")}),
            frozenset({("07", "J-A-001"), ("ALT", "J-A-002")}),
            frozenset({("07", "concealed_preparation")}),
            frozenset({"excl_mold"}),
            frozenset(sw.TERMS_PHRASES),
        ))

    def test_schema_for_narrows_every_reference_to_the_job(self):
        claims = CLAIMS + [row("J-X-001", "exclusion"), row("J-H-009", "header")]
        phrases = {"seal_tool": {"text": "t"}, "greeting": {"text": "g"}, "warranty": {"text": "w"},
                   "excl_mold": {"text": "m"}, "concealed_preparation": {"text": "c"}}
        sch = sw.schema_for(claims, phrases)
        props = sch["properties"]
        task = props["sections"]["items"]["properties"]["tasks"]["items"]["properties"]
        self.assertEqual(props["header"]["properties"]["client"], {"enum": ["J-F-001", "J-H-001", "J-H-002", "J-H-009", ""]})
        self.assertEqual(props["header"]["properties"]["project"], props["header"]["properties"]["address"])
        self.assertEqual(task["items"]["items"]["properties"]["ref"],
                         {"enum": ["J-Q-001", "J-S-001", "J-S-002", "J-S-003", "J-S-004", "J-S-005", "seal_tool"]})
        self.assertEqual(task["allowance"]["items"], {"enum": ["J-A-001", "J-A-002"]})
        self.assertEqual(task["close"], {"enum": ["concealed_preparation", ""]})
        self.assertEqual(props["exclusion_phrases"]["items"], {"enum": ["excl_mold"]})
        self.assertEqual(props["terms"]["items"], {"enum": list(sw.TERMS_PHRASES)})
        self.assertEqual(task["title"], sw.SCHEMA["properties"]["sections"]["items"]["properties"]["tasks"]["items"]
                         ["properties"]["title"])
        self.assertNotEqual(sch, sw.SCHEMA)
        self.assertEqual(sw.SCHEMA["properties"]["terms"]["items"]["pattern"], sw.ID_PATTERN)   # left untouched
        self.assertEqual(validate.errors(layout(), sw.schema_for(CLAIMS, PHRASES)), [])
        bare = sw.schema_for([row("J-S-001", "scope")], {})
        bare_task = bare["properties"]["sections"]["items"]["properties"]["tasks"]["items"]["properties"]
        self.assertEqual(bare_task["allowance"]["items"], {"type": "string", "maxLength": 0})
        self.assertEqual(bare["properties"]["exclusion_phrases"]["items"], {"type": "string", "maxLength": 0})
        self.assertEqual(validate.errors({"header": {"project": "", "address": "", "client": ""}, "sections": [],
                                          "exclusion_phrases": [""], "terms": []}, bare), [])

    def test_agree_keeps_only_what_every_run_places(self):
        a, b = layout(), layout(exclusions=("excl_mold", "excl_mep"), terms=("warranty", "costs"))
        b["header"] = {"project": "J-H-002", "address": "J-H-002", "client": ""}
        b["sections"][1]["tasks"][0]["items"].pop()
        b["sections"][0]["tasks"][0]["close"] = ""
        b["sections"][0]["tasks"][0]["allowance"] = []
        b["sections"][2]["tasks"][0]["items"] = [item("J-S-005")]
        want = layout(terms=("costs", "warranty"), header={"project": "", "address": "J-H-002", "client": ""})
        want["sections"][1]["tasks"][0]["items"].pop()
        want["sections"][0]["tasks"][0]["close"] = ""
        want["sections"][0]["tasks"][0]["allowance"] = []
        want["sections"][2]["tasks"][0]["items"] = []
        self.assertEqual(sw.agree([a, b]), (want, [
            "header project (the runs name ['J-H-001', 'J-H-002'])",
            "header client (the runs name ['', 'J-F-001'])",
            "07 allowance J-A-001 (not in every run)",
            "07 close concealed_preparation (not in every run)",
            "09 row J-Q-001 (not in every run)",
            "ALT row J-S-003 (not in every run)",
            "terms change_orders (not in every run)",
            "terms allowance_definition (not in every run)",
        ]))

    def test_agree_drops_empty_tasks_and_sections_and_checks_every_run(self):
        a = layout()
        b, c = copy.deepcopy(a), copy.deepcopy(a)
        c["sections"] = c["sections"][:2]
        c["exclusion_phrases"] = []
        agreed, dropped = sw.agree([a, b, c])
        self.assertEqual([s["division"] for s in agreed["sections"]], ["07", "09"])
        self.assertEqual(agreed["exclusion_phrases"], [])
        self.assertEqual(dropped, ["ALT row J-S-003 (not in every run)", "ALT allowance J-A-002 (not in every run)",
                                   "exclusion excl_mold (not in every run)"])
        self.assertEqual(sw.agree([a]), (a, []))

    def test_unplaced_lists_scope_and_allowance_rows_left_out(self):
        lay = layout()
        lay["sections"][0]["tasks"][0]["items"].pop()
        lay["sections"][2]["tasks"][0]["allowance"] = []
        self.assertEqual(sw.unplaced(lay, CLAIMS), ["J-S-002 (scope, division 07)",
                                                    "J-A-002 (allowance, division 09)"])
        self.assertEqual(sw.unplaced(layout(), CLAIMS + [row("J-S-009", "scope")]), ["J-S-009 (scope, division -)"])

    def test_sections_of_maps_rows_to_sections(self):
        lay = layout()
        lay["sections"][2]["tasks"][0]["items"].append(item("J-S-004"))
        self.assertEqual(sw.sections_of(lay), {
            "J-S-001": {"07"}, "J-S-002": {"07"}, "J-A-001": {"07"}, "J-S-004": {"09", "ALT"},
            "J-S-005": {"09"}, "J-Q-001": {"09"}, "J-S-003": {"ALT"}, "J-A-002": {"ALT"},
        })


class RunBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.broker = Broker.open_job(Path(self.tmp.name) / "l.db", "intake", job="J", run_id="J-RUN", create=True,
                                      clock=lambda: "2026-10-08T00:00:00Z")
        self.addCleanup(self.broker.close)
        self.broker.write_register([{"source_id": "S-1", "file": "s1.pdf", "sha256": "0" * 64, "kind": "drawing",
                                     "title": "Sheet S-1", "status": "present"}])
        self.broker.ledger.set_meta(status_line="Draft.")
        writer = self.broker.as_principal("spec_reader")
        takeoff = self.broker.as_principal("takeoff")
        for c in CLAIMS:
            (takeoff if c.method == "FIELD" else writer if c.method == "clause" else
             self.broker.as_principal("drawing_reader")).append(copy.deepcopy(c))

    def run_with(self, answers, repeats=2):
        client = Fake(answers)
        return client, sw.run(self.broker, "J", client, fixtures.PHRASE_LIBRARY, repeats=repeats)

    def calls(self):
        return [(e["subject"], e["detail"]) for e in self.broker.ledger.log() if e["action"] == "model-call"]


class RunCase(RunBase):
    def test_two_agreeing_runs_render_the_proposal(self):
        client, res = self.run_with([layout(), json.dumps(layout())])
        self.assertEqual((res.units, res.calls, res.discarded, res.unread), (1, 2, [], []))
        self.assertEqual(res.layout, sw.to_fixture_layout(layout()))
        self.assertEqual(res.unplaced, [])
        self.assertEqual(res.orphans, [])
        self.assertTrue(res.ok)
        self.assertIn("### 7.1 Deck Joint Sealants\n", res.text)
        self.assertIn("- statement of J-S-001 (S-1 X)\n", res.text)
        self.assertIn("_(Contractor Co. allowance of FIELD for statement of J-A-001 (S-1 X). ", res.text)
        self.assertEqual(res.text, sw.render(layout(), sw.current(self.broker), PHRASES, self.broker)[0])
        self.assertEqual([c[0] for c in client.calls], ["scope_writer", "scope_writer"])
        self.assertEqual([c[4] for c in client.calls], [0, 1])
        self.assertEqual(client.calls[0][3], sw.schema_for(sw.current(self.broker), PHRASES))
        self.assertEqual(client.calls[0][2], prompt("scope_writer"))
        self.assertEqual(client.calls[0][1], sw.unit_for("J", sw.current(self.broker), PHRASES))
        self.assertEqual(self.calls(), [("J#scope", f"fake-model; {prompt_version('scope_writer')}; run 1; valid"),
                                        ("J#scope", f"fake-model; {prompt_version('scope_writer')}; run 2; valid")])
        self.assertEqual(res.report(),
                         "scope_writer: 1 units, 2 calls, layout agreed, 0 runs discarded, 0 placements dropped, "
                         "0 rows unplaced, 0 orphan figures")

    def test_the_scope_writer_writes_no_row(self):
        before = len(self.broker.ledger.claims())
        self.run_with([layout(), layout()])
        self.assertEqual(len(self.broker.ledger.claims()), before)

    def test_a_broken_run_leaves_nothing_rendered(self):
        bad = layout()
        bad["sections"][0]["tasks"][0]["items"].append(item("J-NOPE"))
        cases = [
            ("not json", "J#scope run 2: not JSON: Expecting value: line 1 column 1 (char 0)"),
            ({"header": {}}, "J#scope run 2: $: missing sections; $: missing exclusion_phrases; "
                             "$: missing terms"),
            (bad, "J#scope run 2: 07 task 1: 'J-NOPE' is not a ledger row"),
        ]
        for answer, why in cases:
            with self.subTest(answer=str(answer)[:20]):
                _, res = self.run_with([layout(), answer])
                self.assertEqual(res.discarded, [why])
                self.assertEqual(res.unread, ["J#scope: 1 of 2 runs valid"])
                self.assertIsNone(res.layout)
                self.assertEqual(res.text, "")
                self.assertFalse(res.ok)
                self.assertEqual(self.calls()[-1][1].split("; ", 2)[2], "run 2; discarded: " + why.split(": ", 1)[1].split("; ")[0])

    def test_runs_that_disagree_render_what_they_agree_on(self):
        other = layout(exclusions=("excl_mep",))
        other["sections"][1]["tasks"][0]["items"].pop(0)
        _, res = self.run_with([layout(), other])
        want = layout(exclusions=())
        want["sections"][1]["tasks"][0]["items"].pop(0)
        self.assertEqual(res.layout, sw.to_fixture_layout(want))
        self.assertEqual(res.unread, [])
        self.assertEqual(res.dropped, ["09 row J-S-004 (not in every run)", "exclusion excl_mold (not in every run)"])
        self.assertEqual(res.unplaced, ["J-S-004 (scope, division 09)"])
        self.assertEqual(res.report().splitlines()[:3], [
            "scope_writer: 1 units, 2 calls, layout agreed, 0 runs discarded, 2 placements dropped, "
            "1 rows unplaced, 0 orphan figures",
            "  dropped 09 row J-S-004 (not in every run)",
            "  dropped exclusion excl_mold (not in every run)"])

    def test_runs_that_share_no_placement_leave_nothing_rendered(self):
        one = layout(sections=[{"division": "07", "tasks": [task([item("J-S-001")])]}])
        two = layout(sections=[{"division": "07", "tasks": [task([item("J-S-002")])]}])
        _, res = self.run_with([one, two])
        self.assertIsNone(res.layout)
        self.assertEqual(res.text, "")
        self.assertEqual(res.unread, ["J#scope: the runs agree on no placement"])
        self.assertFalse(res.ok)

    def test_one_run_is_enough_when_one_is_asked_for(self):
        client, res = self.run_with([layout()], repeats=1)
        self.assertEqual(len(client.calls), 1)
        self.assertIsNotNone(res.layout)

    def test_a_third_run_must_agree_too(self):
        _, res = self.run_with([layout(), layout(), layout(exclusions=())], repeats=3)
        self.assertEqual(res.layout["exclusion_phrases"], [])
        self.assertEqual(res.dropped, ["exclusion excl_mold (not in every run)"])

    def test_left_out_rows_are_reported(self):
        lay = layout()
        lay["sections"][1]["tasks"][0]["items"].pop(0)
        _, res = self.run_with([lay, lay])
        self.assertEqual(res.unplaced, ["J-S-004 (scope, division 09)"])
        self.assertIn("  unplaced J-S-004 (scope, division 09)", res.report())

    def test_a_figure_with_no_row_is_an_orphan(self):
        claims = sw.current(self.broker)
        res = sw.ScopeResult(layout={}, text="Seal 47 joints\n")
        res.orphans = sw.audit_proposal(res.text, claims, "")
        self.assertEqual([o.figure for o in res.orphans], ["47"])
        self.assertFalse(res.ok)
        self.assertEqual(res.report().splitlines()[-1], "  orphan 47 (line 1): …Seal [47] joints…")

    def test_report_lists_discards_and_unread(self):
        res = sw.ScopeResult(units=1, calls=2, discarded=["d"], unread=["u"], dropped=["x"])
        self.assertEqual(res.report(), "scope_writer: 1 units, 2 calls, no layout, 1 runs discarded, "
                                       "1 placements dropped, 0 rows unplaced, 0 orphan figures\n  discarded d\n  unread u\n  dropped x")

    def test_write_saves_the_proposal_and_xref_only_when_rendered(self):
        _, res = self.run_with([layout(), layout()])
        out = Path(self.tmp.name) / "out"
        sw.write(res, out)
        self.assertEqual((out / "proposal.md").read_text(), res.text)
        self.assertEqual((out / "xref.csv").read_text(), proposal.xref_csv(res.xref))
        empty = Path(self.tmp.name) / "empty"
        sw.write(sw.ScopeResult(), empty)
        self.assertTrue(empty.is_dir())
        self.assertEqual(list(empty.iterdir()), [])

    def test_a_superseded_row_is_not_shown(self):
        field = self.broker.as_principal("field_crew")
        field.append(Claim(claim_id="J-Q-002", statement="measured", source_id="S-1", method="dimensioned",
                           role="quantity", confidence="exact", value="5", value_num=5.0, unit="in",
                           supersedes="J-Q-001"))
        ids = [c.claim_id for c in sw.current(self.broker)]
        self.assertNotIn("J-Q-001", ids)
        self.assertIn("J-Q-002", ids)


class GateBase(FixtureCase):
    def result(self, job, model_layout):
        broker, data, claims = self.loaded[job]
        res = sw.ScopeResult(units=1, calls=2, layout=sw.to_fixture_layout(model_layout))
        return res, data, claims


class GateCase(GateBase):
    def test_the_fixture_layout_passes(self):
        for job, (_, data, claims) in self.loaded.items():
            with self.subTest(job=job):
                res, data, claims = self.result(job, sw.from_fixture_layout(data["proposal"]))
                g = sw.gate(res, data["proposal"], claims)
                self.assertEqual((g.ok, g.failures, g.notes), (True, [], []))
                self.assertEqual(g.text(), "scope writer gate: PASS")

    def test_no_layout_fails_with_the_reasons(self):
        _, data, claims = self.loaded["nantucket"]
        g = sw.gate(sw.ScopeResult(unread=["u"], discarded=["d"]), data["proposal"], claims)
        self.assertEqual((g.ok, g.failures, g.notes), (False, ["no layout was agreed", "u", "d"], []))
        self.assertEqual(g.text(), "scope writer gate: FAIL\n  FAIL no layout was agreed\n  FAIL u\n  FAIL d")

    def test_header_and_misplaced_or_missing_rows_fail(self):
        _, data, _ = self.loaded["ocean-beach"]
        lay = sw.from_fixture_layout(data["proposal"])
        lay["header"]["client"] = "OBV-H-001"
        alt = next(s for s in lay["sections"] if s["division"] == "ALT")
        soffit = next(t for t in alt["tasks"] if {"kind": "row", "ref": "OBV-OB-005"} in t["items"])
        soffit["items"].remove({"kind": "row", "ref": "OBV-OB-005"})
        div09 = next(s for s in lay["sections"] if s["division"] == "09")
        div09["tasks"][0]["items"].append({"kind": "row", "ref": "OBV-OB-005"})     # its own division: allowed
        div02 = next(s for s in lay["sections"] if s["division"] == "02")
        div02["tasks"][0]["items"].remove({"kind": "row", "ref": "OBV-SP-020"})
        alt["tasks"][0]["items"].append({"kind": "row", "ref": "OBV-SP-021"})          # a base row moved to ALT
        res, data, claims = self.result("ocean-beach", lay)
        g = sw.gate(res, data["proposal"], claims)
        self.assertFalse(g.ok)
        self.assertEqual(g.failures, [
            "header {'project': 'OBV-H-001', 'address': 'OBV-H-002', 'client': 'OBV-H-001'} != "
            "{'project': 'OBV-H-001', 'address': 'OBV-H-002', 'client': 'OBV-F-001'}",
            "OBV-SP-020 is not placed (the fixture has it in ['02'])",
            "OBV-SP-021 is in ['02', 'ALT']; the fixture allows ['02']",
        ])
        self.assertEqual(g.notes, [])

    def test_an_alternate_row_may_not_fall_back_to_its_division(self):
        _, data, _ = self.loaded["ocean-beach"]
        lay = sw.from_fixture_layout(data["proposal"])
        div09 = next(s for s in lay["sections"] if s["division"] == "09")
        div09["tasks"][0]["items"].append({"kind": "row", "ref": "OBV-AL-001"})
        res, data, claims = self.result("ocean-beach", lay)
        self.assertEqual(sw.gate(res, data["proposal"], claims).failures,
                         ["OBV-AL-001 is in ['09', 'ALT']; the fixture allows ['ALT']"])

    def test_choices_the_fixture_does_not_fix_are_notes(self):
        _, data, _ = self.loaded["nantucket"]
        lay = sw.from_fixture_layout(data["proposal"])
        div05 = next(s for s in lay["sections"] if s["division"] == "05")
        div05["tasks"][0]["items"].remove({"kind": "row", "ref": "NAN-Q-002"})
        div05["tasks"][0]["items"].append({"kind": "phrase", "ref": "gc_site"})
        div05["tasks"][0]["close"] = "concealed_reinforcement"
        lay["exclusion_phrases"].append("excl_permits")
        lay["terms"].remove("warranty")
        res, data, claims = self.result("nantucket", lay)
        g = sw.gate(res, data["proposal"], claims)
        self.assertTrue(g.ok)
        self.assertEqual(g.notes, [
            "NAN-Q-002 in [], fixture ['05']",
            "phrases only in the run [('05', 'phrase', 'gc_site')]; only in the fixture []",
            "closes only in the run [('05', 'concealed_reinforcement')]; only in the fixture []",
            "exclusions only in the run ['excl_permits']; only in the fixture []",
            "terms only in the run []; only in the fixture ['warranty']",
        ])
        self.assertIn("\n  note NAN-Q-002 in [], fixture ['05']", g.text())

    def test_an_orphan_figure_fails(self):
        _, data, claims = self.loaded["nantucket"]
        res, data, claims = self.result("nantucket", sw.from_fixture_layout(data["proposal"]))
        res.orphans = [Orphan("47", 3, "x 47")]
        self.assertEqual(sw.gate(res, data["proposal"], claims).failures, ["orphan figure 47 (line 3)"])


class GoldenCase(unittest.TestCase):
    def test_replay_passes_on_both_jobs(self):
        with tempfile.TemporaryDirectory() as d:
            for job, code in JOBS.items():
                with self.subTest(job=job):
                    broker, res, g = sw.golden(ROOT / "fixtures" / job, Path(d) / job / "l.db")
                    try:
                        self.assertTrue(g.ok, g.text())
                        self.assertEqual(res.text, (ROOT / "fixtures" / job / "proposal.md").read_text())
                        log = [e for e in broker.ledger.log() if e["action"] == "model-call"]
                        self.assertEqual([e["subject"] for e in log], [f"{code}#scope"] * 2)
                        self.assertTrue(log[0]["detail"].startswith("replay (recorded expected output); "))
                    finally:
                        broker.close()

    def test_an_old_ledger_file_is_replaced(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "l.db"
            db.write_text("junk")
            broker, _, g = sw.golden(ROOT / "fixtures" / "nantucket", db)
            broker.close()
            self.assertTrue(g.ok)

    def test_a_client_that_cannot_bind_closes_the_ledger(self):
        class Refused(Exception):
            pass

        class NoKey(Fake):
            def bind(self, broker):
                self.broker = broker
                raise Refused("no key")

        client = NoKey([])
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(Refused):
                sw.golden(ROOT / "fixtures" / "nantucket", Path(d) / "l.db", client)
        with self.assertRaises(Exception):
            client.broker.ledger.claims()

    def test_a_bound_client_is_used(self):
        rec = json.loads((ROOT / "fixtures" / "nantucket" / "recordings" / "scope_writer.json").read_text())

        class Bound(Fake):
            def bind(self, broker):
                self.bound = True

        client = Bound(rec["units"][0]["runs"], model_id="bound-model")
        with tempfile.TemporaryDirectory() as d:
            broker, res, g = sw.golden(ROOT / "fixtures" / "nantucket", Path(d) / "l.db", client)
            broker.close()
        self.assertTrue(client.bound)
        self.assertEqual(len(client.calls), 2)
        self.assertTrue(g.ok)

    def test_the_recordings_are_the_fixture_layouts(self):
        for job, code in JOBS.items():
            with self.subTest(job=job):
                rec = json.loads((ROOT / "fixtures" / job / "recordings" / "scope_writer.json").read_text())
                data = yaml.safe_load((ROOT / "fixtures" / job / "ledger.yaml").read_text())
                self.assertEqual([u["unit_id"] for u in rec["units"]], [f"{code}#scope"])
                self.assertEqual(rec["units"][0]["runs"], [sw.from_fixture_layout(data["proposal"])] * 2)


class PromptCase(unittest.TestCase):
    def test_the_prompt_example_is_a_valid_layout_for_its_rows(self):
        text = prompt("scope_writer")
        self.assertTrue(text.startswith("# Scope Writer\n"))
        rows_block = text.split("Rows:\n\n```\n", 1)[1].split("```", 1)[0]
        claims = []
        for line in rows_block.strip().splitlines():
            cid, role, div, part, method, statement = [p.strip() for p in line.split("|")]
            claims.append(row(cid, role, "" if div == "-" else div, "" if part == "-" else part, method,
                              statement))
        example = json.loads(text.split("```json\n", 1)[1].split("```", 1)[0])
        self.assertEqual(validate.errors(example, sw.SCHEMA), [])
        self.assertEqual(sw.layout_errors(example, {c.claim_id: c for c in claims}, PHRASES), [])
        self.assertEqual(sw.unplaced(example, claims), [])

    def test_every_division_title_is_one_the_fixtures_use(self):
        used = set()
        for job in JOBS:
            data = yaml.safe_load((ROOT / "fixtures" / job / "ledger.yaml").read_text())
            used |= {s["title"] for s in data["proposal"]["sections"]}
        self.assertEqual(used - set(sw.SECTION_TITLES.values()), set())
        self.assertEqual(sw.SECTION_KEY["Alternates"], "ALT")
        self.assertEqual(sw.NUMBERED, ("01",))
        self.assertEqual(sw.SECTION_INTRO, {"ALT": "alternates_intro"})


class RendererCase(unittest.TestCase):
    """The shared renderer on a small layout, pinned line for line."""

    DATA = {
        "status_line": "S.", "codes_note": "",
        "rows": [
            {"id": "H1", "statement": "Job", "source": "S-1", "method": "clause", "role": "header"},
            {"id": "H2", "statement": "Addr", "source": "S-1", "locator": "tb", "method": "clause", "role": "header"},
            {"id": "C1", "statement": "Acme", "source": "S-1", "tag": "S-1 TB", "method": "clause", "role": "header"},
            {"id": "R1", "statement": "Do work", "source": "S-1", "method": "clause", "role": "scope"},
            {"id": "A1", "statement": "joints.", "source": "S-1", "method": "counted", "role": "allowance",
             "value": 12},
            {"id": "M1", "statement": "Sealant", "source": "S-1", "method": "counted", "role": "material", "value": 3},
            {"id": "K1", "statement": "Code a", "source": "W", "method": "fetched", "role": "code",
             "url": "https://a", "retrieved": "2026-10-08", "quote": "x|y\nz"},
            {"id": "K2", "statement": "Code b", "source": "W", "tag": "T", "method": "fetched", "role": "code",
             "url": "https://b", "retrieved": "2026-10-08"},
            {"id": "K3", "statement": "Code c", "source": "W", "locator": "p.3", "method": "fetched", "role": "code"},
        ],
        "proposal": {"header": {"project": "H1", "address": "H2", "client": "C1"},
                     "sections": [{"title": "Division 05 Metals"},
                                  {"title": "Division 07 Thermal & Moisture Protection",
                                   "tasks": [{"allowance": ["A1"]}, {"items": [{"row": "R1"}]}]}]},
    }
    PHRASES = {"license_line": {"text": "LIC", "loc": "p.1"}, "contractor_block": {"text": "M\nN", "loc": "p.1"},
               "greeting": {"text": "Hello {client}.", "loc": "p.1"},
               "allowance": {"text": "Contractor Co. allowance of {qty} {unit} for {what}.", "loc": "p.2"}}
    REGISTER = {"S-1": {"title": "Sheet", "status": "present"}, "W": {"title": "Web", "status": "missing"}}

    def test_a_sparse_layout_renders_exactly(self):
        by_id = {r["id"]: r for r in self.DATA["rows"]}
        text, xref = proposal.render(self.DATA, by_id, self.PHRASES, self.REGISTER)
        self.assertEqual(text, (
            "# Job\n\n_S._\n\nLIC\n\nJob Address:  \nAddr (S-1 tb)\n\nM  \nN\n\nHello Acme.\n\n"
            "**Scope of Work:**\n\n## Division 05 Metals\n\n## Division 07 Thermal & Moisture Protection\n\n\n"
            "_(Contractor Co. allowance of 12 for joints (S-1).)_\n\n- Do work (S-1)\n\n## Exclusions\n\n\n"
            "## Open questions for the estimator before pricing\n\nNothing below is resolved in this bid. Where two readings "
            "exist both are shown and the figure stays unverified.\n\n\n## Field takeoff (FIELD rows)\n\n"
            "Not in any source document. Each must be measured or answered before the allowance is priced.\n\n\n"
            "## Materials (no pricing)\n\nOrder quantities are printed only where the ledger has a dimensioned or "
            "counted value; spares and stock-length rounding are Contractor Co. purchasing decisions and are not in the "
            "sources.\n\n| Claim | Material | Qty | Unit | Source | Method |\n|---|---|---|---|---|---|\n"
            "| M1 | Sealant | 3 |  | S-1 | counted |\n\n## Codes, permits and standards\n\nEvery row was fetched on "
            "the date shown. Rows marked unverified did not open or did not show the quoted text and must not be "
            "relied on until re-fetched. \n\n| Claim | What applies | Evidence | Source |\n|---|---|---|---|\n"
            "| K1 | Code a | “x\\|y z” | [link](https://a) (2026-10-08) |\n"
            "| K2 | Code b | — | [T](https://b) (2026-10-08) |\n| K3 | Code c | — | W p.3 |\n\n## Terms\n\n---\n\n"
            "_Source tags_  \n_S-1_ = Sheet  \n_W_ = Web — **missing**\n"))
        self.assertEqual([(x["para"], x["section"], x["claims"], x["phrases"]) for x in xref], [
            (1, "header", "", "license_line"), (2, "header", "H2", ""), (3, "header", "", "contractor_block"),
            (4, "header", "C1", "greeting"),
            (5, "Division 07 Thermal & Moisture Protection", "A1", "allowance"),
            (6, "Division 07 Thermal & Moisture Protection", "R1", ""),
            (7, "Materials", "M1", ""), (8, "Codes", "K1", ""), (9, "Codes", "K2", ""), (10, "Codes", "K3", ""),
        ])

    def test_fmt_value(self):
        self.assertEqual(proposal.fmt_value(None), "FIELD")
        self.assertEqual(proposal.fmt_value(2.0), "2")
        self.assertEqual(proposal.fmt_value(2.5), "2.5")
        self.assertEqual(proposal.fmt_value(1200), "1,200")
        self.assertEqual(proposal.fmt_value(1200.0), "1,200")
        self.assertEqual(proposal.fmt_value("3/4"), "3/4")

    def test_tag_falls_back_to_source_and_locator(self):
        self.assertEqual(proposal.tag({"tag": "T", "source": "S"}), "T")
        self.assertEqual(proposal.tag({"source": "S", "locator": "p.2"}), "S p.2")
        self.assertEqual(proposal.tag({"source": "S"}), "S")
        self.assertEqual(proposal.tag({"source": "S", "tag": "", "locator": ""}), "S")


class MoreRunCase(RunBase):
    def test_the_unit_joins_phrases_one_per_line_and_has_no_tag(self):
        unit = sw.unit_for("J", CLAIMS[:1], {"a": {"text": "A"}, "b": {"text": "B"}})
        self.assertTrue(unit.text.endswith("PHRASES (key | text)\na | A\nb | B"))
        self.assertEqual(unit.tag, "")

    def test_calls_are_logged_as_the_scope_writer(self):
        self.run_with([layout(), layout()])
        principals = {e["principal"] for e in self.broker.ledger.log() if e["action"] == "model-call"}
        self.assertEqual(principals, {"scope_writer"})

    def test_write_makes_missing_folders_and_overwrites(self):
        _, res = self.run_with([layout(), layout()])
        out = Path(self.tmp.name) / "a" / "b"
        sw.write(res, out)
        sw.write(res, out)
        self.assertEqual((out / "proposal.md").read_text(), res.text)


class MoreGateCase(GateBase):
    def test_a_missing_allowance_fails_and_is_not_a_note(self):
        _, data, _ = self.loaded["ocean-beach"]
        lay = sw.from_fixture_layout(data["proposal"])
        div02 = next(s for s in lay["sections"] if s["division"] == "02")
        div02["tasks"][0]["allowance"] = []
        res, data, claims = self.result("ocean-beach", lay)
        g = sw.gate(res, data["proposal"], claims)
        self.assertEqual((g.failures, g.notes), (["OBV-A-001 is not placed (the fixture has it in ['02'])"], []))

    def test_a_scope_row_the_fixture_never_places_must_still_be_placed(self):
        _, data, claims = self.loaded["nantucket"]
        res, data, claims = self.result("nantucket", sw.from_fixture_layout(data["proposal"]))
        extra = row("NAN-S-999", "scope", "05")
        self.assertEqual(sw.gate(res, data["proposal"], claims + [extra]).failures,
                         ["NAN-S-999 is not placed (the fixture has it in [])"])
        lay = sw.from_fixture_layout(data["proposal"])
        next(s for s in lay["sections"] if s["division"] == "05")["tasks"][0]["items"].append(
            {"kind": "row", "ref": "NAN-S-999"})
        res.layout = sw.to_fixture_layout(lay)
        self.assertEqual(sw.gate(res, data["proposal"], claims + [extra]).failures, [])

    def test_a_row_only_the_run_places_is_a_note(self):
        _, data, _ = self.loaded["nantucket"]
        lay = sw.from_fixture_layout(data["proposal"])
        next(s for s in lay["sections"] if s["division"] == "05")["tasks"][0]["items"].append(
            {"kind": "row", "ref": "NAN-D-004"})
        res, data, claims = self.result("nantucket", lay)
        self.assertEqual(sw.gate(res, data["proposal"], claims).notes, ["NAN-D-004 in ['05'], fixture []"])


class MoreGoldenCase(unittest.TestCase):
    def test_golden_binds_the_client_to_the_job_ledger_and_passes_repeats(self):
        rec = json.loads((ROOT / "fixtures" / "nantucket" / "recordings" / "scope_writer.json").read_text())

        class Bound(Fake):
            def bind(self, broker):
                self.bound = broker

        client = Bound(rec["units"][0]["runs"])
        with tempfile.TemporaryDirectory() as d:
            broker, res, g = sw.golden(ROOT / "fixtures" / "nantucket", Path(d) / "l.db", client, repeats=1)
            try:
                self.assertIsInstance(client.bound, Broker)
                self.assertEqual(len(client.bound.ledger.claims()), len(broker.ledger.claims()))
            finally:
                broker.close()
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(res.calls, 1)


if __name__ == "__main__":
    unittest.main()
