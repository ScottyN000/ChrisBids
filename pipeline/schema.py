"""Claim-ledger schema and the method rules the Auditor enforces.

Field list and enumerated vocabularies are the architecture doc's ledger table
(p.5-6). The method rules on p.6 are implemented here as pure functions so the
broker can refuse a bad row at write time and the Auditor can re-check every
stored row later with the same code.
"""
from __future__ import annotations

import ast
import itertools
import operator
import re
from dataclasses import dataclass, fields
from typing import Any

# Enumerated vocabularies (architecture p.5-6, p.14 "enumerated vocabularies").
METHODS = ("dimensioned", "counted", "scaled", "clause", "observed", "fetched", "customer", "FIELD")
CONFIDENCE = ("exact", "scaled", "inferred", "missing")
FLAGS = ("", "unverified", "conflict")
ROLES = ("header", "scope", "quantity", "allowance", "material", "code", "exclusion", "question", "note")
PARTS = ("", "base", "alternate")
AUDIT = ("", "pass", "fail", "unverified")
# CSI divisions Contractor Co. writes proposals by (architecture p.7).
DIVISIONS = ("", "01", "02", "03", "05", "07", "08", "09", "31", "33", "35")

# Methods that may feed a Contractor Co. allowance or an order quantity (architecture p.6:
# "scaled ... never becomes an order quantity without a site check").
ALLOWANCE_OK = ("dimensioned", "counted", "clause", "FIELD")
# An order quantity may also rest on a fetched figure: a data sheet's spread
# rate or yield, cited with its URL (architecture p.4: Materials writes "product
# data sheet figures (spread rate, yield, pack size), order quantities"). An
# allowance may not.
MATERIAL_OK = ALLOWANCE_OK + ("fetched",)
# A figure a source states as a range ("320-400", "2 - 4"): one figure, two ends.
RANGE = re.compile(r"^\s*(\d[\d,]*(?:\.\d+)?)\s*[-\u2013]\s*(\d[\d,]*(?:\.\d+)?)\s*$")
# Most range inputs one calc may rest on: it is replayed at every corner.
MAX_RANGES = 3
# Methods that carry an exact figure. A row of either kind may only be worked
# out from rows of these kinds.
EXACT_METHODS = ("dimensioned", "counted")

# The flat ledger field order, as the architecture doc lists it plus the
# bookkeeping columns the fixtures already carry.
LEDGER_FIELDS = (
    "claim_id", "value", "unit", "statement", "source_id", "locator", "method",
    "derivation", "confidence", "agent", "timestamp", "audit",
    "division", "role", "part", "flag", "question", "url", "retrieved", "quote",
    "supersedes",
)

CALC_REF = re.compile(r"\{([A-Z0-9-]+)\}")
CALC_SAFE = re.compile(r"[0-9.+\-*/() ]+")
# A calc is written by an agent, so it is evaluated as data: + - * / and
# parentheses over numbers, nothing else. `**` passes CALC_SAFE but is refused
# here, so "9**9**9**9" cannot hang the broker.
_ARITH_OPS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
    ast.USub: operator.neg, ast.UAdd: operator.pos,
}
CALC_MAX_LEN = 400


class LedgerError(Exception):
    """A row that the access matrix or the method rules refuse."""


@dataclass
class Claim:
    """One ledger row: every figure that could appear in a bid.

    `value` is kept as the canonical string the CSV carries; `value_num` is the
    numeric reading used for arithmetic replay. A row with no figure (a clause,
    an observation, a FIELD placeholder) has both empty.
    """

    claim_id: str
    statement: str
    source_id: str
    method: str
    role: str
    confidence: str
    value: str = ""
    value_num: float | None = None
    unit: str = ""
    locator: str = ""
    tag: str = ""
    derivation: str = ""
    calc: str = ""
    division: str = ""
    part: str = ""
    flag: str = ""
    question: str = ""
    url: str = ""
    retrieved: str = ""
    quote: str = ""
    supersedes: str = ""
    reason: str = ""
    # Stamped by the broker, never by a caller (architecture p.9 "pinned versions").
    agent: str = ""
    timestamp: str = ""
    run_id: str = ""
    model_id: str = ""
    prompt_version: str = ""
    tool_versions: str = ""
    # Set only by the Auditor (architecture p.11: the only field updated in place).
    audit: str = ""
    audit_note: str = ""

    def as_ledger_row(self, *, audit: str | None = None) -> dict[str, str]:
        """The flat row in LEDGER_FIELDS order, as ledger.csv carries it."""
        return {
            "claim_id": self.claim_id,
            "value": self.value,
            "unit": self.unit,
            "statement": self.statement,
            "source_id": self.source_id,
            "locator": self.locator,
            "method": self.method,
            "derivation": self.derivation or self.calc,
            "confidence": self.confidence,
            "agent": self.agent,
            "timestamp": self.timestamp,
            "audit": self.audit if audit is None else audit,
            "division": self.division,
            "role": self.role,
            "part": self.part,
            "flag": self.flag,
            "question": self.question,
            "url": self.url,
            "retrieved": self.retrieved,
            "quote": self.quote,
            "supersedes": self.supersedes,
        }


