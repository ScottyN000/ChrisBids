"""Takeoff: quantities with derivation strings, and FIELD rows (architecture p.4, p.16 phase 3).

Takeoff reads ledger rows, never documents. The model's one job is to write a
formula over the readers' dimensioned and counted rows ("{A} / {B} + 1"); code
evaluates every formula and discards an item whose stated value it does not
reproduce (p.13: "code then evaluates the derivation and must get the same
number"). Scaled and observed rows are never shown to the model, so they cannot
feed a quantity; code turns each one into a FIELD row that says what to
measure, which is also what a job with no drawings gets (p.16: "Takeoff emits a
takeoff checklist instead of allowances").

The model is called only when there is something to derive from, as one
stateless unit holding the rows, read `repeats` times. Items are compared
across runs by what they compute (value, unit and the rows used); an item that
not every run produced is written flagged unverified, never dropped or chosen.
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
from .schema import Claim, LedgerError

NAME = "takeoff"
PRINCIPAL = "takeoff"
# Units a derived quantity may carry. A derived length, area or volume is not
# written yet: the access matrix lets Takeoff write counted, scaled and FIELD
# (p.11), and a derived length is none of those.
UNITS = ("each", "spaces")
# Methods a formula may use as input. Scaled and observed rows never feed a
# quantity (p.6); a FIELD row has no value to feed.
INPUT_METHODS = ("dimensioned", "counted")
CALC_PATTERN = r"^[{}A-Z0-9 +\-*/().]+$"

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["items"],
    "properties": {"items": {"type": "array", "maxItems": 30, "items": {
        "type": "object",
        "additionalProperties": False,
        "required": ["label", "calc", "value", "unit"],
        "properties": {
            "label": {"type": "string", "maxLength": 200},
            # A formula over {ID} references, + - * / and parentheses.
            "calc": {"type": "string", "maxLength": 200, "pattern": CALC_PATTERN},
            "value": {"type": "integer", "minimum": 0},
            "unit": {"enum": list(UNITS)},
        },
    }}},
}

# A plain number in a formula: only whole numbers (two ends, one extra bracket).
# A decimal would be a rate or a yield, which only a fetched data sheet can give.
_CONSTANT = re.compile(r"(?<![\w{-])\d+(\.\d+)?(?![\w}])")
_PHOTO = re.compile(r"^condition (\w+) at (\w+)")


@dataclass
class TakeoffResult:
    reader: str = NAME
    units: int = 0
    calls: int = 0
    discarded: list[str] = field(default_factory=list)
    unread: list[str] = field(default_factory=list)
    rows: list[Claim] = field(default_factory=list)
    refused: list[str] = field(default_factory=list)

    def text(self) -> str:
        derived = sum(1 for r in self.rows if r.calc)
        lines = [f"{self.reader}: {self.units} units, {self.calls} calls, {derived} derived quantities, "
                 f"{len(self.rows) - derived} FIELD rows, {len(self.discarded)} items discarded, "
                 f"{len(self.unread)} units unread"]
        lines += [f"  discarded {d}" for d in self.discarded]
        lines += [f"  unread {u}" for u in self.unread]
        lines += [f"  refused {r}" for r in self.refused]
        return "\n".join(lines)


def current(broker: Broker) -> list[Claim]:
    """The rows in force: everything not superseded, in ledger order."""
    gone = broker.ledger.superseded()
    return [c for c in broker.ledger.claims() if c.claim_id not in gone]


def inputs(claims: list[Claim]) -> list[Claim]:
    """The rows a formula may use: a single dimensioned or counted figure that
    no run disagreed on, written by a reader rather than by Takeoff itself."""
    return [c for c in claims
            if c.method in INPUT_METHODS and c.value_num is not None and c.flag != "conflict"
            and not c.calc]


def unit_for(job: str, rows: list[Claim]) -> Unit:
    """The one unit the model is shown: the input rows as a table."""
    text = "\n".join(
        f"{c.claim_id} | {c.method} | {c.value} {c.unit} | {c.statement} | {c.tag or c.locator}"
        + (f" | {c.flag}" if c.flag else "")
        for c in rows
    )
    return Unit(unit_id=f"{job}#takeoff", source_id="", locator="ledger rows", tag="", text=text)


def item_errors(item: dict, by_id: dict[str, Claim], shown: str = "") -> list[str]:
    """Why code will not write this item. Empty means its formula reproduces its value.

    `shown` is the text the model was given. A number in the label that is not
    in it would reach the ledger with nothing behind it, so the item is dropped.
    """
    stray = [n for n in re.findall(r"\d+", item["label"]) if n not in re.findall(r"\d+", shown)]
    if stray:
        return [f"label {item['label']!r} carries {', '.join(stray)}, which is in none of the rows"]
    calc = item["calc"]
    refs = schema.CALC_REF.findall(calc)
    if not refs:
        return [f"{calc!r} uses no row"]
    unknown = [r for r in refs if r not in by_id]
    if unknown:
        return [f"{calc!r} uses {', '.join(unknown)}, which is not a row it was shown"]
    rest = schema.CALC_REF.sub("", calc)
    decimals = [m.group(0) for m in _CONSTANT.finditer(rest) if m.group(1)]
    if decimals:
        return [f"{calc!r} carries {', '.join(decimals)}; a formula's own numbers are whole"]
    expr = calc
    for r in refs:
        expr = expr.replace("{" + r + "}", repr(by_id[r].value_num))
    try:
        got = schema.arith(expr)
    except (ValueError, ZeroDivisionError) as e:
        return [f"{calc!r} does not evaluate: {e}"]
    if abs(got - item["value"]) > 1e-9:
        return [f"{calc!r} = {_num(got)}, the item says {item['value']}"]
    return []


def _num(x: float) -> str:
    return str(int(x)) if float(x).is_integer() else repr(x)


def derivation(calc: str, by_id: dict[str, Claim]) -> str:
    """The formula with each row's figure in place, as a reviewer replays it."""
    shown = calc
    for r in schema.CALC_REF.findall(calc):
        shown = shown.replace("{" + r + "}", by_id[r].value)
    return re.sub(r"\s+", " ", shown.replace("*", " x ")).strip()


