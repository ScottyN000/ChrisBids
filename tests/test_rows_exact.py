"""Every branch of the reader-to-row mapping, compared as whole Claims.

Field-by-field assertions leave room for a mutant that changes a field nobody
looked at. Comparing the entire Claim does not, so these tests were written to
kill the surviving mutants in pipeline/readers/rows.py.
"""
import unittest

from pipeline.readers import rows
from pipeline.readers.vote import Voted, vote
from pipeline.schema import Claim

DET = rows.Unit(unit_id="S-1#Det1", source_id="S-1", locator="Support Detail 1/S-1",
                tag="S-1 Det 1", scale="3/4\"=1'-0\"")
FND = rows.Unit(unit_id="S-1#Fnd", source_id="S-1", locator="Partial Foundation Plan", tag="S-1 Fnd")


def item(kind, label, text=None, count=None, unit=None):
    return {"kind": kind, "label": label, "text": text, "count": count, "unit": unit}


def one(reader, unit, voted):
    (c,) = rows.to_claims(reader, "NAN", unit, [voted])
    return c


class DrawingCase(unittest.TestCase):
    def test_dimension_agreed(self):
        v = Voted(item("dimension", "  Bracket run ", "15'-2\""), seen=3, runs=3)
        self.assertEqual(one("drawing", FND, v), Claim(
            claim_id="NAN-DR-S1FND-01", source_id="S-1", locator="Partial Foundation Plan", tag="S-1 Fnd",
            statement="Bracket run: 15'-2\"", method="dimensioned", role="quantity", confidence="exact",
            value="182", value_num=182.0, unit="in", flag="",
            derivation="15'-2\" dimension string = 182 in"))

    def test_every_runs_wording_of_a_dimension_label_is_kept(self):
        """Takeoff reads the label to learn what the string spans; one run's wording can miss it."""
        runs = [[item("dimension", "Top wall segment: 11\"", "11\"")],
                [item("dimension", " First bracket from the wall face ", "11\"")],
                [item("dimension", "top wall segment: 11\".", "11\"")]]
        (v,) = vote("drawing", runs)
        self.assertEqual((v.seen, v.status, v.labels), (3, "agree", ["Top wall segment: 11\"", "First bracket from the wall face"]))
        c = one("drawing", FND, v)
        self.assertEqual((c.statement, c.flag, c.derivation), (
            "Top wall segment: 11\" or First bracket from the wall face: 11\"", "",
            "11\" dimension string = 11 in; runs word the label differently"))
        runs = [[item("dimension", "Bracket spacing, printed 5 times along the run", "2'-8\"")],
                [item("dimension", "Bracket spacing: 2'-8\"", "2'-8\"")]]
        (v,) = vote("drawing", runs)
        self.assertEqual((v.labels, v.counted_label), (["Bracket spacing: 2'-8\""], False))
        # When every wording carries a count, the first is kept as printed and the row says so.
        (v,) = vote("drawing", [[item("dimension", "5 spaces at 2'-8\"", "2'-8\"")]])
        self.assertEqual((v.labels, v.counted_label), (["5 spaces at 2'-8\""], True))
        self.assertEqual(one("drawing", FND, v).derivation,
                         "2'-8\" dimension string = 32 in; the label carries a count with no counted row")
        (v,) = vote("drawing", [[item("dimension", "Wall face to first bracket (2 PLACES)", "11\"")],
                                [item("dimension", "Wall face to first bracket, 2 places, see 1/S-1", "11\"")]])
        self.assertEqual((v.labels, v.counted_label), (["Wall face to first bracket (2 PLACES)"], True))
        self.assertEqual(one("drawing", FND, v).statement, "Wall face to first bracket (2 PLACES): 11\"")
        # A count made of the string's own digits is still a count.
        (v,) = vote("drawing", [[item("dimension", "Bracket spacing, 2 places", "2'-8\"")],
                                [item("dimension", "8 brackets at 2'-8\"", "2'-8\"")]])
        self.assertEqual((v.labels, v.counted_label), (["Bracket spacing, 2 places"], True))
        (v,) = vote("drawing", [[item("dimension", "Bracket spacing (5) SPACES", "2'-8\"")],
                                [item("dimension", "11\" TYP. 2 PLACES", "2'-8\"")]])
        self.assertEqual((v.labels, v.counted_label), (["Bracket spacing (5) SPACES"], True))
        # Sheet and detail references, and numbers joined to a letter, are not counts.
        runs = [[item("dimension", "Bracket spacing (REF. DET. 1/S-1)", "11\"")],
                [item("dimension", "Wall face to first bracket (2 places), see S-1", "11\"")]]
        (v,) = vote("drawing", runs)
        self.assertEqual((v.labels, v.counted_label), (["Bracket spacing (REF. DET. 1/S-1)"], False))
        (v,) = vote("drawing", [[item("dimension", "Bracket run, see detail 1 (2x4 blocking)", "15'-2\"")]])
        self.assertEqual((v.labels, v.counted_label), (["Bracket run, see detail 1 (2x4 blocking)"], False))
        (v,) = vote("drawing", [[item("count", "Brackets", count=6, unit="each")],
                                [item("count", "Bracket symbols", count=6, unit="each")]])
        self.assertEqual((v.labels, one("drawing", FND, v).statement), ([], "Brackets"))

    def test_dimension_fractional_keeps_its_fraction(self):
        v = Voted(item("dimension", "Gap", "1/2\""), seen=3, runs=3)
        c = one("drawing", FND, v)
        self.assertEqual((c.value, c.value_num), ("0.5", 0.5))

    def test_dimension_seen_in_some_runs(self):
        v = Voted(item("dimension", "Bracket run", "15'-2\""), seen=2, runs=3)
        c = one("drawing", FND, v)
        self.assertEqual(c.flag, "unverified")
        self.assertEqual(c.derivation, "15'-2\" dimension string = 182 in; seen in 2 of 3 runs")

    def test_dimension_runs_disagree(self):
        v = Voted(item("dimension", "Bracket run", "15'-2\""), seen=3, runs=3,
                  readings={"text": ["15'-2\"", "15'-4\""]})
        self.assertEqual(one("drawing", FND, v), Claim(
            claim_id="NAN-DR-S1FND-01", source_id="S-1", locator="Partial Foundation Plan", tag="S-1 Fnd",
            statement="Bracket run: 15'-2\" (reading A) / 15'-4\" (reading B)", method="dimensioned",
            role="quantity", confidence="exact", value="15'-2\" (reading A) / 15'-4\" (reading B)",
            value_num=None, unit="in", flag="conflict",
            derivation="runs disagree on text; every reading kept, none chosen"))

    def test_count_agreed_with_and_without_a_unit(self):
        c = one("drawing", DET, Voted(item("count", "Anchors", count=3, unit="per bracket"), seen=3, runs=3))
        self.assertEqual(c, Claim(
            claim_id="NAN-DR-S1DET1-01", source_id="S-1", locator="Support Detail 1/S-1", tag="S-1 Det 1",
            statement="Anchors", method="counted", role="quantity", confidence="exact",
            value="3", value_num=3.0, unit="per bracket", flag="", derivation=""))
        c = one("drawing", DET, Voted(item("count", "Brackets", count=6), seen=3, runs=3))
        self.assertEqual(c.unit, "each")

    def test_count_runs_disagree(self):
        v = Voted(item("count", "Anchors", count=3, unit="per bracket"), seen=3, runs=3,
                  readings={"count": [3, 4]})
        self.assertEqual(one("drawing", DET, v), Claim(
            claim_id="NAN-DR-S1DET1-01", source_id="S-1", locator="Support Detail 1/S-1", tag="S-1 Det 1",
            statement="Anchors: 3 (reading A) / 4 (reading B)", method="counted", role="quantity",
            confidence="exact", value="3 (reading A) / 4 (reading B)", value_num=None,
            unit="per bracket", flag="conflict",
            derivation="runs disagree on count; every reading kept, none chosen"))

    def test_scaled_single_reading_on_a_view_with_and_without_a_scale(self):
        v = Voted(item("scaled", "Vertical leg", "1'-8\""), seen=3, runs=3)
        self.assertEqual(one("drawing", DET, v), Claim(
            claim_id="NAN-DR-S1DET1-01", source_id="S-1", locator="Support Detail 1/S-1 at 3/4\"=1'-0\"",
            tag="S-1 Det 1", statement="Vertical leg, scaled", method="scaled", role="note",
            confidence="scaled", value="1'-8\"", flag="", derivation=""))
        self.assertEqual(one("drawing", FND, v).locator, "Partial Foundation Plan")

    def test_scaled_runs_disagree(self):
        v = Voted(item("scaled", "Vertical leg", "1'-8\""), seen=3, runs=3,
                  readings={"text": ["1'-8\"", "1'-10\"", "2'-0\""]})
        c = one("drawing", DET, v)
        self.assertEqual(c.value, "1'-8\" (reading A) / 1'-10\" (reading B) / 2'-0\" (reading C)")
        self.assertEqual(c.flag, "conflict")

    def test_note_is_quoted(self):
        v = Voted(item("note", "General note 4", "ALL STEEL SHALL BE GALVANIZED"), seen=3, runs=3)
        self.assertEqual(one("drawing", FND, v), Claim(
            claim_id="NAN-DR-S1FND-01", source_id="S-1", locator="Partial Foundation Plan", tag="S-1 Fnd",
            statement="General note 4", method="clause", role="note", confidence="exact",
            quote="ALL STEEL SHALL BE GALVANIZED", flag="", derivation=""))

    def test_claim_numbers_run_in_order(self):
        voted = vote("drawing", [[item("count", "a", count=1), item("count", "b", count=2)]])
        ids = [c.claim_id for c in rows.to_claims("drawing", "NAN", FND, voted)]
        self.assertEqual(ids, ["NAN-DR-S1FND-01", "NAN-DR-S1FND-02"])

    def test_slug_keeps_the_last_ten_characters_and_never_goes_empty(self):
        self.assertEqual(rows._slug("ocean-beach-villas#page-17"), "LLASPAGE17")
        self.assertEqual(rows._slug("#-"), "U")


