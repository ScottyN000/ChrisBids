"""A figure stated as a range is one figure with two ends (a spread rate of
320-400 sq ft/gal), and an order quantity may rest on a fetched one
(architecture p.4); the calc is replayed at every corner."""
import tempfile
import unittest
from pathlib import Path

from pipeline import intake, schema
from pipeline.broker import Broker
from pipeline.schema import Claim, LedgerError

REGISTER = [
    {"source_id": "S-1", "title": "Drawing S-1", "file": "s1.pdf", "sha256": "abc", "pages": "1",
     "kind": "drawing", "status": "present"},
    dict(intake.WEB),
]
SHEET = "https://www.example.com/sheet.pdf"


def claim(**kw):
    base = dict(claim_id="T-A-001", statement="deck area", source_id="S-1", method="dimensioned", role="quantity",
                confidence="exact", value="1200", value_num=1200.0, unit="sq ft", locator="A-1")
    base.update(kw)
    return Claim(**base)


def rate(**kw):
    base = dict(claim_id="T-WEB-001", statement="SuperPaint A89: 320-400 sq ft/gal", source_id="WEB",
                method="fetched", role="code", value="320-400", value_num=None, unit="sq ft/gal", locator="ask a1",
                url=SHEET, retrieved="2026-10-09", quote="320-400 sq. ft. per gallon")
    base.update(kw)
    return claim(**base)


def order(**kw):
    base = dict(claim_id="T-M-001", statement="SuperPaint A89, 2 coats on the deck", source_id="S-1 + WEB",
                method="fetched", role="material", confidence="exact", value="6-7.5", value_num=None, unit="gal",
                calc="{T-A-001} * 2 / {T-WEB-001}", url=SHEET, retrieved="2026-10-09",
                quote="320-400 sq. ft. per gallon")
    base.update(kw)
    return Claim(**base)


