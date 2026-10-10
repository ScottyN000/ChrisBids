"""The coat count a clause row states for one product, read by code: one count
tied to this product's code or role, else none and the reason (architecture
p.4, p.9, p.16)."""
import unittest

from pipeline import coats, schema
from pipeline.schema import Claim

SHEET = "https://www.example.com/x100.pdf"


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
S9 = row("X-WEB-008", "Example Seal S9: 24 LF per 10.1 oz tube at a 1/4 in joint", method="fetched", role="code",
         value="24", unit="LF/tube", source="WEB", locator="ask a1", tag="S9 data sheet",
         url="https://www.example.com/s9.pdf", retrieved="2026-10-09", quote="24 linear feet per tube", division="")
X100 = row("X-WEB-003", "Example Satin X100: 320-400 sq ft/gal", method="fetched", role="code", value="320-400",
           unit="sq ft/gal", source="WEB", locator="ask a1", tag="X100 data sheet", url=SHEET, retrieved="2026-10-09",
           quote="320-400 sq. ft. per gallon", division="")


class CoatCountCase(unittest.TestCase):
    def test_the_coat_count_is_read_from_the_clause(self):
        self.assertEqual(coats.coat_count(SYSTEM), (2, ""))
        self.assertEqual(coats.coat_count(ONE_COAT), (1, ""))
        self.assertEqual(coats.coat_count(row("C", "Coats: 3")), (3, ""))
        self.assertEqual(coats.coat_count(row("C", "two finish coats over one prime coat"))[0], None)   # two counts
        self.assertEqual(coats.coat_count(NO_COAT), (None, "X-SP-012 states no coat count for X50"))
        self.assertEqual(coats.coat_count(row("C", "2 coats of primer and 2 coats of finish")), (2, ""))   # one count
        self.assertEqual(coats.coat_count(row("C", "Finish: two coats, sheen not stated")), (2, ""))   # not about coats
        self.assertEqual(coats.coat_count(row("C", "two coats; primer coats not stated")),
                         (None, "C states a coat count and says one is not stated; it does not settle this product's"))

    def test_a_clause_naming_several_products_is_read_by_this_product_s_code(self):
        # each count belongs to the product code in its own stretch of the clause (the repaint's finish clause)
        self.assertEqual(coats.coat_count(TWO_PRODUCTS, X100), (None, "X-SP-013 says the coat count for X100 is not stated"))
        x200 = row("X-WEB-009", "Example Flat X200 data sheet", method="fetched", role="code", source="WEB", tag="X200 data sheet")
        self.assertEqual(coats.coat_count(TWO_PRODUCTS, x200), (1, ""))
        # a count before its code binds to it, and a product the clause gives no count is left open
        primer = row("X-SP-050", "Primer: Example Primer B66 as needed, then 1 coat Example Flat B53")
        b66 = row("X-WEB-010", "Example Primer B66 data sheet", method="fetched", role="code", source="WEB", tag="B66 data sheet")
        b53 = row("X-WEB-011", "Example Flat B53 data sheet", method="fetched", role="code", source="WEB", tag="B53 data sheet")
        self.assertEqual(coats.coat_count(primer, b66), (None, "X-SP-050 states no coat count for B66"))
        self.assertEqual(coats.coat_count(primer, b53), (1, ""))
        # a clause about other products, or several with no product to tie them to, settles nothing
        self.assertEqual(coats.coat_count(TWO_PRODUCTS, S9), (None, "X-SP-013 names X100, X200; which is this product's is not settled"))
        self.assertEqual(coats.coat_count(TWO_PRODUCTS), (None, "X-SP-013 names X100, X200; which is this product's is not settled"))
        self.assertEqual(coats.coat_count(NO_COAT, X100), (None, "X-SP-012 names X50, not this product"))
        self.assertEqual(coats.coat_count(SYSTEM, X100), (2, ""))
        # a stretch is cut at ";", a sentence end, "or", "then", "and", "over", "followed by", "after", "before" and "prior to"
        # outside parentheses; commas and parentheses stay inside
        enamel = row("X-SP-051", "Prime Coat: Example Primer B66 as needed; Finish Coat (1 coat): Example Enamel, B53 series")
        self.assertEqual(coats.coat_count(enamel, b66), (None, "X-SP-051 states no coat count for B66"))
        self.assertEqual(coats.coat_count(enamel, b53), (1, ""))
        two = row("X-SP-052", "Example Primer B66 primer; two coats, Example Flat B53 finish")
        self.assertEqual((coats.coat_count(two, b66), coats.coat_count(two, b53)), ((None, "X-SP-052 states no coat count for B66"), (2, "")))
        # "over" sets a finish on its primer: two stretches, so the finish's count is its own
        both = row("X-SP-053", "Two coats Example Flat B53 over Example Primer B66 primer")
        self.assertEqual((coats.coat_count(both, b53), coats.coat_count(both, b66)), ((2, ""), (None, "X-SP-053 states no coat count for B66")))
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
                    self.assertEqual(coats.coat_count(row("X-SP-054", text), who),
                                     (None, "X-SP-054 does not tie a coat count to one product; which is this product's is not settled"), (text, who.claim_id))
        # "or approved equal" is not a choice between products
        self.assertEqual(coats.coat_count(row("X-SP-056", "Example Flat B53 or approved equal, 2 coats"), b53), (2, ""))
        self.assertEqual(coats.coat_count(row("X-SP-056", "Example Flat B53 or an equivalent, 2 coats"), b53), (2, ""))
        self.assertEqual(coats.coat_count(row("X-SP-056", "Example Flat B53 or an approved equal, 2 coats"), b53), (2, ""))
        # the statement and the quote are two stretches, so a count in one is not tied to a code in the other
        quoted = row("X-SP-055", "Topcoat: Example Flat B53, 2 coats", quote="Example Primer B66 primer as needed")
        self.assertEqual((coats.coat_count(quoted, b53), coats.coat_count(quoted, b66)), ((2, ""), (None, "X-SP-055 states no coat count for B66")))
        # an unclosed parenthesis in the statement does not swallow the quote: they are always two stretches
        clipped = row("X-SP-057", "Finish (K62 series", quote="A89 primer, 1 coat")
        a89 = row("X-WEB-013", "A89 data sheet", method="fetched", role="code", source="WEB", tag="A89 primer")
        k62 = row("X-WEB-014", "K62 data sheet", method="fetched", role="code", source="WEB", tag="K62 finish")
        self.assertEqual((coats.coat_count(clipped, a89), coats.coat_count(clipped, k62)), ((1, ""), (None, "X-SP-057 states no coat count for K62")))
        # a sheet naming no code is read as the clause's one code, with or without a quote; a clause naming several settles nothing for it
        plain = row("X-WEB-015", "the finish data sheet", method="fetched", role="code", source="WEB", tag="finish sheet")
        self.assertEqual(coats.coat_count(row("C", "Topcoat K62: 2 coats"), plain), (2, ""))
        self.assertEqual(coats.coat_count(row("C", "Topcoat K62: 2 coats", quote="Topcoat K62: 2 coats"), plain), (2, ""))
        self.assertEqual(coats.coat_count(row("C", "Topcoat K62: 2 coats", quote="A89 primer as needed"), plain),
                         (None, "C names K62, A89; which is this product's is not settled"))
        # ... but only when every stretch names that code: a stretch naming none may be the codeless sheet's own product
        for text in ("Masonry conditioner as needed; Example Flat B53, 2 coats", "Example Flat B53, 2 coats; see the primer's data sheet"):
            self.assertEqual(coats.coat_count(row("C", text), plain),
                             (None, "C names B53 in one stretch and a product without a code in another; whether the other is this product's is not settled"), text)
        self.assertEqual(coats.coat_count(row("C", "Topcoat K62: 2 coats", quote="Finish: 2 coats"), plain),
                         (None, "C names K62 in one stretch and a product without a code in another; whether the other is this product's is not settled"))
        # ... while a stretch about no product ("Walls", "back-roll") leaves the reading alone
        self.assertEqual(coats.coat_count(row("C", "Walls and ceilings: Example Flat B53, 2 coats, and back-roll the first"), plain), (2, ""))
        # a clause naming no code settles a count only when no stretch names a product the count could be for
        # (by a primer or finish word), and a stretch naming both a primer and a finish holds two products' wording
        for text in ("Prime coat: exterior latex primer; Finish coats: two coats exterior latex satin", "2 coats of finish; primer as needed",
                     "Prime coat: exterior latex primer as needed. Finish coats: two coats exterior latex satin", "Prime as needed, followed by two coats",
                     "Primer as needed, followed by two finish coats"):
            for who in (plain, None):
                self.assertEqual(coats.coat_count(row("C", text), who),
                                 (None, "C names no product code, and names a product in one stretch and speaks of coats in another; which product the count is for is not settled"), text)
        for text in ("Primer as needed with two finish coats", "Finish: two coats on the primer where it shows", "Finish with primer, 2 coats",
                     "Two coats of enamel, primer as needed"):
            self.assertEqual(coats.coat_count(row("C", text), plain),
                             (None, "C names no product code and speaks of a primer and a finish where it states coats; which the count is for is not settled"), text)
        for text in ("Walls and ceilings: two coats", "Apply two coats and back-roll", "Coats: 2 and back-roll the first", "Two coats. Allow 4 hours between coats.",
                     "Two coats, followed by a wash"):
            self.assertEqual(coats.coat_count(row("C", text), plain), (2, ""), text)
        # a codeless clause about a primer is no finish sheet's count, and the reverse
        primer_sheet = row("X-WEB-016", "the primer data sheet", method="fetched", role="code", source="WEB", tag="primer sheet")
        for sheet in (plain, row("X-WEB-017", "Example Enamel data sheet", method="fetched", role="code", source="WEB", tag="enamel sheet")):
            self.assertEqual(coats.coat_count(row("C", "Prime coat: two coats of primer"), sheet),
                             (None, "C speaks of a primer where it states coats, and this product's row is not a primer's alone"), sheet.tag)
        self.assertEqual(coats.coat_count(row("C", "Finish: two coats"), primer_sheet),
                         (None, "C speaks of a finish where it states coats, and this product's row names a primer"))
        self.assertEqual(coats.coat_count(row("C", "Prime coat: two coats of primer"), primer_sheet), (2, ""))
        self.assertEqual(coats.coat_count(row("C", "Walls: two coats"), primer_sheet),   # a bare count is the finish's or the system's
                         (None, "C names no primer where it states coats, and this product's row names a primer"))
        self.assertEqual(coats.coat_count(row("C", "Example B53, 2 coats"), primer_sheet),
                         (None, "C names no primer with the coat count for B53, and this product's row names a primer; whether the count is B53's is not settled"))
        self.assertEqual(coats.coat_count(row("C", "Example Primer B53, 2 coats"), primer_sheet), (2, ""))
        self.assertEqual(coats.coat_count(row("C", "Example B53, 2 coats"), plain), (2, ""))
        self.assertEqual(coats.coat_count(row("C", "Prime coat: two coats of primer")), (2, ""))
        for text in ("one coat; two at patched areas", "one coat (two at repairs)", "a second coat at repairs", "One coat, another coat at repairs",
                     "1 coat overall, then 2 at patched areas", "one coat, or two where patched", "one coat, with 2 at repairs"):
            self.assertEqual(coats.coat_count(row("C", text))[0], None, text)
        self.assertEqual(coats.coat_count(row("C", "one coat (two at repairs)")), (None, "C states a coat count and more coats in places; it does not settle this product's"))
        self.assertEqual(coats.coat_count(row("C", "2 coats of primer and 3 coats of finish"), plain),
                         (None, "C names no product code and states different coat counts in its stretches; which is this product's is not settled"))
        self.assertEqual(coats.coat_count(row("C", "2 coats of primer and 2 coats of finish"), plain), (2, ""))
        # a sentence end cuts a stretch, and a stretch naming a primer and a finish ties its count to neither
        for text in ("Prime Coat: Example Primer B66 as needed. Finish Coat: two coats.", "Example Primer B66 as needed, followed by two finish coats",
                     "Example Flat B53, one coat; two at patched areas", "Example Flat B53: one coat; a second coat at repairs"):
            for who in (b53, b66):
                if who.statement.split()[-3] in text:
                    self.assertEqual(coats.coat_count(row("C", text), who),
                                     (None, "C does not tie a coat count to one product; which is this product's is not settled"), (text, who.claim_id))
        self.assertEqual(coats.coat_count(row("C", "Example Flat B53, 1 coat; 2 at patched areas"), b53),
                         (None, "C does not tie a coat count to one product; which is this product's is not settled"))
        self.assertEqual(coats.coat_count(row("C", "Example Flat B53, 1 coat (10 year)"), b53), (1, ""))
        # a hyphenated count is a count ("2-coat system"), so two of them settle nothing
        self.assertEqual(coats.coat_count(row("C", "Example Flat B53, 2-coat finish"), b53), (2, ""))
        self.assertEqual(coats.coat_count(row("C", "Example Flat B53, one coat (two-coat finish on bare wood)"), b53),
                         (None, "C states 2 coat counts for B53; it does not settle this product's"))
        # a system's count, or one split across products, is nobody's count
        for text in ("Example Flat B53, 2-coat system", "Example B53, 2 coats incl. primer"):   # "incl." ends a sentence, so the primer is cut off
            self.assertEqual(coats.coat_count(row("C", text), b53),
                             (None, "C states a coat count for B53 for a system or split across products; whose coats they are is not settled"), text)
        for text in ("Example B53, 2-coat system including primer", "Example B53, two coats of which one is primer"):
            self.assertEqual(coats.coat_count(row("C", text), b53),
                             (None, "C names a primer with the coat count for B53; whether the count is B53's is not settled"), text)
        self.assertEqual(coats.coat_count(row("C", "Example Flat B53, two coats (one primer, one finish)"), b53),
                         (None, "C does not tie a coat count to one product; which is this product's is not settled"))
        for text in ("2-coat system including primer", "two coats incl. primer", "two coats of which one is primer", "two coats (one primer, one finish)"):
            self.assertEqual(coats.coat_mentions(text)[-1][1], "split", text)
        self.assertEqual(coats.coat_count(row("C", "Two-coat system including primer"), primer_sheet),
                         (None, "C states a coat count for a system or split across products; whose coats they are is not settled"))
        # the whole stretch is judged, the count's own object ("one coat of primer, B53", "B53 with one coat of primer") included
        for text, who in (("Example Flat B53 with one coat of primer", b53), ("Example Primer B66, 2 coats of finish", b66),
                          ("Example Primer B66 under 2 coats of finish", b66), ("Example Flat B53, two coats (primer and finish)", b53)):
            self.assertEqual(coats.coat_count(row("C", text), who),
                             (None, "C does not tie a coat count to one product; which is this product's is not settled"), text)
        self.assertEqual(coats.coat_count(row("C", "Example B53 with one coat of primer"), b53),
                         (None, "C names a primer with the coat count for B53; whether the count is B53's is not settled"))
        for who in (b66, primer_sheet):   # a sheet naming no code takes the clause's one code as its own
            self.assertEqual(coats.coat_count(row("C", "Example B66, 2 coats of finish"), who),
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
                     "Example Flat B53, 2 coats over existing paint; apply more as required for full hide",
                     # an abbreviation ends no sentence; a later sentence's hedge floors the count unless it names another product
                     "Example Flat B53, 2 coats (min. 2.0 mils DFT per coat), or more as required for full hide", "Example Flat B53, 2 coats at approx. 350 sq ft/gal, or more as required",
                     "Example Flat B53, 2 coats; deep colors may require more", "Example Flat B53, 2 coats. Some substrates need more for full hide",
                     "Example Flat B53, 2 coats; remove loose plaster, or as required by the Architect", "Example Flat B53, 2 coats; caulk joints as required",
                     # the count's own code or role in the later sentence names no other product; "more than" hedges unless it compares a measurement
                     "Example Flat B53, 2 coats. Deep colors of B53 may require more.", "Example Flat B53, 2 coats; apply more finish as required for full hide",
                     "Example Flat B53, 2 coats. Apply additional B53 where required for full hide.", "Example B53 enamel, 2 coats. Deep-tone enamels may require more.",
                     "Example Flat B53, 2 coats. Deep colors may require more than two.", "Example Flat B53, 2 coats; dark colors may need more than that",
                     "Example Flat B53, 2 coats; deep colors may require more than two in exterior exposures",
                     # naming the other product does not make the hedge its own unless it stands right before the hedge
                     "Example B53 enamel, 2 coats; deep colors over a tinted primer may require more", "Example Flat B53, 2 coats. Apply more as required to hide the primer.",
                     "Example Flat B53, 2 coats. Accent colors XC6258 may require more for full hide.", "Example Flat B53, 2 coats; apply more as required per ASTM D4258",
                     "Example Flat B53, 2 coats. Where required, back-prime trim"):
            self.assertEqual(coats.coat_count(row("C", text), b53),
                             (None, "C states a minimum coat count for B53, not a fixed one; it does not settle this product's"), text)
        for text in ("Two coats, or more as required for full hide", "Two coats; deep colors may require more than two coats",
                     "Finish: 2 coats; dark colors may need more than the two coats specified"):
            self.assertEqual(coats.coat_count(row("C", text)),
                             (None, "C states a minimum coat count, not a fixed one; it does not settle this product's"), text)
        both = row("C", "Example Primer B66, 1 coat; Example Flat B53, 2 coats; apply more as required")   # a later sentence's hedge floors every count before it
        self.assertEqual((coats.coat_count(both, b66)[0], coats.coat_count(both, b53)[0]), (None, None))
        self.assertEqual(coats.coat_count(row("C", "Example Flat B53, 2 coats. Extra materials: furnish 1 gal of each color."), b53), (2, ""))
        self.assertEqual(coats.coat_count(row("C", "Walls: 2 coats; apply more coats as needed")),   # added coats, not a floor
                         (None, "C states a coat count and more coats in places; it does not settle this product's"))
        for text in ("Example Flat B53, 2 coats; apply more coats as needed", "Example Flat B53, 2 coats; recoat more where needed",
                     "Example Flat B53, 2 coats; apply more paint as required for full hide", "Example Flat B53, 2 coats; recoat as required for full hide",
                     "Example Flat B53, 2 coats; a third coat where needed for full hide", "Example Flat B53, 2 coats. Apply additional paint as required for full hide.",
                     "Example Flat B53, 2 coats; apply extra material where needed", "Example Flat B53, 2 coats; apply additional coating as required"):
            self.assertIsNone(coats.coat_count(row("C", text), b53)[0], text)
        # a ceiling is a range, as "1-2 coats" is
        for text in ("Example Flat B53, up to two coats", "Example Flat B53, no more than two coats", "Example Flat B53, two coats maximum", "Example Flat B53, a maximum of 2 coats",
                     "Example Flat B53, 2 coats (max.)"):
            self.assertEqual(coats.coat_count(row("C", text), b53), (None, "C states a range of coats for B53; it does not settle this product's"), text)
        self.assertEqual(coats.coat_count(row("C", "Example Flat B53, 2 coats, maximum 4 mils DFT per coat"), b53), (2, ""))
        for text in ("Example Flat B53, 2 coats (min. 2.0 mils DFT per coat)", "Example Flat B53, two coats, minimum 4 mils DFT each",   # a film thickness, not a floor
                     "Example Flat B53, 2 coats, at least 3 mils dry", "Example Flat B53, 2 coats, minimum of 3 mils",
                     "Example Flat B53, 2 coats, minimum DFT 2.0 mils per coat", "Example Flat B53, 2 coats, min. dry film thickness 4 mils",
                     # "or more" and "or as required" are floors only after a count
                     "Surfaces 10 ft or more above grade: Example Flat B53, 2 coats", "Scrape loose paint, or as required by the Architect; Example Flat B53, 2 coats.",
                     "Example Flat B53, 2 coats on surfaces 10 ft or more above grade", "Example Flat B53, 2 coats where 50% or more of the surface is bare",
                     # "more than" compares a measurement, else it hedges; the other product right before a later sentence's hedge keeps the count
                     "Example Flat B53, 2 coats on surfaces more than 10 ft above grade", "Example Flat B53, 2 coats, not more than 4 mils DFT per coat",
                     "Example Flat B53, 2 coats; Example Primer B66 as needed", "Example Flat B53, 2 coats. Spot-prime as required.",
                     "Example Flat B53, 2 coats where more than 50% of the surface is bare", "Example Flat B53, 2 coats on surfaces more than ten feet above grade",
                     "Example Flat B53, 2 coats, applied with 9 in rollers", "Example Flat B53, 2 coats on piping over 2 in diameter",
                     "Example Flat B53, 2 coats in color 7006, or 7005 for trim",
                     # a bare number is an added coat only where a second count can start
                     "Example Flat B53, 2 coats; refer to Section 09 01 90 for surface preparation.", "Example Flat B53, 2 coats per Section 9 for all trim",
                     "Example Flat B53, 2 coats; refer to Part 3 for surface preparation", "Example Flat B53, 2 coats with a 9 in roller",
                     "Example Flat B53, 2 coats on surfaces more than 10 ft above grade; prime bare steel first",
                     # a primed substrate names no primer, and "priming" does only as a label
                     "Previously primed surfaces: Example B53, 2 coats", "Shop-primed steel: Example B53, two coats", "After priming, apply Example B53, 2 coats"):
            self.assertEqual(coats.coat_count(row("C", text), b53), (2, ""), text)
        for text in ("Example Flat B53, 2 coats including priming", "Example Flat B53, two coats incl. spot priming"):
            self.assertEqual(coats.coat_count(row("C", text), b53),
                             (None, "C states a coat count for B53 for a system or split across products; whose coats they are is not settled"), text)
        self.assertEqual(coats.coat_count(row("C", "Example B53, two coats, the first a priming coat"), b53),
                         (None, "C names a primer with the coat count for B53; whether the count is B53's is not settled"))
        self.assertEqual(coats.coat_count(row("C", "Priming (one coat)."), primer_sheet), (1, ""))
        self.assertEqual(coats.coat_count(row("C", "Exterior trim: two coats including priming"), plain),
                         (None, "C states a coat count for a system or split across products; whose coats they are is not settled"))
        self.assertEqual(coats.coat_count(row("C", "Previously primed surfaces: Example B53, 2 coats"), primer_sheet),
                         (None, "C names no primer with the coat count for B53, and this product's row names a primer; whether the count is B53's is not settled"))
        self.assertEqual(coats.coat_count(row("C", "Shop-primed steel: two coats"), primer_sheet),
                         (None, "C names no primer where it states coats, and this product's row names a primer"))
        # plural and -ing role words
        self.assertEqual(coats.coat_count(row("C", "Example Primer B66 under finishes, 2 coats"), b66),
                         (None, "C does not tie a coat count to one product; which is this product's is not settled"))
        self.assertEqual(coats.coat_count(row("C", "Priming: one coat"), primer_sheet), (1, ""))
        self.assertEqual(coats.coat_count(row("C", "Priming: Example B66, 1 coat"), primer_sheet), (1, ""))
        self.assertEqual(coats.coat_count(row("C", "Trim: one coat of primer, Example B53"), b53),
                         (None, "C names a primer with the coat count for B53; whether the count is B53's is not settled"))
        self.assertEqual(coats.coat_count(row("C", "2 coats of finish, Example B66"), b66),
                         (None, "C names a finish with the coat count for B66; whether the count is B66's is not settled"))
        # a finish sheet whose quote also names a primer is not a primer's sheet alone
        mixed = row("X-WEB-019", "Example Enamel B53 data sheet", method="fetched", role="code", source="WEB", tag="B53 enamel",
                    quote="Apply over a compatible primer; 350-400 sq ft/gal")
        self.assertEqual(coats.coat_count(row("C", "Example B53 with primer, 2 coats"), mixed),
                         (None, "C names a primer with the coat count for B53; whether the count is B53's is not settled"))
        self.assertEqual(coats.coat_count(row("C", "Siding: primer, Example X100, 2 coats"),
                                              row("X-WEB-020", "Example Paint & Primer X100 data sheet", method="fetched", role="code", source="WEB", tag="X100")),
                         (None, "C names a primer with the coat count for X100; whether the count is X100's is not settled"))
        self.assertEqual(coats.coat_count(row("C", "One coat; 2-coat on new work"))[0], None)
        # a sheet that names a second product (a recommended primer, a system) is read for neither
        both = row("X-WEB-018", "Example Flat B53 data sheet; prime with Example Primer B66", method="fetched", role="code", source="WEB", tag="B53 enamel")
        self.assertEqual(coats.coat_count(row("C", "Example Primer B66, 1 coat"), both),
                         (None, "C names B66, and this product's row names B53, B66; which of them is this product is not settled"))
        for text in ("Example Flat B53, one coat, two at patched areas", "Example Flat B53, one coat (two at repairs)", "Example Flat B53, 1 coat, 2 at patched areas",
                     "Example Flat B53, 1 coat, plus 1 coat at patched areas", "Example Flat B53, 1 coat, plus a further coat at repairs"):
            self.assertEqual(coats.coat_count(row("C", text), b53),
                             (None, "C states a coat count for B53 and more coats in places; it does not settle this product's"), text)
        # a count in the stretch before a sequence word is that stretch's code's ("B53, 2 coats after B66")
        after = row("C", "Example Flat B53, 2 coats after Example Primer B66")
        self.assertEqual((coats.coat_count(after, b53), coats.coat_count(after, b66)), ((2, ""), (None, "C states no coat count for B66")))
        # a primer named in the count's stretch may own it ("B53 with primer, 2 coats"), unless the sheet is the primer's;
        # ... wherever in the count's stretch the primer is named, "B53 with one coat of primer" included
        for text in ("Example B53 with primer, 2 coats", "Base coat as needed, Example B53, 2 coats", "Primer as needed, 1 coat Example B53"):
            self.assertEqual(coats.coat_count(row("C", text), b53),
                             (None, "C names a primer with the coat count for B53; whether the count is B53's is not settled"), text)
        self.assertEqual(coats.coat_count(row("C", "Prime coat: Example Primer B66, 1 coat"), b66), (1, ""))
        enamel_sheet = row("X-WEB-017", "Example Enamel data sheet", method="fetched", role="code", source="WEB", tag="enamel sheet")
        self.assertEqual(coats.coat_count(row("C", "Prime coat: Example Primer B66, 1 coat"), enamel_sheet),
                         (None, "C names a primer with the coat count for B66; whether the count is B66's is not settled"))
        self.assertEqual(coats.coat_count(row("C", "Prime coat: Example Primer B66, 1 coat"), primer_sheet), (1, ""))
        self.assertEqual(coats.coat_count(row("C", "Finish: Example Satin X100, 2 coats"), primer_sheet),
                         (None, "C names a finish with the coat count for X100; whether the count is X100's is not settled"))
        self.assertEqual(coats.coat_count(row("C", "Example Flat B53, 2 coats; recoat after 4 hours at 77F"), b53), (2, ""))
        # "Walls and ceilings: B53, 2 coats" continues no product, so the count after the code is B53's
        self.assertEqual(coats.coat_count(row("C", "Walls and ceilings: Example Flat B53, 2 coats"), b53), (2, ""))
        self.assertEqual(coats.coat_count(row("C", "Example Flat B53, 2 coats. Allow 4 hours between coats."), b53), (2, ""))
        # a count at the head of the quote, or of a stretch opened by "then", is read from where it stands
        self.assertEqual(coats.coat_count(row("C", "Finish: Example Flat B53", quote="Coats: 2, Example Flat B53"), b53), (2, ""))
        self.assertEqual(coats.coat_count(row("C", "Example Primer B66 as needed, then coats: 2, Example Flat B53"), b53), (2, ""))
        # the repo's own wording of an open count is seen beside a count
        self.assertEqual(coats.coat_count(row("C", "Example Flat B53, 2 coats on new work; the B53 coat count on repaint is not stated"), b53),
                         (None, "C states a coat count for B53 and says one is not stated; it does not settle this product's"))
        self.assertEqual(coats.coat_count(row("C", "The B53 coat count is not stated"), b53), (None, "C says the coat count for B53 is not stated"))
        self.assertEqual(coats.coat_count(row("C", "Number of coats on repaint not specified")), (None, "C says the coat count is not stated"))
        # a sheet carrying two of the clause's codes settles nothing
        system = row("X-WEB-012", "Example Flat B53 over Example Primer B66 system", method="fetched", role="code", source="WEB", tag="B53 system")
        self.assertEqual(coats.coat_count(two, system),
                         (None, "X-SP-052 names B53, B66, which this product's row both carries; which count is its is not settled"))

    def test_a_range_of_coats_or_a_recoat_time_is_no_count(self):
        # two readings, neither picked (Determinism); a dry time is not a count
        for text in ("Apply 1-2 coats", "one or two coats", "Coats: 2-3", "coats: 2 to 3", "Apply 2\u20133 coats"):
            self.assertEqual(coats.coat_count(row("C", text)), (None, "C states a range of coats; it does not settle this product's"), text)
        for text in ("Dry time between coats: 4 hours", "Dry time between coats: 1.5 hours", "Dry time between coats: 24 hours",
                     "coats: 16 hrs", "coats: 10 mils", "Finish coat: 400 sq ft/gal", "Apply a 15 mil coat of Example Elastomeric A100",
                     "Coats: 2 - 3", "Coats: 2 \u2013 3", "Coats: 2 to 3", "between two and three coats", "0 coats", "Coats: 0", "Two (0) coats",
                     "Mils per coat: 4", "WFT/coat: 4", "Dry time between coats: 24.", "Spread rate per coat: 350", "Recoat interval between coats: 24;",
                     "Dry time between finish coats: 24.", "mils per top coat: 4", "DFT per prime coat: 3.", "1/2 coat", "Apply 2/3 coats"):
            self.assertEqual(coats.coat_count(row("C", text))[0], None, text)
        self.assertEqual(coats.coat_count(row("C", "Dry time between coats: 24 hours")), (None, "C states no coat count"))
        # the "coats:" form counts only when "coats" is the label itself, not the tail of one ("mils per coat: 4")
        self.assertEqual(coats.coat_count(row("C", "Dry time between coats: 24.")), (None, "C states no coat count"))
        b53 = row("X-WEB-010", "B53 data sheet", method="fetched", role="code", source="WEB", tag="B53 enamel")
        self.assertEqual(coats.coat_count(row("C", "Example Flat B53, mils per coat: 4"), b53), (None, "C states no coat count for B53"))
        self.assertEqual(coats.coat_count(row("C", "Example Flat B53, wet mils per finish coat: 4"), b53), (None, "C states no coat count for B53"))
        self.assertEqual(coats.coat_count(row("C", "Apply 2/3 coats")), (None, "C states a range of coats; it does not settle this product's"))
        for text in ("Finish coats: 2", "Number of coats: 2", "Topcoat, coats: 2", "(coats: 2)", "Prime coats: 2 and finish coats: 2"):
            self.assertEqual(coats.coat_count(row("C", text)), (2, ""), text)
        # "coats: 2 and ..." is a range only when a second number follows
        self.assertEqual(coats.coat_count(row("C", "Coats: 2 and 3")), (None, "C states a range of coats; it does not settle this product's"))
        self.assertEqual(coats.coat_count(row("C", "Example Flat B53: coats: 2 and back-roll the first"), b53), (2, ""))
        self.assertEqual(coats.coat_count(row("C", "between two and three coats")), (None, "C states a range of coats; it does not settle this product's"))
        # after "coats:" the number ends the clause or is followed by punctuation or "coat(s)"
        for text in ("Coats: 2", "Coats: 2.", "Coats: 2, recoat: 4 hours", "(coats: 2)", "coats: 12"):
            self.assertEqual(coats.coat_count(row("C", text))[0], 12 if "12" in text else 2, text)
        self.assertEqual(coats.coat_count(row("C", "Dry time between coats: 1.5 hours")), (None, "C states no coat count"))
        self.assertEqual(coats.coat_count(row("C", "Coats: 2 - 3")), (None, "C states a range of coats; it does not settle this product's"))
        # "two (2) coats" is one count said twice; "two (3)" is two
        self.assertEqual(coats.coat_count(row("C", "Two (2) coats of Example Flat B53")), (2, ""))
        self.assertEqual(coats.coat_count(row("C", "Two (3) coats")), (None, "C states 2 coat counts; it does not settle this product's"))
        self.assertEqual(coats.coat_count(row("C", "Coats: 2, recoat: 4 hours")), (2, ""))
        self.assertEqual(coats.coat_count(row("C", "Example Flat B53, 1-2 coats; Example Primer B66, 1 coat"),
                                              row("W", "Example Flat B53 sheet", method="fetched", role="code", source="WEB")),
                         (None, "C states a range of coats for B53; it does not settle this product's"))
        self.assertEqual(coats.product_codes("see X-SP-011, ft2 and S-1; A89 or K62, then A89"), ["A89", "K62"])



    def test_round_25_readings_each_side_of_the_line(self):
        # #23's advisor round 25: each misread pinned with one input on either side of the boundary
        b53 = row("X-WEB-010", "B53 data sheet", method="fetched", role="code", source="WEB", tag="B53 enamel")
        firm, floor = (2, ""), (None, "C states a minimum coat count for B53, not a fixed one; it does not settle this product's")
        loose = (None, "C does not tie a coat count to one product; which is this product's is not settled")
        cases = [
            # 1, 6: a small number before at/on/over/for/where/in/more is an added coat unless a label word or a tool size owns it
            ("B53, 1 coat; 2 in high-traffic areas", loose), ("B53, 1 coat; 2 in deep colors", loose),
            ("B53, 1 coat; apply two at patched areas", loose), ("B53, 1 coat; 2 at patched areas", loose),
            ("B53, 2 coats; use 2 in diameter brushes", firm), ("B53, 2 coats with 3 in rollers", firm),
            ("B53, 2 coats; use 2 in. rollers", firm), ("B53, 2 coats, see Part 3 for prep", firm),
            ("B53, 2 coats, see No. 2 at the end", firm), ("B53, 2 coats, color 2 at entries", firm),
            ("B53, 2 coats, Section 09 01 90 for prep", firm),
            # 2: "in" is an inch only before a tool word, so "more than two in deep colors" hedges the count
            ("B53, 2 coats. High-traffic areas may need more than two in deep colors.", floor),
            ("B53, 2 coats. Trim may need more than 2 in. wide", firm),
            # 3: an added coat in a later sentence floors the count whoever it names; another hedge there stays that product's
            ("B53, 2 coats. Accent color SW6258: additional coat.", floor),
            ("B53, 2 coats. Accent color SW6258 as required.", firm),
            # 4: "in." and a double quote are units, so the comparison or bound reads as a measurement
            ("B53, 2 coats, trim more than 4 in. wide", firm), ("B53, 2 coats on trim 6 in. or more", firm),
            ('B53, 2 coats on trim 6" or more', firm), ("B53, 2 coats, trim more than 4 in wide", floor),
            # 5: a code in parentheses before the hedge is that product's, as a bare code is
            ("B53, 2 coats; Example Primer (B66) as needed", firm), ("B53, 2 coats; Example Primer B66 as needed", firm),
            ("B53, 2 coats; Example Primer (B53) as needed", floor),
        ]
        for text, want in cases:
            with self.subTest(text=text):
                self.assertEqual(coats.coat_count(row("C", text), b53), want)

if __name__ == "__main__":
    unittest.main()
