"""Takeoff: formulas the model writes, evaluated and written by code.

Rows are compared as whole Claims where a mutant could otherwise change a field
nobody looked at.
"""
import json
import tempfile
import unittest
from pathlib import Path

from pipeline import schema, takeoff
from pipeline.broker import Broker
from pipeline.readers import compare, validate
from pipeline.readers.clients import prompt, prompt_version
from pipeline.schema import Claim, LedgerError

ROOT = Path(__file__).resolve().parent.parent
RUN = "NAN-RUN"
SPAN, SPACING, END, ANCH = "NAN-DR-FND-01", "NAN-DR-FND-02", "NAN-DR-FND-03", "NAN-DR-DET-01"
SPACES = f"({{{SPAN}}} - 2 * {{{END}}}) / {{{SPACING}}}"


def reader_row(claim_id, value, unit, statement, *, method="dimensioned", locator="Partial Foundation Plan",
               tag="S-1 Fnd", flag="", source="S-1"):
    return Claim(claim_id=claim_id, statement=statement, source_id=source, locator=locator, tag=tag,
                 method=method, role="quantity", confidence="exact", value=str(value),
                 value_num=float(value), unit=unit, flag=flag)


ROWS = [
    reader_row(SPAN, 182, "in", "Bracket run: 15'-2\""),
    reader_row(SPACING, 32, "in", "Bracket spacing: 2'-8\""),
    reader_row(END, 11, "in", "End distance: 11\""),
    reader_row(ANCH, 3, "per bracket", "Adhesive anchors (TYP.)", method="counted",
               locator="Support Detail 1/S-1", tag="S-1 Det 1"),
]
BY_ID = {c.claim_id: c for c in ROWS}


def it(label, calc, value, unit="each"):
    return {"label": label, "calc": calc, "value": value, "unit": unit}


class Fake:
    """A client that returns one prepared answer per run."""

    def __init__(self, answers, model_id="fake-model"):
        self.answers, self.model_id, self.calls = answers, model_id, []

    def complete(self, reader, unit, system, schema, run):
        self.calls.append((reader, unit, system, schema, run))
        return self.answers[run]


class LedgerCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.broker = Broker.open_job(Path(self.tmp.name) / "l.db", "intake", job="NAN", run_id=RUN, create=True,
                                      clock=lambda: "2026-10-08T00:00:00Z")
        self.broker.write_register([
            {"source_id": "S-1", "kind": "drawing", "status": "present", "sha256": "a"},
            {"source_id": "IMG_1", "kind": "photo", "status": "present", "sha256": "b"},
        ])
        self.addCleanup(self.broker.close)

    def write(self, principal, *claims):
        w = self.broker.as_principal(principal)
        for c in claims:
            w.append(c)


class ItemCheckCase(unittest.TestCase):
    def test_a_formula_that_reproduces_its_value_passes(self):
        self.assertEqual(takeoff.item_errors(it("spaces", SPACES, 5, "spaces"), BY_ID), [])
        self.assertEqual(takeoff.item_errors(it("anchors", f"{{{ANCH}}} * ({SPACES} + 1)", 18), BY_ID), [])

    def test_a_wrong_value_is_discarded_with_what_code_got(self):
        self.assertEqual(takeoff.item_errors(it("spaces", SPACES, 6, "spaces"), BY_ID),
                         [f"{SPACES!r} = 5, the item says 6"])
        self.assertEqual(takeoff.item_errors(it("half", f"{{{END}}} / 2", 5), BY_ID),
                         [f"'{{{END}}} / 2' = 5.5, the item says 5"])

    def test_a_number_in_the_label_must_be_on_the_rows(self):
        shown = takeoff.unit_for("NAN", ROWS).text
        self.assertEqual(takeoff.item_errors(it("Adhesive anchors, 3 per bracket", SPACES + " + 1", 6), BY_ID, shown), [])
        self.assertEqual(takeoff.item_errors(it("Anchors, 20 with spares", SPACES + " + 1", 6), BY_ID, shown),
                         ["label 'Anchors, 20 with spares' carries 20, which is in none of the rows"])
        self.assertEqual(takeoff.item_errors(it("Anchors, 2 spares and 7", SPACES + " + 1", 6), BY_ID, "x"),
                         ["label 'Anchors, 2 spares and 7' carries 2, 7, which is in none of the rows"])
        # "3" is on the rows, "31" is not: numbers are compared whole.
        self.assertTrue(takeoff.item_errors(it("31 brackets", SPACES + " + 1", 6), BY_ID, "3 | 1"))

    def test_a_formula_with_no_row_is_discarded(self):
        self.assertEqual(takeoff.item_errors(it("six", "5 + 1", 6), BY_ID), ["'5 + 1' uses no row"])

    def test_a_row_it_was_not_shown_is_discarded(self):
        self.assertEqual(takeoff.item_errors(it("x", "{NAN-DR-X-09} + {NAN-DR-Y-01}", 1), BY_ID),
                         ["'{NAN-DR-X-09} + {NAN-DR-Y-01}' uses NAN-DR-X-09, NAN-DR-Y-01, which is not a row it was shown"])

    def test_a_decimal_in_a_formula_is_a_rate_and_is_refused(self):
        self.assertEqual(takeoff.item_errors(it("bags", f"{{{SPAN}}} * 0.44", 80), BY_ID),
                         [f"'{{{SPAN}}} * 0.44' carries 0.44; a formula's own numbers are whole"])
        self.assertEqual(takeoff.item_errors(it("x", f"{{{SPAN}}} * 1.5 + 2.25", 275), BY_ID),
                         [f"'{{{SPAN}}} * 1.5 + 2.25' carries 1.5, 2.25; a formula's own numbers are whole"])

    def test_digits_inside_a_reference_are_not_constants(self):
        self.assertEqual(takeoff.item_errors(it("x", f"{{{SPAN}}} - 82", 100), BY_ID), [])

    def test_a_formula_that_does_not_evaluate_is_discarded(self):
        self.assertEqual(takeoff.item_errors(it("x", f"{{{SPAN}}} / ({{{END}}} - 11)", 1), BY_ID),
                         [f"'{{{SPAN}}} / ({{{END}}} - 11)' does not evaluate: float division by zero"])
        self.assertEqual(takeoff.item_errors(it("x", f"({{{SPAN}}}", 1), BY_ID)[0][:40],
                         f"'({{{SPAN}}}' does not evaluate: not pla"[:40])

    def test_the_schema_holds_units_and_values_to_a_closed_list(self):
        ok = {"items": [it("spaces", SPACES, 5, "spaces")]}
        self.assertEqual(validate.errors(ok, takeoff.SCHEMA), [])
        for bad in (it("run", f"{{{SPAN}}}", 182, "in"), it("x", SPACES, -1), it("x", SPACES, 5.5),
                    it("x", "eval('1')", 1), dict(it("x", SPACES, 5), note="y")):
            with self.subTest(bad=bad):
                self.assertTrue(validate.errors({"items": [bad]}, takeoff.SCHEMA))
        self.assertEqual(takeoff.UNITS, ("each", "spaces"))
        self.assertEqual(takeoff.INPUT_METHODS, ("dimensioned", "counted"))


