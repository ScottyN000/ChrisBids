"""Materials order rows: the quantity a takeoff figure and a data sheet rate give (architecture p.4, p.17).

The Materials agent's second job, after reading the data sheets (webread.py):
"product data sheet figures (spread rate, yield, pack size), order quantities"
(p.4). It reads ledger rows, never documents or pages. The model's one job is
to say, for each product the job's rows name, which rows the order rests on:
the quantity row it covers (a takeoff area or count, the allowance it goes into,
or the FIELD row that says it will be measured), the number of coats the spec gives, the spec's own
coverage row when there is one (the spec's stated rate takes precedence over
the data sheet, p.17) and the fetched data-sheet row. Code works the quantity
out, `{AREA} * coats / {RATE}`, at both ends of a rate stated as a range, and
writes it as a `fetched` material row citing the sheet, the only method the
access matrix lets Materials write (p.11). A product with no data-sheet row in
the ledger gets no row, and the run says so; a quantity that waits on a FIELD
row, a coat count the spec does not give or a rate nobody stated is written
without a figure, flagged unverified, saying what is missing.

The model is called once per bid, as one stateless unit holding the rows, read
`repeats` times. Items are compared across runs by the rows they rest on; an
item that not every run produced is written flagged unverified, never dropped
or chosen. Scaled and observed rows are never shown (p.6), and a rate, a yield,
a waste factor or a spare count is never a number in a formula: it is a row or
it is nothing.
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

NAME = "materials"
PRINCIPAL = "materials"
# The quantity a product is ordered in, and the word a rate's unit ends with
# that names it ("sq ft/gal", "cu ft per bag", "LF/tube").
UNITS = {"gal": "gal", "bags": "bag", "tubes": "tube", "cartridges": "cartridge", "each": ""}
# Rows a product's quantity may rest on: a takeoff figure, the allowance the
# product covers, or the FIELD row for either (the live run of 2026-10-09 named
# the repaint's FIELD allowance rows, "exterior wall surfaces", and was right to).
QUANTITY_METHODS = ("dimensioned", "counted", "FIELD")
QUANTITY_ROLES = ("quantity", "allowance")
# Rows the model is shown: what the sources say, the figures and the data sheets.
SHOWN_METHODS = ("clause", "customer", "fetched", "dimensioned", "counted", "FIELD")
COATS_MAX = 6
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
            # Coats the spec gives; 0 when it does not say.
            "coats": {"type": "integer", "minimum": 0, "maximum": COATS_MAX},
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


def current(broker: Broker) -> list[Claim]:
    gone = broker.ledger.superseded()
    return [c for c in broker.ledger.claims() if c.claim_id not in gone]


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


def rate_unit(c: Claim | None) -> str:
    """The order unit a rate's unit names: `sq ft/gal` orders gallons, `cu ft per bag` bags."""
    if c is None or figure(c) is None:
        return ""
    tail = re.split(r"\s*(?:/|\bper\b)\s*", c.unit.strip())[-1].strip().lower()
    for unit, word in UNITS.items():
        if word and tail.startswith(word):
            return unit
    return ""


def item_errors(item: dict, by_id: dict[str, Claim], shown: str = "") -> list[str]:
    """Why code will not write this item. Empty means the rows it names bear it out."""
    stray = [n for n in re.findall(r"\d+", item["product"]) if n not in re.findall(r"\d+", shown)]
    if stray:
        return [f"product {item['product']!r} carries {', '.join(stray)}, which is in none of the rows"]
    if not item["sheet"]:
        return ["names no data-sheet row; a product with none gets no order row"]
    named = [item["sheet"], item["quantity"], item["spec_rate"], *item["basis"]]
    unknown = [r for r in named if r and r not in by_id]
    if unknown:
        return [f"names {', '.join(unknown)}, which is not a row it was shown"]
    sheet = by_id[item["sheet"]]
    if sheet.method != "fetched" or not sheet.url:
        return [f"sheet {sheet.claim_id} is not a fetched row with a URL"]
    q = by_id.get(item["quantity"]) if item["quantity"] else None
    if item["quantity"] and (q.method not in QUANTITY_METHODS or q.role not in QUANTITY_ROLES):
        return [f"quantity {q.claim_id} is a {q.method} {q.role} row, not a takeoff figure, an allowance or a FIELD row"]
    spec = by_id.get(item["spec_rate"]) if item["spec_rate"] else None
    if spec is not None and (spec.method != "clause" or figure(spec) is None or not spec.unit):
        return [f"spec_rate {spec.claim_id} is not a clause row stating a rate"]
    rate = spec or (sheet if figure(sheet) is not None and sheet.unit else None)
    if item["unit"] != "each":
        # no rate at all is not refused: the row is written with no figure, flagged, saying so (order_claim)
        if rate is not None and rate_unit(rate) != item["unit"]:
            return [f"orders {item['unit']} but the rate {rate.claim_id} is per {rate.unit!r}"]
    elif spec is not None:
        return ["orders each, so no rate applies"]
    return []


def item_key(item: dict) -> tuple:
    return (item["product"].strip().lower(), item["unit"], item["quantity"], item["coats"], item["spec_rate"],
            item["sheet"])


def vote(runs: list[list[dict]]) -> list[tuple[dict, int]]:
    """(first item, runs that produced it) per distinct order, in first-seen order."""
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


def _fig(c: Claim) -> str:
    return f"{c.value} {c.unit}".strip()


