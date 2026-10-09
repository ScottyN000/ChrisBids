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

A number of assemblies comes from the dimension strings (run, spacing, end
offsets), never from the symbols drawn; code compares the two. When a counted
symbol row on the view the dimensions came from (one that counts what the
per-assembly rows are per) disagrees with the number the dimensions give, every item resting on that number is flagged unverified with
both figures in its derivation (determinism: keep both, never pick). An item
resting on a dimension whose runs worded the label differently is flagged the
same way, unless an agreeing symbol row confirms the number, because the model
chose which wording to follow.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from . import schema
from .broker import Broker
from .readers import validate
from .readers.clients import ModelClient, prompt, prompt_version
from .readers.rows import COUNT_IN_LABEL, DIFFERING_LABELS, Unit
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


def assemblies(item: dict, by_id: dict[str, Claim]) -> float | None:
    """The number of assemblies an item's dimension route gives; None when it uses no dimension.

    A `spaces` item gives one more than its value. Any other item gives its
    formula with its one per-assembly row set to 1, so `{PER} * (N)` gives N.
    An item over two per-assembly rows (all fasteners together) is a sum, not
    a count of assemblies, and is not compared.
    """
    refs = schema.CALC_REF.findall(item["calc"])
    if not any(by_id[r].method == "dimensioned" for r in refs):
        return None
    if len({r for r in refs if by_id[r].unit.startswith("per ")}) > 1:
        return None  # a sum of per-assembly products is not a count of assemblies
    if item["unit"] == "spaces":
        return float(item["value"]) + 1
    expr = item["calc"]
    for r in refs:
        c = by_id[r]
        expr = expr.replace("{" + r + "}", "1" if c.unit.startswith("per ") else repr(c.value_num))
    try:
        return schema.arith(expr)
    except (ValueError, ZeroDivisionError, ArithmeticError):
        return None  # a formula that only evaluates with the real per-assembly count gives no number to check


def assembly_names(rows) -> list[str]:
    """What per-assembly rows are per: `3 per bracket` names the bracket (first word only,
    so `per bracket assembly` names it too)."""
    return _join(c.unit[4:].split()[0].lower() for c in rows if c.unit.startswith("per ") and c.unit[4:].split())


def symbol_rows(item: dict, by_id: dict[str, Claim], claims=None) -> list[Claim]:
    """The counted symbol rows that count the item's assemblies: on a view the item's
    dimensions came from, naming what the item's own per-assembly row is per (a pier
    beside the brackets is not one). An item with no per-assembly row (spaces, the
    bracket count) uses the job's per-assembly names that its own dimension rows
    mention, else all of them. When no symbol row on the view names the assembly,
    every symbol count on the view is compared, so a reader's wording can never
    switch the check off. `claims` is every current row, so a symbol count the
    reader runs disagreed on (a conflict row no formula may use) is still compared;
    it defaults to the formula inputs.
    """
    claims = list(by_id.values()) if claims is None else claims
    refs = schema.CALC_REF.findall(item["calc"])
    used = [by_id[r] for r in dict.fromkeys(refs)]
    views = {(c.source_id, c.locator) for c in used if c.method == "dimensioned"}
    names = assembly_names(used)
    if not names:
        # A spaces or assembly-count item names no assembly itself: take the job's
        # assembly names that its own dimension rows mention, else all of them.
        job_names = assembly_names(by_id.values())
        dims = " ".join(c.statement.lower() for c in used if c.method == "dimensioned")
        names = [n for n in job_names if n in dims] or job_names
    on_view = [c for c in claims
               if c.method == "counted" and c.unit == "each" and not c.calc
               and (c.source_id, c.locator) in views]
    named = [c for c in on_view if any(name in c.statement.lower() for name in names)]
    return named or on_view


def symbol_notes(item: dict, by_id: dict[str, Claim], claims=None) -> list[str]:
    """One note per symbol row that disagrees with the number of assemblies the item's
    dimensions give, a row the reader runs disagreed on among them (its readings are
    the value). Both figures stay on the ledger."""
    n = assemblies(item, by_id)
    if n is None:
        return []
    return [
        f"{c.claim_id} counts {c.value} symbols where the dimensions give {_num(n)}"
        for c in symbol_rows(item, by_id, claims) if c.value_num is None or abs(c.value_num - n) > 1e-9
    ]


def label_notes(item: dict, by_id: dict[str, Claim], claims=None) -> list[str]:
    """A note when the item rests on a dimension whose runs worded the label differently,
    since the model then chose which wording to follow, or whose label carries a count
    with no counted row behind it; none for the first when a symbol row on the view
    agrees with the number the dimensions give, which confirms the choice.

    The row says so with `rows.DIFFERING_LABELS` or `rows.COUNT_IN_LABEL` in its
    derivation, the one place a ledger row records what the vote saw; the constants are
    shared, never retyped.
    """
    used = [by_id[r] for r in dict.fromkeys(schema.CALC_REF.findall(item["calc"]))]
    notes = []
    differing = [c.claim_id for c in used if DIFFERING_LABELS in c.derivation]
    if differing and not (assemblies(item, by_id) is not None and symbol_rows(item, by_id, claims)
                          and not symbol_notes(item, by_id, claims)):
        notes.append(f"uses {', '.join(differing)}, whose label the runs word differently")
    counted = [c.claim_id for c in used if COUNT_IN_LABEL in c.derivation]
    if counted:
        notes.append(f"uses {', '.join(counted)}, whose label carries a count with no counted row")
    return notes


def derived_claim(claim_id: str, item: dict, seen: int, runs: int, by_id: dict[str, Claim],
                  claims=None) -> Claim:
    used = [by_id[r] for r in dict.fromkeys(schema.CALC_REF.findall(item["calc"]))]
    notes = []
    if seen < runs:
        notes.append(f"seen in {seen} of {runs} runs")
    shaky = [c.claim_id for c in used if c.flag]
    if shaky:
        notes.append(f"uses flagged {', '.join(shaky)}")
    notes.extend(symbol_notes(item, by_id, claims))
    notes.extend(label_notes(item, by_id, claims))
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
            derived.append(derived_claim(f"{job}-TK-Q-{n:02d}", item, seen, repeats, by_id, claims))
    for claim in derived + field_rows(job, claims):
        try:
            result.rows.append(writer.append(claim))
        except LedgerError as e:
            result.refused.append(f"{claim.claim_id}: {e}")
    return result