class DerivationCase(unittest.TestCase):
    def test_each_reference_is_replaced_by_its_figure(self):
        self.assertEqual(takeoff.derivation(SPACES, BY_ID), "(182 - 2 x 11) / 32")
        self.assertEqual(takeoff.derivation(f"{{{ANCH}}}*({SPACES}+1)", BY_ID), "3 x ((182 - 2 x 11) / 32+1)")

    def test_the_vote_keys_on_what_an_item_computes(self):
        a = it("Spaces", SPACES, 5, "spaces")
        b = it("Gaps", f"({{{SPAN}}} - {{{END}}} - {{{END}}}) / {{{SPACING}}}", 5, "spaces")
        c = it("Brackets", SPACES + " + 1", 6)
        self.assertEqual(takeoff.item_key(a), (5, "spaces", (SPAN, SPACING, END)))
        self.assertEqual(takeoff.vote([[a, a, c], [b], [c]]), [(a, 2), (c, 2)])
        self.assertEqual(takeoff.vote([]), [])

    def test_a_derived_row_agreed_by_every_run(self):
        item = it("  Adhesive anchors ", f"{{{ANCH}}} * ({SPACES} + 1)", 18)
        self.assertEqual(takeoff.derived_claim("NAN-TK-Q-03", item, 2, 2, BY_ID), Claim(
            claim_id="NAN-TK-Q-03", statement="Adhesive anchors: 3 x ((182 - 2 x 11) / 32 + 1) = 18",
            source_id="S-1", locator="Support Detail 1/S-1 and Partial Foundation Plan", tag="S-1 Det 1 + S-1 Fnd",
            method="counted", role="quantity", confidence="exact", value="18", value_num=18.0, unit="each",
            calc=item["calc"], derivation="3 x ((182 - 2 x 11) / 32 + 1) = 18", flag=""))

    def test_a_derived_row_some_runs_missed_or_resting_on_a_flagged_row(self):
        rows = dict(BY_ID)
        rows[END] = reader_row(END, 11, "in", "End distance", flag="unverified", source="S-1 + IMG_1")
        rows[SPACING] = reader_row(SPACING, 32, "in", "Spacing", flag="unverified")
        c = takeoff.derived_claim("NAN-TK-Q-01", it("Spaces", SPACES, 5, "spaces"), 1, 3, rows)
        self.assertEqual((c.flag, c.derivation, c.source_id), (
            "unverified", f"(182 - 2 x 11) / 32 = 5; seen in 1 of 3 runs; uses flagged {END}, {SPACING}", "S-1 + IMG_1"))
        c = takeoff.derived_claim("NAN-TK-Q-01", it("Spaces", SPACES, 5, "spaces"), 3, 3, rows)
        self.assertEqual((c.flag, c.derivation), ("unverified", f"(182 - 2 x 11) / 32 = 5; uses flagged {END}, {SPACING}"))


class InputsCase(unittest.TestCase):
    def test_only_single_agreed_exact_figures_are_shown(self):
        scaled = Claim(claim_id="S", statement="leg, scaled", source_id="S-1", method="scaled", role="note",
                       confidence="scaled", value="1'-8\"")
        conflict = reader_row("C", 6, "each", "x", method="counted", flag="conflict")
        unverified = reader_row("U", 6, "each", "x", method="counted", flag="unverified")
        derived = Claim(**{**reader_row("Q", 6, "each", "x", method="counted").__dict__, "calc": "{U}"})
        clause = Claim(claim_id="N", statement="note", source_id="S-1", method="clause", role="note",
                       confidence="exact", quote="x")
        readings = Claim(**{**reader_row("R", 6, "each", "x").__dict__, "value": "6 (reading A) / 7 (reading B)",
                            "value_num": None})
        got = takeoff.inputs(ROWS + [scaled, conflict, unverified, derived, clause, readings])
        self.assertEqual([c.claim_id for c in got], [SPAN, SPACING, END, ANCH, "U"])

    def test_the_unit_is_the_rows_as_a_table(self):
        rows = ROWS[:1] + [reader_row("U", 6, "each", "Brackets", method="counted", flag="unverified")]
        u = takeoff.unit_for("NAN", rows)
        self.assertEqual((u.unit_id, u.source_id, u.locator, u.tag, u.path), ("NAN#takeoff", "", "ledger rows", "", ""))
        self.assertEqual(u.text, f"{SPAN} | dimensioned | 182 in | Bracket run: 15'-2\" | S-1 Fnd\n"
                                 "U | counted | 6 each | Brackets | S-1 Fnd | unverified")
        no_tag = Claim(**{**ROWS[0].__dict__, "tag": ""})
        self.assertTrue(takeoff.unit_for("NAN", [no_tag]).text.endswith("| Partial Foundation Plan"))


