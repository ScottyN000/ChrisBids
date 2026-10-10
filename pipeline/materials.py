"""Materials order rows: the quantity a takeoff figure and a data sheet rate give (architecture p.4, p.16).

The Materials agent's second job, after reading the data sheets (webread.py):
"product data sheet figures (spread rate, yield, pack size), order quantities"
(p.4). It reads ledger rows, never documents or pages. The model's one job is
to say, for each product the job's rows name, which rows the order rests on:
the quantity row it covers (a takeoff area or count, the allowance it goes into,
or the FIELD row that says it will be measured), the clause row that states the
coat count, the spec's own coverage row when there is one (the spec's stated
rate takes precedence over the data sheet, p.16) and the fetched data-sheet
row. Code checks that those rows fit together (the quantity's unit is the
rate's numerator; a count is a counted row in each; the coat row states one
count), works the quantity out, `{AREA} * coats / {RATE}` for a coating and
`{Q} / {RATE}` for a mortar, sealant or adhesive, at both ends of a rate stated
as a range, and writes it as a `fetched` material row citing the sheet, the
only method the access matrix lets Materials write (p.11). The row is named
after the sheet's page, never in the model's words. A product with no
data-sheet row in the ledger gets no row, and the run says so; a quantity that
waits on a FIELD row, a coat count no clause gives or a rate nobody stated is
written without a figure, flagged unverified, saying what is missing.

The model is called once per bid, as one stateless unit holding the rows, read
`repeats` times, on the client the Orchestrator gives it (the Sonnet client
Takeoff uses: the step reconciles rows from three agents, which the frugality
rule puts on Sonnet, p.13 placing Materials on Haiku for its page reads).
Items are compared across runs by the rows they rest on; an item that not
every valid run produced is written flagged unverified, never dropped or
chosen. Scaled and observed rows are never shown (p.6), and a rate, a yield, a
coat count, a waste factor or a spare count is never a number from the model:
it is a row or it is nothing.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from . import schema
from .broker import Broker
from .readers import validate
from .readers.clients import ModelClient, prompt, prompt_version
from .readers.rows import Unit
from .schema import CalcError, Claim, LedgerError
from .takeoff import _join, current

NAME = "materials"
PRINCIPAL = "materials"
# The quantity a product is ordered in, and the word a rate's unit ends with
# that names it ("sq ft/gal", "cu ft per bag", "LF/tube").
UNITS = {"gal": "gal", "bags": "bag", "tubes": "tube", "cartridges": "cartridge", "each": ""}
# The quantity units a rate's numerator may name, each under the one spelling
# code compares ("sq ft/gal" covers a row in SF; "LF/tube" a row in lin ft).
UNIT_WORDS = {
    "sq ft": ("sq ft", "sqft", "sq. ft", "sf", "square feet", "square foot", "ft2", "sq feet"),
    "lf": ("lf", "lin ft", "lin. ft", "linear feet", "linear foot", "ft", "feet", "foot"),
    "cu ft": ("cu ft", "cf", "cu. ft", "cubic feet", "cubic foot", "ft3"),
    "each": ("each", "ea", "pcs", "pieces", "count"),
}
# How a clause states a coat count: "2 coats", "two finish coats", "coats: 1" (not "coats: 4 hours",
# a recoat time, nor the first end of "coats: 2-3", a range).
# A count: a whole number of one or more ("2", "two", "two (2)"), not a decimal's tail ("1.5
# hours") nor a thickness ("a 15 mil coat": the word before "coat" may only say which coat it
# is). The "coats:" form counts only when "coats" is the label itself: the label starts a
# stretch (the start of the text, after punctuation, or after a cut word, see STRETCH)
# and may be led by "number of" or one role word ("finish coats: 2"); a label that only ends
# in "coat" ("mils per coat: 4", "WFT/coat: 4", "between coats: 24", "between finish coats:
# 24") states no count. After the colon the number must end the clause or be followed by
# punctuation, "and" or "coat(s)", so "finish coat: 400 sq ft/gal" is no count either. A "/"
# is a range mark ("2/3 coats"), never part of a count.
N = r"(?<![.\d/])([1-9]\d*|one|two|three|four|five|six)"
N2 = r"([1-9]\d*|one|two|three|four|five|six)"   # a range's far end, which its mark precedes
ROLE_WORDS = r"finish|final|top|prime|primer|base|first|second|third|full|intermediate|stripe"
ROLE = rf"(?:(?:{ROLE_WORDS})[\s-]+)?"
LABEL = (rf"(?:^|(?<=[;:,.(\n])\s*|(?<=\band )|(?<=\bor )|(?<=\bthen )|(?<=\bover )|(?<=\bfollowed by )|(?<=\bafter )|(?<=\bbefore )|(?<=\bprior to ))"
         rf"(?:number\s+of\s+|(?:{ROLE_WORDS})\s+)?")
TO = r"(?:-|/|\u2013|to\b|or\b|and\b)"
COATS = re.compile(rf"\b{N}(?:\s*\(([1-9]\d*)\))?[\s-]+{ROLE}coats?\b|{LABEL}coats?\s*[:=]\s*([1-9]\d*)(?!\d|[.,]\d)(?=\s*(?:$|[;,.)]|and\b|coats?\b))", re.I)
# A range or a choice of counts ("1-2 coats", "one or two coats", "between two and three coats",
# "coats: 2 - 3"): two readings, neither picked. "Coats: 2 and back-roll" is no range: a second
# number must follow.
COAT_RANGE = re.compile(rf"\b{N}\s*{TO}\s*{N2}[\s-]+{ROLE}coats?\b|{LABEL}coats?\s*[:=]\s*\d+(?!\d)\s*{TO}\s*{N2}\b", re.I)
COAT_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6}
# How a clause leaves the count open: "(coats not stated)", "coats are not stated", "the A89
# coat count is not stated", "number of coats on repaint not specified". "sheen not stated" is
# not about coats.
NOT_STATED = re.compile(r"\bcoats?\s+(?:is\s+|are\s+|were\s+)?not stated\b|\bnot stated\s+coats?\b"
                        r"|\b(?:coat count|number of coats)\b(?:\s+\w+){0,3}\s+not (?:stated|specified)\b", re.I)
# A product code as a spec or a data sheet writes it: letters then digits ("A89", "X100", "B-66"),
# never part of a row ID ("X-SP-011") or a unit ("ft2").
PRODUCT_CODE = re.compile(r"(?<![A-Za-z0-9-])[A-Z]{1,4}-?\d{2,5}[A-Z]?(?![A-Za-z0-9-])")
# Where one product's wording ends and the next begins in a clause that names several
# ("A89 (coats not stated), or K62, 1 coat"; "Primer B66 as needed, then 1 coat B53";
# "B53 over one coat of primer"; "Primer as needed. Finish: two coats"): a semicolon, a
# sentence end (a period before a blank, never a decimal point), "or", "then", "and",
# "over", "followed by", "after", "before" or "prior to" outside parentheses, but not the
# "or" of "or approved equal". Commas and
# parentheses stay inside their stretch, so "Finish coat (1 coat): Enamel, B53 series" is
# one stretch. A cut can lose a count, or tie one to the code of its own narrower
# stretch ("B53, 2 coats after B66" is B53's two coats); it never invents one.
STRETCH = re.compile(r";|\.(?=\s|$)|\bor\b(?!\s+(?:an\s+)?(?:approved\s+)?(?:equal|equivalent)\b|\s+more\b|\s+as\s+(?:required|needed|necessary)\b)"
                     r"|\b(?:then|and|over|followed\s+by|after|before|prior\s+to)\b", re.I)
# Words that name a product without a code, by its place in the system. A stretch that speaks
# of coats and names both a primer and a finish ("B53 finish with primer, 2 coats", "B53 with
# one coat of primer", "two coats (primer and finish)") holds two products' wording, so its
# count is tied to nothing; the whole stretch is judged, in a clause naming codes as in one
# naming none. A stretch naming neither and no code ("Walls", "back-roll the first") is
# about no product.
# "primed" describes the substrate, not a primer in the system ("previously primed surfaces: B53, 2
# coats" is the finish's count), and "priming" names one as a label or a coat ("Priming: one coat",
# "Priming (one coat)", "the first a priming coat"), not as a prior step ("after priming, B53, 2 coats").
PRIME_WORD = re.compile(r"\b(?:prim(?:e|ers?)|priming(?=\s*(?::|\(|coats?\b))|sealers?|conditioners?|base coats?|undercoats?|undercoaters?|block fillers?|fillers?|surfacers?)\b", re.I)
FINISH_WORD = re.compile(r"\b(?:finish(?:es)?|final|top|topcoats?|intermediate|stripe|enamels?|satin|semi-gloss|gloss|eggshell|flat|paints?|coatings?)\b", re.I)
# A second count said without the word "coat", or an added coat ("one coat; two at patched
# areas", "1 coat; 2 at patched areas", "a second coat at repairs", "plus 1 coat at repairs",
# "double coat at repairs", "recoat patched areas"): a second reading, so the clause settles
# nothing. A number counts only before at/on/over/for/where/in/more, so "1 coat (10 year)"
# stays one coat.
MORE = re.compile(rf"\b(?:second|third|fourth|additional|extra|another|plus|further|double|(?<!\bor )(?<!\band )more)[\s-]+(?:(?:[1-9]\d*|one|two|three|four|five|six)\s+)?{ROLE}coats?\b"
                  rf"|\b(?:more|additional|extra|further)\s+(?:paint|material|product|coating)s?\b(?!\s*:)"   # not an "Extra materials:" article
                  rf"|\bre-?coat\b(?:\s+[A-Za-z]+){{0,3}}\s+(?:at|where|areas|repairs)\b"   # not "recoat after 4 hours at 77F", a time
                  rf"|\bre-?coat\s+(?:as|if|where|when)\s+(?:required|needed|necessary)\b"
                  rf"|(?<![\w./-])(?:^|(?<=[;,(:] )|(?<=[;,(:])|(?<=\. )|(?<=\band )|(?<=\bplus )|(?<=\bthen )|(?<=\bor )|(?<=\bover )|(?<=\bafter )|(?<=\bbefore )|(?<=\bby )|(?<=\bto )|(?<=\bwith )|(?<=\bbut ))"
                  rf"([1-9]\d*|one|two|three|four|five|six)\b(?![\s-]*(?:\(\d+\)\s*)?{ROLE}coats?\b)(?=\s+(?:at|on|over|for|where|in|more)\b)", re.I)   # only where a second count can start (after punctuation or a cut word), so not "Part 3 for" or "a 9 in roller"
# A count split across products, or a system's count ("2-coat system including primer", "two
# coats (one primer, one finish)", "2-coat system"): whose coats they are is not settled.
SPLIT = re.compile(rf"\b(?:including|incl\.?|of which)(?:\s+\w+){{0,3}}?\s+(?:prime|primer|priming|finish|topcoat|coats?)\b"
                   rf"|\bcoats?[\s-]+system\b"
                   rf"|\b(?:[1-9]\d*|one|two|three|four|five|six)\s+(?:{ROLE_WORDS})\b(?![\s-]*coats?\b)", re.I)
# A floor, not a count ("two coats, or more as required for full hide", "at least two (2) coats",
# "minimum of 2 coats", "no less than two coats", "2 coats minimum", "two coats (minimum)", "2 coats
# min.", "two coats or as required to achieve full hide"): the count is not fixed, so no figure is written.
# A trailing floor word followed by a figure ("2 coats, minimum of 3 mils") is a film thickness, not
# a floor on the count, and so is one set off by a comma or a parenthesis and followed by a thickness
# word ("2 coats (min. 2.0 mils DFT per coat)", "2 coats, minimum DFT 2.0 mils"); run together, "2
# coats minimum WFT 6 mils" may be read either way, so it stays a floor and no figure is written.
FIGURE = r"\d|one\b|two\b|three\b|four\b|five\b|six\b|of\b|an?\b"
THICKNESS = r"DFT\b|WFT\b|dry\b|wet\b|film\b|mils?\b|thickness\b"
FLOOR = re.compile(rf"\b{N2}\s+or\s+more[\s-]+{ROLE}coats?\b|\b(?:more than|in excess of)\s+{N2}(?:\s*\(\d+\))?[\s-]+{ROLE}coats?\b"
                   rf"|\bcoats?[\s,]*as (?:required|needed|necessary) (?:for|to achieve|to obtain|to get) (?:a )?(?:full |complete |uniform )?(?:hide|coverage|hiding)\b"
                   rf"|\b(?:at least|a minimum of|minimum(?: of)?|not less than|no less than|no fewer than)\s+{N2}(?:\s*\(\d+\))?[\s-]+{ROLE}coats?\b"
                   rf"|\bcoats?\s*[,(]\s*(?:minimum|min|at least)\b(?!\.?\s*(?:{FIGURE}|{THICKNESS}))"
                   rf"|\bcoats?\s+(?:minimum|min|at least)\b(?!\.?\s*(?:{FIGURE}))", re.I)
# A hedge after a count in the same sentence ("or more", "and more", "additional", "extra", "further",
# "as/if/where required/needed/necessary", whatever follows and whatever the hedge governs) makes the
# count a floor, since code cannot tell the count's work from another's within one sentence ("two
# coats of B53, or more as required for full hide", "2 coats over the prepared surface, or more",
# "2 coats, caulk joints as required"); a hedge in a later sentence of the same part makes every
# count before it in the part a floor unless that sentence names another product, a code the count's
# stretch does not carry or the other role (see other_product; the count's own code or role, "deep
# colors of B53 may require more", "apply more finish as required", names no other product) ("2
# coats; apply more as required", "2 coats; deep colors may require more", and, since code
# cannot tell whose work the hedge is, "2 coats; remove loose plaster, or as required by the
# Architect" too: the safe direction, a flagged order with no figure that the estimator settles,
# never wrong gallons; "2 coats. Where required, back-prime trim" keeps the count); a hedge before
# any count ("B66 as needed, then 1 coat B53", "10 ft or more above grade: B53, 2 coats") says
# nothing about coats. A bare "or more" that follows another figure directly bounds that figure ("2
# coats on surfaces 10 ft or more above grade"), and "more than" is a comparison ("more than 10 ft
# above grade") unless it bounds coats ("may require more than two coats", a floor, see FLOOR). A
# ceiling ("up to two coats", "no more than two coats", "two coats maximum") is a range, as "1-2
# coats" is, with the film-thickness exception the floor words have. coat_mentions settles which;
# sentences end at ";" or at a full stop (a period before a capital or the end) outside parentheses,
# so "min." and "approx." end none.
MEASURE_UNITS = r"%|ft|feet|in|inches|mils?|DFT|WFT|sq\s*ft(?:/gal)?|gal|gallons?|mm|cm|m|hours?|hrs?|days?|years?|percent"
# "more than" followed directly by a figure and a unit is a comparison ("more than 10 ft above grade"); "more than two.",
# "more than that" and "more than two coats" hedge the count
COMPARISON = rf"(?!\s+than\s+(?:\d+(?:[.,]\d+)*%?|one|two|three|four|five|six)\s*(?:{MEASURE_UNITS})\b)"
HEDGE = re.compile(rf"\b(?:or|and)\s+more\b{COMPARISON}|\b(?:more{COMPARISON}|additional|extra|further)\b(?!\s+materials?\s*:)|\b(?:as|if|where|when)\s+(?:required|needed|necessary)\b", re.I)
CEILING = re.compile(rf"\b(?:up to|no more than|not more than|not to exceed|a maximum of|maximum(?: of)?|max\.?)\s+{N2}(?:\s*\(\d+\))?[\s-]+{ROLE}coats?\b"
                     rf"|\bcoats?\s*[,(]\s*(?:maximum|max)\b(?!\.?\s*(?:{FIGURE}|{THICKNESS}))"
                     rf"|\bcoats?\s+(?:maximum|max)\b(?!\.?\s*(?:{FIGURE}))", re.I)
BARE_MORE = re.compile(r"(?:or|and)\s+more", re.I)
SENTENCE_MARK = re.compile(r"[();.]")
FULL_STOP = re.compile(r"\.(?:\s+[A-Z(\"']|\s*$)")
# text ending in a figure and at most its unit words, which a bare "or more" then follows directly
BOUNDED = re.compile(rf"(?:\d+(?:[.,]\d+)*%?|\b(?:one|two|three|four|five|six)\b)(?:\s*(?:{MEASURE_UNITS})\b)*\s*$", re.I)
# A stretch opened by a sequence word continues whatever came before it in the same part
# (a product, "Base coat as needed", or a step, "Scrape"), and one opened by "and" continues
# a product named before it; so a count written after its code there ("B53 over B66 primer,
# 2 coats"; "B66 as needed, then B53, 2 coats"; "B66 as needed. Then B53, 2 coats"; "X100 and
# X200, 2 coats each"; "B66, followed by B53, 2 coats") may be the system's as well as that
# code's: two readings, neither picked. A count before the code ("then 1 coat B53", "over one
# coat of B66") is that code's alone, after "or" the count is the named alternative's on
# either reading, and "Walls and ceilings: B53, 2 coats" continues no product.
SEQUENCE = ("then", "over", "followed by", "after", "before", "prior to")
# One spare run, made only when a run was discarded or the runs name different
# orders (the page reader does the same): an order one run saw is written flagged,
# and the spare says whether a second run sees it too.
SPARES = 1
# Rows a product's quantity may rest on: a takeoff figure, the allowance the
# product covers, or the FIELD row for either (the live run of 2026-10-09 named
# the repaint's FIELD allowance rows, "exterior wall surfaces", and was right to).
QUANTITY_METHODS = ("dimensioned", "counted", "FIELD")
QUANTITY_ROLES = ("quantity", "allowance")
# Rows the model is shown: what the sources say, the figures and the data sheets.
SHOWN_METHODS = ("clause", "customer", "fetched", "dimensioned", "counted", "FIELD")
ROW_ID = r"^[A-Z0-9-]{1,40}$"

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["items"],
    "properties": {"items": {"type": "array", "maxItems": 40, "items": {
        "type": "object",
        "additionalProperties": False,
        "required": ["product", "unit", "quantity", "coats", "spec_rate", "sheet", "basis"],
        "properties": {
            "product": {"type": "string", "maxLength": 150},
            "unit": {"enum": list(UNITS)},
            # The row the quantity covers: a takeoff figure, an allowance or the FIELD row for one; "" for none.
            "quantity": {"type": "string", "maxLength": 40},
            # The clause row that states the coat count (a coating only); "" when none does.
            "coats": {"type": "string", "maxLength": 40},
            # The spec's own coverage row for the product, "" when the spec gives none.
            "spec_rate": {"type": "string", "maxLength": 40},
            # The fetched data-sheet row for the product.
            "sheet": {"type": "string", "maxLength": 40, "pattern": ROW_ID},
            # The rows that name the product and its system, each once.
            "basis": {"type": "array", "maxItems": 8, "items": {"type": "string", "maxLength": 40}},
        },
    }}},
}


@dataclass
class OrderResult:
    reader: str = NAME
    units: int = 0
    calls: int = 0
    discarded: list[str] = field(default_factory=list)
    unread: list[str] = field(default_factory=list)
    rows: list[Claim] = field(default_factory=list)
    refused: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def text(self) -> str:
        figured = sum(1 for r in self.rows if r.calc)
        lines = [f"{self.reader}: {self.units} units, {self.calls} calls, {len(self.rows)} order rows "
                 f"({figured} with a figure), {len(self.discarded)} items discarded, {len(self.unread)} units unread"]
        lines += [f"  note {n}" for n in self.notes]
        # every row written, so a run log alone shows what was ordered from which page
        lines += [f"  row {c.claim_id} | {c.value} {c.unit} | {c.statement[:140]} | {c.url}" + (f" | {c.flag}" if c.flag else "")
                  for c in self.rows]
        lines += [f"  discarded {d}" for d in self.discarded]
        lines += [f"  unread {u}" for u in self.unread]
        lines += [f"  refused {r}" for r in self.refused]
        return "\n".join(lines)


def inputs(claims: list[Claim]) -> list[Claim]:
    """The rows the model is shown: what the documents say (clause, customer),
    the data sheets and code pages (fetched), the figures (dimensioned, counted)
    and the FIELD rows for what will be measured. Never a scaled or observed row
    (p.6), never a material row (ours)."""
    return [c for c in claims if c.method in SHOWN_METHODS and c.role != "material"]


def sheets(rows: list[Claim]) -> list[Claim]:
    """The fetched rows an order may cite: a data sheet or product page."""
    return [c for c in rows if c.method == "fetched" and c.quote]


def unit_for(job: str, rows: list[Claim]) -> Unit:
    text = "\n".join(
        f"{c.claim_id} | {c.method} | {c.value} {c.unit} | {c.statement} | {c.tag or c.locator}"
        + (f" | {c.flag}" if c.flag else "")
        for c in rows
    )
    return Unit(unit_id=f"{job}#materials", source_id="", locator="ledger rows", tag="", text=text)


def figure(c: Claim | None) -> tuple[float, ...] | None:
    """The figure a row gives: one end, or a range's two ends; None when it has none."""
    if c is None:
        return None
    if c.value_num is not None:
        return (c.value_num,)
    return schema.value_range(c.value)


