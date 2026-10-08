"""The reader layer: schemas, arithmetic, the vote, the method rules and the golden replay."""
import json
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path

from pipeline import auditor
from pipeline.broker import Broker
from pipeline.readers import compare, golden, rows, run, schemas, units, validate
from pipeline.readers.clients import ReplayClient, prompt_version
from pipeline.readers.vote import vote
from pipeline.schema import Claim, LedgerError

ROOT = Path(__file__).resolve().parent.parent


def dim(label, text):
    return {"kind": "dimension", "label": label, "text": text, "count": None, "unit": None}


def cnt(label, n, unit="each"):
    return {"kind": "count", "label": label, "text": None, "count": n, "unit": unit}


class UnitsCase(unittest.TestCase):
    def test_feet_and_inches_become_inches_in_code(self):
        self.assertEqual(units.to_inches("15'-2\""), 182)
        self.assertEqual(units.to_inches("2'-8\""), 32)
        self.assertEqual(units.to_inches("11\""), 11)
        self.assertEqual(units.to_inches("12'"), 144)
        self.assertEqual(units.to_inches("3/4\""), Fraction(3, 4))
        self.assertEqual(units.to_inches("15'-2 1/2\""), Fraction(365, 2))
        self.assertEqual(units.derivation("15'-2\""), "15'-2\" dimension string = 182 in")

    def test_anything_else_is_refused_not_guessed(self):
        for text in ("about 15 feet", "15.2", "", "~1'-8\"", "15'-2"):
            with self.assertRaises(units.DimensionError, msg=text):
                units.to_inches(text)


class SchemaCase(unittest.TestCase):
    def test_the_photo_schema_cannot_carry_a_number(self):
        ok = {"items": [{"location": "walkway", "condition": "peeling", "severity": None,
                         "description": "deck coating peeling over a wide area"}]}
        self.assertEqual(validate.errors(ok, schemas.PHOTO), [])
        bad = {"items": [{"location": "walkway", "condition": "peeling", "severity": None,
                          "description": "about 40 SF of peeling"}]}
        self.assertTrue(validate.errors(bad, schemas.PHOTO))
        extra = {"items": [dict(ok["items"][0], quantity=40)]}
        self.assertTrue(any("unexpected field" in e for e in validate.errors(extra, schemas.PHOTO)))

    def test_vocabularies_are_closed(self):
        bad = {"items": [{"location": "lanai", "condition": "peeling", "severity": None, "description": "x"}]}
        self.assertTrue(validate.errors(bad, schemas.PHOTO))
        bad = {"items": [dict(dim("run", "15'-2\""), kind="estimate")]}
        self.assertTrue(validate.errors(bad, schemas.DRAWING))

    def test_not_stated_is_null_and_every_field_is_required(self):
        missing = {"items": [{"kind": "count", "label": "brackets", "count": 6}]}
        errs = validate.errors(missing, schemas.DRAWING)
        self.assertTrue(any("missing text" in e for e in errs))
        self.assertTrue(any("missing unit" in e for e in errs))

    def test_drawing_rules_the_schema_cannot_express(self):
        self.assertEqual(rows.semantic_errors("drawing", {"items": [dim("run", "15'-2\"")]}), [])
        self.assertTrue(rows.semantic_errors("drawing", {"items": [dim("run", "about fifteen feet")]}))
        self.assertTrue(rows.semantic_errors("drawing", {"items": [cnt("anchors", None)]}))
        scaled_with_count = {"kind": "scaled", "label": "leg", "text": "1'-8\"", "count": 2, "unit": None}
        self.assertTrue(rows.semantic_errors("drawing", {"items": [scaled_with_count]}))

    def test_prompt_versions_are_content_hashes(self):
        v = prompt_version("drawing")
        self.assertRegex(v, r"^drawing@[0-9a-f]{12}$")
        self.assertEqual(v, prompt_version("drawing"))
        self.assertNotEqual(v.split("@")[1], prompt_version("photo").split("@")[1])

    def test_prompt_examples_are_schema_valid(self):
        """Every JSON example in a prompt must itself pass the reader's schema."""
        import re
        for reader, schema in schemas.BY_READER.items():
            text = (ROOT / "pipeline" / "readers" / "prompts" / f"{reader}.md").read_text()
            blocks = re.findall(r"```json\n(.*?)```", text, re.S)
            self.assertTrue(blocks, reader)
            for block in blocks:
                data = json.loads(block)
                self.assertEqual(validate.errors(data, schema), [], f"{reader}: {block[:60]}")
                self.assertEqual(rows.semantic_errors(reader, data), [], reader)


