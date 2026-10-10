"""Materials order rows: the model names the rows an order rests on, code works
the quantity out and writes it as a fetched row citing the sheet (architecture
p.4, p.11, p.16)."""
import json
import tempfile
import unittest
from pathlib import Path

from pipeline import intake, materials, schema
from pipeline.broker import Broker
from pipeline.readers import validate
from pipeline.readers.clients import prompt, prompt_version
from pipeline.schema import Claim, LedgerError

ROOT = Path(__file__).resolve().parent.parent
SHEET = "https://www.example.com/x100.pdf"
BULLETIN = "https://www.example.com/a7.pdf"


def row(claim_id, statement, *, method="clause", role="scope", value="", unit="", source="SW", locator="p.4",
        tag="SW p.4", flag="", url="", retrieved="", quote="", calc="", division="09", confidence="exact"):
    v, n = schema.format_value(value)
    return Claim(claim_id=claim_id, statement=statement, source_id=source, method=method, role=role,
                 confidence=confidence, value=v, value_num=n, unit=unit, locator=locator, tag=tag, flag=flag,
                 url=url, retrieved=retrieved, quote=quote, calc=calc, division=division)


SYSTEM = row("X-SP-010", "Finish coat: Example Satin X100, 2 coats")
ONE_COAT = row("X-SP-011", "Touch-up: one coat of Example Satin X100 where scuffed", locator="p.7", tag="SW p.7")
NO_COAT = row("X-SP-012", "Prime coat: Example Primer X50", locator="p.4")
TWO_PRODUCTS = row("X-SP-013", "Finish: Example Satin X100 (coats not stated), or Example Flat X200, 1 coat", locator="p.4")
SEAL_SPEC = row("X-SP-020", "Joint sealant: Example Seal S9 at all control joints", locator="p.6", tag="SW p.6", division="07")
JOINTS = row("X-TK-Q-03", "Control joints, both elevations", method="dimensioned", role="quantity", value="240",
             unit="LF", source="S-1", locator="Sheet A-2", tag="S-1 A-2", division="")
S9 = row("X-WEB-008", "Example Seal S9: 24 LF per 10.1 oz tube at a 1/4 in joint", method="fetched", role="code",
         value="24", unit="LF/tube", source="WEB", locator="ask a1", tag="S9 data sheet",
         url="https://www.example.com/s9.pdf", retrieved="2026-10-09", quote="24 linear feet per tube", division="")
SPEC_RATE = row("X-R-001", "Example Satin X100 coverage per coat", role="quantity", value="300-350",
                unit="sq ft/gal", locator="p.5", tag="SW p.5")
AREA = row("X-TK-Q-01", "Wall area, north and south elevations", method="dimensioned", role="quantity", value="1200",
           unit="sq ft", source="S-1", locator="Sheet A-2", tag="S-1 A-2", division="")
FIELD = row("X-F-002", "Wall SF by elevation", method="FIELD", role="quantity", source="none", locator="", tag="FIELD",
            division="", confidence="missing")
X100 = row("X-WEB-003", "Example Satin X100: 320-400 sq ft/gal", method="fetched", role="code", value="320-400",
           unit="sq ft/gal", source="WEB", locator="ask a1", tag="X100 data sheet", url=SHEET, retrieved="2026-10-09",
           quote="320-400 sq. ft. per gallon", division="")
COUNT = row("X-TK-Q-02", "Adhesive anchors, 3 per bracket x 6 brackets", method="counted", role="quantity", value="18",
            unit="each", source="S-1", locator="Det 1", tag="S-1 Det 1", calc="{X-DR-003} * {X-TK-Q-00}", division="")
A7 = row("X-WEB-006", "Example Anchor A7: ESR-0000 covers hollow masonry", method="fetched", role="code", source="WEB",
         locator="ask a1", tag="A7 evaluation report", url=BULLETIN, retrieved="2026-10-09",
         quote="ESR-0000 covers hollow masonry", division="")
ANCHOR = row("X-DR-005", '1/2" adhesive anchors, Example Anchor A7, 3 per bracket', source="S-1", locator="Det 1",
             tag="S-1 Det 1", division="05")
SCALED = row("X-DR-009", "Angle leg, scaled", method="scaled", role="quantity", value="40", unit="in", source="S-1",
             confidence="scaled", division="")
ALLOW = row("X-A-004", "exterior wall surfaces", method="FIELD", role="allowance", unit="sq ft", source="none",
            locator="", tag="FIELD", division="", confidence="missing")
NOPAGE = row("X-WEB-007", "No product named Example Patch P20 was found on the page", method="fetched", role="code",
             source="WEB", locator="ask a1", tag="P20 page", url="https://www.example.com/p20", retrieved="2026-10-09",
             flag="unverified", division="")
EMAIL = row("X-CU-01", "The owner asks for two coats on the finish", method="customer", source="EM-1", locator="", tag="email")
ROWS = [SYSTEM, ONE_COAT, NO_COAT, TWO_PRODUCTS, SPEC_RATE, AREA, FIELD, X100, COUNT, A7, ANCHOR, SEAL_SPEC, JOINTS, S9, EMAIL]
BY_ID = {c.claim_id: c for c in ROWS}


def item(product="Example Satin X100", unit="gal", quantity="X-TK-Q-01", coats="X-SP-010", spec_rate="X-R-001",
         sheet="X-WEB-003", basis=("X-SP-010",)):
    return {"product": product, "unit": unit, "quantity": quantity, "coats": coats, "spec_rate": spec_rate,
            "sheet": sheet, "basis": list(basis)}


ANCHORS = item("Example Anchor A7 adhesive anchors", "each", "X-TK-Q-02", "", "", "X-WEB-006", ("X-DR-005",))
SEALANT = item("Example Seal S9", "tubes", "X-TK-Q-03", "", "", "X-WEB-008", ("X-SP-020",))
GOVERNS = "; the spec's rate (X-R-001, SW p.5) governs over the sheet's 320-400 sq ft/gal"


class Fake:
    def __init__(self, answers, model_id="fake-model"):
        self.answers, self.model_id, self.calls = answers, model_id, []

    def complete(self, reader, unit, system, schema_, run):
        self.calls.append((reader, unit, system, schema_, run))
        return self.answers[run]