def norm_unit(unit: str) -> str:
    """A quantity unit under the one spelling code compares ("SF" and "sq. ft." are "sq ft")."""
    t = re.sub(r"\s+", " ", unit.strip().lower().replace(".", ""))
    for one, words in UNIT_WORDS.items():
        if t == one or t in words:
            return one
    return t


def rate_parts(c: Claim | None) -> tuple[str, str]:
    """What a rate's unit says: (the quantity unit it covers, the order unit it is
    per): "sq ft/gal" is ("sq ft", "gal"), "cu ft per bag" is ("cu ft", "bags").
    A row with no figure, or a unit that names no order unit, gives ("", "")."""
    if c is None or figure(c) is None or not c.unit:
        return "", ""
    parts = re.split(r"\s*(?:/|\bper\b)\s*", c.unit.strip())
    if len(parts) < 2:
        return "", ""
    tail = parts[-1].strip().lower()
    for unit, word in UNITS.items():
        if word and tail.startswith(word):
            return norm_unit(parts[0]), unit
    return "", ""


def rate_unit(c: Claim | None) -> str:
    """The order unit a rate's unit names: `sq ft/gal` orders gallons, `cu ft per bag` bags."""
    return rate_parts(c)[1]


def rate_of(spec: Claim | None, sheet: Claim) -> Claim | None:
    """The one rate an order rests on: the spec's own when it states one (p.16), else the sheet's."""
    if spec is not None:
        return spec
    return sheet if rate_unit(sheet) else None


