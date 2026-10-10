"""The coat count a spec clause states for one product, read by code (architecture p.4, p.9, p.16).

The Materials order step (materials.py, next) works a coating's order out as
`{AREA} x coats / {RATE}`. The coat count is never a number from the model:
code reads it from the clause row the model names, and it settles a count only
where the clause ties exactly one count to this product. A clause naming
several products gives each the count in its own stretch; a range, a floor
("at least two coats"), a count with more coats in places, a count for a
system or split across products, or a count two products may share settles
nothing, and the reason says why, so the order is written with no figure and
flagged for the estimator (Determinism: two reasonable readings, neither
picked).

It reads one clause row. A spec-wide clause elsewhere that makes every stated
count a floor ("additional coats regardless of the number specified") is the
order step's to weigh before it uses a count from here.
"""
from __future__ import annotations

import re

from .schema import Claim

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
# Words after "in" that make a small number a tool or part size, not an added coat ("9 in rollers", "2 in diameter").
TOOL_WORDS = r"diameter|dia|nap|rollers?|brush(?:es)?"
# A small number after a label word or another number is that label's ("Part 3 for", "Section 09 01 90 for", "color 2 at").
LABEL_WORDS = ("part", "section", "division", "article", "note", "table", "sheet", "detail", "item", "no.", "color", "colour",
               "level", "floor", "phase", "building", "unit", "type", "class", "grade", "step", "zone", "area", "figure",
               "page", "paragraph",
               "than")   # "more than two in exterior exposures" compares, and HEDGE reads it as a floor
NOT_LABELLED = "".join(rf"(?<!\b{re.escape(w)} )" for w in LABEL_WORDS) + r"(?<!\d )"
MORE = re.compile(rf"\b(?:second|third|fourth|additional|extra|another|plus|further|double|(?<!\bor )(?<!\band )more)[\s-]+(?:(?:[1-9]\d*|one|two|three|four|five|six)\s+)?{ROLE}coats?\b"
                  rf"|\b(?:more|additional|extra|further)\s+(?:paint|material|product|coating)s?\b(?!\s*:)"   # not an "Extra materials:" article
                  rf"|\bre-?coat\b(?:\s+[A-Za-z]+){{0,3}}\s+(?:at|where|areas|repairs)\b"   # not "recoat after 4 hours at 77F", a time
                  rf"|\bre-?coat\s+(?:as|if|where|when)\s+(?:required|needed|necessary)\b"
                  rf"|(?<![\w./-]){NOT_LABELLED}"
                  rf"([1-6]|one|two|three|four|five|six)\b(?![\s-]*(?:\(\d+\)\s*)?{ROLE}coats?\b)(?!\s*(?:in\.|\"))(?!\s+in\s+(?:{TOOL_WORDS})\b)(?=\s+(?:at|on|over|for|where|in|more)\b)", re.I)
                  # a small number only, not a label's number ("Part 3 for", "No. 2 at", "Section 09 01 90 for"), a tool size ("9 in rollers",
                  # "2 in diameter", "4 in. wide", '6" or more') or part of a longer number ("7005 for trim"); any other ("apply two at patched areas",
                  # "2 in high-traffic areas", "2 in deep colors") is an added coat, the safe direction
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
# a measurement's unit; the inch only as "in.", "inch(es)", a double quote or "in" before a tool word ("2 in rollers"),
# since "in" is otherwise a preposition ("more than two in exterior exposures", "more than two in deep colors")
MEASURE_UNITS = (r"(?:ft|feet|inch(?:es)?|mils?|DFT|WFT|sq\s*ft(?:/gal)?|gal|gallons?|mm|cm|m|hours?|hrs?|days?|years?|percent)\b"
                 rf"|in\.|\"|in(?=\s+(?:{TOOL_WORDS})\b)")   # a word unit ends at a word boundary; "in." and a double quote end in punctuation