class VoteCase(unittest.TestCase):
    def test_agreement_passes(self):
        runs = [[cnt("brackets", 6)]] * 3
        (v,) = vote("drawing", runs)
        self.assertEqual(v.status, "agree")

    def test_disagreement_keeps_every_reading_and_picks_none(self):
        a = {"kind": "scaled", "label": "leg", "text": "1'-8\"", "count": None, "unit": None}
        (v,) = vote("drawing", [[a], [dict(a, text="1'-10\"")], [a]])
        self.assertEqual(v.status, "conflict")
        self.assertEqual(v.readings["text"], ["1'-8\"", "1'-10\""])

    def test_a_count_read_differently_is_two_flagged_rows_and_neither_is_chosen(self):
        runs = [[cnt("brackets", 6)], [cnt("brackets", 7)], [cnt("brackets", 6)]]
        self.assertEqual([(v.item["count"], v.seen, v.status) for v in vote("drawing", runs)],
                         [(6, 2, "partial"), (7, 1, "partial")])

    def test_drawing_figures_match_across_runs_whatever_their_label(self):
        runs = [[dim("Bracket spacing on center", "2'-8\""), cnt("Bracket symbols", 6)],
                [dim("Spacing, segment 1", "2'-8\""), dim("Spacing, segment 2", "2'-8\""), cnt("brackets drawn", 6)],
                [cnt("Brackets", 6), dim("Bracket spacing", "2'-8\"")]]
        voted = vote("drawing", runs)
        self.assertEqual([(v.item["label"], v.status) for v in voted],
                         [("Bracket spacing on center", "agree"), ("Bracket symbols", "agree")])

    def test_two_equal_counts_in_one_view_stay_two(self):
        runs = [[cnt("anchors", 3, "per bracket"), cnt("bolts", 3, "per bracket")],
                [cnt("bolts (TYP.)", 3, "per bracket"), cnt("anchors (TYP.)", 3, "per bracket")],
                [cnt("anchors", 3, "per bracket")]]
        self.assertEqual([(v.item["label"], v.seen) for v in vote("drawing", runs)],
                         [("anchors", 3), ("bolts", 2)])

    def test_an_item_only_some_runs_saw_is_flagged(self):
        runs = [[cnt("brackets", 6), cnt("bolts", 3)], [cnt("brackets", 6)], [cnt("brackets", 6)]]
        voted = vote("drawing", runs)
        self.assertEqual([v.status for v in voted], ["agree", "partial"])

    def test_description_wording_may_vary_between_photo_runs(self):
        a = {"location": "walkway", "condition": "peeling", "severity": None, "description": "coating peeling"}
        b = dict(a, description="the coating is peeling")
        (v,) = vote("photo", [[a], [b], [a]])
        self.assertEqual(v.status, "agree")


class RowsCase(unittest.TestCase):
    unit = rows.Unit(unit_id="S-1#Det1", source_id="S-1", locator="Support Detail 1/S-1",
                     tag="S-1 Det 1", scale="3/4\"=1'-0\"")

    def test_the_method_is_fixed_by_rule(self):
        scaled = {"kind": "scaled", "label": "Vertical angle leg length", "text": "1'-8\"",
                  "count": None, "unit": None}
        note = {"kind": "note", "label": "General note", "text": "ALL STEEL SHALL BE GALVANIZED",
                "count": None, "unit": None}
        voted = vote("drawing", [[dim("run", "15'-2\""), cnt("anchors", 3, "per bracket"), scaled, note]])
        got = rows.to_claims("drawing", "NAN", self.unit, voted)
        self.assertEqual([c.method for c in got], ["dimensioned", "counted", "scaled", "clause"])
        self.assertEqual((got[0].value, got[0].unit), ("182", "in"))
        self.assertEqual(got[2].confidence, "scaled")
        self.assertEqual(got[2].locator, "Support Detail 1/S-1 at 3/4\"=1'-0\"")
        self.assertEqual(got[3].quote, "ALL STEEL SHALL BE GALVANIZED")

    def test_two_scalings_print_the_way_the_fixtures_do(self):
        a = {"kind": "scaled", "label": "Vertical angle leg length", "text": "1'-8\"", "count": None, "unit": None}
        b = dict(a, text="1'-10\"")
        (c,) = rows.to_claims("drawing", "NAN", self.unit, vote("drawing", [[a], [b], [a]]))
        self.assertEqual(c.value, "1'-8\" (reading A) / 1'-10\" (reading B)")
        self.assertEqual(c.flag, "conflict")

    def test_claim_ids_are_deterministic(self):
        voted = vote("drawing", [[cnt("brackets", 6)]])
        first = rows.to_claims("drawing", "NAN", self.unit, voted)[0].claim_id
        self.assertEqual(first, rows.to_claims("drawing", "NAN", self.unit, voted)[0].claim_id)
        self.assertEqual(first, "NAN-DR-S1DET1-01")


