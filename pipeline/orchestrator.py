"""Orchestrator: the work plan for a bid, and the run that carries it out (architecture p.2-3, p.12).

The Orchestrator reads the Source Register and nothing else: no document
content, no ledger row (p.12, "does not see document content, only the Source
Register"). From it, it plans which agents run and on which units: every page
of a spec, every photo, every message, and each drawing sheet in grid tiles. A
source no reader can open yet, or one that is missing, is listed with the
reason, so the plan accounts for every row of the register.

The plan is code, not a model call. The architecture puts the Orchestrator on
Sonnet (p.13), but with the register in hand the plan is a lookup by document
kind, and a lookup in code gives the same plan every time and costs nothing.
A model earns its place here once Intake tags divisions and a plan has choices
to make (one Takeoff and Materials pass per division, p.7).

`bid` carries the plan out: Intake, the readers, Takeoff, Codes & Regs and
Materials (when the run has network), the Scope Writer and the Auditor, each
through its own principal, with every result written to the
run folder. The Orchestrator itself writes no row.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import auditor, fixtures, intake, scope_writer, takeoff, web, webread
from .broker import Broker
from .readers import live, run as reader_run
from .readers import tiles
from .readers.clients import ModelClient

PRINCIPAL = "orchestrator"
STATUS_LINE = "Draft rendered from the claim ledger. Not priced. Not released: only the estimator releases a bid."

# Which reader opens which kind of source. Intake writes "correspondence";
# the fixtures' registers call a missing email a "message".
READER_FOR_KIND = {
    "drawing": "drawing",
    "spec": "spec",
    "photo": "photo",
    "correspondence": "correspondence",
    "message": "correspondence",
}
# Kinds no reader opens, and why. Each still appears in the plan.
NOT_READ = {
    "proposal-template": "the Contractor Co. template; its paragraphs are the phrase library, kept by hand",
    "prior-bid": "a past bid is a regression fixture, not a source for this bid",
    "design": "a design document about the pipeline, not about the job",
    "spreadsheet": "no reader for spreadsheets yet",
    "unknown": "Intake could not tell what it is; a person must classify it",
    "fetched": "the pages Codes & Regs and Materials fetch; each row carries its own URL",
}
READERS = ("drawing", "spec", "photo", "correspondence")
# A drawing sheet with no hand-drawn view boxes is read in a fixed grid
# (readers/tiles.py); 3 x 2 keeps a 24 x 36 sheet's tiles near a letter page.
DRAWING_GRID = (3, 2)


@dataclass
class Step:
    agent: str
    run: bool
    reason: str
    units: list[dict] = field(default_factory=list)


@dataclass
class Plan:
    job: str
    steps: list[Step]
    sources: list[str]          # one line per register row: what the plan does with it

    def step(self, agent: str) -> Step:
        return next(s for s in self.steps if s.agent == agent)

    def units_spec(self) -> dict[str, list[dict]]:
        """The readers' units in the units.yaml shape `live.prepare_units` reads."""
        return {s.agent: s.units for s in self.steps if s.agent in READERS and s.run}

    def text(self) -> str:
        lines = [f"plan for {self.job}:"]
        for s in self.steps:
            units = f", {len(s.units)} units" if s.units else ""
            lines.append(f"  {'run ' if s.run else 'skip'} {s.agent}{units}: {s.reason}")
        lines.append("sources:")
        lines += [f"  {line}" for line in self.sources]
        return "\n".join(lines)

    def as_json(self) -> str:
        return json.dumps(asdict(self), indent=1, ensure_ascii=False) + "\n"


def _pages(s: dict) -> int:
    try:
        return max(1, int(s.get("pages") or 1))
    except ValueError:
        return 1


def units_for_source(reader: str, s: dict) -> list[dict]:
    """The units one source is read in, in the units.yaml shape."""
    sid = s["source_id"]
    if reader == "spec":
        return [{"unit_id": f"{sid}#p{n}", "source_id": sid, "locator": f"p.{n}", "tag": f"{sid} p.{n}", "page": n}
                for n in range(1, _pages(s) + 1)]
    if reader == "drawing":
        out = []
        for n in range(1, _pages(s) + 1):
            for name, box in tiles.grid(*DRAWING_GRID):
                where = f"p.{n} {name}" if _pages(s) > 1 else name
                out.append({"unit_id": f"{sid}#{where.replace(' ', '-')}", "source_id": sid,
                            "locator": f"grid tile {where}", "tag": f"{sid} {where}", "page": n,
                            "box": [round(v, 4) for v in (box.left, box.top, box.right, box.bottom)]})
        return out
    return [{"unit_id": sid, "source_id": sid, "locator": "", "tag": sid}]


NO_WEB = "no network in this run (--no-web), so no page is fetched"