def product_codes(*texts: str) -> list[str]:
    """The product codes the texts name, each once, in order of appearance."""
    return list(dict.fromkeys(k for t in texts for k in PRODUCT_CODE.findall(t)))


def coat_mentions(text: str, spans: list[tuple[int, int, str, int]] | None = None) -> list[tuple[int, int | str | None]]:
    """Where the text speaks of coats: (position, count) for each count it states,
    (position, None) where it says the count is not stated, (position, "range")
    where it states a range or a choice of counts, and "more", "split" and "floor"
    where it adds coats, splits a count or sets a minimum (see MORE, SPLIT, FLOOR
    HEDGE and CEILING). `spans` are the text's stretches, from `stretches`; without
    them the whole text is one."""
    # a mention's position is its first word's, never the blank a label may begin with,
    # so it falls inside the stretch that holds it
    ranges = [(m.end() - len(m.group().lstrip()), m.end()) for m in COAT_RANGE.finditer(text)]
    found: list[tuple[int, int | str | None]] = [(a, "range") for a, _ in ranges]
    found += [(m.start(), None) for m in NOT_STATED.finditer(text)]
    found += [(m.start(), "more") for m in MORE.finditer(text)]
    found += [(m.start(), "split") for m in SPLIT.finditer(text)]
    found += [(m.start(), "floor") for m in FLOOR.finditer(text)]
    counted: list[tuple[int, int]] = []   # each count's number position and where its wording ends
    for m in COATS.finditer(text):
        pos = m.start(1) if m.group(1) else m.start(3)   # the number's position
        if any(a <= pos < b for a, b in ranges):
            continue
        counted.append((pos, m.end()))
        for word in (w for w in (m.group(1), m.group(2), m.group(3)) if w):
            found.append((pos, COAT_WORDS.get(word.lower()) or int(word)))   # "two (2)" twice, the same count
    spans = spans or [(0, len(text), "", 0)]
    found += [(m.start(), "range") for m in CEILING.finditer(text)]
    for m in HEDGE.finditer(text):
        i = next((i for i, (a, b, *_) in enumerate(spans) if a <= m.start() < b), None)
        if i is None:
            continue
        part_start = max(s[0] for s in spans[:i + 1] if not s[2])   # the part this hedge is in
        part_end = next((s[0] for s in spans[i + 1:] if not s[2]), len(text))
        starts = sentences(text, part_start, part_end)
        sentence = max(st for st in starts if st <= m.start())
        sentence_end = min([st for st in starts if st > m.start()] + [part_end])
        before = [(p, e) for p, e in counted if sentence <= p < m.start()]
        if before:
            if BARE_MORE.fullmatch(m.group()) and BOUNDED.search(PRODUCT_CODE.sub(" ", text[before[-1][1]:m.start()])):
                continue   # "10 ft or more above grade" bounds the height
            found.append((before[-1][0], "floor"))   # placed with the count it bounds
        elif sentence > part_start:
            said = text[sentence:sentence_end]
            for p, _ in counted:
                if part_start <= p < sentence and not other_product(text[sentence_at(spans, p)], said):
                    found.append((p, "floor"))   # every count before, in the part, that the sentence does not hand to another product
    return sorted(found, key=lambda f: f[0])