class RunCase(unittest.TestCase):
    """The run loop against a fake client: discarding, unread units, broker scope."""

    REGISTER = [{"source_id": "S-1", "title": "S-1", "file": "", "sha256": "", "pages": "1",
                 "kind": "drawing", "status": "present"}]

    class Fake:
        model_id = "fake"

        def __init__(self, answers):
            self.answers = answers

        def complete(self, reader, unit, system, schema, run):
            return self.answers[run]

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.broker = Broker.open_job(Path(self.tmp.name) / "l.db", "intake", job="T", run_id="r", create=True)
        self.broker.write_register(self.REGISTER)
        self.unit = rows.Unit(unit_id="S-1#Fnd", source_id="S-1", locator="Partial Foundation Plan", tag="S-1 Fnd")

    def tearDown(self):
        self.broker.close()
        self.tmp.cleanup()

    def test_invalid_responses_are_discarded_not_repaired(self):
        good = {"items": [cnt("brackets", 6)]}
        answers = ["not json", {"items": [{"kind": "count"}]}, json.dumps(good)]
        res = run.read(self.broker, "drawing", "T", [self.unit], self.Fake(answers))
        self.assertEqual(len(res.discarded), 2)
        self.assertEqual(len(res.rows), 1)
        # One valid run of three: still written, but it cannot be called agreement.
        self.assertEqual(res.rows[0].flag, "unverified")
        self.assertIn("only 1 of 3 runs", res.rows[0].derivation)

    def test_a_unit_with_no_valid_response_writes_nothing(self):
        res = run.read(self.broker, "drawing", "T", [self.unit], self.Fake(["x", "y", "z"]))
        self.assertEqual(res.unread, ["S-1#Fnd"])
        self.assertEqual(self.broker.ledger.claims(), [])

    def test_rows_are_written_as_the_readers_own_principal(self):
        run.read(self.broker, "drawing", "T", [self.unit], self.Fake([{"items": [cnt("brackets", 6)]}] * 3))
        principals = {r["principal"] for r in self.broker.ledger.db.execute("SELECT principal FROM claims")}
        self.assertEqual(principals, {"drawing_reader"})
        (c,) = self.broker.ledger.claims()
        self.assertEqual(c.model_id, "fake")
        self.assertTrue(c.prompt_version.startswith("drawing@"))

    def test_every_call_is_logged(self):
        run.read(self.broker, "drawing", "T", [self.unit], self.Fake([{"items": []}] * 3))
        calls = [r for r in self.broker.ledger.log() if r["action"] == "model-call"]
        self.assertEqual(len(calls), 3)
        self.assertIn("output", calls[0]["detail"])

    def test_the_photo_reader_cannot_write_a_dimension_even_if_the_mapping_tried(self):
        writer = self.broker.as_principal("photo_reader")
        with self.assertRaises(LedgerError):
            writer.append(Claim(claim_id="X-1", statement="run", source_id="S-1", method="dimensioned",
                                role="quantity", confidence="exact", value="182", value_num=182.0, unit="in"))


class GoldenReplayCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def replay(self, job, **kw):
        broker, results, comparison = golden.replay(ROOT / "fixtures" / job, Path(self.tmp.name) / f"{job}.db", **kw)
        self.addCleanup(broker.close)
        return broker, results, comparison

    def test_nantucket_drawing_rows_reproduce_the_fixture_exactly(self):
        broker, _results, comparison = self.replay("nantucket")
        self.assertTrue(comparison.exact_ok, comparison.text())
        self.assertEqual(comparison.missing, [])
        self.assertEqual(comparison.extra, [])
        exact = [k for k in comparison.matched if k[0] in compare.EXACT]
        self.assertEqual(len(exact), 10)  # NAN-D-001 to NAN-D-008, D-016, D-017
        scaled = [c for c in broker.ledger.claims() if c.method == "scaled"]
        self.assertEqual({c.flag for c in scaled}, {"conflict"})

    def test_ocean_beach_photos_and_spec_page_replay(self):
        broker, results, comparison = self.replay("ocean-beach")
        self.assertTrue(comparison.exact_ok, comparison.text())
        self.assertEqual(len([k for k in comparison.matched if k[0] == "observed"]), 5)
        self.assertEqual(len(results["spec"].rows), 4)
        for c in broker.ledger.claims():
            if c.method == "observed":
                self.assertEqual(c.value, "")

    def test_a_reader_regression_fails_the_comparison(self):
        """Change one recorded count and the gate must catch it."""
        rec = ROOT / "fixtures" / "nantucket" / "recordings" / "drawing.json"
        data = json.loads(rec.read_text())
        for r in data["units"][0]["runs"]:
            r["items"][3]["count"] = 5          # bracket symbols on the foundation plan
        alt = Path(self.tmp.name) / "rec"
        alt.mkdir()
        (alt / "drawing.json").write_text(json.dumps(data))
        _, _, comparison = self.replay("nantucket", client=_ReplayWithUnits(alt))
        self.assertFalse(comparison.exact_ok)
        self.assertIn(("counted", "S-1", "Partial Foundation Plan", "6", "each"), comparison.missing)

    @unittest.skipUnless(Path("/mnt/project-files").exists(), "the project's source packet is not mounted")
    def test_replayed_spec_rows_are_confirmed_on_their_page(self):
        broker, _, _ = self.replay("ocean-beach")
        auditor.run(broker.as_principal("auditor"), packet=Path("/mnt/project-files"))
        spec = [c for c in broker.ledger.claims() if c.claim_id.startswith("OBV-SP-")]
        self.assertEqual({c.audit for c in spec}, {"pass"})


class _ReplayWithUnits(ReplayClient):
    """A ReplayClient pointed at a temporary recordings folder."""


if __name__ == "__main__":
    unittest.main()
