"""Takeoff: formulas the model writes, evaluated and written by code.

Rows are compared as whole Claims where a mutant could otherwise change a field
nobody looked at.
"""
import json
import tempfile
import unittest
from pathlib import Path

from pipeline import takeoff
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

    def test_the_same_figure_by_a_second_route_is_not_extra(self):
        """Run 1 wrote 18 anchors as 3 x the 6 symbols, run 2 as 3 x the brackets the
        dimensions space out (live run 37938174100): one fixture figure, not one extra."""
        split = lambda c: Claim(**{**c.__dict__, "flag": "unverified", "derivation": c.derivation + "; seen in 1 of 2 runs"})
        t2 = self.q("T2", 18, "each", f"{{{ANCH}}} * {{{self.BRACKETS}}}")
        t3 = self.q("T3", 18, "each", f"{{{ANCH}}} * ({SPACES} + 1)")
        cmp = self.compare([self.q("T1", 5, "spaces", SPACES), split(t2), split(t3)])
        self.assertTrue(cmp.exact_ok, cmp.text())
        self.assertEqual(cmp.extra, [])
        self.assertEqual(cmp.matched.count(("counted", "derived", "18", "each")), 1)
        # The same pair written by every run is a second 18 the fixture lacks: one extra.
        cmp = self.compare([self.q("T1", 5, "spaces", SPACES), t2, t3])
        self.assertFalse(cmp.exact_ok)
        self.assertEqual(cmp.extra, [("counted", "derived", "18", "each")])
        # One split row covers one surplus: a third 18 every run wrote is still extra.
        cmp = self.compare([self.q("T1", 5, "spaces", SPACES), t2, split(t3),
                            self.q("T4", 18, "each", f"{{{ANCH}}} * 6")])
        self.assertEqual(cmp.extra, [("counted", "derived", "18", "each")])
        # A figure the fixture lacks is still extra, however many routes reach it.
        cmp = self.compare([self.q("T1", 5, "spaces", SPACES), self.q("T2", 21, "each", f"{{{ANCH}}} * 7"),
                            self.q("T3", 21, "each", f"({{{ANCH}}} + 4) * 3")])
        self.assertEqual(cmp.extra, [("counted", "derived", "21", "each")] * 2)

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


if __name__ == "__main__":
    unittest.main()