def sentences(text: str, start: int, end: int) -> list[int]:
    """Where the sentences of text[start:end] begin: at `start`, and after each ";" or
    full stop (a period before a capital letter or the end) outside parentheses, so an
    abbreviation ("min.", "approx.", "No.") ends none."""
    starts, depth = [start], 0
    for m in SENTENCE_MARK.finditer(text, start, end):
        if m.group() == "(":
            depth += 1
        elif m.group() == ")":
            depth = max(0, depth - 1)
        elif depth == 0 and (m.group() == ";" or FULL_STOP.match(text, m.start(), end)):
            starts.append(m.end())
    return starts


def stretches(*parts: str) -> list[tuple[int, int, str, int]]:
    """The spans of a clause's stretches in the parts joined by SEP, each as (start,
    end, the mark that opened it, "" at a part's start, and where that mark begins);
    each part is cut at STRETCH
    marks outside its own parentheses (an unclosed one in the statement does not
    swallow the quote), and a part is never joined to the next."""
    spans, at = [], 0
    for part in parts:
        cuts, depth = [(0, "", 0)], 0
        for m in re.finditer(r"[()]|" + STRETCH.pattern, part, re.I):
            if m.group() == "(":
                depth += 1
            elif m.group() == ")":
                depth = max(0, depth - 1)
            elif depth == 0:
                cuts.append((m.end(), " ".join(m.group().lower().split()), m.start()))
        cuts.append((len(part), "", len(part)))
        spans += [(at + a, at + b, mark, at + cut) for (a, mark, cut), (b, _, _) in zip(cuts, cuts[1:]) if part[a:b].strip()]
        at += len(part) + len(SEP)
    return spans