CLAIM_FIELDS = tuple(f.name for f in fields(Claim))


def format_value(v: Any) -> tuple[str, float | None]:
    """Canonical string form of a figure, plus its numeric reading if it has one.

    Integral floats print without a decimal point so a ledger written from YAML
    and one written from a reader's JSON produce the same CSV.
    """
    if v is None or v == "":
        return "", None
    if isinstance(v, bool):
        raise LedgerError(f"value {v!r} is not a figure")
    if isinstance(v, int):
        return str(v), float(v)
    if isinstance(v, float):
        return (str(int(v)) if v.is_integer() else repr(v)), v
    text = str(v)
    try:
        return text, float(text.replace(",", ""))
    except ValueError:
        return text, None


def value_range(value: str) -> tuple[float, float] | None:
    """The two ends of a figure stated as a range ("320-400"), low end first;
    None for a single figure or no figure."""
    m = RANGE.match(value or "")
    if not m:
        return None
    lo, hi = (float(x.replace(",", "")) for x in m.groups())
    return (lo, hi) if lo <= hi else (hi, lo)


def format_range(lo: float, hi: float) -> str:
    """A replayed range as a ledger value: "40-50", whole numbers without a point."""
    return "-".join(str(int(x)) if float(x).is_integer() else repr(x) for x in (lo, hi))


def sources_of(source_id: str) -> list[str]:
    """Register IDs a row cites. `A + B` is one row resting on two sources."""
    return [s.strip() for s in str(source_id).split("+") if s.strip() and s.strip() != "none"]


def check_vocabulary(c: Claim) -> list[str]:
    errors = []
    if not c.claim_id:
        errors.append("claim_id is blank")
    for name in ("statement", "source_id", "method", "role", "confidence"):
        if not getattr(c, name):
            errors.append(f"{c.claim_id or '?'}: missing {name}")
    for name, allowed in (
        ("method", METHODS), ("confidence", CONFIDENCE), ("flag", FLAGS),
        ("role", ROLES), ("part", PARTS), ("division", DIVISIONS), ("audit", AUDIT),
    ):
        got = getattr(c, name)
        if got not in allowed:
            errors.append(f"{c.claim_id}: {name} {got!r} not in {list(allowed)}")
    return errors


def check_method_rules(c: Claim) -> list[str]:
    """The method rules the auditor enforces (architecture p.6)."""
    errors = []
    m, has_value = c.method, bool(c.value)
    if m == "observed" and has_value:
        errors.append(f"{c.claim_id}: observed rows may name a condition and a location, never a number")
    if m == "FIELD" and has_value:
        errors.append(f"{c.claim_id}: FIELD rows have a blank value and say what to measure")
    if m == "scaled" and c.confidence != "scaled":
        errors.append(f"{c.claim_id}: scaled rows carry confidence scaled")
    if m == "fetched":
        if not c.url or not c.retrieved:
            errors.append(f"{c.claim_id}: fetched rows store the URL and the retrieval date")
        if not c.quote and c.flag != "unverified":
            errors.append(f"{c.claim_id}: fetched row without a quote must be flagged unverified")
    if m == "customer" and not c.quote:
        errors.append(f"{c.claim_id}: customer rows carry the instruction verbatim in quote")
    if c.role == "allowance" and m not in ALLOWANCE_OK:
        errors.append(f"{c.claim_id}: a {m} value may not feed an allowance")
    if c.role == "material" and m not in MATERIAL_OK:
        errors.append(f"{c.claim_id}: a {m} value may not feed an order quantity")
    if c.method == "FIELD" and c.confidence != "missing":
        errors.append(f"{c.claim_id}: FIELD rows carry confidence missing")
    return errors