class FieldRowsCase(unittest.TestCase):
    def test_scaled_and_photo_rows_become_field_rows_with_no_figure(self):
        scaled = Claim(claim_id="NAN-DR-DET-03", statement="Vertical angle leg length, scaled", source_id="S-1",
                       locator="Support Detail 1/S-1 at 3/4\"=1'-0\"", tag="S-1 Det 1", method="scaled",
                       role="note", confidence="scaled", value="1'-8\" (reading A) / 1'-10\" (reading B)")
        peeling = Claim(claim_id="OBV-PH-1", statement="coating peeling", source_id="IMG_1", tag="IMG_1",
                        method="observed", role="note", confidence="inferred",
                        derivation="condition peeling at walkway, severity severe")
        fine = Claim(**{**peeling.__dict__, "claim_id": "OBV-PH-2", "derivation": "condition none at wall"})
        odd = Claim(**{**peeling.__dict__, "claim_id": "OBV-PH-3", "derivation": "seen in 1 of 2 runs"})
        got = takeoff.field_rows("NAN", ROWS + [scaled, fine, odd, peeling])
        self.assertEqual(got, [
            Claim(claim_id="NAN-TK-F-01", source_id="S-1", locator="Support Detail 1/S-1 at 3/4\"=1'-0\"",
                  tag="S-1 Det 1", method="FIELD", role="quantity", confidence="missing",
                  statement="Vertical angle leg length: measure on site or get the dimension from the engineer; "
                            "the scaled reading in NAN-DR-DET-03 is not a quantity",
                  derivation="from NAN-DR-DET-03"),
            Claim(claim_id="NAN-TK-F-02", source_id="IMG_1", locator="", tag="IMG_1", method="FIELD",
                  role="quantity", confidence="missing",
                  statement="Extent of peeling at the walkway seen in IMG_1: measure on site (OBV-PH-1)",
                  derivation="from OBV-PH-1"),
        ])
        self.assertNotIn("1'-8", got[0].statement)


class RunCase(LedgerCase):
    def setUp(self):
        super().setUp()
        self.write("drawing_reader", *ROWS)

    def good(self):
        return {"items": [it("Spaces", SPACES, 5, "spaces"), it("Brackets", SPACES + " + 1", 6)]}

    def test_agreed_runs_write_derived_rows_as_takeoff(self):
        client = Fake([self.good(), json.dumps(self.good())])
        res = takeoff.run(self.broker, "NAN", client, repeats=2)
        self.assertEqual([c.claim_id for c in res.rows], ["NAN-TK-Q-01", "NAN-TK-Q-02"])
        self.assertEqual([c.flag for c in res.rows], ["", ""])
        (reader, unit, system, schema, run), _ = client.calls
        self.assertEqual((reader, unit.unit_id, system, schema, run), ("takeoff", "NAN#takeoff", prompt("takeoff"),
                                                                       takeoff.SCHEMA, 0))
        stored = self.broker.ledger.by_id()["NAN-TK-Q-02"]
        self.assertEqual((stored.agent, stored.model_id, stored.prompt_version),
                         ("takeoff", "fake-model", prompt_version("takeoff")))
        self.assertEqual(res.text(), "takeoff: 1 units, 2 calls, 2 derived quantities, 0 FIELD rows, "
                                     "0 items discarded, 0 units unread")
        calls = [r for r in self.broker.ledger.log() if r["action"] == "model-call"]
        self.assertEqual([r["detail"].split("; ")[-1] for r in calls], ["valid", "valid"])
        self.assertEqual({r["principal"] for r in calls}, {"takeoff"})

    def test_bad_runs_and_items_are_discarded_and_reported(self):
        bad_item = {"items": [it("Spaces", SPACES, 6, "spaces"), it("Brackets", SPACES + " + 1", 6)]}
        client = Fake(["not json", {"items": [{"label": "x"}]}, bad_item])
        res = takeoff.run(self.broker, "NAN", client, repeats=3)
        self.assertEqual(res.calls, 3)
        self.assertEqual(res.discarded[0][:42], "NAN#takeoff run 1: not JSON: Expecting val")
        self.assertEqual(res.discarded[1], "NAN#takeoff run 2: $.items[0]: missing calc; "
                                           "$.items[0]: missing value; $.items[0]: missing unit")
        self.assertEqual(res.discarded[2], f"NAN#takeoff run 3: items[0] {SPACES!r} = 5, the item says 6")
        (row,) = res.rows
        self.assertEqual((row.value, row.flag, row.derivation),
                         ("6", "unverified", "(182 - 2 x 11) / 32 + 1 = 6; seen in 1 of 3 runs"))
        self.assertEqual(res.unread, [])
        details = [r["detail"] for r in self.broker.ledger.log() if r["action"] == "model-call"]
        self.assertTrue(details[0].endswith("run 1; discarded: not JSON: Expecting value: line 1 column 1 (char 0)"))
        self.assertTrue(details[2].endswith("run 3; valid"))

    def test_field_rows_carry_the_job_and_calls_are_logged_by_unit(self):
        self.write("drawing_reader", Claim(claim_id="NAN-DR-S", statement="leg, scaled", source_id="S-1",
                                           method="scaled", role="note", confidence="scaled", value="1'-8\""))
        many = {"items": [{"label": "x"}, {"label": "y"}]}   # six schema errors; three are reported
        res = takeoff.run(self.broker, "NAN", Fake([many]), repeats=1)
        self.assertEqual([c.claim_id for c in res.rows], ["NAN-TK-F-01"])
        self.assertEqual(res.discarded[0].count("$.items"), 3)
        (call,) = [r for r in self.broker.ledger.log() if r["action"] == "model-call"]
        self.assertEqual(call["subject"], "NAN#takeoff")

    def test_no_valid_run_leaves_the_unit_unread_and_writes_no_figure(self):
        res = takeoff.run(self.broker, "NAN", Fake(["x", "y"]), repeats=2)
        self.assertEqual((res.unread, res.rows), (["NAN#takeoff"], []))
        self.assertIn("1 units unread", res.text())
        self.assertIn("  unread NAN#takeoff", res.text().splitlines())
        self.assertIn("  discarded NAN#takeoff run 2: not JSON: Expecting value: line 1 column 1 (char 0)",
                      res.text().splitlines())

    def test_with_nothing_to_derive_from_no_model_is_called(self):
        led = Broker.open_job(Path(self.tmp.name) / "empty.db", "intake", job="OBV", run_id="R", create=True)
        led.write_register([{"source_id": "IMG_1", "kind": "photo", "status": "present", "sha256": "b"}])
        led.as_principal("photo_reader").append(Claim(
            claim_id="OBV-PH-1", statement="peeling", source_id="IMG_1", tag="IMG_1", method="observed",
            role="note", confidence="inferred", derivation="condition peeling at walkway"))
        client = Fake([])
        res = takeoff.run(led, "OBV", client, repeats=2)
        led.close()
        self.assertEqual((client.calls, res.units, res.calls), ([], 0, 0))
        self.assertEqual([c.method for c in res.rows], ["FIELD"])
        self.assertEqual(res.rows[0].model_id, "none (code only)")
        self.assertEqual(res.text(), "takeoff: 0 units, 0 calls, 0 derived quantities, 1 FIELD rows, "
                                     "0 items discarded, 0 units unread")

    def test_no_client_writes_field_rows_only(self):
        res = takeoff.run(self.broker, "NAN", None)
        self.assertEqual((res.calls, res.rows), (0, []))

    def test_a_row_the_broker_refuses_is_reported_not_raised(self):
        takeoff.run(self.broker, "NAN", Fake([self.good()]), repeats=1)
        res = takeoff.run(self.broker, "NAN", Fake([self.good()]), repeats=1)
        self.assertEqual(len(res.refused), 2)
        self.assertTrue(res.refused[0].startswith("NAN-TK-Q-01: NAN-TK-Q-01: already in the ledger"))
        self.assertIn("  refused NAN-TK-Q-01", res.text())

    def test_superseded_rows_are_not_used(self):
        self.write("field_crew", Claim(claim_id="NAN-FC-1", statement="End distance measured", source_id="S-1",
                                       method="dimensioned", role="quantity", confidence="exact", value="11",
                                       value_num=11.0, unit="in", supersedes=END))
        self.assertNotIn(END, [c.claim_id for c in takeoff.current(self.broker)])
        self.assertIn("NAN-FC-1", [c.claim_id for c in takeoff.current(self.broker)])