SEP = "; "


def sentence_at(spans: list[tuple[int, int, str, int]], pos: int) -> slice:
    """The stretch that holds `pos`, as a slice of the text."""
    a, b, *_ = next((s for s in spans if s[0] <= pos < s[1]), (0, 0))
    return slice(a, b)


def other_product(own: str, said: str) -> bool:
    """Whether `said` names a product other than the one `own` (a count's stretch) speaks
    of: a code `own` does not carry, or, with no code, the other role (a primer where
    `own` speaks of a finish, or the reverse). The count's own code, its own role or a
    generic word names no other product."""
    codes = product_codes(said)
    if codes:
        return not set(codes) & set(product_codes(own))
    role = (bool(PRIME_WORD.search(said)), bool(FINISH_WORD.search(said)))
    mine = (bool(PRIME_WORD.search(own)), bool(FINISH_WORD.search(own)))
    return (role == (True, False) and mine == (False, True)) or (role == (False, True) and mine == (True, False))


def names_product(stretch: str) -> bool:
    """Whether a stretch names a product: by code, or by its place in the system."""
    return bool(PRODUCT_CODE.search(stretch) or PRIME_WORD.search(stretch) or FINISH_WORD.search(stretch))


def two_roles(stretch: str) -> bool:
    """Whether a stretch names both a primer and a finish, so holds two products' wording."""
    return bool(PRIME_WORD.search(stretch) and FINISH_WORD.search(stretch))


def _settle(c: Claim, mentions: list[tuple[int, int | str | None]], whom: str = "") -> tuple[int | None, str]:
    counts = sorted({n for _, n in mentions if isinstance(n, int)})
    if any(n == "range" for _, n in mentions):
        return None, f"{c.claim_id} states a range of coats{whom}; it does not settle this product's"
    if any(n == "more" for _, n in mentions):
        return None, f"{c.claim_id} states a coat count{whom} and more coats in places; it does not settle this product's"
    if any(n == "split" for _, n in mentions):
        return None, f"{c.claim_id} states a coat count{whom} for a system or split across products; whose coats they are is not settled"
    if any(n == "floor" for _, n in mentions):
        return None, f"{c.claim_id} states a minimum coat count{whom}, not a fixed one; it does not settle this product's"
    left_open = any(n is None for _, n in mentions)
    if not mentions:
        return None, f"{c.claim_id} states no coat count{whom}"
    if left_open and not counts:
        return None, f"{c.claim_id} says the coat count{whom} is not stated"
    if left_open:
        return None, f"{c.claim_id} states a coat count{whom} and says one is not stated; it does not settle this product's"
    if len(counts) > 1:
        return None, f"{c.claim_id} states {len(counts)} coat counts{whom}; it does not settle this product's"
    return counts[0], ""


def coat_count(c: Claim, product: Claim | None = None) -> tuple[int | None, str]:
    """The coat count a clause row states for a product, read by code: (count, "")
    when it states exactly one; (None, why) when it states none, several, a range,
    says the count is not stated, or speaks of other products. A clause naming a
    product code is read by the code the product's own row carries: a count
    belongs to the one code its own stretch of the clause names ("A89 (coats not
    stated), or K62, 1 coat" gives K62 one coat and leaves A89 open; "B53 over one
    coat of primer" gives B53 nothing), and a count whose stretch names no code, or
    two, is tied to nothing: the clause then settles no product's count; so is a
    count after its code in a stretch opened by a sequence word ("then", "over",
    "followed by", "after", "before", "prior to") that continues anything, or by
    "and" after a product ("B53 over B66 primer, 2 coats" may give the system two
    coats), see SEQUENCE. A clause
    naming no code settles a count only when every stretch of it states the same
    one, since a stretch with none may be another product's ("primer; finish
    coats: two coats" gives the primer nothing). A sheet row naming no code is
    read as the clause's one code when every stretch names it or names no
    product; when a stretch names a product without a code, or the clause names
    several, it settles nothing for that sheet. A count whose stretch names a
    primer word is read only for a sheet that is a primer's alone (a primer word
    and no finish word), and one whose stretch names a finish word is no such
    sheet's ("B53 with primer, 2 coats" and "B53 with one coat of primer" may be
    the primer's coats), and a count whose stretch names no role is no primer-only
    sheet's when the sheet names no code and takes the clause's one code on trust
    ("B53, 2 coats" read for "the primer data sheet"; a bare count is usually the
    finish's or the system's); a count for a system or split across products
    ("2-coat system including primer") settles nothing, see SPLIT; a floor ("at
    least two coats", "two coats or as required") is not a fixed count, see FLOOR."""
    parts = [c.statement, c.quote] if c.quote else [c.statement]
    text = SEP.join(parts)
    codes = product_codes(text)
    spans = stretches(*parts)
    mentions = coat_mentions(text, spans)
    if not codes:
        if len(spans) > 1 and not any(n == "range" for _, n in mentions):   # a range ("one or two coats") is read whole
            each = [[n for pos, n in mentions if a <= pos < b] for a, b, *_ in spans]
            if any(not ns and names_product(text[a:b]) for ns, (a, b, *_) in zip(each, spans)):
                return None, f"{c.claim_id} names no product code, and names a product in one stretch and speaks of coats in another; which product the count is for is not settled"
            if len({n for ns in each for n in ns if isinstance(n, int)}) > 1:
                return None, f"{c.claim_id} names no product code and states different coat counts in its stretches; which is this product's is not settled"
        counting = [text[a:b] for a, b, *_ in spans if any(a <= pos < b for pos, _ in mentions)]
        if any(two_roles(here) for here in counting):
            return None, f"{c.claim_id} names no product code and speaks of a primer and a finish where it states coats; which the count is for is not settled"
        # a clause about a primer only is read for a sheet that is a primer's alone; one about a finish only is no such sheet's
        said, sheet = " ".join(counting), (f"{product.statement} {product.quote} {product.tag}" if product else "")
        roles = (bool(PRIME_WORD.search(said)), bool(FINISH_WORD.search(said)))
        sheet_roles = (bool(PRIME_WORD.search(sheet)), bool(FINISH_WORD.search(sheet)))
        if roles == (True, False) and product is not None and sheet_roles != (True, False):
            return None, f"{c.claim_id} speaks of a primer where it states coats, and this product's row is not a primer's alone"
        if roles == (False, True) and sheet_roles == (True, False):
            return None, f"{c.claim_id} speaks of a finish where it states coats, and this product's row names a primer"
        if roles == (False, False) and sheet_roles == (True, False):   # a bare count is usually the finish's or the system's
            return None, f"{c.claim_id} names no primer where it states coats, and this product's row names a primer"
        return _settle(c, mentions)
    own = product_codes(f"{product.statement} {product.quote} {product.tag}") if product else []
    if own:
        mine = [k for k in own if k in codes]
    elif len(codes) == 1 and all(codes[0] in product_codes(text[a:b]) or not names_product(text[a:b]) for a, b, *_ in spans):
        mine = codes
    elif len(codes) == 1:
        return None, f"{c.claim_id} names {codes[0]} in one stretch and a product without a code in another; whether the other is this product's is not settled"
    else:
        mine = []
    if len(mine) > 1:
        return None, f"{c.claim_id} names {', '.join(mine)}, which this product's row both carries; which count is its is not settled"
    if mine and len(own) > 1:   # the sheet names a system or a recommended primer too: which code is this product is not settled
        return None, f"{c.claim_id} names {mine[0]}, and this product's row names {', '.join(own)}; which of them is this product is not settled"
    if not mine:
        if len(codes) == 1:
            return None, f"{c.claim_id} names {codes[0]}, not this product"
        return None, f"{c.claim_id} names {', '.join(codes)}; which is this product's is not settled"
    whom = f" for {mine[0]}"
    sheet = f"{product.statement} {product.quote} {product.tag}" if product else ""
    sheet_roles = (bool(PRIME_WORD.search(sheet)), bool(FINISH_WORD.search(sheet)))
    owned = []
    for pos, n in mentions:
        i = next((i for i, (a, b, *_) in enumerate(spans) if a <= pos < b), None)
        a, b, opener, cut = spans[i] if i is not None else (0, 0, "", 0)
        here = text[a:b]
        named = product_codes(here)
        code_at = next((a + m.start() for m in PRODUCT_CODE.finditer(here) if m.group() == named[0]), pos) if named else pos
        part_start = max(st for st in (0, len(parts[0]) + len(SEP)) if st <= a)   # the text before the cut, in the same part
        before = text[part_start:cut] if opener else ""
        continued = (opener in SEQUENCE and before.strip(" ,;:.")) or (opener == "and" and names_product(before))
        # the whole stretch is judged: a primer or finish word anywhere in it bears on whose count it is
        counted = isinstance(n, int) or n is None   # a count or an open count; a range, an added coat, a split or a floor refuses on its own
        if len(named) != 1 or (counted and two_roles(here)) or (continued and code_at < pos):
            return None, f"{c.claim_id} does not tie a coat count to one product; which is this product's is not settled"
        # a primer named in the count's stretch ("B53 with primer, 2 coats", "B53 with one coat of primer") may own the
        # count, unless this sheet is a primer's alone; a finish named there is not a primer's count
        if counted and named[0] == mine[0] and ((PRIME_WORD.search(here) and sheet_roles != (True, False)) or (FINISH_WORD.search(here) and sheet_roles == (True, False))):
            return None, f"{c.claim_id} names a {'primer' if PRIME_WORD.search(here) else 'finish'} with the coat count{whom}; whether the count is {mine[0]}'s is not settled"
        # a sheet that is a primer's alone and names no code takes the clause's code on trust; a bare count there is
        # usually the finish's or the system's, so it is read only where the stretch names a primer
        if counted and named[0] == mine[0] and not own and sheet_roles == (True, False) and not PRIME_WORD.search(here):
            return None, f"{c.claim_id} names no primer with the coat count{whom}, and this product's row names a primer; whether the count is {mine[0]}'s is not settled"
        if named[0] == mine[0]:
            owned.append((pos, n))
    return _settle(c, owned, whom)