def replay_calc(c: Claim, by_id: dict[str, Claim]) -> list[str]:
    """Code recomputes every derived figure (architecture p.9).

    A `calc` expression references other claims by `{ID}`; after substitution it
    must be plain arithmetic and must equal the row's own value. An input stated
    as a range (a spread rate of 320-400 sq ft/gal) is replayed at both ends, and
    the row's value must then be the range the ends give (written either way
    round). The
    ends bound a bid's formulas, which are sums, products and quotients of
    positive figures, each range input named once; a calc rests on at most
    MAX_RANGES ranges. A row whose input is flagged (unverified or conflict) must
    carry a flag itself.
    """
    if not c.calc:
        return []
    errors, ends = [], {}
    for ref in CALC_REF.findall(c.calc):
        src = by_id.get(ref)
        if src is None:
            return [f"{c.claim_id}: calc references unknown claim {ref}"]
        span = (src.value_num,) if src.value_num is not None else value_range(src.value)
        if span is None:
            return [f"{c.claim_id}: calc input {ref} has no numeric value"]
        if ref in ends:
            if len(span) > 1:
                # The corner replay bounds a formula that uses each range once; a
                # range named twice ({A} * (10 - {A})) can peak between the corners.
                errors.append(f"{c.claim_id}: calc names range input {ref} more than once; write it once (2 * {{{ref}}})")
            continue
        ends[ref] = span
        if src.flag and not c.flag:
            # Arithmetic on a reading nobody has settled is itself unsettled (p.9):
            # the derived row carries a flag, so the bid never shows it as firm.
            errors.append(f"{c.claim_id}: calc input {ref} is flagged {src.flag}; the row must be flagged")
        if c.role == "allowance" and src.method not in ALLOWANCE_OK:
            errors.append(f"{c.claim_id}: calc input {ref} is {src.method}; it cannot feed an allowance")
        elif c.role == "material" and src.method not in MATERIAL_OK:
            errors.append(f"{c.claim_id}: calc input {ref} is {src.method}; it cannot feed an order quantity")
        elif c.method in EXACT_METHODS and src.method not in EXACT_METHODS:
            # A scaled or observed figure never becomes a dimensioned or counted one (p.6).
            errors.append(f"{c.claim_id}: calc input {ref} is {src.method}; a {c.method} row rests on "
                          f"dimensioned and counted rows only")
    if sum(1 for span in ends.values() if len(span) > 1) > MAX_RANGES:
        return errors + [f"{c.claim_id}: calc {c.calc!r} rests on more than {MAX_RANGES} ranges"]
    got = []
    for corner in itertools.product(*ends.values()):
        expr = c.calc
        for ref, v in zip(ends, corner):
            expr = expr.replace("{" + ref + "}", repr(v))
        if not CALC_SAFE.fullmatch(expr):
            return errors + [f"{c.claim_id}: calc {c.calc!r} is not plain arithmetic"]
        try:
            got.append(arith(expr))
        except (ValueError, ZeroDivisionError) as e:
            return errors + [f"{c.claim_id}: calc {c.calc!r} does not evaluate: {e}"]
    lo, hi = min(got), max(got)
    if hi - lo <= 1e-9:
        if c.value_num is None or abs(lo - c.value_num) > 1e-9:
            errors.append(f"{c.claim_id}: calc {c.calc} = {lo}, ledger says {c.value or '(blank)'}")
    else:
        own = value_range(c.value)
        if own is None or abs(own[0] - lo) > 1e-9 or abs(own[1] - hi) > 1e-9:
            errors.append(f"{c.claim_id}: calc {c.calc} = {format_range(lo, hi)}, ledger says {c.value or '(blank)'}")
    return errors


def arith(expr: str) -> float:
    """Evaluate plain arithmetic without eval: numbers, + - * / and parentheses."""
    if len(expr) > CALC_MAX_LEN or not CALC_SAFE.fullmatch(expr):
        raise ValueError("not plain arithmetic")
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as e:
        raise ValueError(f"not plain arithmetic ({e.msg})") from None

    def ev(node):
        if isinstance(node, ast.Expression):
            return ev(node.body)
        if isinstance(node, ast.Constant) and type(node.value) in (int, float):
            return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in _ARITH_OPS:
            return _ARITH_OPS[type(node.op)](ev(node.left), ev(node.right))
        if isinstance(node, ast.UnaryOp) and type(node.op) in _ARITH_OPS:
            return _ARITH_OPS[type(node.op)](ev(node.operand))
        raise ValueError(f"{type(node).__name__} is not allowed in a calc")

    return ev(tree)