class AccessCase(LedgerCase):
    def test_takeoff_cannot_write_a_dimension_or_a_clause(self):
        w = self.broker.as_principal("takeoff")
        for method in ("dimensioned", "clause", "observed"):
            with self.subTest(method=method), self.assertRaises(LedgerError):
                w.append(reader_row("X", 1, "each", "x", method=method))

    def test_a_scaled_row_cannot_feed_a_counted_one(self):
        self.write("drawing_reader", Claim(claim_id="NAN-S", statement="leg", source_id="S-1", method="scaled",
                                           role="note", confidence="scaled", value="20", value_num=20.0))
        with self.assertRaises(LedgerError) as cm:
            self.write("takeoff", Claim(**{**reader_row("NAN-Q", 40, "each", "x", method="counted").__dict__,
                                           "calc": "{NAN-S} * 2"}))
        self.assertIn("NAN-Q: calc input NAN-S is scaled; a counted row rests on dimensioned and counted rows only",
                      str(cm.exception))


class GoldenCompareCase(unittest.TestCase):
    BRACKETS = "NAN-DR-FND-04"

    def setUp(self):
        self.symbols = reader_row(self.BRACKETS, 6, "each", "Bracket symbols", method="counted")
        q = lambda cid, v, u, calc: Claim(**{**reader_row(cid, v, u, "x", method="counted").__dict__, "calc": calc})
        self.fixture = ROWS + [self.symbols, q("Q1", 5, "spaces", SPACES), q("Q2", 6, "each", "{Q1} + 1"),
                               q("Q3", 18, "each", f"{{{ANCH}}} * {{Q2}}")]
        self.q = q

    def compare(self, produced, extra_rows=()):
        by_id = {c.claim_id: c for c in ROWS + [self.symbols, *extra_rows, *produced]}
        cmp = compare.Comparison(matched=[("z",)], missing=[], extra=[])
        return compare.add_derived(cmp, by_id, produced, self.fixture, {"S-1"})

    def test_the_route_to_a_figure_is_not_compared(self):
        """18 anchors from 3 x the 6 symbols drawn is the fixture's 18 from the dimensions."""
        cmp = self.compare([self.q("T1", 5, "spaces", f"{{{self.BRACKETS}}} - 1"),
                            self.q("T2", 18, "each", f"{{{ANCH}}} * {{{self.BRACKETS}}}")])
        self.assertTrue(cmp.exact_ok, cmp.text())
        # 6 brackets is already a counted row, so it matches without being derived.
        self.assertEqual(cmp.matched, sorted([("z",), ("counted", "derived", "5", "spaces"),
                                              ("counted", "derived", "6", "each"),
                                              ("counted", "derived", "18", "each")]))

    def test_a_wrong_figure_is_missing_and_extra(self):
        cmp = self.compare([self.q("T1", 5, "spaces", SPACES), self.q("T2", 21, "each", f"{{{ANCH}}} * 7")])
        self.assertFalse(cmp.exact_ok)
        self.assertEqual(cmp.missing, [("counted", "derived", "18", "each")])
        self.assertEqual(cmp.extra, [("counted", "derived", "21", "each")])

    def test_a_figure_resting_on_a_row_the_fixture_lacks_is_extra(self):
        odd = reader_row("NAN-DR-ODD", 18, "each", "x", method="counted", locator="Elsewhere")
        cmp = self.compare([self.q("T1", 5, "spaces", SPACES), self.q("T2", 18, "each", "{NAN-DR-ODD}")], [odd])
        self.assertEqual(cmp.missing, [("counted", "derived", "18", "each")])
        self.assertEqual(cmp.extra, [("counted", "derived", "18", "each", (compare.key(odd),))])

    def test_a_flagged_count_does_not_stand_in_for_a_derived_figure(self):
        self.symbols = reader_row(self.BRACKETS, 6, "each", "Bracket symbols", method="counted", flag="unverified")
        cmp = self.compare([self.q("T1", 5, "spaces", SPACES), self.q("T2", 18, "each", f"{{{ANCH}}} * 6")])
        self.assertEqual(cmp.missing, [("counted", "derived", "6", "each")])

    def test_leaves_follow_derived_rows_and_stop_on_a_loop(self):
        q1 = self.q("Q1", 5, "spaces", SPACES)
        q2 = self.q("Q2", 6, "each", "{Q1} + 1 + {NAN-GONE}")
        by_id = {**BY_ID, "Q1": q1, "Q2": q2}
        self.assertEqual(compare.leaves(q2, by_id), tuple(sorted(compare.key(BY_ID[r]) for r in (SPAN, SPACING, END))))
        self.assertEqual(compare.leaves(ROWS[0], by_id), (compare.key(ROWS[0]),))
        loop = self.q("L", 1, "each", "{L} + 1")
        self.assertEqual(compare.leaves(loop, {"L": loop}), (compare.key(loop),))
        self.assertEqual(compare.derived_key(q2), ("counted", "derived", "6", "each"))

    def test_fixture_derived_rows(self):
        q = self.q("Q", 6, "each", SPACES + " + 1")
        allowance = Claim(**{**q.__dict__, "claim_id": "A", "role": "allowance"})
        offsite = Claim(**{**q.__dict__, "claim_id": "W", "source_id": "S-1 + WEB"})
        scaled = Claim(**{**q.__dict__, "claim_id": "S", "method": "scaled"})
        fixture = ROWS + [q, allowance, offsite, scaled]
        self.assertEqual([c.claim_id for c in compare.fixture_derived(fixture, {"S-1"})], ["Q"])
        self.assertEqual([c.claim_id for c in compare.fixture_derived(fixture, {"S-1", "WEB"})], ["Q", "W"])