class SemanticCase(unittest.TestCase):
    def errs(self, *items):
        return rows.semantic_errors("drawing", {"items": list(items)})

    def test_each_rule_names_its_item(self):
        self.assertEqual(self.errs(item("dimension", "x", None)),
                         ["items[0]: a dimension needs the dimension string as printed"])
        self.assertEqual(self.errs(item("dimension", "x", "11\"", count=2)),
                         ["items[0]: a dimension carries no count"])
        self.assertEqual(self.errs(item("count", "x")),
                         ["items[0]: a count needs the number of symbols drawn"])
        self.assertEqual(self.errs(item("scaled", "x")),
                         ["items[0]: a scaled item needs the reading"])
        self.assertEqual(self.errs(item("note", "x")),
                         ["items[0]: a note item needs the text as printed"])
        self.assertEqual(self.errs(item("count", "ok", count=1), item("load", "x")),
                         ["items[1]: a load item needs the text as printed"])

    def test_a_count_of_zero_is_a_count(self):
        self.assertEqual(self.errs(item("count", "x", count=0)), [])

    def test_other_readers_have_no_extra_rules(self):
        self.assertEqual(rows.semantic_errors("photo", {"items": [{"kind": "dimension", "text": None}]}), [])


class OtherReadersCase(unittest.TestCase):
    PAGE = rows.Unit(unit_id="SW#p17", source_id="SW", locator="p.17", tag="SW p.17")
    PHOTO = rows.Unit(unit_id="IMG_8343", source_id="IMG_8343", locator="", tag="IMG_8343")
    MAIL = rows.Unit(unit_id="CT#1", source_id="CT", locator="email", tag="CT")

    def test_spec_with_clause_and_product(self):
        v = Voted({"clause": "B", "requirement": "Finish Coat (1 coat)", "division": "09",
                   "product": "B53 series"}, seen=3, runs=3)
        self.assertEqual(one("spec", self.PAGE, v), Claim(
            claim_id="NAN-SP-SWP17-01", source_id="SW", locator="p.17 B", tag="SW p.17",
            statement="Finish Coat (1 coat) [product: B53 series]", method="clause", role="scope",
            confidence="exact", division="09", quote="Finish Coat (1 coat)", flag="", derivation=""))

    def test_spec_without_clause_product_or_division(self):
        v = Voted({"clause": None, "requirement": "Remove loose paint", "division": None,
                   "product": None}, seen=3, runs=3)
        c = one("spec", self.PAGE, v)
        self.assertEqual((c.locator, c.statement, c.division), ("p.17", "Remove loose paint", ""))

    def test_photo_without_severity(self):
        v = Voted({"location": "walkway", "condition": "peeling", "severity": None,
                   "description": "coating peeling"}, seen=3, runs=3)
        self.assertEqual(one("photo", self.PHOTO, v), Claim(
            claim_id="NAN-PH-IMG8343-01", source_id="IMG_8343", locator="", tag="IMG_8343",
            statement="coating peeling", method="observed", role="note", confidence="inferred",
            flag="", derivation="condition peeling at walkway"))

    def test_photo_with_severity_and_with_disagreeing_severity(self):
        v = Voted({"location": "wall", "condition": "crack", "severity": "minor",
                   "description": "hairline cracks"}, seen=3, runs=3)
        self.assertEqual(one("photo", self.PHOTO, v).derivation, "condition crack at wall, severity minor")
        v = Voted({"location": "wall", "condition": "crack", "severity": "minor",
                   "description": "hairline cracks"}, seen=3, runs=3, readings={"severity": ["minor", None]})
        c = one("photo", self.PHOTO, v)
        self.assertEqual(c.derivation, "condition crack at wall, severity minor / not stated; "
                                       "runs disagree on severity; every reading kept, none chosen")
        self.assertEqual(c.flag, "conflict")

    def test_correspondence_with_and_without_a_date(self):
        v = Voted({"sender": "Estimator", "date": "2026-10-06",
                   "instruction": "Keep the base bid simple"}, seen=3, runs=3)
        self.assertEqual(one("correspondence", self.MAIL, v), Claim(
            claim_id="NAN-CO-CT1-01", source_id="CT", locator="email, Estimator, 2026-10-06", tag="CT",
            statement="Keep the base bid simple", method="customer", role="note", confidence="exact",
            quote="Keep the base bid simple", flag="", derivation=""))
        v.item["date"] = None
        self.assertEqual(one("correspondence", self.MAIL, v).locator, "email, Estimator, date not stated")


if __name__ == "__main__":
    unittest.main()