def cited_text(item: dict, by_id: dict[str, Claim]) -> str:
    rows = [by_id[r] for r in (item["sheet"], item["quantity"], item["spec_rate"], item["coats"], *item["basis"])
            if r and r in by_id]
    return " ".join(f"{c.statement} {c.quote} {c.tag}" for c in rows)


def item_errors(item: dict, by_id: dict[str, Claim]) -> list[str]:
    """Why code will not write this item. Empty means the rows it names bear it out."""
    if not item["sheet"]:
        return ["names no data-sheet row; a product with none gets no order row"]
    named = [item["sheet"], item["quantity"], item["spec_rate"], item["coats"], *item["basis"]]
    unknown = [r for r in named if r and r not in by_id]
    if unknown:
        return [f"names {', '.join(unknown)}, which is not a row it was shown"]
    # a product code the model wrote must be in the rows the item cites, not just somewhere in the ledger
    stray = [n for n in re.findall(r"\d+", item["product"]) if n not in re.findall(r"\d+", cited_text(item, by_id))]
    if stray:
        return [f"product {item['product']!r} carries {', '.join(stray)}, which is in none of the rows it cites"]
    sheet = by_id[item["sheet"]]
    if sheet.method != "fetched" or not sheet.url:
        return [f"sheet {sheet.claim_id} is not a fetched row with a URL"]
    q = by_id.get(item["quantity"]) if item["quantity"] else None
    if q is not None:
        if q.method not in QUANTITY_METHODS or q.role not in QUANTITY_ROLES:
            return [f"quantity {q.claim_id} is a {q.method} {q.role} row, not a takeoff figure, an allowance or a FIELD row"]
        if q.method != "FIELD" and figure(q) is None:
            return [f"quantity {q.claim_id} carries no figure"]
    spec = by_id.get(item["spec_rate"]) if item["spec_rate"] else None
    if spec is not None and (spec.method != "clause" or not rate_unit(spec)):
        return [f"spec_rate {spec.claim_id} is not a clause row stating a rate per gallon, bag, tube or cartridge"]
    coats = by_id.get(item["coats"]) if item["coats"] else None
    if coats is not None:
        if item["unit"] != "gal":
            return [f"coats apply to a coating ordered by the gallon, not to {item['unit']}"]
        if coats.method != "clause":
            # a customer row never overrides a spec clause, so it does not set a count either
            return [f"coats {coats.claim_id} is a {coats.method} row, not a clause"]
        # what the clause states (one count, none, several, or that it is not stated) is read in
        # order_claim: a clause that does not settle the count leaves the order waiting on it, flagged
    if item["unit"] == "each":
        if spec is not None:
            return ["orders each, so no rate applies"]
        # a count is a counted row in each, or the FIELD row that will count it; an area or a length is never a count
        if q is not None and q.method != "FIELD" and (q.method != "counted" or norm_unit(q.unit) != "each"):
            return [f"orders each, but {q.claim_id} is a {q.method} row in {q.unit or 'no unit'}, not a count"]
        return []
    rate = rate_of(spec, sheet)
    if rate is not None:
        # no rate at all is not refused: the row is written with no figure, flagged, saying so (order_claim)
        covers, per = rate_parts(rate)
        if per != item["unit"]:
            return [f"orders {item['unit']} but the rate {rate.claim_id} is per {rate.unit!r}"]
        if q is not None and q.method != "FIELD" and norm_unit(q.unit) != covers:
            # only a FIELD row may lack a unit: a figure with none cannot be checked against the rate
            return [f"quantity {q.claim_id} is in {q.unit or 'no unit'}, but the rate {rate.claim_id} covers {covers}"]
    return []