class GoldenReplayCase(unittest.TestCase):
    def test_both_jobs(self):
        from pipeline.readers import golden
        with tempfile.TemporaryDirectory() as tmp:
            broker, results, cmp = golden.replay(ROOT / "fixtures" / "nantucket", Path(tmp) / "n.db")
            rows = results["takeoff"].rows
            broker.close()
            self.assertTrue(cmp.exact_ok, cmp.text())
            self.assertEqual([(c.value, c.unit, c.flag) for c in rows if c.calc],
                             [("5", "spaces", ""), ("6", "each", ""), ("18", "each", ""), ("18", "each", "")])
            self.assertEqual([c.method for c in rows if not c.calc], ["FIELD"] * 3)
            broker, results, cmp = golden.replay(ROOT / "fixtures" / "ocean-beach", Path(tmp) / "o.db")
            broker.close()
            self.assertTrue(cmp.exact_ok, cmp.text())
            self.assertEqual(results["takeoff"].calls, 0)
            self.assertEqual([(c.method, c.value, c.source_id) for c in results["takeoff"].rows],
                             [("FIELD", "", "IMG_8343")])

    def test_the_route_is_the_dimensions_not_the_symbol_count(self):
        """The prompt's per-assembly template and every recorded Nantucket total multiply by
        the brackets the run, spacing and end offsets give, never by the 6 symbols drawn."""
        self.assertIn("{PER} * (({RUN} - 2 * {END}) / {SPACING} + 1)", prompt("takeoff"))
        recorded = json.loads((ROOT / "fixtures" / "nantucket" / "recordings" / "takeoff.json").read_text())
        calcs = [it["calc"] for u in recorded["units"] for r in u["runs"] for it in r["items"]]
        totals = [c for c in calcs if any(ref.startswith("NAN-DR-S1DET1-") for ref in schema.CALC_REF.findall(c))]
        self.assertEqual(len(totals), 2 * len(recorded["units"][0]["runs"]))
        for calc in totals:
            refs = set(schema.CALC_REF.findall(calc))
            self.assertTrue({"NAN-DR-S1FND-01", "NAN-DR-S1FND-02", "NAN-DR-S1FND-03"} <= refs, calc)
            self.assertNotIn("NAN-DR-S1FND-04", refs, calc)

    def test_the_prompt_example_is_schema_valid_and_reproduces(self):
        text = prompt("takeoff")
        blocks = [b.split("```", 1)[0] for b in text.split("```json\n")[1:]]
        answers = [json.loads(b) for b in blocks]
        self.assertEqual(len(answers), 2)
        rows = {"D-001": reader_row("D-001", 120, "in", "Rail run"), "D-002": reader_row("D-002", 24, "in", "Spacing")}
        for a in answers:
            self.assertEqual(validate.errors(a, takeoff.SCHEMA), [])
            for item in a["items"]:
                self.assertEqual(takeoff.item_errors(item, rows), [], item)


