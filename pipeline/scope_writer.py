"""Scope Writer: the proposal, selected by a model and rendered by code (architecture p.4, p.10, p.13).

The Scope Writer reads the claim ledger and the Mersco phrase library and
nothing else: no document, no price (p.11). Its model's one job is to select:
which rows and which library phrases go in which division, grouped into tasks,
with which allowance rows and which exclusion and terms phrases. It composes no
prose (p.10, "Rendered, not written"). The only words it supplies are task
titles, which code refuses if they carry a digit, so no figure can enter the
proposal except through a ledger row or a library phrase.

Code checks every layout against the ledger and the library and discards one
that breaks a rule. The layout is read `repeats` times; the runs must agree on
what goes where (order and titles aside), or nothing is rendered: a choice
between two layouts is Chris's, not code's. The agreed layout is rendered by
`proposal.render`, the same code that renders the golden fixtures, and the
Auditor's orphan check runs on the result.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from . import fixtures, proposal
from .auditor import Orphan, audit_proposal
from .broker import Broker
from .readers import validate
from .readers.clients import ModelClient, ReplayClient, prompt, prompt_version
from .readers.rows import Unit
from .schema import Claim

NAME = "scope_writer"
PRINCIPAL = "scope_writer"

# Section titles, as the Proposal Format Example and the two test bids write
# them. A division with no title here cannot be placed until Chris's template
# names it; its rows are reported unplaced, never dropped silently.
DIVISION_TITLES = {
    "01": "Division 01 General Conditions",
    "02": "Division 02 Site Construction",
    "03": "Division 03 Concrete",
    "05": "Division 05 Metals",
    "07": "Division 07 Thermal & Moisture Protection",
    "09": "Division 09 Finishes",
}
ALTERNATES = "ALT"
SECTION_TITLES = {**DIVISION_TITLES, ALTERNATES: "Alternates"}
SECTION_KEY = {v: k for k, v in SECTION_TITLES.items()}
# General Conditions is a numbered list in the Format Example; every other
# division is bulleted. The alternates section opens with the library's intro.
NUMBERED = ("01",)
SECTION_INTRO = {ALTERNATES: "alternates_intro"}

# Rows a task may list. Header, exclusion, question and material rows have
# sections of their own that code fills; an allowance goes in a task's allowance.
# A reader writes drawing notes as `note` rows, so a note may describe a task.
ITEM_ROLES = ("scope", "quantity", "code", "note")
# Library phrases code places itself, so the model never lists them.
RESERVED_PHRASES = ("greeting", "contractor_block", "license_line", "allowance", "alternates_intro")
TERMS_PHRASES = ("change_orders", "costs", "warranty", "allowance_definition")
EXCLUSION_PREFIX = "excl_"
CLOSE_PREFIX = "concealed_"

TITLE_PATTERN = r"^[A-Za-z ,&/'()-]*$"
ID_PATTERN = r"^[A-Za-z0-9_-]+$"

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["header", "sections", "exclusion_phrases", "terms"],
    "properties": {
        "header": {
            "type": "object", "additionalProperties": False,
            "required": ["project", "address", "client"],
            # "" when the ledger has no row for the slot; the proposal then says FIELD.
            "properties": {k: {"type": "string", "maxLength": 40, "pattern": r"^[A-Za-z0-9_-]*$"}
                           for k in ("project", "address", "client")},
        },
        "sections": {"type": "array", "maxItems": 12, "items": {
            "type": "object", "additionalProperties": False,
            "required": ["division", "tasks"],
            "properties": {
                "division": {"enum": list(SECTION_TITLES)},
                "tasks": {"type": "array", "maxItems": 20, "items": {
                    "type": "object", "additionalProperties": False,
                    "required": ["title", "items", "allowance", "close"],
                    "properties": {
                        "title": {"type": "string", "maxLength": 60, "pattern": TITLE_PATTERN},
                        "items": {"type": "array", "maxItems": 40, "items": {
                            "type": "object", "additionalProperties": False,
                            "required": ["kind", "ref"],
                            "properties": {
                                "kind": {"enum": ["row", "phrase"]},
                                "ref": {"type": "string", "maxLength": 40, "pattern": ID_PATTERN},
                            },
                        }},
                        "allowance": {"type": "array", "maxItems": 10, "items": {
                            "type": "string", "maxLength": 40, "pattern": ID_PATTERN}},
                        "close": {"type": "string", "maxLength": 40, "pattern": r"^[A-Za-z0-9_-]*$"},
                    },
                }},
            },
        }},
        "exclusion_phrases": {"type": "array", "maxItems": 30, "items": {
            "type": "string", "maxLength": 40, "pattern": ID_PATTERN}},
        "terms": {"type": "array", "maxItems": 10, "items": {
            "type": "string", "maxLength": 40, "pattern": ID_PATTERN}},
    },
}


@dataclass
class ScopeResult:
    reader: str = NAME
    units: int = 0
    calls: int = 0
    discarded: list[str] = field(default_factory=list)
    unread: list[str] = field(default_factory=list)
    layout: dict | None = None          # the agreed layout, in the fixture's `proposal:` shape
    text: str = ""                      # the rendered proposal
    xref: list[dict] = field(default_factory=list)
    unplaced: list[str] = field(default_factory=list)
    orphans: list[Orphan] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.layout is not None and not self.orphans

    def report(self) -> str:
        lines = [f"{self.reader}: {self.units} units, {self.calls} calls, "
                 f"{'layout agreed' if self.layout else 'no layout'}, {len(self.discarded)} runs discarded, "
                 f"{len(self.unplaced)} rows unplaced, {len(self.orphans)} orphan figures"]
        lines += [f"  discarded {d}" for d in self.discarded]
        lines += [f"  unread {u}" for u in self.unread]
        lines += [f"  unplaced {u}" for u in self.unplaced]
        lines += [f"  orphan {o.figure} (line {o.line}): {o.context}" for o in self.orphans]
        return "\n".join(lines)


def load_phrases(path: Path) -> dict[str, dict]:
    return yaml.safe_load(Path(path).read_text())["phrases"]


def current(broker: Broker) -> list[Claim]:
    gone = broker.ledger.superseded()
    return [c for c in broker.ledger.claims() if c.claim_id not in gone]


def _short(text: str, n: int = 160) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= n else text[: n - 3] + "..."


def selectable_phrases(phrases: dict[str, dict]) -> dict[str, dict]:
    return {k: v for k, v in phrases.items() if k not in RESERVED_PHRASES}


def unit_for(job: str, claims: list[Claim], phrases: dict[str, dict]) -> Unit:
    """The one unit the model is shown: the ledger rows and the phrase library as tables.

    A row shows what it is and where it belongs, never its figure: the model
    places a row, and code prints the row's own statement and value.
    """
    rows = "\n".join(
        f"{c.claim_id} | {c.role} | {c.division or '-'} | {c.part or '-'} | {c.method} | {_short(c.statement)}"
        for c in claims
    )
    lib = "\n".join(f"{k} | {_short(v['text'])}" for k, v in selectable_phrases(phrases).items())
    text = (f"ROWS (ID | role | division | part | method | statement)\n{rows}\n\n"
            f"PHRASES (key | text)\n{lib}")
    return Unit(unit_id=f"{job}#scope", source_id="", locator="ledger rows and phrase library", tag="", text=text)


def _section_errors(c: Claim, div: str, where: str) -> list[str]:
    """A row goes in its own division, or under Alternates; an alternate row only there."""
    if c.part == "alternate" and div != ALTERNATES:
        return [f"{where}: {c.claim_id} is alternate work and goes under Alternates"]
    if c.part == "base" and div == ALTERNATES:
        return [f"{where}: {c.claim_id} is base-bid work, not an alternate"]
    if c.division and div != ALTERNATES and c.division != div:
        return [f"{where}: {c.claim_id} belongs to division {c.division}"]
    return []


def layout_errors(layout: dict, by_id: dict[str, Claim], phrases: dict[str, dict]) -> list[str]:
    """Why code will not render this layout. Empty means every reference is to a
    ledger row or library phrase of the right kind, in a section it belongs in."""
    errs = []
    h = layout["header"]
    for slot in ("project", "address", "client"):
        if not h[slot]:
            continue
        c = by_id.get(h[slot])
        if c is None or c.role != "header":
            errs.append(f"header {slot} {h[slot]!r} is not a header row")
    seen_sections = set()
    for sec in layout["sections"]:
        div = sec["division"]
        if div in seen_sections:
            errs.append(f"section {div} appears twice")
        seen_sections.add(div)
        for t, task in enumerate(sec["tasks"], 1):
            where = f"{div} task {t}"
            refs = [(it["kind"], it["ref"]) for it in task["items"]]
            if len(set(refs)) < len(refs):
                errs.append(f"{where} lists the same item twice")
            for kind, ref in refs:
                if kind == "phrase":
                    if ref not in phrases or ref in RESERVED_PHRASES or ref in TERMS_PHRASES \
                            or ref.startswith((EXCLUSION_PREFIX, CLOSE_PREFIX)):
                        errs.append(f"{where}: {ref!r} is not a scope phrase in the library")
                    continue
                c = by_id.get(ref)
                if c is None:
                    errs.append(f"{where}: {ref!r} is not a ledger row")
                elif c.role not in ITEM_ROLES:
                    errs.append(f"{where}: {ref} is a {c.role} row, which has its own section")
                else:
                    errs += _section_errors(c, div, where)
            for ref in task["allowance"]:
                c = by_id.get(ref)
                if c is None or c.role != "allowance":
                    errs.append(f"{where}: {ref!r} is not an allowance row")
                else:
                    errs += _section_errors(c, div, where)
            if len(set(task["allowance"])) < len(task["allowance"]):
                errs.append(f"{where} lists the same allowance twice")
            if task["close"] and (task["close"] not in phrases or not task["close"].startswith(CLOSE_PREFIX)):
                errs.append(f"{where}: close {task['close']!r} is not a concealed-conditions phrase")
            if not task["items"] and not task["allowance"]:
                errs.append(f"{where} is empty")
    for key in layout["exclusion_phrases"]:
        if key not in phrases or not key.startswith(EXCLUSION_PREFIX):
            errs.append(f"exclusion {key!r} is not an exclusion phrase in the library")
    for key in layout["terms"]:
        if key not in TERMS_PHRASES:
            errs.append(f"terms {key!r} is not a terms phrase")
    for name, keys in (("exclusion", layout["exclusion_phrases"]), ("terms", layout["terms"])):
        if len(set(keys)) < len(keys):
            errs.append(f"an {name} phrase is listed twice")
    return errs


def canonical(layout: dict) -> tuple:
    """What a layout says, order and titles aside: what is placed in which section."""
    placed, allowances, closes = set(), set(), set()
    for sec in layout["sections"]:
        div = sec["division"]
        for task in sec["tasks"]:
            placed |= {(div, it["kind"], it["ref"]) for it in task["items"]}
            allowances |= {(div, a) for a in task["allowance"]}
            if task["close"]:
                closes.add((div, task["close"]))
    h = layout["header"]
    return ((h["project"], h["address"], h["client"]), frozenset(placed), frozenset(allowances),
            frozenset(closes), frozenset(layout["exclusion_phrases"]), frozenset(layout["terms"]))


def differences(a: dict, b: dict) -> list[str]:
    """What two layouts place differently, in words."""
    names = ("header", "items", "allowances", "concealed-conditions closes", "exclusions", "terms")
    out = []
    for name, x, y in zip(names, canonical(a), canonical(b)):
        if x == y:
            continue
        if isinstance(x, tuple):
            out.append(f"{name}: {x} vs {y}")
            continue
        only_a, only_b = sorted(x - y), sorted(y - x)
        out.append(f"{name}: only in the first {only_a or 'nothing'}; only in the second {only_b or 'nothing'}")
    return out


def to_fixture_layout(layout: dict) -> dict:
    """The model's layout in the fixture's `proposal:` shape, with titles numbered by code."""
    sections = []
    for sec in sorted(layout["sections"], key=lambda s: (s["division"] == ALTERNATES, s["division"])):
        div = sec["division"]
        out = {"title": SECTION_TITLES[div]}
        if div in NUMBERED:
            out["numbered"] = True
        if div in SECTION_INTRO:
            out["intro_phrase"] = SECTION_INTRO[div]
        tasks = []
        for n, task in enumerate(sec["tasks"], 1):
            t = {}
            if task["title"].strip():
                number = f"A.{n}" if div == ALTERNATES else f"{int(div)}.{n}"
                t["title"] = f"{number} {task['title'].strip()}"
            t["items"] = [{it["kind"]: it["ref"]} for it in task["items"]]
            if task["allowance"]:
                t["allowance"] = list(task["allowance"])
            if task["close"]:
                t["close"] = task["close"]
            tasks.append(t)
        out["tasks"] = tasks
        sections.append(out)
    return {"header": dict(layout["header"]), "sections": sections,
            "exclusion_phrases": list(layout["exclusion_phrases"]), "terms": list(layout["terms"])}


def from_fixture_layout(fixture: dict) -> dict:
    """A fixture's hand-made `proposal:` layout in the model's output shape (for recordings and tests)."""
    sections = []
    for sec in fixture["sections"]:
        div = SECTION_KEY[sec["title"]]
        tasks = []
        for task in sec.get("tasks", []):
            title = re.sub(r"^[A-Z0-9]+\.\d+\s+", "", task.get("title", ""))
            items = [{"kind": "row" if "row" in it else "phrase", "ref": it.get("row") or it.get("phrase")}
                     for it in task["items"]]
            tasks.append({"title": title, "items": items, "allowance": list(task.get("allowance", [])),
                          "close": task.get("close", "")})
        sections.append({"division": div, "tasks": tasks})
    return {"header": dict(fixture["header"]), "sections": sections,
            "exclusion_phrases": list(fixture.get("exclusion_phrases", [])), "terms": list(fixture.get("terms", []))}


def unplaced(layout: dict, claims: list[Claim]) -> list[str]:
    """Scope and allowance rows the layout leaves out of the proposal."""
    placed = {it["ref"] for sec in layout["sections"] for t in sec["tasks"] for it in t["items"] if it["kind"] == "row"}
    placed |= {a for sec in layout["sections"] for t in sec["tasks"] for a in t["allowance"]}
    return [f"{c.claim_id} ({c.role}, division {c.division or '-'})" for c in claims
            if c.role in ("scope", "allowance") and c.claim_id not in placed]


def render(layout: dict, claims: list[Claim], phrases: dict[str, dict], broker: Broker) -> tuple[str, list[dict]]:
    rows = [proposal.row_of(c) for c in claims]
    data = {
        "proposal": to_fixture_layout(layout),
        "rows": rows,
        "status_line": broker.ledger.meta("status_line") or "Draft rendered from the claim ledger. Not priced. "
                                                             "Not released: only Chris releases a bid.",
        "codes_note": broker.ledger.meta("codes_note"),
    }
    return proposal.render(data, {r["id"]: r for r in rows}, phrases, broker.ledger.register())


def run(broker: Broker, job: str, client: ModelClient, phrase_library: Path, *, repeats: int = 2) -> ScopeResult:
    claims = current(broker)
    phrases = load_phrases(phrase_library)
    writer = broker.as_principal(PRINCIPAL, model_id=client.model_id, prompt_version=prompt_version(NAME))
    by_id = {c.claim_id: c for c in claims}
    unit = unit_for(job, claims, phrases)
    system = prompt(NAME)
    result = ScopeResult(units=1)
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
            if not errs:
                errs = layout_errors(data, by_id, phrases)
        writer.log_call(unit.unit_id, f"run {r + 1}; {'discarded: ' + errs[0] if errs else 'valid'}")
        if errs:
            result.discarded.append(f"{unit.unit_id} run {r + 1}: {'; '.join(errs[:3])}")
        else:
            valid.append(data)
    # The redundancy rule: every run asked for must be valid and say the same thing.
    if len(valid) < repeats:
        result.unread.append(f"{unit.unit_id}: {len(valid)} of {repeats} runs valid")
        return result
    split = next((differences(valid[0], v) for v in valid[1:] if canonical(v) != canonical(valid[0])), None)
    if split:
        result.unread.append(f"{unit.unit_id}: the runs place rows differently; " + "; ".join(split[:4]))
        return result
    result.layout = to_fixture_layout(valid[0])
    result.text, result.xref = render(valid[0], claims, phrases, broker)
    result.unplaced = unplaced(valid[0], claims)
    result.orphans = audit_proposal(result.text, claims, Path(phrase_library).read_text())
    return result


def sections_of(layout: dict) -> dict[str, set[str]]:
    """Row ID -> the sections it is placed in (items and allowances alike)."""
    out: dict[str, set[str]] = defaultdict(set)
    for sec in layout["sections"]:
        for task in sec["tasks"]:
            refs = [it["ref"] for it in task["items"] if it["kind"] == "row"] + list(task["allowance"])
            for ref in refs:
                out[ref].add(sec["division"])
    return out


@dataclass
class Gate:
    ok: bool
    failures: list[str]
    notes: list[str]

    def text(self) -> str:
        lines = [f"scope writer gate: {'PASS' if self.ok else 'FAIL'}"]
        lines += [f"  FAIL {f}" for f in self.failures]
        lines += [f"  note {n}" for n in self.notes]
        return "\n".join(lines)


def gate(result: ScopeResult, fixture_layout: dict, claims: list[Claim]) -> Gate:
    """The golden test for the Scope Writer, on a fixture's own ledger.

    Gated: the header rows, and every scope and allowance row placed in a
    section the fixture's hand-made layout puts it in (a base row may also sit
    in its own division), with nothing unplaced and no orphan figure. Reported
    only: which phrases, closes, exclusions and division-less rows (quantities,
    code rows) the model chose, since the fixture's choice there is one
    reasonable layout among several.
    """
    if result.layout is None:
        return Gate(False, ["no layout was agreed"] + result.unread + result.discarded, [])
    got, want = from_fixture_layout(result.layout), from_fixture_layout(fixture_layout)
    failures, notes = [], []
    if got["header"] != want["header"]:
        failures.append(f"header {got['header']} != {want['header']}")
    have, expect = sections_of(got), sections_of(want)
    for c in claims:
        if c.role not in ("scope", "allowance"):
            continue
        allowed = set(expect[c.claim_id])
        if c.part != "alternate" and c.division:
            allowed.add(c.division)
        placed = have[c.claim_id]
        if not placed:
            failures.append(f"{c.claim_id} is not placed (the fixture has it in {sorted(expect[c.claim_id])})")
        elif not placed <= allowed:
            failures.append(f"{c.claim_id} is in {sorted(placed)}; the fixture allows {sorted(allowed)}")
    failures += [f"orphan figure {o.figure} (line {o.line})" for o in result.orphans]
    gated = {c.claim_id for c in claims if c.role in ("scope", "allowance")}
    for cid in sorted((set(have) | set(expect)) - gated):
        if have[cid] != expect[cid]:
            notes.append(f"{cid} in {sorted(have[cid])}, fixture {sorted(expect[cid])}")
    a, b = canonical(got), canonical(want)
    phrases_got = {x for x in a[1] if x[1] == "phrase"}
    phrases_want = {x for x in b[1] if x[1] == "phrase"}
    if phrases_got != phrases_want:
        notes.append(f"phrases only in the run {sorted(phrases_got - phrases_want)}; "
                     f"only in the fixture {sorted(phrases_want - phrases_got)}")
    for name, i in (("closes", 3), ("exclusions", 4), ("terms", 5)):
        if a[i] != b[i]:
            notes.append(f"{name} only in the run {sorted(a[i] - b[i])}; only in the fixture {sorted(b[i] - a[i])}")
    return Gate(not failures, failures, notes)


def write(result: ScopeResult, out: Path) -> None:
    """proposal.md and xref.csv, regenerated on every run and never hand-edited (p.11)."""
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    if result.text:
        (out / "proposal.md").write_text(result.text)
        (out / "xref.csv").write_text(proposal.xref_csv(result.xref))


def golden(job_dir: Path, ledger_path: Path, client: ModelClient | None = None, *, repeats: int = 2):
    """Load a fixture's ledger, lay out its proposal and gate it against the fixture's layout.

    Replay answers from `<fixture>/recordings/scope_writer.json`; a live client
    makes this the deployment gate for a Scope Writer prompt or model change.
    Returns (broker, ScopeResult, Gate).
    """
    job_dir = Path(job_dir)
    if Path(ledger_path).exists():
        Path(ledger_path).unlink()
    broker, data = fixtures.load(job_dir, Path(ledger_path))
    if hasattr(client, "bind"):
        try:
            client.bind(broker)   # a live client takes its key from the broker
        except Exception:
            broker.close()
            raise
    client = client or ReplayClient(job_dir / "recordings", model_id="replay (recorded expected output)")
    result = run(broker, data["job"], client, fixtures.PHRASE_LIBRARY, repeats=repeats)
    return broker, result, gate(result, data["proposal"], current(broker))