class ItemCase(unittest.TestCase):
    def test_the_prompt_and_schema(self):
        text = prompt("materials")
        self.assertIn("## Rules", text)
        self.assertEqual(validate.errors({"items": [item(), ANCHORS, SEALANT]}, materials.SCHEMA), [])
        self.assertTrue(validate.errors({"items": [dict(item(), unit="LF")]}, materials.SCHEMA))
        # the coat count is a row, never a number from the model
        self.assertTrue(validate.errors({"items": [dict(item(), coats=2)]}, materials.SCHEMA))
        self.assertTrue(validate.errors({"items": [dict(item(), sheet="x y")]}, materials.SCHEMA))
        self.assertIn("never write a number yourself", text)
        # every example in the prompt matches the schema and names no test bid's rows (p.7)
        examples = [json.loads(b.split("```")[0]) for b in text.split("```json")[1:]]
        self.assertEqual(len(examples), 4)
        for ex in examples:
            self.assertEqual(validate.errors(ex, materials.SCHEMA), [])
        self.assertNotRegex(text, r"NAN-|OBV-|Nantucket|Ocean|Sherwin|Loxon|Hilti")

    def test_rate_unit(self):
        for unit, want in (("sq ft/gal", ("sq ft", "gal")), ("sq. ft. per gallon", ("sq ft", "gal")),
                           ("SF/gal", ("sq ft", "gal")), ("cu ft per bag", ("cu ft", "bags")), ("CF/bag", ("cu ft", "bags")),
                           ("LF/tube", ("lf", "tubes")), ("lin. ft. per cartridge", ("lf", "cartridges")),
                           ("weeks", ("", "")), ("gal", ("", "")), ("", ("", ""))):
            self.assertEqual(materials.rate_parts(row("R", "r", value="200-300", unit=unit)), want, unit)
            self.assertEqual(materials.rate_unit(row("R", "r", value="200-300", unit=unit)), want[1], unit)
        self.assertEqual(materials.rate_unit(row("R", "r", value="", unit="sq ft/gal")), "")   # no figure, no rate
        self.assertEqual(materials.rate_unit(None), "")
        for unit, want in (("SF", "sq ft"), ("sq. ft.", "sq ft"), ("Sq Ft", "sq ft"), ("LF", "lf"), ("ft", "lf"),
                           ("CF", "cu ft"), ("each", "each"), ("ea", "each"), ("in", "in"), ("", "")):
            self.assertEqual(materials.norm_unit(unit), want, unit)

    def test_the_coat_count_is_read_from_the_clause(self):
        self.assertEqual(materials.coat_count(SYSTEM), (2, ""))
        self.assertEqual(materials.coat_count(ONE_COAT), (1, ""))
        self.assertEqual(materials.coat_count(row("C", "Coats: 3")), (3, ""))
        self.assertEqual(materials.coat_count(row("C", "two finish coats over one prime coat"))[0], None)   # two counts
        self.assertEqual(materials.coat_count(NO_COAT), (None, "X-SP-012 states no coat count for X50"))
        self.assertEqual(materials.coat_count(row("C", "2 coats of primer and 2 coats of finish")), (2, ""))   # one count
        self.assertEqual(materials.coat_count(row("C", "Finish: two coats, sheen not stated")), (2, ""))   # not about coats
        self.assertEqual(materials.coat_count(row("C", "two coats; primer coats not stated")),
                         (None, "C states a coat count and says one is not stated; it does not settle this product's"))

    def test_a_clause_naming_several_products_is_read_by_this_product_s_code(self):
        # each count belongs to the product code in its own stretch of the clause (the repaint's finish clause)
        self.assertEqual(materials.coat_count(TWO_PRODUCTS, X100), (None, "X-SP-013 says the coat count for X100 is not stated"))
        x200 = row("X-WEB-009", "Example Flat X200 data sheet", method="fetched", role="code", source="WEB", tag="X200 data sheet")
        self.assertEqual(materials.coat_count(TWO_PRODUCTS, x200), (1, ""))
        # a count before its code binds to it, and a product the clause gives no count is left open
        primer = row("X-SP-050", "Primer: Example Primer B66 as needed, then 1 coat Example Flat B53")
        b66 = row("X-WEB-010", "Example Primer B66 data sheet", method="fetched", role="code", source="WEB", tag="B66 data sheet")
        b53 = row("X-WEB-011", "Example Flat B53 data sheet", method="fetched", role="code", source="WEB", tag="B53 data sheet")
        self.assertEqual(materials.coat_count(primer, b66), (None, "X-SP-050 states no coat count for B66"))
        self.assertEqual(materials.coat_count(primer, b53), (1, ""))
        # a clause about other products, or several with no product to tie them to, settles nothing
        self.assertEqual(materials.coat_count(TWO_PRODUCTS, S9), (None, "X-SP-013 names X100, X200; which is this product's is not settled"))
        self.assertEqual(materials.coat_count(TWO_PRODUCTS), (None, "X-SP-013 names X100, X200; which is this product's is not settled"))
        self.assertEqual(materials.coat_count(NO_COAT, X100), (None, "X-SP-012 names X50, not this product"))
        self.assertEqual(materials.coat_count(SYSTEM, X100), (2, ""))
        # a stretch is cut at ";", a sentence end, "or", "then", "and", "over", "followed by", "after", "before" and "prior to"
        # outside parentheses; commas and parentheses stay inside
        enamel = row("X-SP-051", "Prime Coat: Example Primer B66 as needed; Finish Coat (1 coat): Example Enamel, B53 series")
        self.assertEqual(materials.coat_count(enamel, b66), (None, "X-SP-051 states no coat count for B66"))
        self.assertEqual(materials.coat_count(enamel, b53), (1, ""))
        two = row("X-SP-052", "Example Primer B66 primer; two coats, Example Flat B53 finish")
        self.assertEqual((materials.coat_count(two, b66), materials.coat_count(two, b53)), ((None, "X-SP-052 states no coat count for B66"), (2, "")))
        # "over" sets a finish on its primer: two stretches, so the finish's count is its own
        both = row("X-SP-053", "Two coats Example Flat B53 over Example Primer B66 primer")
        self.assertEqual((materials.coat_count(both, b53), materials.coat_count(both, b66)), ((2, ""), (None, "X-SP-053 states no coat count for B66")))
        # a count whose stretch names no code, or two, is tied to nothing, and the clause then settles no product's count;
        # so is a count after its code in a stretch opened by a sequence word, or by "and" after a product, which may be the
        # system's (Determinism: two readings, neither picked), while a count before the code, or after "or", is that code's alone
        for text in ("Example Primer B66 as needed for rust, then 1 coat Example Enamel", "Finish: Example Flat B53 over one coat of primer",
                     "Example Flat B53 finish over 1 primer coat", "Example Flat B53, 2 coats; touch up 1 coat as needed",
                     "Two coats Example Flat B53 on Example Primer B66", "Example Flat B53 over Example Primer B66, 2 coats",
                     "Example Primer B66 as needed, then Example Flat B53, 2 coats", "Finish: Example Flat B53 over Example Primer B66 primer, 2 coats",
                     "Example Primer B66 and Example Flat B53, 2 coats each", "Example Primer B66 as needed. Then Example Flat B53, 2 coats.",
                     "Example Primer B66 as needed; then Example Flat B53, 2 coats", "Example Primer B66 primer; and Example Flat B53, 2 coats",
                     "Example Primer B66, followed by Example Flat B53, 2 coats", "Example Primer B66, followed by 2 coats",
                     "Primer, 2 coats, followed by Example Enamel B53", "Prime with Example Primer B66 prior to Example Flat B53, 2 coats",
                     "Base coat as needed, then Example Flat B53, 2 coats", "Block filler as needed, followed by Example Flat B53, 2 coats",
                     "Scrape and sand, then Example Flat B53, 2 coats", "Example Flat B53 finish with primer, 2 coats", "Example Flat B53 with primer, 2 coats",
                     "Example Flat B53, 1 coat; double coat at repairs", "Example Flat B53, 1 coat; recoat patched areas",
                     "Example Flat B53, 1 coat; double-coat at repairs", "Example Flat B53, 1 coat; re-coat patched areas"):
            for who in (b53, b66):
                if who.statement.split()[-3] in text:
                    self.assertEqual(materials.coat_count(row("X-SP-054", text), who),
                                     (None, "X-SP-054 does not tie a coat count to one product; which is this product's is not settled"), (text, who.claim_id))
        # "or approved equal" is not a choice between products
        self.assertEqual(materials.coat_count(row("X-SP-056", "Example Flat B53 or approved equal, 2 coats"), b53), (2, ""))
        self.assertEqual(materials.coat_count(row("X-SP-056", "Example Flat B53 or an equivalent, 2 coats"), b53), (2, ""))
        self.assertEqual(materials.coat_count(row("X-SP-056", "Example Flat B53 or an approved equal, 2 coats"), b53), (2, ""))
        # the statement and the quote are two stretches, so a count in one is not tied to a code in the other
        quoted = row("X-SP-055", "Topcoat: Example Flat B53, 2 coats", quote="Example Primer B66 primer as needed")
        self.assertEqual((materials.coat_count(quoted, b53), materials.coat_count(quoted, b66)), ((2, ""), (None, "X-SP-055 states no coat count for B66")))
        # an unclosed parenthesis in the statement does not swallow the quote: they are always two stretches
        clipped = row("X-SP-057", "Finish (K62 series", quote="A89 primer, 1 coat")
        a89 = row("X-WEB-013", "A89 data sheet", method="fetched", role="code", source="WEB", tag="A89 primer")
        k62 = row("X-WEB-014", "K62 data sheet", method="fetched", role="code", source="WEB", tag="K62 finish")
        self.assertEqual((materials.coat_count(clipped, a89), materials.coat_count(clipped, k62)), ((1, ""), (None, "X-SP-057 states no coat count for K62")))
        # a sheet naming no code is read as the clause's one code, with or without a quote; a clause naming several settles nothing for it
        plain = row("X-WEB-015", "the finish data sheet", method="fetched", role="code", source="WEB", tag="finish sheet")
        self.assertEqual(materials.coat_count(row("C", "Topcoat K62: 2 coats"), plain), (2, ""))
        self.assertEqual(materials.coat_count(row("C", "Topcoat K62: 2 coats", quote="Topcoat K62: 2 coats"), plain), (2, ""))
        self.assertEqual(materials.coat_count(row("C", "Topcoat K62: 2 coats", quote="A89 primer as needed"), plain),
                         (None, "C names K62, A89; which is this product's is not settled"))
        # ... but only when every stretch names that code: a stretch naming none may be the codeless sheet's own product
        for text in ("Masonry conditioner as needed; Example Flat B53, 2 coats", "Example Flat B53, 2 coats; see the primer's data sheet"):
            self.assertEqual(materials.coat_count(row("C", text), plain),
                             (None, "C names B53 in one stretch and a product without a code in another; whether the other is this product's is not settled"), text)
        self.assertEqual(materials.coat_count(row("C", "Topcoat K62: 2 coats", quote="Finish: 2 coats"), plain),
                         (None, "C names K62 in one stretch and a product without a code in another; whether the other is this product's is not settled"))
        # ... while a stretch about no product ("Walls", "back-roll") leaves the reading alone
        self.assertEqual(materials.coat_count(row("C", "Walls and ceilings: Example Flat B53, 2 coats, and back-roll the first"), plain), (2, ""))
        # a clause naming no code settles a count only when no stretch names a product the count could be for
        # (by a primer or finish word), and a stretch naming both a primer and a finish holds two products' wording
        for text in ("Prime coat: exterior latex primer; Finish coats: two coats exterior latex satin", "2 coats of finish; primer as needed",
                     "Prime coat: exterior latex primer as needed. Finish coats: two coats exterior latex satin", "Prime as needed, followed by two coats",
                     "Primer as needed, followed by two finish coats"):
            for who in (plain, None):
                self.assertEqual(materials.coat_count(row("C", text), who),
                                 (None, "C names no product code, and names a product in one stretch and speaks of coats in another; which product the count is for is not settled"), text)
        for text in ("Primer as needed with two finish coats", "Finish: two coats on the primer where it shows", "Finish with primer, 2 coats",
                     "Two coats of enamel, primer as needed"):
            self.assertEqual(materials.coat_count(row("C", text), plain),
                             (None, "C names no product code and speaks of a primer and a finish where it states coats; which the count is for is not settled"), text)
        for text in ("Walls and ceilings: two coats", "Apply two coats and back-roll", "Coats: 2 and back-roll the first", "Two coats. Allow 4 hours between coats.",
                     "Two coats, followed by a wash"):
            self.assertEqual(materials.coat_count(row("C", text), plain), (2, ""), text)
        # a codeless clause about a primer is no finish sheet's count, and the reverse
        primer_sheet = row("X-WEB-016", "the primer data sheet", method="fetched", role="code", source="WEB", tag="primer sheet")
        for sheet in (plain, row("X-WEB-017", "Example Enamel data sheet", method="fetched", role="code", source="WEB", tag="enamel sheet")):
            self.assertEqual(materials.coat_count(row("C", "Prime coat: two coats of primer"), sheet),
                             (None, "C speaks of a primer where it states coats, and this product's row is not a primer's alone"), sheet.tag)
        self.assertEqual(materials.coat_count(row("C", "Finish: two coats"), primer_sheet),
                         (None, "C speaks of a finish where it states coats, and this product's row names a primer"))
        self.assertEqual(materials.coat_count(row("C", "Prime coat: two coats of primer"), primer_sheet), (2, ""))
        self.assertEqual(materials.coat_count(row("C", "Walls: two coats"), primer_sheet),   # a bare count is the finish's or the system's
                         (None, "C names no primer where it states coats, and this product's row names a primer"))
        self.assertEqual(materials.coat_count(row("C", "Example B53, 2 coats"), primer_sheet),
                         (None, "C names no primer with the coat count for B53, and this product's row names a primer; whether the count is B53's is not settled"))
        self.assertEqual(materials.coat_count(row("C", "Example Primer B53, 2 coats"), primer_sheet), (2, ""))
        self.assertEqual(materials.coat_count(row("C", "Example B53, 2 coats"), plain), (2, ""))
        self.assertEqual(materials.coat_count(row("C", "Prime coat: two coats of primer")), (2, ""))
        for text in ("one coat; two at patched areas", "one coat (two at repairs)", "a second coat at repairs", "One coat, another coat at repairs"):
            self.assertEqual(materials.coat_count(row("C", text))[0], None, text)
        self.assertEqual(materials.coat_count(row("C", "one coat (two at repairs)")), (None, "C states a coat count and more coats in places; it does not settle this product's"))
        self.assertEqual(materials.coat_count(row("C", "2 coats of primer and 3 coats of finish"), plain),
                         (None, "C names no product code and states different coat counts in its stretches; which is this product's is not settled"))
        self.assertEqual(materials.coat_count(row("C", "2 coats of primer and 2 coats of finish"), plain), (2, ""))
        # a sentence end cuts a stretch, and a stretch naming a primer and a finish ties its count to neither
        for text in ("Prime Coat: Example Primer B66 as needed. Finish Coat: two coats.", "Example Primer B66 as needed, followed by two finish coats",
                     "Example Flat B53, one coat; two at patched areas", "Example Flat B53: one coat; a second coat at repairs"):
            for who in (b53, b66):
                if who.statement.split()[-3] in text:
                    self.assertEqual(materials.coat_count(row("C", text), who),
                                     (None, "C does not tie a coat count to one product; which is this product's is not settled"), (text, who.claim_id))
        self.assertEqual(materials.coat_count(row("C", "Example Flat B53, 1 coat; 2 at patched areas"), b53),
                         (None, "C does not tie a coat count to one product; which is this product's is not settled"))
        self.assertEqual(materials.coat_count(row("C", "Example Flat B53, 1 coat (10 year)"), b53), (1, ""))
        # a hyphenated count is a count ("2-coat system"), so two of them settle nothing
        self.assertEqual(materials.coat_count(row("C", "Example Flat B53, 2-coat finish"), b53), (2, ""))
        self.assertEqual(materials.coat_count(row("C", "Example Flat B53, one coat (two-coat finish on bare wood)"), b53),
                         (None, "C states 2 coat counts for B53; it does not settle this product's"))
        # a system's count, or one split across products, is nobody's count
        for text in ("Example Flat B53, 2-coat system", "Example B53, 2 coats incl. primer"):   # "incl." ends a sentence, so the primer is cut off
            self.assertEqual(materials.coat_count(row("C", text), b53),
                             (None, "C states a coat count for B53 for a system or split across products; whose coats they are is not settled"), text)
        for text in ("Example B53, 2-coat system including primer", "Example B53, two coats of which one is primer"):
            self.assertEqual(materials.coat_count(row("C", text), b53),
                             (None, "C names a primer with the coat count for B53; whether the count is B53's is not settled"), text)
        self.assertEqual(materials.coat_count(row("C", "Example Flat B53, two coats (one primer, one finish)"), b53),
                         (None, "C does not tie a coat count to one product; which is this product's is not settled"))
        for text in ("2-coat system including primer", "two coats incl. primer", "two coats of which one is primer", "two coats (one primer, one finish)"):
            self.assertEqual(materials.coat_mentions(text)[-1][1], "split", text)
        self.assertEqual(materials.coat_count(row("C", "Two-coat system including primer"), primer_sheet),
                         (None, "C states a coat count for a system or split across products; whose coats they are is not settled"))
        # the whole stretch is judged, the count's own object ("one coat of primer, B53", "B53 with one coat of primer") included
        for text, who in (("Example Flat B53 with one coat of primer", b53), ("Example Primer B66, 2 coats of finish", b66),
                          ("Example Primer B66 under 2 coats of finish", b66), ("Example Flat B53, two coats (primer and finish)", b53)):
            self.assertEqual(materials.coat_count(row("C", text), who),
                             (None, "C does not tie a coat count to one product; which is this product's is not settled"), text)
        self.assertEqual(materials.coat_count(row("C", "Example B53 with one coat of primer"), b53),
                         (None, "C names a primer with the coat count for B53; whether the count is B53's is not settled"))
        for who in (b66, primer_sheet):   # a sheet naming no code takes the clause's one code as its own
            self.assertEqual(materials.coat_count(row("C", "Example B66, 2 coats of finish"), who),
                             (None, "C names a finish with the coat count for B66; whether the count is B66's is not settled"))
        # a floor is not a fixed count
        for text in ("Example Flat B53, two coats, or more as required for full hide", "Example Flat B53: at least two coats", "Example Flat B53, a minimum of two coats",
                     "Example Flat B53, 2 coats minimum", "Example Flat B53, minimum 2 coats", "Example Flat B53, two coats as needed for complete coverage",
                     "Example Flat B53: at least two (2) coats", "Example Flat B53, minimum of two (2) coats", "Example Flat B53: no less than two coats",
                     "Example Flat B53, two coats, minimum", "Example Flat B53, two coats (minimum)", "Example Flat B53, 2 coats min.",
                     "Example Flat B53, two coats or as required to achieve full hide", "Example Flat B53, 2 coats or as necessary to obtain a uniform hide",
                     "Example Flat B53, two coats, or as required to provide full hide", "Example Flat B53, two coats or as required", "Example Flat B53, 2 coats or as needed to cover",
                     "Apply two coats of Example Flat B53, or more as required for full hide", "Example Flat B53, two coats (or as required)",
                     "Example Flat B53, two coats; or more as required for full hide", "Example Flat B53, 2 coats minimum WFT 6 mils", "Example Flat B53, two or more coats",
                     "Example Flat B53, two coats, and more as required for full hide", "Example Flat B53, two coats, more if required for full hide", "Example Flat B53, two coats (more if needed)",
                     # a figure between the count and the tail claims it only when the tail follows it directly
                     "Example Flat B53, 2 coats at 350 sq ft/gal, or more as required for full hide", "Example Flat B53, 2 coats at 4 mils DFT each, or as required for full hide",
                     "Example Flat B53, 2 coats, color 7006, or more as required",
                     # a hedge in a later sentence bounds the last count before it when its sentence holds only an application verb, "or" or "and" before it
                     "Example Flat B53, 2 coats; apply more as required for full hide", "Example Flat B53, 2 coats. Apply more if needed.",
                     "Example Flat B53, 2 coats; then apply more as required for full hide", "Example Flat B53, 2 coats. Then apply more if needed.",
                     "Example Flat B53, 2 coats over sanded wood; apply more as required",
                     # the "as required" forms bound the work whatever figure stands between, with or without a comma
                     "Example Flat B53, 2 coats at 350 sq ft/gal or more as required for full hide", "Example Flat B53, 2 coats at 4 mils DFT or as required for full hide",
                     # a hedge after the count in its sentence is a floor whatever cut words stand between, and whatever work it governs
                     "Apply two coats of Example Flat B53 over the prepared surface, or more as required for full hide", "Example Flat B53, 2 coats over sanded wood, or more as required",
                     "Example Flat B53, 2 coats after sanding, or as required for full hide", "Example Flat B53, 2 coats, caulk joints as required",
                     "Example Flat B53, 2 coats; or as required for full hide", "Example Flat B53, 2 coats. Or as needed.",
                     "Example Flat B53, 2 coats over existing paint; apply more as required for full hide"):
            self.assertEqual(materials.coat_count(row("C", text), b53),
                             (None, "C states a minimum coat count for B53, not a fixed one; it does not settle this product's"), text)
        self.assertEqual(materials.coat_count(row("C", "Two coats, or more as required for full hide")),
                         (None, "C states a minimum coat count, not a fixed one; it does not settle this product's"))
        self.assertEqual(materials.coat_count(row("C", "Walls: 2 coats; apply more coats as needed")),   # added coats, not a floor
                         (None, "C states a coat count and more coats in places; it does not settle this product's"))
        for text in ("Example Flat B53, 2 coats; apply more coats as needed", "Example Flat B53, 2 coats; recoat more where needed",
                     "Example Flat B53, 2 coats; apply more paint as required for full hide", "Example Flat B53, 2 coats; recoat as required for full hide",
                     "Example Flat B53, 2 coats; a third coat where needed for full hide", "Example Flat B53, 2 coats. Apply additional paint as required for full hide.",
                     "Example Flat B53, 2 coats; apply extra material where needed", "Example Flat B53, 2 coats; apply additional coating as required"):
            self.assertIsNone(materials.coat_count(row("C", text), b53)[0], text)
        # a ceiling is a range, as "1-2 coats" is
        for text in ("Example Flat B53, up to two coats", "Example Flat B53, no more than two coats", "Example Flat B53, two coats maximum", "Example Flat B53, a maximum of 2 coats",
                     "Example Flat B53, 2 coats (max.)"):
            self.assertEqual(materials.coat_count(row("C", text), b53), (None, "C states a range of coats for B53; it does not settle this product's"), text)
        self.assertEqual(materials.coat_count(row("C", "Example Flat B53, 2 coats, maximum 4 mils DFT per coat"), b53), (2, ""))
        for text in ("Example Flat B53, 2 coats (min. 2.0 mils DFT per coat)", "Example Flat B53, two coats, minimum 4 mils DFT each",   # a film thickness, not a floor
                     "Example Flat B53, 2 coats, at least 3 mils dry", "Example Flat B53, 2 coats, minimum of 3 mils",
                     "Example Flat B53, 2 coats, minimum DFT 2.0 mils per coat", "Example Flat B53, 2 coats, min. dry film thickness 4 mils",
                     # "or more" and "or as required" are floors only after a count
                     "Surfaces 10 ft or more above grade: Example Flat B53, 2 coats", "Scrape loose paint, or as required by the Architect; Example Flat B53, 2 coats.",
                     "Example Flat B53, 2 coats on surfaces 10 ft or more above grade", "Example Flat B53, 2 coats where 50% or more of the surface is bare",
                     # a later stretch with its own work keeps its tail
                     "Example Flat B53, 2 coats; remove loose plaster, or as required by the Architect", "Example Flat B53, 2 coats; caulk joints as required",
                     "Example Flat B53, 2 coats; refer to Section 09 01 90 for surface preparation.", "Example Flat B53, 2 coats per Section 9 for all trim",
                     # a primed substrate names no primer, and "priming" does only as a label
                     "Previously primed surfaces: Example B53, 2 coats", "Shop-primed steel: Example B53, two coats", "After priming, apply Example B53, 2 coats"):
            self.assertEqual(materials.coat_count(row("C", text), b53), (2, ""), text)
        for text in ("Example Flat B53, 2 coats including priming", "Example Flat B53, two coats incl. spot priming"):
            self.assertEqual(materials.coat_count(row("C", text), b53),
                             (None, "C states a coat count for B53 for a system or split across products; whose coats they are is not settled"), text)
        self.assertEqual(materials.coat_count(row("C", "Example B53, two coats, the first a priming coat"), b53),
                         (None, "C names a primer with the coat count for B53; whether the count is B53's is not settled"))
        self.assertEqual(materials.coat_count(row("C", "Priming (one coat)."), primer_sheet), (1, ""))
        self.assertEqual(materials.coat_count(row("C", "Exterior trim: two coats including priming"), plain),
                         (None, "C states a coat count for a system or split across products; whose coats they are is not settled"))
        self.assertEqual(materials.coat_count(row("C", "Previously primed surfaces: Example B53, 2 coats"), primer_sheet),
                         (None, "C names no primer with the coat count for B53, and this product's row names a primer; whether the count is B53's is not settled"))
        self.assertEqual(materials.coat_count(row("C", "Shop-primed steel: two coats"), primer_sheet),
                         (None, "C names no primer where it states coats, and this product's row names a primer"))
        # plural and -ing role words
        self.assertEqual(materials.coat_count(row("C", "Example Primer B66 under finishes, 2 coats"), b66),
                         (None, "C does not tie a coat count to one product; which is this product's is not settled"))
        self.assertEqual(materials.coat_count(row("C", "Priming: one coat"), primer_sheet), (1, ""))
        self.assertEqual(materials.coat_count(row("C", "Priming: Example B66, 1 coat"), primer_sheet), (1, ""))
        self.assertEqual(materials.coat_count(row("C", "Trim: one coat of primer, Example B53"), b53),
                         (None, "C names a primer with the coat count for B53; whether the count is B53's is not settled"))
        self.assertEqual(materials.coat_count(row("C", "2 coats of finish, Example B66"), b66),
                         (None, "C names a finish with the coat count for B66; whether the count is B66's is not settled"))
        # a finish sheet whose quote also names a primer is not a primer's sheet alone
        mixed = row("X-WEB-019", "Example Enamel B53 data sheet", method="fetched", role="code", source="WEB", tag="B53 enamel",
                    quote="Apply over a compatible primer; 350-400 sq ft/gal")
        self.assertEqual(materials.coat_count(row("C", "Example B53 with primer, 2 coats"), mixed),
                         (None, "C names a primer with the coat count for B53; whether the count is B53's is not settled"))
        self.assertEqual(materials.coat_count(row("C", "Siding: primer, Example X100, 2 coats"),
                                              row("X-WEB-020", "Example Paint & Primer X100 data sheet", method="fetched", role="code", source="WEB", tag="X100")),
                         (None, "C names a primer with the coat count for X100; whether the count is X100's is not settled"))
        self.assertEqual(materials.coat_count(row("C", "One coat; 2-coat on new work"))[0], None)
        # a sheet that names a second product (a recommended primer, a system) is read for neither
        both = row("X-WEB-018", "Example Flat B53 data sheet; prime with Example Primer B66", method="fetched", role="code", source="WEB", tag="B53 enamel")
        self.assertEqual(materials.coat_count(row("C", "Example Primer B66, 1 coat"), both),
                         (None, "C names B66, and this product's row names B53, B66; which of them is this product is not settled"))
        for text in ("Example Flat B53, one coat, two at patched areas", "Example Flat B53, one coat (two at repairs)", "Example Flat B53, 1 coat, 2 at patched areas",
                     "Example Flat B53, 1 coat, plus 1 coat at patched areas", "Example Flat B53, 1 coat, plus a further coat at repairs"):
            self.assertEqual(materials.coat_count(row("C", text), b53),
                             (None, "C states a coat count for B53 and more coats in places; it does not settle this product's"), text)
        # a count in the stretch before a sequence word is that stretch's code's ("B53, 2 coats after B66")
        after = row("C", "Example Flat B53, 2 coats after Example Primer B66")
        self.assertEqual((materials.coat_count(after, b53), materials.coat_count(after, b66)), ((2, ""), (None, "C states no coat count for B66")))
        # a primer named in the count's stretch may own it ("B53 with primer, 2 coats"), unless the sheet is the primer's;
        # ... wherever in the count's stretch the primer is named, "B53 with one coat of primer" included
        for text in ("Example B53 with primer, 2 coats", "Base coat as needed, Example B53, 2 coats", "Primer as needed, 1 coat Example B53"):
            self.assertEqual(materials.coat_count(row("C", text), b53),
                             (None, "C names a primer with the coat count for B53; whether the count is B53's is not settled"), text)
        self.assertEqual(materials.coat_count(row("C", "Prime coat: Example Primer B66, 1 coat"), b66), (1, ""))
        enamel_sheet = row("X-WEB-017", "Example Enamel data sheet", method="fetched", role="code", source="WEB", tag="enamel sheet")
        self.assertEqual(materials.coat_count(row("C", "Prime coat: Example Primer B66, 1 coat"), enamel_sheet),
                         (None, "C names a primer with the coat count for B66; whether the count is B66's is not settled"))
        self.assertEqual(materials.coat_count(row("C", "Prime coat: Example Primer B66, 1 coat"), primer_sheet), (1, ""))
        self.assertEqual(materials.coat_count(row("C", "Finish: Example Satin X100, 2 coats"), primer_sheet),
                         (None, "C names a finish with the coat count for X100; whether the count is X100's is not settled"))
        self.assertEqual(materials.coat_count(row("C", "Example Flat B53, 2 coats; recoat after 4 hours at 77F"), b53), (2, ""))
        # "Walls and ceilings: B53, 2 coats" continues no product, so the count after the code is B53's
        self.assertEqual(materials.coat_count(row("C", "Walls and ceilings: Example Flat B53, 2 coats"), b53), (2, ""))
        self.assertEqual(materials.coat_count(row("C", "Example Flat B53, 2 coats. Allow 4 hours between coats."), b53), (2, ""))
        # a count at the head of the quote, or of a stretch opened by "then", is read from where it stands
        self.assertEqual(materials.coat_count(row("C", "Finish: Example Flat B53", quote="Coats: 2, Example Flat B53"), b53), (2, ""))
        self.assertEqual(materials.coat_count(row("C", "Example Primer B66 as needed, then coats: 2, Example Flat B53"), b53), (2, ""))
        # the repo's own wording of an open count is seen beside a count
        self.assertEqual(materials.coat_count(row("C", "Example Flat B53, 2 coats on new work; the B53 coat count on repaint is not stated"), b53),
                         (None, "C states a coat count for B53 and says one is not stated; it does not settle this product's"))
        self.assertEqual(materials.coat_count(row("C", "The B53 coat count is not stated"), b53), (None, "C says the coat count for B53 is not stated"))
        self.assertEqual(materials.coat_count(row("C", "Number of coats on repaint not specified")), (None, "C says the coat count is not stated"))
        # a sheet carrying two of the clause's codes settles nothing
        system = row("X-WEB-012", "Example Flat B53 over Example Primer B66 system", method="fetched", role="code", source="WEB", tag="B53 system")
        self.assertEqual(materials.coat_count(two, system),
                         (None, "X-SP-052 names B53, B66, which this product's row both carries; which count is its is not settled"))

    def test_a_range_of_coats_or_a_recoat_time_is_no_count(self):
        # two readings, neither picked (Determinism); a dry time is not a count
        for text in ("Apply 1-2 coats", "one or two coats", "Coats: 2-3", "coats: 2 to 3", "Apply 2\u20133 coats"):
            self.assertEqual(materials.coat_count(row("C", text)), (None, "C states a range of coats; it does not settle this product's"), text)
        for text in ("Dry time between coats: 4 hours", "Dry time between coats: 1.5 hours", "Dry time between coats: 24 hours",
                     "coats: 16 hrs", "coats: 10 mils", "Finish coat: 400 sq ft/gal", "Apply a 15 mil coat of Example Elastomeric A100",
                     "Coats: 2 - 3", "Coats: 2 \u2013 3", "Coats: 2 to 3", "between two and three coats", "0 coats", "Coats: 0", "Two (0) coats",
                     "Mils per coat: 4", "WFT/coat: 4", "Dry time between coats: 24.", "Spread rate per coat: 350", "Recoat interval between coats: 24;",
                     "Dry time between finish coats: 24.", "mils per top coat: 4", "DFT per prime coat: 3.", "1/2 coat", "Apply 2/3 coats"):
            self.assertEqual(materials.coat_count(row("C", text))[0], None, text)
        self.assertEqual(materials.coat_count(row("C", "Dry time between coats: 24 hours")), (None, "C states no coat count"))
        # the "coats:" form counts only when "coats" is the label itself, not the tail of one ("mils per coat: 4")
        self.assertEqual(materials.coat_count(row("C", "Dry time between coats: 24.")), (None, "C states no coat count"))
        b53 = row("X-WEB-010", "B53 data sheet", method="fetched", role="code", source="WEB", tag="B53 enamel")
        self.assertEqual(materials.coat_count(row("C", "Example Flat B53, mils per coat: 4"), b53), (None, "C states no coat count for B53"))
        self.assertEqual(materials.coat_count(row("C", "Example Flat B53, wet mils per finish coat: 4"), b53), (None, "C states no coat count for B53"))
        self.assertEqual(materials.coat_count(row("C", "Apply 2/3 coats")), (None, "C states a range of coats; it does not settle this product's"))
        for text in ("Finish coats: 2", "Number of coats: 2", "Topcoat, coats: 2", "(coats: 2)", "Prime coats: 2 and finish coats: 2"):
            self.assertEqual(materials.coat_count(row("C", text)), (2, ""), text)
        # "coats: 2 and ..." is a range only when a second number follows
        self.assertEqual(materials.coat_count(row("C", "Coats: 2 and 3")), (None, "C states a range of coats; it does not settle this product's"))
        self.assertEqual(materials.coat_count(row("C", "Example Flat B53: coats: 2 and back-roll the first"), b53), (2, ""))
        self.assertEqual(materials.coat_count(row("C", "between two and three coats")), (None, "C states a range of coats; it does not settle this product's"))
        # after "coats:" the number ends the clause or is followed by punctuation or "coat(s)"
        for text in ("Coats: 2", "Coats: 2.", "Coats: 2, recoat: 4 hours", "(coats: 2)", "coats: 12"):
            self.assertEqual(materials.coat_count(row("C", text))[0], 12 if "12" in text else 2, text)
        self.assertEqual(materials.coat_count(row("C", "Dry time between coats: 1.5 hours")), (None, "C states no coat count"))
        self.assertEqual(materials.coat_count(row("C", "Coats: 2 - 3")), (None, "C states a range of coats; it does not settle this product's"))
        # "two (2) coats" is one count said twice; "two (3)" is two
        self.assertEqual(materials.coat_count(row("C", "Two (2) coats of Example Flat B53")), (2, ""))
        self.assertEqual(materials.coat_count(row("C", "Two (3) coats")), (None, "C states 2 coat counts; it does not settle this product's"))
        self.assertEqual(materials.coat_count(row("C", "Coats: 2, recoat: 4 hours")), (2, ""))
        self.assertEqual(materials.coat_count(row("C", "Example Flat B53, 1-2 coats; Example Primer B66, 1 coat"),
                                              row("W", "Example Flat B53 sheet", method="fetched", role="code", source="WEB")),
                         (None, "C states a range of coats for B53; it does not settle this product's"))
        self.assertEqual(materials.product_codes("see X-SP-011, ft2 and S-1; A89 or K62, then A89"), ["A89", "K62"])

    def test_what_code_refuses(self):
        cases = [
            (item(product="Example Satin X900"), "product 'Example Satin X900' carries 900, which is in none of the rows it cites"),
            # a code that is in the ledger but not in the rows the item cites (p.7: HY 70 against HY 270)
            (item(product="Example Satin X100 and Seal S9"), "product 'Example Satin X100 and Seal S9' carries 9, which is in none of the rows it cites"),
            (item(sheet="X-WEB-999"), "names X-WEB-999, which is not a row it was shown"),
            (item(sheet=""), "names no data-sheet row; a product with none gets no order row"),
            (item(basis=("X-SP-010", "X-NO")), "names X-NO, which is not a row it was shown"),
            (item(sheet="X-SP-010"), "sheet X-SP-010 is not a fetched row with a URL"),
            (item(quantity="X-SP-010"), "quantity X-SP-010 is a clause scope row, not a takeoff figure, an allowance or a FIELD row"),
            (item(spec_rate="X-SP-010"), "spec_rate X-SP-010 is not a clause row stating a rate per gallon, bag, tube or cartridge"),
            (item(unit="bags", coats=""), "orders bags but the rate X-R-001 is per 'sq ft/gal'"),
            (dict(ANCHORS, spec_rate="X-R-001"), "orders each, so no rate applies"),
            # the quantity row's unit must be what the rate covers, and a count is a counted row in each
            (item(quantity="X-TK-Q-03"), "quantity X-TK-Q-03 is in LF, but the rate X-R-001 covers sq ft"),
            (dict(SEALANT, quantity="X-TK-Q-01"), "quantity X-TK-Q-01 is in sq ft, but the rate X-WEB-008 covers lf"),
            (dict(ANCHORS, quantity="X-TK-Q-01"), "orders each, but X-TK-Q-01 is a dimensioned row in sq ft, not a count"),
            (item(quantity="X-R-001"), "quantity X-R-001 is a clause quantity row, not a takeoff figure, an allowance or a FIELD row"),
            # the coat count is read from a clause row; what it states is the order row's business (order_claim)
            (item(coats="X-TK-Q-01"), "coats X-TK-Q-01 is a dimensioned row, not a clause"),
            # a customer row never overrides a spec clause (owner's rule), so it sets no count either
            (item(coats="X-CU-01"), "coats X-CU-01 is a customer row, not a clause"),
            (dict(SEALANT, coats="X-SP-010"), "coats apply to a coating ordered by the gallon, not to tubes"),
        ]
        for it, want in cases:
            self.assertEqual(materials.item_errors(it, BY_ID), [want], it)
        self.assertEqual(materials.item_errors(item(), BY_ID), [])
        self.assertEqual(materials.item_errors(ANCHORS, BY_ID), [])
        self.assertEqual(materials.item_errors(SEALANT, BY_ID), [])
        # a clause that says the count is not stated, or states none, is evidence, not a refusal: the order waits on it
        self.assertEqual(materials.item_errors(item(coats="X-SP-013"), BY_ID), [])
        self.assertEqual(materials.item_errors(item(coats="X-SP-012"), BY_ID), [])
        # a quantity row with no figure is refused unless it is a FIELD row, and so is a figure with no unit
        blank = dict(BY_ID, **{"X-TK-Q-04": row("X-TK-Q-04", "area", method="dimensioned", role="quantity", unit="sq ft", source="S-1"),
                               "X-TK-Q-05": row("X-TK-Q-05", "walls", method="dimensioned", role="quantity", value="6", source="S-1")})
        self.assertEqual(materials.item_errors(item(quantity="X-TK-Q-04"), blank), ["quantity X-TK-Q-04 carries no figure"])
        self.assertEqual(materials.item_errors(item(quantity="X-TK-Q-05"), blank),
                         ["quantity X-TK-Q-05 is in no unit, but the rate X-R-001 covers sq ft"])
        self.assertEqual(materials.item_errors(item(quantity="X-F-002"), BY_ID), [])
        # a FIELD allowance row is the area the coating covers, to be measured: accepted, and the order waits on it
        by_id = dict(BY_ID, **{ALLOW.claim_id: ALLOW})
        self.assertEqual(materials.item_errors(item(quantity="X-A-004"), by_id), [])
        c = materials.order_claim("X-MT-05", item(quantity="X-A-004"), 2, 2, by_id)
        self.assertEqual((c.value, c.value_num, c.flag, c.method, c.role), ("", None, "unverified", "fetched", "material"))
        self.assertIn("X-A-004 (FIELD)", c.statement)
        # no row states a rate (the plank job's mortar, 2026-10-09): written with no figure, flagged, saying so
        no_rate = item(spec_rate="", sheet="X-WEB-006")
        self.assertEqual(materials.item_errors(no_rate, BY_ID), [])
        c = materials.order_claim("X-MT-06", no_rate, 2, 2, BY_ID)
        self.assertEqual((c.value, c.flag, c.url), ("", "unverified", BULLETIN))
        self.assertIn("no row states a rate", c.statement)
        # a page looked at that did not show the product is cited all the same, and the order says so
        by_id = dict(BY_ID, **{NOPAGE.claim_id: NOPAGE})
        patch = item("Example Patch P20", "gal", "X-A-004", "", "", "X-WEB-007", ("X-SP-010",))
        by_id[ALLOW.claim_id] = ALLOW
        self.assertEqual(materials.item_errors(patch, by_id), [])
        c = materials.order_claim("X-MT-07", patch, 2, 2, by_id)
        self.assertEqual((c.value, c.flag, c.url, c.quote), ("", "unverified", "https://www.example.com/p20", ""))
        self.assertIn("uses flagged X-WEB-007", c.statement)
        self.assertIn("the page X-WEB-007 cites quotes nothing for the product", c.statement)
        self.assertEqual(materials.item_errors(item(spec_rate=""), BY_ID), [])   # the sheet's rate will do
        self.assertEqual(materials.item_errors(item(quantity=""), BY_ID), [])    # nothing covered yet

    def test_vote_keeps_every_distinct_order(self):
        a, b = item(), item(coats="X-SP-011")
        again = dict(a, product="Satin X100 finish")
        orders, dupes = materials.vote([[a, again, ANCHORS], [b, ANCHORS]])
        self.assertEqual(orders, [(a, 1), (ANCHORS, 2), (b, 1)])
        self.assertEqual(dupes, ["run 1: 'Satin X100 finish' names the same order as 'Example Satin X100'; one row"])
        self.assertEqual(materials.vote([]), ([], []))

    def test_inputs_leave_out_scaled_observed_and_material_rows(self):
        mine = row("X-MT-01", "ours", method="fetched", role="material", url=SHEET, retrieved="2026", quote="q")
        obs = row("X-PH-01", "peeling", method="observed", role="scope", source="IMG_1", confidence="inferred")
        self.assertEqual(materials.inputs(ROWS + [SCALED, obs, mine]), ROWS)
        self.assertEqual(materials.sheets(ROWS), [X100, A7, S9])
        unit = materials.unit_for("X", [SYSTEM, SPEC_RATE])
        self.assertEqual(unit.unit_id, "X#materials")
        self.assertEqual(unit.text.splitlines()[1], "X-R-001 | clause | 300-350 sq ft/gal | Example Satin X100 coverage "
                                                    "per coat | SW p.5")


