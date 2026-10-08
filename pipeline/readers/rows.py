"""Reader output to ledger rows.

The method on every row is fixed by rule from the reader and the item kind,
never chosen by the model: a dimension string becomes `dimensioned`, a symbol
count `counted`, a length read against a scale `scaled`, printed text
`clause`, a photo item `observed`, a message instruction `customer`
(architecture p.6, p.13 "dimensioned vs scaled is a rule, not a judgment").
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from ..schema import Claim
from . import units
from .vote import Voted


@dataclass(frozen=True)
class Unit:
    """One unit of input: a drawing view, a spec page, a photo, a message."""

    unit_id: str          # stable key, e.g. "S-1#Fnd", "SW#p17", "IMG_8343"
    source_id: str        # Source Register ID
    locator: str          # what a human opens: "Partial Foundation Plan", "p.17"
    tag: str              # the short cite printed in the proposal: "S-1 Fnd", "SW p.17"
    path: str = ""        # the page image, tile or file the model is shown
    scale: str = ""       # drawing views: the stated scale, e.g. 3/4"=1'-0"
    text: str = ""        # text units: the text layer or message body the model is shown


class ReaderOutputError(ValueError):
    pass


CODES = {"drawing": "DR", "spec": "SP", "photo": "PH", "correspondence": "CO"}


def semantic_errors(reader: str, response: dict) -> list[str]:
    """Rules the JSON schema cannot express. A response that breaks one is discarded."""
    errs = []
    for i, it in enumerate(response.get("items", [])):
        if reader != "drawing":
            continue
        kind = it["kind"]
        if kind == "dimension":
            if not it["text"]:
                errs.append(f"items[{i}]: a dimension needs the dimension string as printed")
            else:
                try:
                    units.to_inches(it["text"])
                except units.DimensionError as e:
                    errs.append(f"items[{i}]: {e}")
            if it["count"] is not None:
                errs.append(f"items[{i}]: a dimension carries no count")
        elif kind == "count":
            if it["count"] is None:
                errs.append(f"items[{i}]: a count needs the number of symbols drawn")
        elif kind == "scaled":
            if not it["text"]:
                errs.append(f"items[{i}]: a scaled item needs the reading")
            if it["count"] is not None:
                errs.append(f"items[{i}]: a scaled item carries no count")
        elif not it["text"]:
            errs.append(f"items[{i}]: a {kind} item needs the text as printed")
    return errs


# Fields a reader must copy from the unit word for word. On a text unit each one
# has to appear in the unit's own text; on a raster (a drawing view, a photo)
# there is no text to hold it to, and the vote and the Auditor do that job.
VERBATIM = {
    "drawing": {"note": ("text",), "load": ("text",), "standard": ("text",)},
    "spec": {None: ("requirement", "product")},
    "correspondence": {None: ("instruction",)},
}


def _squash(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def not_in_source(reader: str, unit: Unit, item: dict) -> list[str]:
    """The verbatim fields of `item` that are not in the unit's text.

    Whitespace is normalised (a clause wraps across lines on the page);
    nothing else is. An item that fails was not copied from the source, whether
    the model made it up or a document told it to write it, so it is dropped
    in code rather than trusted to the prompt.
    """
    if not unit.text:
        return []
    by_kind = VERBATIM.get(reader, {})
    fields = by_kind.get(item.get("kind"), by_kind.get(None, ()))
    page = _squash(unit.text)
    return [f for f in fields if item.get(f) and _squash(str(item[f])) not in page]


def _slug(unit_id: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "", unit_id.upper())[-10:] or "U"


def _readings(values: list) -> str:
    """Two readings kept side by side, in the form the fixtures use."""
    return " / ".join(f"{v} (reading {chr(65 + i)})" for i, v in enumerate(values))


def _spread(texts: list) -> str:
    """How far scaled readings of one length disagree, computed in code.

    Every reading is listed first and no centre value is given: a scaled length
    is never a quantity, and a midpoint would read as one chosen (Scott's rules).
    """
    if len(texts) < 2:
        return ""
    try:
        inches = [units.to_inches(t) for t in texts]
    except units.DimensionError:
        return ""
    each = ", ".join(f"{t.strip()} = {units.figure(i)} in" for t, i in zip(texts, inches))
    return f"scaled readings {each}; range {units.figure(max(inches) - min(inches))} in"


def _flag(v: Voted) -> tuple[str, str]:
    """(flag, note) for a voted item."""
    if v.readings:
        fields = ", ".join(v.readings)
        return "conflict", f"runs disagree on {fields}; every reading kept, none chosen"
    if v.seen < v.runs:
        return "unverified", f"seen in {v.seen} of {v.runs} runs"
    return "", ""


def to_claims(reader: str, job: str, unit: Unit, voted: list[Voted]) -> list[Claim]:
    builders = {"drawing": _drawing, "spec": _spec, "photo": _photo, "correspondence": _correspondence}
    out = []
    for n, v in enumerate(voted, 1):
        claim_id = f"{job}-{CODES[reader]}-{_slug(unit.unit_id)}-{n:02d}"
        flag, note = _flag(v)
        out.append(builders[reader](claim_id, unit, v, flag, note))
    return out


def _base(claim_id: str, src: Unit, **kw) -> Claim:
    return Claim(claim_id=claim_id, source_id=src.source_id, locator=src.locator, tag=src.tag, **kw)


def _drawing(claim_id: str, unit: Unit, v: Voted, flag: str, note: str) -> Claim:
    it = v.item
    kind, label = it["kind"], it["label"].strip()
    if kind == "dimension":
        texts = v.readings.get("text")
        if texts:
            return _base(claim_id, unit, statement=f"{label}: {_readings(texts)}", method="dimensioned",
                         role="quantity", confidence="exact", value=_readings(texts), unit="in",
                         flag=flag, derivation=note)
        inches = units.to_inches(it["text"])
        return _base(claim_id, unit, statement=f"{label}: {it['text']}", method="dimensioned",
                     role="quantity", confidence="exact", value=str(units.figure(inches)),
                     value_num=float(inches), unit="in",
                     derivation="; ".join(filter(None, [units.derivation(it["text"]), note])), flag=flag)
    if kind == "count":
        counts = v.readings.get("count")
        unit_name = it["unit"] or "each"
        if counts:
            return _base(claim_id, unit, statement=f"{label}: {_readings(counts)}", method="counted",
                         role="quantity", confidence="exact", value=_readings(counts), unit=unit_name,
                         flag=flag, derivation=note)
        return _base(claim_id, unit, statement=label, method="counted", role="quantity",
                     confidence="exact", value=str(it["count"]), value_num=float(it["count"]),
                     unit=unit_name, flag=flag, derivation=note)
    if kind == "scaled":
        texts = v.readings.get("text") or [it["text"]]
        locator = f"{unit.locator} at {unit.scale}" if unit.scale else unit.locator
        value = _readings(texts) if len(texts) > 1 else texts[0]
        return Claim(claim_id=claim_id, source_id=unit.source_id, locator=locator, tag=unit.tag,
                     statement=f"{label}, scaled", method="scaled", role="note", confidence="scaled",
                     value=value, flag=flag, derivation="; ".join(filter(None, [_spread(texts), note])))
    # note, load, standard: printed text, quoted so the Auditor can find it.
    return _base(claim_id, unit, statement=label, method="clause", role="note", confidence="exact",
                 quote=it["text"], flag=flag, derivation=note)


def _spec(claim_id: str, unit: Unit, v: Voted, flag: str, note: str) -> Claim:
    it = v.item
    locator = f"{unit.locator} {it['clause']}".strip() if it["clause"] else unit.locator
    statement = it["requirement"] if not it["product"] else f"{it['requirement']} [product: {it['product']}]"
    return Claim(claim_id=claim_id, source_id=unit.source_id, locator=locator, tag=unit.tag,
                 statement=statement, method="clause", role="scope", confidence="exact",
                 division=it["division"] or "", quote=it["requirement"], flag=flag, derivation=note)


def _photo(claim_id: str, unit: Unit, v: Voted, flag: str, note: str) -> Claim:
    it = v.item
    severities = v.readings.get("severity") or [it["severity"]]
    words = f"condition {it['condition']} at {it['location']}"
    if any(severities):
        words += ", severity " + " / ".join(s or "not stated" for s in severities)
    return _base(claim_id, unit, statement=it["description"], method="observed", role="note",
                 confidence="inferred", flag=flag, derivation="; ".join(filter(None, [words, note])))


def _correspondence(claim_id: str, unit: Unit, v: Voted, flag: str, note: str) -> Claim:
    it = v.item
    locator = f"{unit.locator}, {it['sender']}, {it['date'] or 'date not stated'}"
    return Claim(claim_id=claim_id, source_id=unit.source_id, locator=locator, tag=unit.tag,
                 statement=it["instruction"], method="customer", role="note", confidence="exact",
                 quote=it["instruction"], flag=flag, derivation=note)
