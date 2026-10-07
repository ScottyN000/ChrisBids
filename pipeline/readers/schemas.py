"""The output schema of each reader, and its enumerated vocabularies.

Every call returns `{"items": [...]}` against one of these, or the response is
discarded (architecture p.9 "schema-constrained output", p.14 "the schema or
nothing"). Free text is limited to the fields that copy the source verbatim
and to a short description. A field the input does not state is null.

The photo schema has no field that can hold a number, and its description
refuses digits, so "observed never carries a number" (p.6) is a property of
the schema rather than a request in a prompt.
"""
from __future__ import annotations

from ..schema import DIVISIONS

# ---- Drawing Reader (one tile or view per call) -----------------------------

DRAWING_KINDS = ("dimension", "count", "scaled", "note", "load", "standard")

DRAWING = {
    "type": "object",
    "additionalProperties": False,
    "required": ["items"],
    "properties": {"items": {"type": "array", "maxItems": 60, "items": {
        "type": "object",
        "additionalProperties": False,
        "required": ["kind", "label", "text", "count", "unit"],
        "properties": {
            "kind": {"enum": list(DRAWING_KINDS)},
            # What the figure is, in the sheet's own words.
            "label": {"type": "string", "maxLength": 200},
            # dimension: the dimension string exactly as printed, e.g. 15'-2".
            # scaled: the length read against the stated scale, same notation.
            # note / load / standard: the text exactly as printed.
            "text": {"type": ["string", "null"], "maxLength": 600},
            # count: how many symbols are drawn. Never inferred from TYP.
            "count": {"type": ["integer", "null"], "minimum": 0},
            # count: what was counted, "each", or "per <thing>".
            "unit": {"type": ["string", "null"], "pattern": r"^(each|per [a-z ]+|[a-z]+)$"},
        },
    }}},
}

# ---- Spec Reader (one page per call) ------------------------------------------

SPEC = {
    "type": "object",
    "additionalProperties": False,
    "required": ["items"],
    "properties": {"items": {"type": "array", "maxItems": 40, "items": {
        "type": "object",
        "additionalProperties": False,
        "required": ["clause", "requirement", "division", "product"],
        "properties": {
            # The clause number as printed (I.6, B.2, 3.1), or null.
            "clause": {"type": ["string", "null"], "maxLength": 20},
            # The requirement copied verbatim from the page.
            "requirement": {"type": "string", "maxLength": 1200},
            "division": {"enum": [d for d in DIVISIONS if d] + [None]},
            # A product name exactly as the page prints it, or null.
            "product": {"type": ["string", "null"], "maxLength": 200},
        },
    }}},
}

# ---- Photo Reader (one photo per call) ----------------------------------------

CONDITIONS = ("none", "crack", "spall", "rust", "peeling", "delamination", "staining", "loose", "missing")
LOCATIONS = (
    "wall", "balcony", "railing", "walkway", "stair", "soffit", "ceiling", "window", "door",
    "roof", "column", "deck", "fence", "garage", "elevation", "other",
)
SEVERITY = ("minor", "moderate", "severe")

PHOTO = {
    "type": "object",
    "additionalProperties": False,
    "required": ["items"],
    "properties": {"items": {"type": "array", "minItems": 1, "maxItems": 12, "items": {
        "type": "object",
        "additionalProperties": False,
        "required": ["location", "condition", "severity", "description"],
        "properties": {
            "location": {"enum": list(LOCATIONS)},
            "condition": {"enum": list(CONDITIONS)},
            "severity": {"enum": list(SEVERITY) + [None]},
            # What and where, in words. No digits: a photo never carries a number.
            "description": {"type": "string", "maxLength": 160, "pattern": r"^[^0-9]*$"},
        },
    }}},
}

# ---- Correspondence Reader (one message per call) ------------------------------

CORRESPONDENCE = {
    "type": "object",
    "additionalProperties": False,
    "required": ["items"],
    "properties": {"items": {"type": "array", "maxItems": 30, "items": {
        "type": "object",
        "additionalProperties": False,
        "required": ["sender", "date", "instruction"],
        "properties": {
            "sender": {"type": "string", "maxLength": 120},
            "date": {"type": ["string", "null"], "pattern": r"^\d{4}-\d{2}-\d{2}$"},
            # The instruction copied verbatim from the message.
            "instruction": {"type": "string", "maxLength": 1200},
        },
    }}},
}

BY_READER = {
    "drawing": DRAWING,
    "spec": SPEC,
    "photo": PHOTO,
    "correspondence": CORRESPONDENCE,
}