def item_key(item: dict) -> tuple:
    return (item["value"], item["unit"], tuple(sorted(set(schema.CALC_REF.findall(item["calc"])))))


def vote(runs: list[list[dict]]) -> list[tuple[dict, int]]:
    """(first item, runs that produced it) per distinct quantity, in first-seen order."""
    order, seen = [], {}
    for items in runs:
        this_run = set()
        for it in items:
            k = item_key(it)
            if k in this_run:
                continue
            this_run.add(k)
            if k not in seen:
                seen[k] = [it, 0]
                order.append(k)
            seen[k][1] += 1
    return [(seen[k][0], seen[k][1]) for k in order]


def _join(values) -> list[str]:
    out = []
    for v in values:
        if v and v not in out:
            out.append(v)
    return out


def derived_claim(claim_id: str, item: dict, seen: int, runs: int, by_id: dict[str, Claim]) -> Claim:
    used = [by_id[r] for r in dict.fromkeys(schema.CALC_REF.findall(item["calc"]))]
    notes = []
    if seen < runs:
        notes.append(f"seen in {seen} of {runs} runs")
    shaky = [c.claim_id for c in used if c.flag]
    if shaky:
        notes.append(f"uses flagged {', '.join(shaky)}")
    shown = derivation(item["calc"], by_id)
    sources = _join(s for c in used for s in schema.sources_of(c.source_id))
    return Claim(
        claim_id=claim_id, statement=f"{item['label'].strip()}: {shown} = {item['value']}",
        source_id=" + ".join(sources), locator=" and ".join(_join(c.locator for c in used)),
        tag=" + ".join(_join(c.tag for c in used)), method="counted", role="quantity", confidence="exact",
        value=str(item["value"]), value_num=float(item["value"]), unit=item["unit"], calc=item["calc"],
        derivation="; ".join([f"{shown} = {item['value']}"] + notes),
        flag="unverified" if notes else "",
    )


def field_rows(job: str, claims: list[Claim]) -> list[Claim]:
    """A FIELD row for every scaled length and every photo condition, in code.

    Neither carries a number a bid may use: a scaled length is never an order
    quantity until it is checked on site, and a photo shows where, not how much.
    The FIELD row names the row it comes from and repeats none of its readings.
    """
    out = []
    for c in claims:
        if c.method == "scaled":
            what = re.sub(r",\s*scaled$", "", c.statement)
            statement = (f"{what}: measure on site or get the dimension from the engineer; "
                         f"the scaled reading in {c.claim_id} is not a quantity")
        elif c.method == "observed":
            m = _PHOTO.match(c.derivation)
            if not m or m.group(1) == "none":
                continue
            statement = (f"Extent of {m.group(1)} at the {m.group(2)} seen in {c.source_id}: "
                         f"measure on site ({c.claim_id})")
        else:
            continue
        out.append(Claim(
            claim_id=f"{job}-TK-F-{len(out) + 1:02d}", statement=statement, source_id=c.source_id,
            locator=c.locator, tag=c.tag, method="FIELD", role="quantity", confidence="missing",
            derivation=f"from {c.claim_id}",
        ))
    return out


def run(broker: Broker, job: str, client: ModelClient | None, *, repeats: int = 2) -> TakeoffResult:
    claims = current(broker)
    rows = inputs(claims)
    writer = broker.as_principal(
        PRINCIPAL, model_id=client.model_id if client and rows else "none (code only)",
        prompt_version=prompt_version(NAME), agent_label="takeoff",
    )
    result = TakeoffResult()
    derived: list[Claim] = []
    if rows and client is not None:
        result.units = 1
        unit = unit_for(job, rows)
        by_id = {c.claim_id: c for c in rows}
        system = prompt(NAME)
        valid = []
        for r in range(repeats):
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
                why = item_errors(item, by_id, unit.text)
                if why:
                    result.discarded.append(f"{unit.unit_id} run {r + 1}: items[{i}] {why[0]}")
                else:
                    kept.append(item)
            valid.append(kept)
        if not valid:
            result.unread.append(unit.unit_id)
        for n, (item, seen) in enumerate(vote(valid), 1):
            # Fewer valid runs than asked is less agreement than the redundancy rule wants.
            derived.append(derived_claim(f"{job}-TK-Q-{n:02d}", item, seen, repeats, by_id))
    for claim in derived + field_rows(job, claims):
        try:
            result.rows.append(writer.append(claim))
        except LedgerError as e:
            result.refused.append(f"{claim.claim_id}: {e}")
    return result


# ---- the golden comparison ----------------------------------------------------

def leaves(c: Claim, by_id: dict[str, Claim], key, _depth: int = 0) -> tuple:
    """The reader figures a derived row rests on, by what a human would check."""
    if not c.calc or _depth > 20:
        return (key(c),)
    out = set()
    for r in schema.CALC_REF.findall(c.calc):
        if r in by_id:
            out.update(leaves(by_id[r], by_id, key, _depth + 1))
    return tuple(sorted(out))


def derived_key(c: Claim, by_id: dict[str, Claim], key) -> tuple:
    return (c.method, "derived", c.value, c.unit, leaves(c, by_id, key))