A_NUMBER = r"\d+(?:[.,]\d+)*|one|two|three|four|five|six|seven|eight|nine|ten|twelve|fifteen|twenty|thirty|forty|fifty|hundred"
# "more than" followed directly by a figure and a unit is a comparison ("more than 10 ft above grade"); "more than two.",
# "more than that" and "more than two coats" hedge the count
COMPARISON = rf"(?!\s+than\s+(?:{A_NUMBER})(?:%|\s*(?:{MEASURE_UNITS})))"
HEDGE = re.compile(rf"\b(?:or|and)\s+more\b{COMPARISON}|\b(?:more{COMPARISON}|additional|extra|further)\b(?!\s+materials?\s*:)|\b(?:as|if|where|when)\s+(?:required|needed|necessary)\b", re.I)
CEILING = re.compile(rf"\b(?:up to|no more than|not more than|not to exceed|a maximum of|maximum(?: of)?|max\.?)\s+{N2}(?:\s*\(\d+\))?[\s-]+{ROLE}coats?\b"
                     rf"|\bcoats?\s*[,(]\s*(?:maximum|max)\b(?!\.?\s*(?:{FIGURE}|{THICKNESS}))"
                     rf"|\bcoats?\s+(?:maximum|max)\b(?!\.?\s*(?:{FIGURE}))", re.I)
BARE_MORE = re.compile(r"(?:or|and)\s+more", re.I)
SENTENCE_MARK = re.compile(r"[();.]")
FULL_STOP = re.compile(r"\.(?:\s+[A-Z(\"']|\s*$)")
# text ending in a figure and at most its unit words, which a bare "or more" then follows directly
BOUNDED = re.compile(rf"(?:\d+(?:[.,]\d+)*|\b(?:{A_NUMBER})\b)(?:%|(?:\s*(?:{MEASURE_UNITS}))*)\s*$", re.I)
# The mark a clause's statement and quote are joined with, one text to read.
SEP = "; "
# A stretch opened by a sequence word continues whatever came before it in the same part
# (a product, "Base coat as needed", or a step, "Scrape"), and one opened by "and" continues
# a product named before it; so a count written after its code there ("B53 over B66 primer,
# 2 coats"; "B66 as needed, then B53, 2 coats"; "B66 as needed. Then B53, 2 coats"; "X100 and
# X200, 2 coats each"; "B66, followed by B53, 2 coats") may be the system's as well as that
# code's: two readings, neither picked. A count before the code ("then 1 coat B53", "over one
# coat of B66") is that code's alone, after "or" the count is the named alternative's on
# either reading, and "Walls and ceilings: B53, 2 coats" continues no product.
SEQUENCE = ("then", "over", "followed by", "after", "before", "prior to")


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
        before = [(p, e) for p, e in counted if sentence <= p < m.start()]
        if before:
            if BARE_MORE.fullmatch(m.group()) and BOUNDED.search(PRODUCT_CODE.sub(" ", text[before[-1][1]:m.start()])):
                continue   # "10 ft or more above grade" bounds the height
            found.append((before[-1][0], "floor"))   # placed with the count it bounds
        elif sentence > part_start:
            lead = text[sentence:m.start()]
            # an added coat ("Accent color SW6258: additional coat") is about coats whoever it names, so it floors them all
            adds = any(a.start() <= m.start() < a.end() for a in MORE.finditer(text))
            for p, _ in counted:
                if part_start <= p < sentence and (adds or not other_product(text[stretch_at(spans, p)], lead)):
                    found.append((p, "floor"))   # every count before, in the part, unless the other product stands right before the hedge
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


def stretch_at(spans: list[tuple[int, int, str, int]], pos: int) -> slice:
    """The stretch that holds `pos`, as a slice of the text."""
    a, b, *_ = next((s for s in spans if s[0] <= pos < s[1]), (0, 0))
    return slice(a, b)


def other_product(own: str, lead: str) -> bool:
    """Whether `lead`, the words before a hedge in a later sentence, puts a product other
    than the one `own` (a count's stretch) speaks of right before the hedge, so the hedge
    is that product's ("Example Primer B66 as needed", "Spot-prime as required"): its
    last word is a code `own` does not carry, or, with no code in the lead, a word of the
    other role (a primer where `own` speaks of a finish, or the reverse). A product named
    earlier in the lead ("deep colors over a tinted primer may require more"), the
    count's own code or role, a colour number or a standard makes the hedge no other
    product's, so the count is a floor: the safe direction."""
    words = lead.strip(" ,;:.()-\u2013").split()
    if not words:
        return False
    last = words[-1].strip("()[],;:.")   # "Example Primer (B66)" ends in B66
    codes = product_codes(lead)
    if codes:
        return last in codes and not set(codes) & set(product_codes(own))
    role = (bool(PRIME_WORD.search(last)), bool(FINISH_WORD.search(last)))
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