def plan(job: str, register: list[dict], *, web: bool = True) -> Plan:
    """The work plan, from the Source Register and whether the run may fetch pages."""
    units: dict[str, list[dict]] = {r: [] for r in READERS}
    sources = []
    for s in register:
        sid, kind, status = s["source_id"], s.get("kind", ""), s.get("status", "")
        reader = READER_FOR_KIND.get(kind)
        if status == "duplicate":
            sources.append(f"{sid}: duplicate, collapsed at Intake; not read twice")
        elif status != "present":
            sources.append(f"{sid}: {status or 'no status'}; not read, and rows that need it stay FIELD or unverified")
        elif reader is None:
            sources.append(f"{sid}: not read ({NOT_READ.get(kind, f'no reader for kind {kind!r}')})")
        else:
            got = units_for_source(reader, s)
            units[reader] += got
            sources.append(f"{sid}: {reader} reader, {len(got)} units")
    steps = []
    for r in READERS:
        n = len(units[r])
        steps.append(Step(r, bool(n), f"{n} units from the register" if n else f"no {r} source present",
                          units[r]))
    read_any = any(units[r] for r in ("drawing", "photo"))
    steps += [
        Step("takeoff", True, "derives quantities from the drawing rows and writes FIELD rows"
             if read_any else "no drawing or photo rows; writes nothing"),
        Step("customer", False, "not built yet; it waits on the estimator's Oct 6 email to be tested"),
        Step("codes", web, "fetches the code, permit and licensing pages the page table matches to the job"
             if web else NO_WEB),
        Step("materials", web, "fetches the product data sheets the page table matches to the job"
             if web else NO_WEB),
        Step("scope_writer", True, "lays out the proposal from the ledger"),
        Step("auditor", True, "re-hashes the sources, checks every row and traces every figure in the proposal"),
    ]
    return Plan(job=job, steps=steps, sources=sources)


@dataclass
class BidResult:
    plan: Plan
    results: dict = field(default_factory=dict)
    scope: scope_writer.ScopeResult | None = None
    audit: auditor.AuditReport | None = None
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        reads_ok = all(not (r.refused or r.unread) for r in self.results.values())
        return (not self.problems and reads_ok and self.scope is not None and self.scope.ok
                and self.audit is not None and self.audit.ok)

    def text(self) -> str:
        lines = [self.plan.text()]
        lines += [f"PROBLEM {p}" for p in self.problems]
        lines += [r.text() for r in self.results.values()]
        if self.scope is not None:
            lines.append(self.scope.report())
        if self.audit is not None:
            lines.append(self.audit.text())
        lines.append("bid: OK" if self.ok else "bid: NOT OK")
        return "\n".join(lines)


def bid(packet: Path, job: str, out: Path, *, reader_client: ModelClient, takeoff_client: ModelClient,
        scope_client: ModelClient, repeats: int = 2, phrase_library: Path = fixtures.PHRASE_LIBRARY,
        fetcher: web.Fetcher | None = None, web_table: webread.Table | None = None) -> BidResult:
    """Run a bid end to end on a packet folder. Everything lands in `out`."""
    packet, out = Path(packet).resolve(), Path(out)
    out.mkdir(parents=True, exist_ok=True)
    db = out / "ledger.db"
    if db.exists():
        db.unlink()
    broker = Broker.open_job(db, "intake", job=job, run_id=f"bid-{job}", create=True)
    try:
        broker.ledger.set_meta(status_line=STATUS_LINE)
        intake.run(packet, broker)
        for c in {id(c): c for c in (reader_client, takeoff_client, scope_client)}.values():
            if hasattr(c, "bind"):
                c.bind(broker)   # a live client takes its key from the broker
        register = list(broker.ledger.register().values())
        p = plan(job, register, web=fetcher is not None)
        result = BidResult(plan=p)
        (out / "plan.json").write_text(p.as_json())
        units = live.prepare_units(job, p.units_spec(), register, packet, out)
        for reader in READERS:
            if p.step(reader).run:
                result.results[reader] = reader_run.read(broker, reader, job, units[reader], reader_client,
                                                         repeats=repeats)
        result.results["takeoff"] = takeoff.run(broker, job, takeoff_client, repeats=repeats)
        if fetcher is not None:
            result.results["web"] = webread.run(broker, job, reader_client, fetcher, table=web_table,
                                                    repeats=repeats)
        result.scope = scope_writer.run(broker, job, scope_client, phrase_library, repeats=repeats)
        scope_writer.write(result.scope, out)
        result.audit = auditor.run(
            broker.as_principal("auditor"), packet=packet,
            proposal=out / "proposal.md" if result.scope.text else None,
            phrase_library=phrase_library,
        )
        if not result.scope.text:
            result.problems.append("no proposal was rendered: " + "; ".join(result.scope.unread))
        (out / "ledger.csv").write_text(broker.ledger.ledger_csv())
        (out / "bid.txt").write_text(result.text() + "\n")
        return result
    finally:
        broker.close()