def item_key(item: dict) -> tuple:
    """What makes two items one order: the rows it rests on and its unit, never
    the product's wording (the plank job's runs named one sleeve two ways)."""
    return (item["unit"], item["quantity"], item["coats"], item["spec_rate"], item["sheet"])


def vote(runs: list[list[dict]]) -> tuple[list[tuple[dict, int]], list[str]]:
    """(first item, runs that produced it) per distinct order, in first-seen order,
    and a note for each later item in a run that named an order already named
    in that run (two wordings of one order give one row)."""
    order, seen, dupes = [], {}, []
    for n, items in enumerate(runs, 1):
        this_run = {}
        for it in items:
            k = item_key(it)
            if k in this_run:
                dupes.append(f"run {n}: {it['product']!r} names the same order as {this_run[k]!r}; one row")
                continue
            this_run[k] = it["product"]
            if k not in seen:
                seen[k] = [it, 0]
                order.append(k)
            seen[k][1] += 1
    return [(seen[k][0], seen[k][1]) for k in order], dupes


def _fig(c: Claim) -> str:
    return f"{c.value} {c.unit}".strip()


def order_claim(claim_id: str, item: dict, seen: int, runs: int, by_id: dict[str, Claim]) -> Claim:
    """The material row an item gives: a figure when its rows carry one, else
    the formula with what is missing named, flagged unverified. The row is
    named after the sheet's page (the page table's title), not the model's words."""
    sheet = by_id[item["sheet"]]
    q = by_id.get(item["quantity"]) if item["quantity"] else None
    spec = by_id.get(item["spec_rate"]) if item["spec_rate"] else None
    coat_row = by_id.get(item["coats"]) if item["coats"] else None
    coats, coats_why = coat_count(coat_row, sheet) if coat_row is not None else (None, "")
    rate = rate_of(spec, sheet)
    basis = [by_id[r] for r in dict.fromkeys(item["basis"]) if r in by_id]
    cited = [c for c in [q, spec, coat_row, sheet, *basis] if c is not None]
    notes, calc, value, value_num = [], "", "", None
    if seen < runs:
        notes.append(f"seen in {seen} of {runs} runs")
    shaky = [c.claim_id for c in cited if c.flag]
    if shaky:
        notes.append(f"uses flagged {', '.join(_join(shaky))}")
    questions = _join(c.question for c in cited if c.question)
    if questions:
        notes.append(f"open question {', '.join(questions)}")
    if not sheet.quote:
        # the page was looked at and did not show the product (the repaint's "or equivalent" caulk, 2026-10-09)
        notes.append(f"the page {sheet.claim_id} cites quotes nothing for the product")
    per_coat = item["unit"] == "gal"
    if item["unit"] == "each":
        if q is None:
            how = "no count row"
            notes.append("no count row names how many")
        elif q.method == "FIELD":
            how = f"count per {q.claim_id}"
            notes.append(f"the count waits on {q.claim_id} (FIELD)")
        else:
            calc, how = f"{{{q.claim_id}}}", f"count from {q.claim_id} ({q.method})"
    else:
        # the clause the count was read from is named with it, so the replay can see where the literal came from
        times = f" x {coats or '?'} coat{'s' if coats != 1 else ''}" + (f" ({coat_row.claim_id})" if coat_row is not None else "")
        how = (f"{q.claim_id if q is not None else 'FIELD'}" + (times if per_coat else "")
               + f" / {rate.claim_id}" if rate is not None else "no rate")
        if q is None:
            notes.append("no quantity row names what it covers")
        elif q.method == "FIELD":
            notes.append(f"the quantity waits on {q.claim_id} (FIELD)")
        if per_coat and coats is None:
            notes.append(coats_why or "no clause states how many coats")
        if rate is None:
            notes.append("no row states a rate")
        if q is not None and q.method != "FIELD" and rate is not None and (coats or not per_coat):
            calc = f"{{{q.claim_id}}} * {coats} / {{{rate.claim_id}}}" if per_coat else f"{{{q.claim_id}}} / {{{rate.claim_id}}}"
    confidence = "missing"
    if calc:
        lo, hi = schema.evaluate(calc, by_id)     # CalcError reaches run(), which refuses the item
        value, value_num = schema.value_of(lo, hi)
        # no firmer than its weakest input: an inferred rate (an old sheet) makes an inferred order
        read = [c for c in (q, rate) if c is not None and f"{{{c.claim_id}}}" in calc] + ([coat_row] if per_coat else [])
        confidence = max((c.confidence for c in read), key=lambda k: schema.CONFIDENCE_RANK.get(k, 0))
        if item["unit"] == "each":
            how = f"{_fig(q)} ({q.claim_id}, {q.method})"
        else:
            how = f"{_fig(q)} ({q.claim_id}){times if per_coat else ''} / {_fig(rate)} ({rate.claim_id}) = {value} {item['unit']}"
    if spec is not None and rate_unit(sheet):
        # the precedence rule settles the choice (p.16); it is part of the derivation, not a doubt that flags the row
        how += f"; the spec's rate ({spec.claim_id}, {spec.tag or spec.locator}) governs over the sheet's {_fig(sheet)}"
    derivation = "; ".join([how] + notes)
    sources = _join(s for c in [q, *basis, spec, coat_row] if c is not None for s in schema.sources_of(c.source_id))
    name = sheet.tag or sheet.statement[:80]
    return Claim(
        claim_id=claim_id, statement=f"{name}: {derivation}",
        source_id=" + ".join(sources + ["WEB"]), locator=" and ".join(_join(c.locator for c in [q, *basis, spec, coat_row] if c)),
        tag=" + ".join(_join([*(c.tag for c in [q, *basis, spec, coat_row] if c), sheet.tag])), method="fetched", role="material",
        confidence=confidence, value=value, value_num=value_num, unit=item["unit"],
        calc=calc, derivation=derivation, division=next((c.division for c in [*basis, q] if c and c.division), ""),
        flag="unverified" if notes else "", question=questions[0] if questions else "",
        url=sheet.url, retrieved=sheet.retrieved, quote=sheet.quote,
    )