class ClaimCase(unittest.TestCase):
    def test_a_figure_from_the_spec_s_rate_over_the_sheet_s(self):
        c = materials.order_claim("X-MT-01", item(), 2, 2, BY_ID)
        self.assertEqual(c, Claim(
            claim_id="X-MT-01",
            # named after the sheet's page, not in the model's words; the precedence rule settles the rate, so no flag
            # every row the figure rests on is named with it, and cited in the locator and tag (p.6: click from any number)
            statement="X100 data sheet: 1200 sq ft (X-TK-Q-01) x 2 coats (X-SP-010) / 300-350 sq ft/gal (X-R-001) = 6.857142857142857-8 gal" + GOVERNS,
            source_id="S-1 + SW + WEB", method="fetched", role="material", confidence="exact",
            value="6.857142857142857-8", value_num=None, unit="gal", locator="Sheet A-2 and p.4 and p.5",
            tag="S-1 A-2 + SW p.4 + SW p.5 + X100 data sheet", calc="{X-TK-Q-01} * 2 / {X-R-001}",
            derivation="1200 sq ft (X-TK-Q-01) x 2 coats (X-SP-010) / 300-350 sq ft/gal (X-R-001) = 6.857142857142857-8 gal" + GOVERNS,
            division="09", flag="", url=SHEET, retrieved="2026-10-09", quote="320-400 sq. ft. per gallon",
        ))
        # the sheet's rate when the spec gives none; one coat, read from the clause the item names, which the
        # row cites (derivation, locator and tag), so the replay can see where the literal came from
        c = materials.order_claim("X-MT-01", item(spec_rate="", coats="X-SP-011"), 2, 2, BY_ID)
        self.assertEqual((c.value, c.calc, c.flag, c.derivation, c.source_id, c.locator, c.tag),
                         ("3-3.75", "{X-TK-Q-01} * 1 / {X-WEB-003}", "",
                          "1200 sq ft (X-TK-Q-01) x 1 coat (X-SP-011) / 320-400 sq ft/gal (X-WEB-003) = 3-3.75 gal",
                          "S-1 + SW + WEB", "Sheet A-2 and p.4 and p.7", "S-1 A-2 + SW p.4 + SW p.7 + X100 data sheet"))
        self.assertEqual(schema.replay_calc(c, BY_ID), [])

    def test_a_sealant_is_a_length_over_a_rate_per_tube_with_no_coats(self):
        c = materials.order_claim("X-MT-03", SEALANT, 2, 2, BY_ID)
        self.assertEqual((c.value, c.value_num, c.unit, c.calc, c.flag, c.statement, c.division),
                         ("10", 10.0, "tubes", "{X-TK-Q-03} / {X-WEB-008}", "",
                          "S9 data sheet: 240 LF (X-TK-Q-03) / 24 LF/tube (X-WEB-008) = 10 tubes", "07"))
        self.assertEqual(schema.replay_calc(c, BY_ID), [])
        # a mortar by the bag: a volume over a yield per bag
        by_id = dict(BY_ID, **{
            "X-TK-Q-05": row("X-TK-Q-05", "Repair volume", method="dimensioned", role="quantity", value="4.4", unit="CF", source="S-1"),
            "X-WEB-009": row("X-WEB-009", "Example Mortar M1: 0.44 cu ft per bag", method="fetched", role="code", value="0.44",
                             unit="cu ft/bag", source="WEB", tag="M1 data sheet", url="https://www.example.com/m1", retrieved="2026",
                             quote="0.44 cu ft")})
        mortar = item("Example Mortar M1", "bags", "X-TK-Q-05", "", "", "X-WEB-009", ("X-SP-020",))
        self.assertEqual(materials.item_errors(mortar, by_id), [])
        c = materials.order_claim("X-MT-04", mortar, 2, 2, by_id)
        self.assertEqual((c.value, c.unit, c.flag, c.derivation), ("10", "bags", "", "4.4 CF (X-TK-Q-05) / 0.44 cu ft/bag (X-WEB-009) = 10 bags"))

    def test_a_count_cites_the_count_row(self):
        c = materials.order_claim("X-MT-02", ANCHORS, 2, 2, BY_ID)
        self.assertEqual((c.value, c.value_num, c.unit, c.calc, c.flag, c.statement, c.division, c.url),
                         ("18", 18.0, "each", "{X-TK-Q-02}", "", "A7 evaluation report: 18 each (X-TK-Q-02, counted)",
                          "05", BULLETIN))
        # an open question or a flag on a row the order cites is carried onto the order
        asked = dict(BY_ID, **{"X-DR-005": row("X-DR-005", ANCHOR.statement, source="S-1", flag="conflict", division="05")})
        asked["X-DR-005"].question = "X-OQ-01"
        c = materials.order_claim("X-MT-02", ANCHORS, 2, 2, asked)
        self.assertEqual((c.flag, c.question, c.derivation),
                         ("unverified", "X-OQ-01", "18 each (X-TK-Q-02, counted); uses flagged X-DR-005; open question X-OQ-01"))

    def test_what_is_missing_is_named_and_the_row_carries_no_figure(self):
        cases = [
            (item(quantity="X-F-002"), "X-F-002 x 2 coats (X-SP-010) / X-R-001; the quantity waits on X-F-002 (FIELD)"),
            (item(quantity=""), "FIELD x 2 coats (X-SP-010) / X-R-001; no quantity row names what it covers"),
            (item(coats=""), "X-TK-Q-01 x ? coats / X-R-001; no clause states how many coats"),
            # a clause that leaves this product's count open, states none, or is about another product: no figure, said so
            (item(coats="X-SP-013"), "X-TK-Q-01 x ? coats (X-SP-013) / X-R-001; X-SP-013 says the coat count for X100 is not stated"),
            (item(coats="X-SP-012"), "X-TK-Q-01 x ? coats (X-SP-012) / X-R-001; X-SP-012 names X50, not this product"),
            (item(coats="", quantity="X-F-002"),
             "X-F-002 x ? coats / X-R-001; the quantity waits on X-F-002 (FIELD); no clause states how many coats"),
            (dict(ANCHORS, quantity=""), "no count row; no count row names how many"),
            (dict(ANCHORS, quantity="X-F-002"), "count per X-F-002; the count waits on X-F-002 (FIELD)"),
            (dict(SEALANT, quantity="X-F-002"), "X-F-002 / X-WEB-008; the quantity waits on X-F-002 (FIELD)"),
        ]
        for it, how in cases:
            c = materials.order_claim("X-MT-01", it, 2, 2, BY_ID)
            formula, sep, notes = how.partition("; ")     # the precedence remark follows the formula, before the notes
            want = formula + (GOVERNS if it["spec_rate"] else "") + sep + notes
            self.assertEqual((c.value, c.value_num, c.calc, c.flag, c.confidence, c.derivation),
                             ("", None, "", "unverified", "missing", want), it)
        # a coating with a sheet that states no rate, and no spec rate: no figure, said so
        c = materials.order_claim("X-MT-01", item(spec_rate="", sheet="X-WEB-006", coats=""), 2, 2, BY_ID)
        self.assertEqual((c.value, c.derivation), ("", "no rate; no clause states how many coats; no row states a rate"))
        # a coat clause about another product than the sheet's is said so, not read
        c = materials.order_claim("X-MT-01", item(spec_rate="", sheet="X-WEB-006"), 2, 2, BY_ID)
        self.assertEqual(c.derivation, "no rate; X-SP-010 names X100, not this product; no row states a rate")

    def test_fewer_runs_and_flagged_inputs_are_noted(self):
        c = materials.order_claim("X-MT-01", item(spec_rate=""), 1, 2, BY_ID)
        self.assertEqual((c.flag, c.derivation), ("unverified", "1200 sq ft (X-TK-Q-01) x 2 coats (X-SP-010) / 320-400 sq ft/gal (X-WEB-003) = 6-7.5 gal; "
                                                                "seen in 1 of 2 runs"))
        by_id = dict(BY_ID, **{"X-TK-Q-01": row("X-TK-Q-01", "Wall area", method="dimensioned", role="quantity",
                                                  value="1200", unit="sq ft", source="S-1", flag="unverified")})
        c = materials.order_claim("X-MT-01", item(spec_rate=""), 2, 2, by_id)
        self.assertEqual(c.derivation, "1200 sq ft (X-TK-Q-01) x 2 coats (X-SP-010) / 320-400 sq ft/gal (X-WEB-003) = 6-7.5 gal; uses flagged X-TK-Q-01")
        # the precedence rule flags nothing: a spec rate over a sheet rate, both firm, is a firm order
        c = materials.order_claim("X-MT-01", item(), 2, 2, BY_ID)
        self.assertEqual((c.flag, c.confidence), ("", "exact"))

    def test_an_order_is_no_firmer_than_its_inputs(self):
        # an inferred rate (a legacy sheet) makes an inferred order, unflagged: confidence is not doubt
        old = dict(BY_ID, **{"X-WEB-003": row("X-WEB-003", X100.statement, method="fetched", role="code", value="320-400",
                                              unit="sq ft/gal", source="WEB", tag="X100 data sheet", url=SHEET,
                                              retrieved="2026-10-09", quote="320-400 sq. ft. per gallon",
                                              confidence="inferred")})
        c = materials.order_claim("X-MT-01", item(spec_rate=""), 2, 2, old)
        self.assertEqual((c.value, c.confidence, c.flag), ("6-7.5", "inferred", ""))
        self.assertEqual(schema.replay_calc(c, old), [])
        # the spec's rate governs, so the sheet's confidence does not reach the order
        self.assertEqual(materials.order_claim("X-MT-01", item(), 2, 2, old).confidence, "exact")
        # the coat count is a literal in the calc, so the clause it was read from counts here, not in the replay
        soft = dict(BY_ID, **{"X-SP-010": row("X-SP-010", SYSTEM.statement, confidence="inferred")})
        self.assertEqual(materials.order_claim("X-MT-01", item(), 2, 2, soft).confidence, "inferred")
        # a scaled count makes a scaled order
        scaled = dict(BY_ID, **{"X-TK-Q-02": row("X-TK-Q-02", COUNT.statement, method="counted", role="quantity", value="18",
                                                 unit="each", source="S-1", confidence="scaled")})
        self.assertEqual(materials.order_claim("X-MT-02", ANCHORS, 2, 2, scaled).confidence, "scaled")


class RunCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.broker = Broker.open_job(Path(self.tmp.name) / "l.db", "intake", job="X", run_id="X-RUN", create=True,
                                      clock=lambda: "2026-10-09T00:00:00Z")
        self.addCleanup(self.broker.close)
        self.broker.write_register([
            {"source_id": "S-1", "kind": "drawing", "status": "present", "sha256": "a"},
            {"source_id": "SW", "kind": "spec", "status": "present", "sha256": "b"},
            dict(intake.WEB),
        ])
        self.write("spec_reader", SYSTEM, ONE_COAT, NO_COAT, TWO_PRODUCTS, SPEC_RATE, SEAL_SPEC)
        self.write("drawing_reader", AREA, JOINTS, ANCHOR, row("X-DR-003", "3 per bracket", method="counted", role="quantity",
                                                       value="3", unit="per bracket", source="S-1", division=""))
        self.write("takeoff", FIELD, row("X-TK-Q-00", "brackets", method="counted", role="quantity", value="6",
                                        unit="each", source="S-1", division=""), COUNT)
        self.write("materials", X100, A7, S9)

    def write(self, principal, *claims):
        w = self.broker.as_principal(principal)
        for c in claims:
            w.append(c)

    def test_agreed_items_become_order_rows(self):
        client = Fake([{"items": [item(), ANCHORS, SEALANT]}, json.dumps({"items": [ANCHORS, SEALANT, item()]})])
        res = materials.run(self.broker, "X", client)
        self.assertEqual((res.units, res.calls, res.discarded, res.unread, res.refused, res.notes), (1, 2, [], [], [], []))
        self.assertEqual([(c.claim_id, c.value, c.unit, c.agent, c.flag) for c in res.rows],
                         [("X-MT-01", "6.857142857142857-8", "gal", "materials", ""),
                          ("X-MT-02", "18", "each", "materials", ""),
                          ("X-MT-03", "10", "tubes", "materials", "")])
        back = self.broker.ledger.by_id()
        self.assertEqual((back["X-MT-01"].calc, back["X-MT-02"].calc, back["X-MT-03"].calc),
                         ("{X-TK-Q-01} * 2 / {X-R-001}", "{X-TK-Q-02}", "{X-TK-Q-03} / {X-WEB-008}"))
        self.assertTrue(all(c.statement.startswith(("X100 data sheet:", "A7 evaluation report:", "S9 data sheet:"))
                            for c in res.rows), [c.statement for c in res.rows])
        reader, unit, system, schema_, r = client.calls[0]
        self.assertEqual((reader, system, schema_, r), ("materials", prompt("materials"), materials.SCHEMA, 0))
        self.assertIn("X-WEB-003 | fetched | 320-400 sq ft/gal |", unit.text)
        self.assertNotIn("X-MT-", unit.text)
        log = [(e["principal"], e["action"], e["subject"], e["detail"]) for e in self.broker.ledger.log()
               if e["action"] == "model-call"]
        self.assertEqual(log, [("materials", "model-call", "X#materials",
                                f"fake-model; {prompt_version('materials')}; run {n}; valid") for n in (1, 2)])
        self.assertEqual(res.text().splitlines()[0],
                         "materials: 1 units, 2 calls, 3 order rows (3 with a figure), 0 items discarded, 0 units unread")

    def test_a_bad_run_and_bad_items_are_discarded(self):
        # a discarded run earns one spare run; the anchors are then seen in both valid runs, so agreed
        client = Fake(["not json", {"items": [item(sheet="X-NO"), ANCHORS]}, {"items": [ANCHORS]}])
        res = materials.run(self.broker, "X", client)
        self.assertEqual(res.discarded, ["X#materials run 1: not JSON: Expecting value: line 1 column 1 (char 0)",
                                         "X#materials run 2: items[0] names X-NO, which is not a row it was shown"])
        self.assertEqual([(c.claim_id, c.flag, c.derivation) for c in res.rows],
                         [("X-MT-01", "", "18 each (X-TK-Q-02, counted)")])
        self.assertEqual(res.calls, 3)
        self.assertIn("  discarded " + res.discarded[0], res.text())
        self.assertTrue(any(line.startswith(f"  row {res.rows[0].claim_id} | ") and res.rows[0].url in line
                            for line in res.text().splitlines()), res.text())
        res = materials.run(self.broker, "X", Fake(["no", "no", "no"]))
        self.assertEqual((res.unread, res.rows, res.calls), (["X#materials"], [], 3))

    def test_runs_that_name_different_orders_earn_a_spare_run(self):
        worded = dict(ANCHORS, product="A7 anchors, 1/2 in")     # the same order in other words
        client = Fake([{"items": [item()]}, {"items": [worded]}, {"items": [item(), ANCHORS]}])
        res = materials.run(self.broker, "X", client)
        self.assertEqual(res.calls, 3)
        self.assertEqual([(c.claim_id, c.flag, "seen in 2 of 3 runs" in c.derivation) for c in res.rows],
                         [("X-MT-01", "unverified", True), ("X-MT-02", "unverified", True)])
        self.assertTrue(res.rows[1].statement.startswith("A7 evaluation report: "))     # the page names the row, not the model

    def test_two_wordings_of_one_order_in_one_run_make_one_row_and_a_note(self):
        worded = dict(ANCHORS, product="A7 anchors, 1/2 in")
        client = Fake([{"items": [ANCHORS, worded]}, {"items": [ANCHORS]}])
        res = materials.run(self.broker, "X", client)
        self.assertEqual(([c.claim_id for c in res.rows], res.notes),
                         (["X-MT-01"], ["run 1: 'A7 anchors, 1/2 in' names the same order as 'Example Anchor A7 adhesive anchors'; one row"]))

    def test_a_calc_that_does_not_evaluate_refuses_the_item_and_the_run_goes_on(self):
        zero = row("X-WEB-010", "Example Satin X100: 0 sq ft/gal (misprint)", method="fetched", role="code", value="0",
                   unit="sq ft/gal", source="WEB", tag="X100 misprint", url="https://www.example.com/x100b", retrieved="2026",
                   quote="0 sq ft/gal")
        self.write("materials", zero)
        bad = item(spec_rate="", sheet="X-WEB-010")
        client = Fake([{"items": [bad, ANCHORS]}, {"items": [bad, ANCHORS]}])
        res = materials.run(self.broker, "X", client)
        self.assertEqual([c.claim_id for c in res.rows], ["X-MT-02"])
        self.assertEqual(len(res.refused), 1)
        self.assertTrue(res.refused[0].startswith("X-MT-01: calc '{X-TK-Q-01} * 2 / {X-WEB-010}' does not evaluate"), res.refused)

    def test_a_ledger_with_no_sheet_gets_no_call(self):
        broker = Broker.open_job(Path(self.tmp.name) / "m.db", "intake", job="Y", run_id="Y-RUN", create=True)
        self.addCleanup(broker.close)
        broker.write_register([{"source_id": "SW", "kind": "spec", "status": "present", "sha256": "b"}])
        broker.as_principal("spec_reader").append(SYSTEM)
        client = Fake([{"items": []}])
        res = materials.run(broker, "Y", client)
        self.assertEqual((res.calls, res.rows, res.notes, client.calls),
                         (0, [], ["no data sheet rows in the ledger: no order rows (Materials writes fetched rows only)"], []))
        res = materials.run(self.broker, "X", None)
        self.assertEqual((res.calls, res.notes), (0, ["no model client: no order rows"]))

    def test_the_broker_holds_the_order_to_the_rules(self):
        # a scaled row is never shown, so the model cannot name it; an order over one is refused all the same
        by_id = dict(BY_ID, **{SCALED.claim_id: SCALED})
        c = materials.order_claim("X-MT-09", item(spec_rate="", quantity="X-DR-009"), 2, 2, by_id)
        self.assertEqual(materials.item_errors(item(quantity="X-DR-009"), by_id),
                         ["quantity X-DR-009 is a scaled quantity row, not a takeoff figure, an allowance or a FIELD row"])
        self.write("takeoff", SCALED)
        with self.assertRaises(LedgerError) as e:
            self.broker.as_principal("materials").append(c)
        self.assertIn("cannot feed an order quantity", str(e.exception))