class SymbolCheckCase(unittest.TestCase):
    """The symbols drawn are compared with the number the dimensions give, in code."""

    SYMBOLS = reader_row("NAN-DR-FND-04", 7, "each", "Bracket symbols drawn", method="counted")
    ANCHORS = it("Adhesive anchors", f"{{{ANCH}}} * ({SPACES} + 1)", 18)

    def test_the_number_of_assemblies_an_item_rests_on(self):
        self.assertEqual(takeoff.assemblies(it("Spaces", SPACES, 5, "spaces"), BY_ID), 6)
        self.assertEqual(takeoff.assemblies(it("Brackets", f"{SPACES} + 1", 6), BY_ID), 6)
        # A formula that only evaluates with the real per-assembly count gives no number to check.
        self.assertIsNone(takeoff.assemblies(it("Odd", f"{{{SPAN}}} / ({{{ANCH}}} - 1)", 91), BY_ID))
        self.assertEqual(takeoff.assemblies(self.ANCHORS, BY_ID), 6)
        rows = dict(BY_ID, **{self.SYMBOLS.claim_id: self.SYMBOLS})
        self.assertIsNone(takeoff.assemblies(it("Anchors", f"{{{ANCH}}} * {{{self.SYMBOLS.claim_id}}}", 21), rows))
        bolts = reader_row("NAN-DR-DET-02", 3, "per bracket", "Bolts (TYP.)", method="counted")
        rows[bolts.claim_id] = bolts
        summed = it("All fasteners", f"{{{ANCH}}} * ({SPACES} + 1) + {{{bolts.claim_id}}} * ({SPACES} + 1)", 36)
        self.assertIsNone(takeoff.assemblies(summed, rows))
        self.assertEqual(takeoff.symbol_notes(summed, rows), [])

    def test_a_symbol_count_that_disagrees_flags_every_item_on_that_route(self):
        rows = dict(BY_ID, **{self.SYMBOLS.claim_id: self.SYMBOLS})
        c = takeoff.derived_claim("NAN-TK-Q-03", self.ANCHORS, 2, 2, rows)
        self.assertEqual((c.flag, c.derivation), (
            "unverified", "3 x ((182 - 2 x 11) / 32 + 1) = 18; NAN-DR-FND-04 counts 7 symbols where the dimensions give 6"))
        c = takeoff.derived_claim("NAN-TK-Q-01", it("Spaces", SPACES, 5, "spaces"), 2, 2, rows)
        self.assertEqual(c.flag, "unverified")
        self.assertTrue(c.derivation.endswith("NAN-DR-FND-04 counts 7 symbols where the dimensions give 6"))
        # A symbol count the reader runs disagreed on is no formula input, but it is still
        # compared: the check reads every current row, and both readings go on the note.
        conflict = Claim(**{**self.SYMBOLS.__dict__, "value": "6 (reading A) / 7 (reading B)",
                            "value_num": None, "flag": "conflict"})
        claims = list(BY_ID.values()) + [conflict]
        self.assertEqual(takeoff.inputs(claims), list(BY_ID.values()))
        c = takeoff.derived_claim("NAN-TK-Q-03", self.ANCHORS, 2, 2, BY_ID, claims)
        self.assertEqual((c.flag, c.derivation), ("unverified", "3 x ((182 - 2 x 11) / 32 + 1) = 18; "
                         "NAN-DR-FND-04 counts 6 (reading A) / 7 (reading B) symbols where the dimensions give 6"))
        self.assertEqual(takeoff.derived_claim("NAN-TK-Q-03", self.ANCHORS, 2, 2, BY_ID).flag, "")

    def test_an_agreeing_symbol_count_or_one_on_another_view_is_no_note(self):
        same = reader_row("NAN-DR-FND-04", 6, "each", "Bracket symbols drawn", method="counted")
        other = reader_row("NAN-DR-FRM-01", 7, "each", "Marks on the framing plan", method="counted",
                           locator="Partial First Floor Framing Plan", tag="S-1 Frm")
        per = reader_row("NAN-DR-DET-02", 7, "per bracket", "Bolts (TYP.)", method="counted")
        rows = dict(BY_ID, **{r.claim_id: r for r in (same, other, per)})
        self.assertEqual(takeoff.symbol_notes(self.ANCHORS, rows), [])
        self.assertEqual(takeoff.derived_claim("NAN-TK-Q-03", self.ANCHORS, 2, 2, rows).flag, "")
        self.assertEqual(takeoff.symbol_notes(it("Anchors", f"{{{ANCH}}} * {{{other.claim_id}}}", 21), rows), [])

    def test_a_dimension_the_runs_labelled_differently_flags_the_item_unless_the_symbols_confirm_it(self):
        worded = reader_row(END, 11, "in", "Top wall segment or First bracket from the wall face")
        worded = Claim(**{**worded.__dict__, "derivation": "11\" dimension string = 11 in; runs word the label differently"})
        rows = dict(BY_ID, **{END: worded})
        c = takeoff.derived_claim("NAN-TK-Q-03", self.ANCHORS, 2, 2, rows)
        self.assertEqual((c.flag, c.derivation), (
            "unverified", f"3 x ((182 - 2 x 11) / 32 + 1) = 18; uses {END}, whose label the runs word differently"))
        self.assertEqual(takeoff.label_notes(it("Spaces", SPACES, 5, "spaces"), rows),
                         [f"uses {END}, whose label the runs word differently"])
        six = reader_row("NAN-DR-FND-04", 6, "each", "Bracket symbols drawn", method="counted")
        self.assertEqual(takeoff.label_notes(self.ANCHORS, {**rows, six.claim_id: six}), [])
        self.assertEqual(takeoff.derived_claim("NAN-TK-Q-03", self.ANCHORS, 2, 2, {**rows, six.claim_id: six}).flag, "")
        self.assertEqual(len(takeoff.label_notes(self.ANCHORS, {**rows, self.SYMBOLS.claim_id: self.SYMBOLS})), 1)
        self.assertEqual(takeoff.label_notes(self.ANCHORS, BY_ID), [])
        # A label the vote let a count through on flags the item even when the symbols agree.
        counted = Claim(**{**reader_row(END, 11, "in", "Wall face to first bracket (2 PLACES)").__dict__,
                           "derivation": "11\" dimension string = 11 in; a run put a count in the label with no counted row"})
        self.assertEqual(takeoff.label_notes(self.ANCHORS, {**BY_ID, END: counted, six.claim_id: six}),
                         [f"uses {END}, whose label a run gave a count with no counted row"])
        bolts = reader_row("NAN-DR-DET-02", 3, "per bracket", "Bolts (TYP.)", method="counted")
        summed = it("All fasteners", f"{{{ANCH}}} * ({SPACES} + 1) + {{{bolts.claim_id}}} * ({SPACES} + 1)", 36)
        with_six = {**rows, six.claim_id: six, bolts.claim_id: bolts}
        self.assertEqual(len(takeoff.label_notes(summed, with_six)), 1)
        elsewhere = reader_row("NAN-DR-FRM-01", 6, "each", "Bracket symbols", method="counted",
                               locator="Partial First Floor Framing Plan", tag="S-1 Frm")
        self.assertEqual(len(takeoff.label_notes(self.ANCHORS, {**rows, elsewhere.claim_id: elsewhere})), 1)

    def test_an_item_on_the_symbol_route_is_flagged_where_the_view_carries_dimensions(self):
        """`3 x 6 symbols` passes over the run, spacing and end offset on the same view."""
        six = reader_row("NAN-DR-FND-04", 6, "each", "Bracket symbols drawn", method="counted")
        rows = {**BY_ID, six.claim_id: six}
        by_symbols = it("Adhesive anchors", f"{{{ANCH}}} * {{{six.claim_id}}}", 18)
        note = (f"rests on the symbol count {six.claim_id} where Partial Foundation Plan carries bracket dimension "
                f"strings ({SPAN}, {SPACING})")   # the end distance row names no bracket
        self.assertEqual(takeoff.route_notes(by_symbols, rows), [note])
        c = takeoff.derived_claim("NAN-TK-Q-03", by_symbols, 2, 2, rows)
        self.assertEqual((c.flag, c.derivation), ("unverified", f"3 x 6 = 18; {note}"))
        # The dimension route is not on this note (the symbol check compares it with the symbols).
        self.assertEqual(takeoff.route_notes(self.ANCHORS, rows), [])
        self.assertEqual(takeoff.derived_claim("NAN-TK-Q-03", self.ANCHORS, 2, 2, rows).flag, "")
        # A total that takes the anchors from the symbols and the bolts from the dimensions is on the
        # symbol route too, which the symbol check passes by (a sum of per-assembly products): whether the
        # symbols agree with the dimensions (6) or not (7), the item is flagged with the view's rows named.
        bolts = reader_row("NAN-DR-DET-02", 3, "per bracket", "Bolts (TYP.)", method="counted")
        for symbols, total in [(six, 36), (self.SYMBOLS, 39)]:
            mixed = it("All fasteners", f"{{{ANCH}}} * {{{symbols.claim_id}}} + {{{bolts.claim_id}}} * ({SPACES} + 1)", total)
            with_both = {**BY_ID, symbols.claim_id: symbols, bolts.claim_id: bolts}
            self.assertEqual(takeoff.symbol_notes(mixed, with_both), [])
            c = takeoff.derived_claim("NAN-TK-Q-05", mixed, 2, 2, with_both)
            self.assertEqual((c.flag, c.derivation), ("unverified", f"3 x {symbols.value} + 3 x ((182 - 2 x 11) / 32 + 1) = {total}; "
                             f"rests on the symbol count {symbols.claim_id} where Partial Foundation Plan carries bracket dimension strings ({SPAN}, {SPACING})"))
        # Window tags beside a room width: no dimension string names the window, so the symbol count is the
        # prompt's own route when no run and spacing are listed, and the item is not flagged.
        tags = reader_row("NAN-DR-A1-01", 12, "each", "Window tags", method="counted", locator="Floor Plan", tag="A-1")
        per_window = reader_row("NAN-DR-A1-02", 4, "per window", "Clips per window", method="counted", locator="Floor Plan", tag="A-1")
        width = reader_row("NAN-DR-A1-03", 144, "in", "Living room width: 12'-0\"", locator="Floor Plan", tag="A-1")
        plan = {tags.claim_id: tags, per_window.claim_id: per_window, width.claim_id: width}
        clips = it("Window clips", f"{{{per_window.claim_id}}} * {{{tags.claim_id}}}", 48)
        self.assertEqual(takeoff.route_notes(clips, plan), [])
        self.assertEqual(takeoff.derived_claim("NAN-TK-Q-06", clips, 2, 2, plan).flag, "")
        # A view whose only dimension is a wall thickness names no bracket either.
        wall = reader_row("NAN-DR-FND-06", 8, "in", "East wall thickness: 8\"")
        self.assertEqual(takeoff.route_notes(by_symbols, {six.claim_id: six, ANCH: BY_ID[ANCH], wall.claim_id: wall}), [])
        # An item with no per-assembly row of its own (spaces) is held to what the job's are per.
        spaces = it("Spaces", f"{{{six.claim_id}}} - 1", 5, "spaces")
        self.assertEqual(takeoff.route_notes(spaces, rows), [note])
        # A job with no per-assembly row at all is held to every dimension string on the symbols' view.
        no_per = {k: v for k, v in rows.items() if k != ANCH}
        self.assertEqual(takeoff.route_notes(spaces, no_per), [
            f"rests on the symbol count {six.claim_id} where Partial Foundation Plan carries dimension strings ({SPAN}, {SPACING}, {END})"])
        # The brackets drawn again on a plan with no dimension strings (arch p.5: 6 symbols on both plans) are
        # still the brackets the foundation plan dimensions: the rule is job-wide, and the note names that view.
        elsewhere = reader_row("NAN-DR-FRM-01", 6, "each", "Bracket symbols drawn on the framing plan", method="counted",
                               locator="Partial First Floor Framing Plan", tag="S-1 Frm")
        self.assertEqual(takeoff.route_notes(it("Anchors", f"{{{ANCH}}} * {{{elsewhere.claim_id}}}", 18),
                                             {**BY_ID, elsewhere.claim_id: elsewhere}), [
            f"rests on the symbol count {elsewhere.claim_id} where Partial Foundation Plan carries bracket dimension strings ({SPAN}, {SPACING})"])
        # A bracket run on another view, with no spacing, is held the same way; strings on two views name both.
        far = Claim(**{**BY_ID[SPAN].__dict__, "claim_id": "NAN-DR-FRM-02", "locator": "Partial First Floor Framing Plan"})
        self.assertEqual(takeoff.route_notes(by_symbols, {six.claim_id: six, "NAN-DR-FRM-02": far, ANCH: BY_ID[ANCH]}), [
            f"rests on the symbol count {six.claim_id} where Partial First Floor Framing Plan carries bracket dimension strings (NAN-DR-FRM-02)"])
        self.assertEqual(takeoff.route_notes(by_symbols, {**rows, "NAN-DR-FRM-02": far}), [
            f"rests on the symbol count {six.claim_id} where Partial Foundation Plan and Partial First Floor Framing Plan "
            f"carry bracket dimension strings ({SPAN}, {SPACING}, NAN-DR-FRM-02)"])
        # A view with no dimension string anywhere in the job naming the assembly is the prompt's own route.
        self.assertEqual(takeoff.route_notes(it("Anchors", f"{{{ANCH}}} * {{{elsewhere.claim_id}}}", 18),
                                             {ANCH: BY_ID[ANCH], elsewhere.claim_id: elsewhere, END: BY_ID[END]}), [])
        # The rows the symbol check reads (`claims`) are the ones searched for dimension strings.
        self.assertEqual(takeoff.route_notes(by_symbols, rows, [six, BY_ID[ANCH]]), [])
        self.assertEqual(takeoff.route_notes(by_symbols, {six.claim_id: six, ANCH: BY_ID[ANCH]}, list(rows.values())), [note])
        # A count in another unit (`1 plank`) and a derived counted row are not symbol counts.
        plank = reader_row("NAN-DR-FND-05", 1, "plank", "Hatched plank", method="counted")
        self.assertEqual(takeoff.route_notes(it("Planks", f"{{{plank.claim_id}}} * 1", 1), {**rows, plank.claim_id: plank}), [])
        derived = Claim(**{**six.__dict__, "claim_id": "NAN-TK-Q-01", "calc": f"{SPACES} + 1"})
        self.assertEqual(takeoff.route_notes(it("Anchors", f"{{{ANCH}}} * {{{derived.claim_id}}}", 18),
                                             {**rows, derived.claim_id: derived}), [])

    def test_a_count_kept_in_a_label_and_used_as_a_plain_number_is_noted(self):
        """`{PER} * (5 + 1)` takes the 5 from `5 spaces at 2'-8"`, a label whose count no row backs."""
        kept = Claim(**{**reader_row(SPACING, 32, "in", "5 spaces at 2'-8\": 2'-8\"").__dict__,
                        "derivation": "2'-8\" dimension string = 32 in; a run put a count in the label with no counted row"})
        rows = {**BY_ID, SPACING: kept}
        by_number = it("Adhesive anchors", f"{{{ANCH}}} * (5 + 1)", 18)
        note = f"takes 5 from the label of {SPACING}, a count with no counted row"
        self.assertEqual(takeoff.label_notes(by_number, rows), [note])
        c = takeoff.derived_claim("NAN-TK-Q-03", by_number, 2, 2, rows)
        self.assertEqual((c.flag, c.derivation), ("unverified", f"3 x (5 + 1) = 18; {note}"))
        # A number the label does not carry, or a row not so marked, gives no note; the rows searched are `claims`.
        self.assertEqual(takeoff.label_notes(it("Anchors", f"{{{ANCH}}} * 6", 18), rows), [])
        self.assertEqual(takeoff.label_notes(by_number, BY_ID), [])
        self.assertEqual(takeoff.label_notes(by_number, BY_ID, list(rows.values())), [note])
        self.assertEqual(takeoff.label_notes(by_number, rows, list(BY_ID.values())), [])

    def test_only_symbols_of_the_assembly_the_rows_are_per_are_compared(self):
        piers = reader_row("NAN-DR-FND-05", 2, "each", "New pier symbols drawn", method="counted")
        rows = dict(BY_ID, **{r.claim_id: r for r in (self.SYMBOLS, piers)})
        self.assertEqual(takeoff.assembly_names(rows.values()), ["bracket"])
        self.assertEqual(takeoff.symbol_notes(self.ANCHORS, rows), [
            "NAN-DR-FND-04 counts 7 symbols where the dimensions give 6"])
        no_per = {k: v for k, v in rows.items() if k != ANCH}
        self.assertEqual(takeoff.assembly_names(no_per.values()), [])
        self.assertEqual(len(takeoff.symbol_notes(it("Brackets", f"{SPACES} + 1", 6), no_per)), 2)
        # The reader's wording of the unit cannot switch the check off.
        worded = reader_row(ANCH, 3, "per bracket assembly (TYP.)", "Adhesive anchors", method="counted",
                            locator="Support Detail 1/S-1", tag="S-1 Det 1")
        self.assertEqual(takeoff.assembly_names([worded]), ["bracket"])
        self.assertEqual(len(takeoff.symbol_notes(self.ANCHORS, {**rows, ANCH: worded})), 1)
        odd = reader_row(ANCH, 3, "per support unit", "Adhesive anchors", method="counted",
                         locator="Support Detail 1/S-1", tag="S-1 Det 1")
        self.assertEqual(len(takeoff.symbol_notes(self.ANCHORS, {**rows, ANCH: odd})), 2)
        # Two kinds of assembly on one view: an item is compared with its own kind's symbols.
        pier_anch = reader_row("NAN-DR-DET-09", 4, "per pier", "Pier anchors", method="counted",
                               locator="Support Detail 2/S-1", tag="S-1 Det 2")
        pier_run = reader_row("NAN-DR-FND-06", 48, "in", "Pier run between wall faces")
        pier_space = reader_row("NAN-DR-FND-07", 48, "in", "Pier spacing on center")
        two = {**rows, pier_anch.claim_id: pier_anch, pier_run.claim_id: pier_run, pier_space.claim_id: pier_space}
        pier_item = it("Pier anchors", f"{{{pier_anch.claim_id}}} * ({{{pier_run.claim_id}}} / {{{pier_space.claim_id}}} + 1)", 8)
        self.assertEqual(takeoff.assemblies(pier_item, two), 2)
        self.assertEqual(takeoff.symbol_notes(pier_item, two), [])
        self.assertEqual(takeoff.symbol_notes(self.ANCHORS, two),
                         ["NAN-DR-FND-04 counts 7 symbols where the dimensions give 6"])
        # A spaces item names no assembly: its own dimension rows say which symbols it is compared with.
        self.assertEqual([c.claim_id for c in takeoff.symbol_rows(it("Spaces", SPACES, 5, "spaces"), two)],
                         ["NAN-DR-FND-04"])
        pier_spaces = it("Pier spaces", f"{{{pier_run.claim_id}}} / {{{pier_space.claim_id}}}", 1, "spaces")
        self.assertEqual([c.claim_id for c in takeoff.symbol_rows(pier_spaces, two)], [piers.claim_id])
        self.assertEqual(takeoff.symbol_notes(pier_spaces, two), [])
        unnamed = {k: v for k, v in two.items()}
        unnamed[SPAN] = reader_row(SPAN, 182, "in", "Run between wall faces")
        unnamed[SPACING] = reader_row(SPACING, 32, "in", "Spacing on center")
        unnamed[END] = reader_row(END, 11, "in", "End distance")
        self.assertEqual(len(takeoff.symbol_rows(it("Spaces", SPACES, 5, "spaces"), unnamed)), 2)



if __name__ == "__main__":
    unittest.main()