def order_claim(claim_id: str, item: dict, seen: int, runs: int, by_id: dict[str, Claim]) -> Claim:
    """The material row an item gives: a figure when its rows carry one, else
    the formula with what is missing named, flagged unverified."""
    sheet = by_id[item["sheet"]]
    q = by_id.get(item["quantity"]) if item["quantity"] else None
    spec = by_id.get(item["spec_rate"]) if item["spec_rate"] else None
    rate = spec or (sheet if rate_unit(sheet) else None)
    basis = [by_id[r] for r in dict.fromkeys(item["basis"]) if r in by_id]
    coats = item["coats"]
    notes, calc, value, value_num = [], "", "", None
    if seen < runs:
        notes.append(f"seen in {seen} of {runs} runs")
    shaky = [c.claim_id for c in [q, spec, sheet, *basis] if c is not None and c.flag]
    if shaky:
        notes.append(f"uses flagged {', '.join(_join(shaky))}")
    if not sheet.quote:
        # the page was looked at and did not show the product (the repaint's "or equivalent" caulk, 2026-10-09)
        notes.append(f"the page {sheet.claim_id} cites quotes nothing for the product")
    if item["unit"] == "each":
        how = f"{q.claim_id}" if q is not None else "no count row"
        if q is None:
            notes.append("no count row names how many")
        elif q.method == "FIELD":
            notes.append(f"the count waits on {q.claim_id} (FIELD)")
        else:
            calc = f"{{{q.claim_id}}}"
    else:
        how = (f"{q.claim_id if q is not None else 'FIELD'} x {coats or '?'} coat{'s' if coats != 1 else ''}"
               f" / {rate.claim_id}" if rate is not None else "no rate")
        if q is None:
            notes.append("no quantity row names what it covers")
        elif q.method == "FIELD":
            notes.append(f"the quantity waits on {q.claim_id} (FIELD)")
        if not coats:
            notes.append("the spec does not say how many coats")
        if rate is None:
            notes.append("no row states a rate")
        if q is not None and q.method != "FIELD" and coats and rate is not None:
            calc = f"{{{q.claim_id}}} * {coats} / {{{rate.claim_id}}}"
    if calc:
        lo, hi = schema.evaluate(calc, by_id)
        value, value_num = schema.value_of(lo, hi)
        if item["unit"] == "each":
            how = f"{_fig(q)} ({q.claim_id})"
        else:
            shown = calc
            for c in (q, rate):
                shown = shown.replace("{" + c.claim_id + "}", _fig(c))
            how = f"{shown.replace('*', 'x')} = {value} {item['unit']}"
    if spec is not None and rate_unit(sheet):
        notes.append(f"the spec's rate ({spec.claim_id}) governs over the sheet's {_fig(sheet)} (p.17)")
    derivation = "; ".join([how] + notes)
    sources = _join(s for c in [q, *basis, spec] if c is not None for s in schema.sources_of(c.source_id))
    return Claim(
        claim_id=claim_id, statement=f"{item['product'].strip()}: {derivation}",
        source_id=" + ".join(sources + ["WEB"]), locator=" and ".join(_join(c.locator for c in [*basis, spec] if c)),
        tag=" + ".join(_join([*(c.tag for c in basis), sheet.tag])), method="fetched", role="material",
        confidence="exact" if calc else "missing", value=value, value_num=value_num, unit=item["unit"],
        calc=calc, derivation=derivation, division=next((c.division for c in [*basis, q] if c and c.division), ""),
        flag="unverified" if notes else "", url=sheet.url, retrieved=sheet.retrieved, quote=sheet.quote,
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
        claim = order_claim(f"{job}-MT-{n:02d}", item, seen, repeats, by_id)
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
    citing the same page, in the fixture's unit, and with a figure exactly when
    the fixture has one, equal to it. A row that does not is a wrong order and
    fails the gate. A fixture order with no run row is a miss: safe, since the
    bid then has no figure for it, but incomplete, so at least ANSWERED of the
    compared orders must have one. A fixture order that cites no page is noted
    and left out: Materials writes fetched rows only (p.11)."""
    by_id = {r["id"]: r for r in fixture_rows}
    failures, notes, misses, compared = [], [], [], 0
    for f in fixture_rows:
        if f.get("role") != "material":
            continue
        urls = _join(by_id[c].get("url", "") for c in CITED.findall(f"{f.get('tag', '')} {f.get('statement', '')}")
                     if c in by_id)
        if not urls:
            notes.append(f"{f['id']}: cites no fetched page; not compared")
            continue
        compared += 1
        got = [c for c in result.rows if c.url in urls]
        if not got:
            misses.append(f"{f['id']}: no order row cites {urls[0]}")
            continue
        unit = f.get("unit", "") or ""
        same_unit = [c for c in got if c.unit == unit]
        if not same_unit:
            # the same page can cover another product (the adhesive's cartridges beside the anchors it sets):
            # a row in another unit is not this order, and not a wrong one either
            misses.append(f"{f['id']}: no order row cites {urls[0]} in {unit} (the run orders "
                          f"{', '.join(_join(c.unit for c in got))} from it)")
            continue
        want = schema.format_value(f.get("value"))[0]
        if not want and all(c.value for c in same_unit):
            failures.append(f"{f['id']}: gives {same_unit[0].value} {unit} where the fixture waits on a FIELD measure")
        elif want and not any(c.value == want for c in same_unit):
            failures.append(f"{f['id']}: gives {', '.join(_join(c.value or '(blank)' for c in same_unit))} {unit}, "
                            f"the fixture {want}")
    ok = not failures and compared > 0 and compared - len(misses) >= ANSWERED * compared
    return Gate(ok=ok, compared=compared, failures=failures, notes=notes, misses=misses)