FIX_C = {"id": "OBV-C-026", "role": "code", "method": "fetched", "url": SHEET, "quote": "350-400"}
FIX_M = {"id": "OBV-M-002", "role": "material", "method": "FIELD", "unit": "gal", "tag": "SW p.16 + OBV-C-026",
         "statement": "SuperPaint A89, coats not stated"}


def order_row(value="", unit="gal", url=SHEET):
    return row("OBV-MT-01", "x", method="fetched", role="material", value=value, unit=unit, url=url, retrieved="2026",
               quote="q", source="WEB")


class GateCase(unittest.TestCase):
    def gate(self, rows, fixture=(FIX_C, FIX_M)):
        res = materials.OrderResult(rows=list(rows))
        return materials.gate(res, list(fixture))

    def test_a_matching_order_passes_and_a_missing_one_is_a_miss(self):
        g = self.gate([order_row()])
        self.assertEqual((g.ok, g.compared, g.failures, g.misses, g.notes), (True, 1, [], [], []))
        self.assertEqual(g.text().splitlines()[0], "materials gate: PASS (1 fixture orders compared, 1 matched; 80% "
                                                   "needed, and no wrong figure or unit)")
        g = self.gate([])
        self.assertEqual((g.ok, g.misses), (False, [f"OBV-M-002: no order row cites {SHEET}"]))
        self.assertIn("  miss " + g.misses[0], g.text())

    def test_a_wrong_unit_or_figure_fails(self):
        g = self.gate([order_row(unit="each")])
        self.assertEqual(g.failures, [])
        self.assertEqual(g.misses, [f"OBV-M-002: no order row cites {SHEET} in gal (the run orders each from it)"])
        g = self.gate([order_row(value="6-7")])
        self.assertEqual(g.failures, ["OBV-M-002: gives 6-7 gal where the fixture waits on a FIELD measure"])
        # a sealant's tube is not an adhesive's cartridge: another unit is a miss, not a match
        tubes = dict(FIX_M, unit="tubes")
        g = self.gate([order_row(unit="cartridges")], (FIX_C, tubes))
        self.assertEqual((g.ok, g.misses, g.failures),
                         (False, [f"OBV-M-002: no order row cites {SHEET} in tubes (the run orders cartridges from it)"], []))
        # a firm run row where the hand bid flags its order is noted
        hedged = dict(FIX_M, method="counted", value=18, unit="each", flag="conflict")
        g = self.gate([order_row(value="18", unit="each")], (FIX_C, hedged))
        self.assertEqual((g.ok, g.failures, g.notes), (True, [], ["OBV-M-002: the hand bid flags it conflict; the run's row is firm"]))
        flagged = row("OBV-MT-01", "x", method="fetched", role="material", value="18", unit="each", url=SHEET, retrieved="2026",
                      quote="q", source="WEB", flag="unverified")
        self.assertEqual(self.gate([flagged], (FIX_C, hedged)).notes, [])
        counted = dict(FIX_M, method="counted", value=18, unit="each")
        g = self.gate([order_row(value="20", unit="each")], (FIX_C, counted))
        self.assertEqual(g.failures, ["OBV-M-002: gives 20 each, the fixture 18"])
        g = self.gate([order_row(value="18", unit="each"), order_row(value="", unit="each")], (FIX_C, counted))
        self.assertEqual((g.ok, g.failures), (True, []))
        g = self.gate([order_row(value="", unit="each")], (FIX_C, counted))      # still waiting: incomplete, not wrong
        self.assertEqual((g.ok, g.failures, g.misses),
                         (False, [], ["OBV-M-002: the run's each rows carry no figure where the fixture gives 18"]))
        self.assertIn("  FAIL " + self.gate([order_row(value="6-7")]).failures[0], self.gate([order_row(value="6-7")]).text())

    def test_a_row_citing_another_of_the_products_pages_matches_with_a_note(self):
        other = dict(FIX_C, id="OBV-C-027", url=BULLETIN)
        g = self.gate([order_row(url=BULLETIN)], (FIX_C, other, FIX_M))
        self.assertEqual((g.ok, g.failures, g.misses), (True, [], []))
        self.assertEqual(g.notes, [f"OBV-M-002: matched through {BULLETIN}; the hand bid cited {SHEET}"])
        g = self.gate([order_row(url=BULLETIN, unit="each")], (FIX_C, other, FIX_M))   # another unit: still a miss
        self.assertEqual((g.ok, g.misses), (False, [f"OBV-M-002: no order row cites {SHEET}"]))
        g = self.gate([order_row(url="https://www.example.com/elsewhere")], (FIX_C, other, FIX_M))   # not the job's page
        self.assertEqual(g.misses, [f"OBV-M-002: no order row cites {SHEET}"])

    def test_an_order_citing_no_page_is_noted_not_compared(self):
        no_page = dict(FIX_M, id="OBV-M-004", tag="S-1 Det 1", statement="angle per NAN-F-002")
        g = self.gate([order_row()], (FIX_C, FIX_M, no_page))
        self.assertEqual((g.ok, g.compared, g.notes), (True, 1, ["OBV-M-004: cites no fetched page; not compared"]))
        g = self.gate([], (no_page,))
        self.assertEqual((g.ok, g.compared), (False, 0))

    def test_most_orders_must_match(self):
        fix = [FIX_C] + [dict(FIX_M, id=f"OBV-M-00{n}") for n in range(1, 6)]
        g = self.gate([order_row()], fix)                                   # every fixture order cites the one page
        self.assertEqual((g.ok, g.compared, len(g.misses)), (True, 5, 0))
        other = dict(FIX_C, id="OBV-C-030", url=BULLETIN)
        fix = [FIX_C, other] + [dict(FIX_M, id=f"OBV-M-00{n}", tag="OBV-C-030") for n in range(1, 5)] + [FIX_M]
        g = self.gate([order_row()], fix)                                   # 1 of 5 matched
        self.assertEqual((g.ok, g.compared, len(g.misses)), (False, 5, 4))