def run(broker: Broker, job: str, client: ModelClient | None, *, repeats: int = 2) -> OrderResult:
    claims = current(broker)
    rows = inputs(claims)
    result = OrderResult()
    if not sheets(rows):
        result.notes.append("no data sheet rows in the ledger: no order rows (Materials writes fetched rows only)")
        return result
    if client is None:
        result.notes.append("no model client: no order rows")
        return result
    writer = broker.as_principal(PRINCIPAL, model_id=client.model_id, prompt_version=prompt_version(NAME),
                                 agent_label="materials")
    result.units = 1
    unit = unit_for(job, rows)
    by_id = {c.claim_id: c for c in rows}
    system = prompt(NAME)
    valid = []
    for r in range(repeats + SPARES):
        if r >= repeats and len(valid) >= repeats and len({frozenset(item_key(i) for i in v) for v in valid}) == 1:
            break
        result.calls += 1
        raw = client.complete(NAME, unit, system, SCHEMA, r)
        try:
            data = json.loads(raw) if isinstance(raw, str) else raw
        except json.JSONDecodeError as e:
            data, errs = None, [f"not JSON: {e}"]
        else:
            errs = validate.errors(data, SCHEMA)
        writer.log_call(unit.unit_id, f"run {r + 1}; {'discarded: ' + errs[0] if errs else 'valid'}")
        if errs:
            result.discarded.append(f"{unit.unit_id} run {r + 1}: {'; '.join(errs[:3])}")
            continue
        kept = []
        for i, item in enumerate(data["items"]):
            why = item_errors(item, by_id)
            if why:
                result.discarded.append(f"{unit.unit_id} run {r + 1}: items[{i}] {why[0]}")
            else:
                kept.append(item)
        valid.append(kept)
    if not valid:
        result.unread.append(unit.unit_id)
    orders, dupes = vote(valid)
    result.notes += dupes
    for n, (item, seen) in enumerate(orders, 1):
        claim_id = f"{job}-MT-{n:02d}"
        try:
            claim = order_claim(claim_id, item, seen, len(valid), by_id)
        except CalcError as e:
            result.refused.append(f"{claim_id}: {e}")
            continue
        try:
            result.rows.append(writer.append(claim))
        except LedgerError as e:
            result.refused.append(f"{claim.claim_id}: {e}")
    return result


# ---- the golden gate ------------------------------------------------------


@dataclass
class Gate:
    ok: bool
    compared: int
    failures: list[str]
    notes: list[str]
    misses: list[str] = field(default_factory=list)

    def text(self) -> str:
        matched = self.compared - len(self.misses)
        lines = [f"materials gate: {'PASS' if self.ok else 'FAIL'} ({self.compared} fixture orders compared, "
                 f"{matched} matched; {ANSWERED:.0%} needed, and no wrong figure or unit)"]
        lines += [f"  FAIL {f}" for f in self.failures]
        lines += [f"  miss {m}" for m in self.misses]
        lines += [f"  note {n}" for n in self.notes]
        return "\n".join(lines)


# The share of compared fixture orders the gate needs a matching run row for.
ANSWERED = 0.8
CITED = re.compile(r"\b([A-Z]+-C-\d+)\b")


def gate(result: OrderResult, fixture_rows: list[dict]) -> Gate:
    """For every material row of the fixture that cites a fetched page (a C-row
    named in its tag or statement, with a URL), the run must have an order row
    in the fixture's unit (a tube is not a cartridge) citing a page the fixture holds for
    the job, that page first, and with a figure exactly when the fixture has one, equal to it. A
    product has several pages (its evaluation report, its data sheet, the maker's
    letter), and the hand bid cited one of them; a run row citing another is
    noted, not missed. A row with the wrong figure is a wrong order and fails
    the gate; a firm row where the hand bid flags its order is noted. A fixture order with no run row is a miss: safe, since the
    bid then has no figure for it, but incomplete, so at least ANSWERED of the
    compared orders must have one. A fixture order that cites no page is noted
    and left out: Materials writes fetched rows only (p.11)."""
    by_id = {r["id"]: r for r in fixture_rows}
    pages = {r.get("url") for r in fixture_rows if r.get("method") == "fetched" and r.get("url")}
    failures, notes, misses = [], [], []

    def same(c: Claim, unit: str) -> bool:
        return c.unit == unit

    def check(f: dict, rows: list[Claim]) -> None:
        unit, want = f.get("unit", "") or "", schema.format_value(f.get("value"))[0]
        if f.get("flag") and not all(c.flag for c in rows):
            # the estimator's doubt (a pending approval, a phased-out product) lives in an open question
            # the run's rows may not cite; reported, since a firm row where the hand bid hedged is worth a look
            notes.append(f"{f['id']}: the hand bid flags it {f['flag']}; the run's row is firm")
        if not want and all(c.value for c in rows):
            failures.append(f"{f['id']}: gives {rows[0].value} {unit} where the fixture waits on a FIELD measure")
        elif want and not any(c.value == want for c in rows):
            if any(c.value for c in rows):
                failures.append(f"{f['id']}: gives {', '.join(_join(c.value for c in rows if c.value))} {unit}, "
                                f"the fixture {want}")
            else:   # a row still waiting on its figure is incomplete, never wrong
                misses.append(f"{f['id']}: the run's {unit} rows carry no figure where the fixture gives {want}")

    # First pass: each fixture order against the rows citing its own page, in its unit.
    pending, used = [], set()
    for f in fixture_rows:
        if f.get("role") != "material":
            continue
        urls = _join(by_id[c].get("url", "") for c in CITED.findall(f"{f.get('tag', '')} {f.get('statement', '')}")
                     if c in by_id)
        if not urls:
            notes.append(f"{f['id']}: cites no fetched page; not compared")
            continue
        unit = f.get("unit", "") or ""
        got = [c for c in result.rows if c.url in urls]
        # the same page can cover another product (the adhesive's cartridges beside the anchors it sets):
        # a row in another unit is not this order, and not a wrong one either
        same_unit = [c for c in got if same(c, unit)]
        if same_unit:
            used.update(c.claim_id for c in same_unit)
            check(f, same_unit)
        else:
            pending.append((f, urls, got))
    # Second pass: an order the hand bid tied to one of the product's pages may rest
    # on another of the job's pages in the run; each such row answers one order.
    for f, urls, got in pending:
        unit = f.get("unit", "") or ""
        want = schema.format_value(f.get("value"))[0]
        cands = [c for c in result.rows if c.url in pages and c.url not in urls and same(c, unit)
                 and c.claim_id not in used]
        # the row that agrees with the hand bid first: its figure, or no figure where the hand bid waits
        other = next((c for c in cands if (c.value == want if want else not c.value)), cands[0] if cands else None)
        if other is None:
            how = f" in {unit} (the run orders {', '.join(_join(c.unit for c in got))} from it)" if got else ""
            misses.append(f"{f['id']}: no order row cites {urls[0]}{how}")
            continue
        used.add(other.claim_id)
        notes.append(f"{f['id']}: matched through {other.url}; the hand bid cited {urls[0]}")
        check(f, [other])
    compared = sum(1 for f in fixture_rows if f.get("role") == "material"
                   and any(c in by_id and by_id[c].get("url") for c in CITED.findall(f"{f.get('tag', '')} {f.get('statement', '')}")))
    ok = not failures and compared > 0 and compared - len(misses) >= ANSWERED * compared
    return Gate(ok=ok, compared=compared, failures=failures, notes=notes, misses=misses)