class RangeCase(unittest.TestCase):
    def test_a_range_is_two_ends_low_first(self):
        for text, want in (("320-400", (320.0, 400.0)), ("320 - 400", (320.0, 400.0)),
                           ("320 – 400", (320.0, 400.0)), ("400-320", (320.0, 400.0)),
                           ("2,000-2,500", (2000.0, 2500.0)), ("1.5-2", (1.5, 2.0)), ("2-4", (2.0, 4.0))):
            self.assertEqual(schema.value_range(text), want, text)
        # a single figure, a date, a fraction and words are not ranges
        for text in ("", "350", "2026-10-07", "1-1/2", "a-b", "-5", "3-", None):
            self.assertIsNone(schema.value_range(text), text)

    def test_a_replayed_range_prints_whole_ends_without_a_point(self):
        self.assertEqual(schema.format_range(40.0, 50.0), "40-50")
        self.assertEqual(schema.format_range(6.0, 7.5), "6-7.5")

    def test_a_calc_over_a_range_is_replayed_at_both_ends(self):
        by_id = {"T-A-001": claim(), "T-WEB-001": rate()}
        self.assertEqual(schema.replay_calc(order(), by_id), [])
        # the row's value must be the range the ends give (read in either order)
        self.assertEqual(schema.replay_calc(order(value="7.5-6"), by_id), [])
        for value in ("6-7", "6", ""):
            self.assertEqual(schema.replay_calc(order(value=value, value_num=6.0 if value == "6" else None), by_id),
                             [f"T-M-001: calc {{T-A-001}} * 2 / {{T-WEB-001}} = 6-7.5, ledger says {value or '(blank)'}"],
                             value)

    def test_two_ranges_give_the_corners(self):
        by_id = {"A": claim(claim_id="A", value="100-200", value_num=None),
                 "B": claim(claim_id="B", value="4-5", value_num=None)}
        self.assertEqual(schema.replay_calc(claim(claim_id="Q", method="counted", value="20-50", value_num=None,
                                                  calc="{A} / {B}"), by_id), [])
        self.assertEqual(schema.replay_calc(claim(claim_id="Q", method="counted", value="400-1000", value_num=None,
                                                  calc="{A} * {B}"), by_id), [])

    def test_a_range_with_equal_ends_is_one_figure(self):
        by_id = {"T-A-001": claim(), "T-WEB-001": rate(value="400-400")}
        self.assertEqual(schema.replay_calc(order(value="6", value_num=6.0), by_id), [])
        self.assertEqual(schema.replay_calc(order(value="6-7.5"), by_id),
                         ["T-M-001: calc {T-A-001} * 2 / {T-WEB-001} = 6.0, ledger says 6-7.5"])

    def test_a_calc_rests_on_a_few_ranges_at_most(self):
        by_id = {f"R{i}": claim(claim_id=f"R{i}", value="1-2", value_num=None) for i in range(4)}
        three = claim(claim_id="Q", method="counted", value="3-6", value_num=None, calc="{R0} + {R1} + {R2}")
        self.assertEqual(schema.replay_calc(three, by_id), [])
        four = claim(claim_id="Q", method="counted", value="4-8", value_num=None, calc="{R0} + {R1} + {R2} + {R3}")
        self.assertEqual(schema.replay_calc(four, by_id),
                         ["Q: calc '{R0} + {R1} + {R2} + {R3}' rests on more than 3 ranges"])

    def test_a_range_end_that_does_not_evaluate_is_an_error(self):
        by_id = {"T-A-001": claim(), "T-WEB-001": rate(value="0-400")}
        self.assertEqual(schema.replay_calc(order(), by_id),
                         ["T-M-001: calc '{T-A-001} * 2 / {T-WEB-001}' does not evaluate: float division by zero"])
        by_id["T-WEB-001"] = rate(value="about 350")
        self.assertEqual(schema.replay_calc(order(), by_id), ["T-M-001: calc input T-WEB-001 has no numeric value"])

    def test_what_may_feed_an_order_quantity_and_an_allowance(self):
        by_id = {"T-A-001": claim(), "T-WEB-001": rate(), "T-S-001": claim(claim_id="T-S-001", method="scaled",
                                                                           confidence="scaled", value="40", value_num=40.0)}
        # a fetched rate may feed an order quantity (p.4), never an allowance; scaled feeds neither (p.6)
        self.assertEqual(schema.replay_calc(order(), by_id), [])
        self.assertEqual(schema.replay_calc(order(role="allowance", method="counted"), by_id),
                         ["T-M-001: calc input T-WEB-001 is fetched; it cannot feed an allowance"])
        self.assertEqual(schema.replay_calc(order(calc="{T-S-001} * 2 / {T-WEB-001}", value="0.2-0.25"), by_id),
                         ["T-M-001: calc input T-S-001 is scaled; it cannot feed an order quantity"])
        # one method note per row used, however often the formula names it; a range named twice is refused
        # (the corner replay bounds a formula that uses each range once), a single figure may repeat
        self.assertEqual(schema.replay_calc(order(role="allowance", method="counted", calc="{T-WEB-001} + {T-WEB-001}",
                                                  value="640-800"), by_id),
                         ["T-M-001: calc input T-WEB-001 is fetched; it cannot feed an allowance",
                          "T-M-001: calc names range input T-WEB-001 more than once; write it once (2 * {T-WEB-001})"])
        self.assertEqual(schema.replay_calc(claim(claim_id="Q", method="counted", role="quantity", value="16",
                                                  value_num=16.0, calc="{T-A-001} * (10 - {T-A-001})"),
                                            {"T-A-001": claim(claim_id="T-A-001", value="2", value_num=2.0)}), [])
        # a counted row still rests on dimensioned and counted rows only
        self.assertEqual(schema.replay_calc(claim(claim_id="Q", method="counted", role="quantity", value="640-800",
                                                  value_num=None, calc="{T-WEB-001} * 2"), by_id),
                         ["Q: calc input T-WEB-001 is fetched; a counted row rests on dimensioned and counted rows only"])

    def test_the_method_rules_for_material_and_allowance_rows(self):
        self.assertEqual(schema.check_method_rules(order()), [])
        self.assertEqual(schema.check_method_rules(order(role="allowance")),
                         ["T-M-001: a fetched value may not feed an allowance"])
        self.assertEqual(schema.check_method_rules(order(method="scaled", confidence="scaled", url="", retrieved="",
                                                         quote="")),
                         ["T-M-001: a scaled value may not feed an order quantity"])
        self.assertEqual(schema.MATERIAL_OK, ("dimensioned", "counted", "clause", "FIELD", "fetched"))
        self.assertEqual(schema.ALLOWANCE_OK, ("dimensioned", "counted", "clause", "FIELD"))


class LedgerCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "ledger.db"
        self.intake = Broker.open_job(self.db, "intake", job="T", run_id="r1", create=True,
                                      clock=lambda: "2026-10-09T00:00:00Z")
        self.intake.write_register(REGISTER)
        self.who("drawing_reader").append(claim())
        self.who("materials").append(rate())

    def tearDown(self):
        self.intake.close()
        self.tmp.cleanup()

    def who(self, principal):
        return self.intake.as_principal(principal)

    def test_materials_writes_an_order_quantity_over_a_fetched_rate(self):
        row = self.who("materials").append(order())
        back = self.intake.ledger.by_id()["T-M-001"]
        self.assertEqual((row.agent, back.value, back.value_num, back.unit, back.calc, back.method, back.role),
                         ("materials", "6-7.5", None, "gal", "{T-A-001} * 2 / {T-WEB-001}", "fetched", "material"))
        self.assertEqual(schema.value_range(back.value), (6.0, 7.5))

    def test_the_broker_refuses_a_wrong_range_and_a_fetched_allowance(self):
        with self.assertRaises(LedgerError) as e:
            self.who("materials").append(order(value="6-7"))
        self.assertIn("= 6-7.5, ledger says 6-7", str(e.exception))
        with self.assertRaises(LedgerError) as e:
            self.who("takeoff").append(order(claim_id="T-AL-001", role="allowance", method="counted", url="",
                                             retrieved="", quote=""))
        self.assertIn("cannot feed an allowance", str(e.exception))
        self.assertEqual(set(self.intake.ledger.by_id()) - {"T-A-001", "T-WEB-001"}, set())


if __name__ == "__main__":
    unittest.main()