if __name__ == "__main__":
    unittest.main()


class CliCase(unittest.TestCase):
    def test_the_web_command_binds_the_order_client_to_the_broker(self):
        # live run 8 (2026-10-10): the order client had no key, since only the page reader's client was bound
        import contextlib
        import io
        from types import SimpleNamespace
        from unittest import mock
        from pipeline import cli, fixtures, webread
        from pipeline.readers import live
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        out = Path(tmp.name) / "out"
        made, root = [], Path(__file__).resolve().parent.parent

        class Bound(Fake):
            bound = None

            def bind(self, broker):
                self.bound = (broker.principal.name, len(self.calls))

        def golden(job_dir, ledger_path, client, fetcher, *, repeats, table):
            Path(ledger_path).unlink(missing_ok=True)
            broker, _ = fixtures.load(Path(job_dir), Path(ledger_path))
            return broker, SimpleNamespace(text=lambda: "pages"), SimpleNamespace(text=lambda: "page gate", ok=True)

        with mock.patch.object(live, "LiveClient", lambda **kw: made.append(Bound([{"items": []}] * 3, kw["model"]))
                               or made[-1]), \
                mock.patch.object(webread, "golden", golden), \
                contextlib.redirect_stdout(io.StringIO()) as log:
            rc = cli.main(["web", str(root / "fixtures" / "nantucket"), "--out", str(out)])
        self.assertEqual([c.model_id for c in made], ["claude-haiku-5-5", "claude-sonnet-5-5"])
        self.assertEqual(made[1].bound, ("auditor", 0))     # bound (the fixture loader opens the ledger as the auditor), before its first call
        self.assertEqual(len(made[1].calls), 2)
        self.assertEqual(rc, 1)                             # no order rows: the order gate fails
        self.assertIn("materials: 1 units, 2 calls, 0 order rows", log.getvalue())
        self.assertTrue((out / "gate.txt").exists())
        # a client the broker cannot give a key to stops the run before any call, as for the page reader
        class Keyless(Bound):
            def bind(self, broker):
                raise live.LiveRunError("no key")

        made.clear()
        with mock.patch.object(live, "LiveClient", lambda **kw: made.append(Keyless([], kw["model"])) or made[-1]), \
                mock.patch.object(webread, "golden", golden), contextlib.redirect_stderr(io.StringIO()) as err:
            rc = cli.main(["web", str(root / "fixtures" / "nantucket"), "--out", str(out)])
        self.assertEqual((rc, err.getvalue().strip(), [c.calls for c in made]), (3, "NOT RUN: no key", [[], []]))
