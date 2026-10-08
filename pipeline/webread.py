"""Codes & Regs and Materials: read the pages a job needs and cite them (architecture p.4, p.7-8, p.16).

Which pages a job needs comes from the page table (`web_sources.yaml`), the
per-jurisdiction and per-manufacturer cache the architecture describes: a page
is fetched when the job's ledger rows name its place, product or hazard. A
cold-cache search for a jurisdiction the table does not know is not built yet,
so such a job gets no rows from these agents, and the run says so.

Every page is fetched fresh on every bid. A cached row is evidence, never a
conclusion (p.7-8). Code fetches the page and turns it into text. A model (Haiku,
p.13) is shown the text and the page's asks, and answers each ask with a
verbatim quote and one sentence. Code keeps an answer only if:

* the quote is on the page (`web.quote_in`);
* every number in the sentence is also in the quote;
* every run gives it, with quotes that overlap.

Each kept answer is one `fetched` row with the URL, the retrieval date and the
quote. A page that does not open is one row flagged unverified, saying why, which
is how the hand-made test bids record a dead link.

Codes & Regs and Materials share this module. They differ only in their
principal, and each page in the table names the agent that reads it.
"""
from __future__ import annotations

import copy
import difflib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from . import fixtures, web
from .broker import Broker
from .readers import validate
from .readers.clients import ModelClient, prompt, prompt_version
from .readers.rows import Unit
from .schema import Claim, LedgerError

NAME = "web_reader"
AGENTS = ("codes", "materials")
SOURCES = Path(__file__).resolve().parent / "web_sources.yaml"
SOURCE_ID = "WEB"
# The text a model is shown from one page. Longer pages are cut, and the run says so.
MAX_CHARS = 80_000
QUOTE_MAX = 600
STATEMENT_MAX = 300
# Two runs agree on an answer when the shorter quote is mostly inside the longer.
OVERLAP = 0.6
NUMBER = re.compile(r"\d+(?:[.,/]\d+)*")

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["answers"],
    "properties": {
        "answers": {"type": "array", "maxItems": 20, "items": {
            "type": "object", "additionalProperties": False,
            "required": ["ask", "found", "quote", "statement"],
            "properties": {
                "ask": {"type": "string", "maxLength": 20, "pattern": r"^[a-z0-9]+$"},
                "found": {"type": "boolean"},
                "quote": {"type": "string", "maxLength": QUOTE_MAX},
                "statement": {"type": "string", "maxLength": STATEMENT_MAX},
            },
        }},
    },
}


@dataclass(frozen=True)
class Ask:
    id: str
    ask: str
    fixture: str = ""           # the golden row this ask is gated against, if any


@dataclass(frozen=True)
class Source:
    url: str
    title: str
    agent: str
    when: tuple[tuple[str, ...], ...]   # every group must match; any term in a group will do
    asks: tuple[Ask, ...]


@dataclass
class Table:
    named: frozenset[str]
    pages: list[Source]


def load(path: Path = SOURCES) -> Table:
    data = yaml.safe_load(Path(path).read_text())
    pages = []
    for p in data["pages"]:
        if p["agent"] not in AGENTS:
            raise ValueError(f"{p['url']}: agent {p['agent']!r} is not one of {AGENTS}")
        pages.append(Source(
            url=p["url"], title=p["title"], agent=p["agent"],
            when=tuple(tuple(t.lower() for t in g) for g in p["when"]),
            asks=tuple(Ask(a["id"], a["ask"], a.get("fixture", "")) for a in p["asks"]),
        ))
    return Table(named=frozenset(data["named_domains"]), pages=pages)


def _term(term: str, text: str) -> bool:
    # A short term (a state code) must be a whole word; a longer one may start a word.
    end = r"(?![a-z0-9])" if len(term) <= 3 else ""
    return re.search(r"(?<![a-z0-9])" + re.escape(term) + end, text) is not None


def matches(source: Source, job_text: str) -> bool:
    text = job_text.lower()
    return all(any(_term(t, text) for t in group) for group in source.when)


def job_text(claims: list[Claim]) -> str:
    """What the page table is matched against: every row's statement and value."""
    return "\n".join(f"{c.statement} {c.value}" for c in claims)


def pick(table: Table, claims: list[Claim], agents: tuple[str, ...] = AGENTS) -> list[Source]:
    text = job_text(claims)
    return [s for s in table.pages if s.agent in agents and matches(s, text)]


def schema_for(source: Source) -> dict:
    """SCHEMA with the ask IDs narrowed to this page's own."""
    out = copy.deepcopy(SCHEMA)
    answers = out["properties"]["answers"]
    answers["maxItems"] = len(source.asks)
    answers["items"]["properties"]["ask"] = {"enum": [a.id for a in source.asks]}
    return out


def brief(source: Source) -> str:
    lines = ["Answer each ask from the page:"]
    lines += [f"{a.id}: {a.ask}" for a in source.asks]
    return "\n".join(lines)


def unit_for(job: str, n: int, source: Source, page: web.Page) -> tuple[Unit, bool]:
    text, cut = page.text, len(page.text) > MAX_CHARS
    if cut:
        text = text[:MAX_CHARS]
    return Unit(unit_id=f"{job}#web{n}", source_id=SOURCE_ID, locator=source.url, tag=source.title,
                text=text, brief=brief(source)), cut


def answer_errors(answer: dict, page_text: str) -> list[str]:
    """Why code will not keep this answer. Empty means it may become a row."""
    if not answer["found"]:
        return []
    errs = []
    if not answer["quote"].strip():
        errs.append("found, but no quote")
    elif not web.quote_in(answer["quote"], page_text):
        errs.append("the quote is not on the page")
    if not answer["statement"].strip():
        errs.append("found, but no statement")
    extra = sorted(set(NUMBER.findall(answer["statement"])) - set(NUMBER.findall(answer["quote"])))
    if extra:
        errs.append(f"the statement has numbers the quote does not: {', '.join(extra)}")
    return errs


def response_errors(data: dict, source: Source) -> list[str]:
    errs = validate.errors(data, SCHEMA)
    if errs:
        return errs
    got = [a["ask"] for a in data["answers"]]
    want = [a.id for a in source.asks]
    if sorted(got) != sorted(want):
        errs.append(f"answers {got} do not match the asks {want}")
    return errs


def overlap(a: str, b: str) -> float:
    """How much of the shorter quote is one run of the longer, after normalising."""
    x, y = web.normalize(a), web.normalize(b)
    if not x or not y:
        return 0.0
    if len(x) > len(y):
        x, y = y, x
    m = difflib.SequenceMatcher(None, x, y, autojunk=False).find_longest_match(0, len(x), 0, len(y))
    return m.size / len(x)


def covered(fixture_quote: str, got: str) -> float:
    """How much of the fixture's quote the run's quote carries, passage by passage."""
    parts = [web.normalize(x) for x in web.GAP.split(fixture_quote or "")]
    parts = [x for x in parts if x]
    y = web.normalize(got)
    total = sum(len(x) for x in parts)
    if not total or not y:
        return 0.0
    found = sum(difflib.SequenceMatcher(None, x, y, autojunk=False).find_longest_match(0, len(x), 0, len(y)).size
                for x in parts)
    return found / total


@dataclass
class WebResult:
    pages: int = 0
    calls: int = 0
    fetched: list[web.Page] = field(default_factory=list)
    rows: list[Claim] = field(default_factory=list)
    discarded: list[str] = field(default_factory=list)   # runs or answers code refused
    unanswered: list[str] = field(default_factory=list)  # asks with no agreed answer, and why
    unopened: list[str] = field(default_factory=list)    # pages that did not open: each is an unverified row
    unread: list[str] = field(default_factory=list)      # pages that opened but gave no valid run
    refused: list[str] = field(default_factory=list)     # rows the broker refused
    notes: list[str] = field(default_factory=list)

    def text(self) -> str:
        opened = sum(1 for p in self.fetched if p.ok)
        lines = [f"{NAME}: {self.pages} pages ({opened} opened), {self.calls} calls, {len(self.rows)} rows, "
                 f"{len(self.unanswered)} asks unanswered, {len(self.discarded)} answers or runs discarded"]
        for name, items in (("note", self.notes), ("unopened", self.unopened), ("unread", self.unread),
                            ("unanswered", self.unanswered),
                            ("discarded", self.discarded), ("refused", self.refused)):
            lines += [f"  {name} {x}" for x in items]
        return "\n".join(lines)


def _row(job: str, n: int, source: Source, page: web.Page, **kw) -> Claim:
    return Claim(claim_id=f"{job}-WEB-{n:03d}", source_id=SOURCE_ID, method="fetched", role="code",
                 tag=source.title, url=source.url, retrieved=page.retrieved, **kw)


def _agree(runs: list[dict[str, dict]], ask: Ask) -> tuple[dict | None, str]:
    """The one answer every run gives to this ask, or why there is none."""
    answers = [r.get(ask.id) for r in runs]
    if any(a is None for a in answers):
        return None, "a run's answer was refused"
    found = [a["found"] for a in answers]
    if not any(found):
        return None, "not on the page"
    if not all(found):
        return None, "the runs disagree on whether the page says it"
    first = answers[0]
    if any(overlap(first["quote"], a["quote"]) < OVERLAP for a in answers[1:]):
        return None, "the runs quote different passages"
    return first, ""


def run(broker: Broker, job: str, client: ModelClient, fetcher: web.Fetcher, *, table: Table | None = None,
        agents: tuple[str, ...] = AGENTS, repeats: int = 2) -> WebResult:
    table = table or load()
    gone = broker.ledger.superseded()
    claims = [c for c in broker.ledger.claims() if c.claim_id not in gone]
    # Matched on what the readers and Takeoff wrote, not on fetched rows: those
    # are this agent's own output, from this run or an earlier one.
    sources = pick(table, [c for c in claims if c.method != "fetched"], agents)
    result = WebResult(pages=len(sources))
    if not sources:
        result.notes.append("no page in the table matches this job's rows (a cold-cache search is not built)")
        return result
    system = prompt(NAME)
    writers = {a: broker.as_principal(a, model_id=client.model_id, prompt_version=prompt_version(NAME))
               for a in agents}
    taken = {c.claim_id for c in claims}
    n = 0

    def write(claim: Claim, agent: str) -> None:
        try:
            result.rows.append(writers[agent].append(claim))
        except LedgerError as e:
            result.refused.append(f"{claim.claim_id}: {e}")

    def next_id() -> int:
        nonlocal n
        n += 1
        while f"{job}-WEB-{n:03d}" in taken:
            n += 1
        return n

    for i, source in enumerate(sources, 1):
        writer = writers[source.agent]
        if not writer.may_fetch(source.url):
            result.unread.append(f"{source.url}: the broker refused the fetch")
            continue
        page = fetcher.fetch(source.url)
        result.fetched.append(page)
        writer.log_fetch(source.url, f"{page.retrieved}; {page.error or 'sha256 ' + page.sha256}")
        if not page.ok:
            result.unopened.append(f"{source.url}: {page.error}")
            write(_row(job, next_id(), source, page, flag="unverified", confidence="missing", quote="",
                       statement=f"{source.title}: the page did not open ({page.error}); nothing on it is verified"),
                  source.agent)
            continue
        unit, cut = unit_for(job, i, source, page)
        if cut:
            result.notes.append(f"{source.url}: page cut to its first {MAX_CHARS} characters")
        schema = schema_for(source)
        runs = []
        for r in range(repeats):
            result.calls += 1
            raw = client.complete(NAME, unit, system, schema, r)
            try:
                data = json.loads(raw) if isinstance(raw, str) else raw
            except json.JSONDecodeError as e:
                data, errs = None, [f"not JSON: {e}"]
            else:
                errs = response_errors(data, source)
            writer.log_call(unit.unit_id, f"run {r + 1}; {'discarded: ' + errs[0] if errs else 'valid'}")
            if errs:
                result.discarded.append(f"{unit.unit_id} run {r + 1}: {'; '.join(errs[:3])}")
                continue
            kept = {}
            for a in data["answers"]:
                why = answer_errors(a, unit.text)
                if why:
                    result.discarded.append(f"{unit.unit_id} run {r + 1} {a['ask']}: {'; '.join(why)}")
                else:
                    kept[a["ask"]] = a
            runs.append(kept)
        if len(runs) < repeats:
            result.unread.append(f"{source.url}: {len(runs)} of {repeats} runs valid")
            continue
        for ask in source.asks:
            agreed, why = _agree(runs, ask)
            if agreed is None:
                result.unanswered.append(f"{source.url} {ask.id} ({ask.ask}): {why}")
                continue
            write(_row(job, next_id(), source, page, confidence="exact", quote=agreed["quote"].strip(),
                       statement=agreed["statement"].strip(), locator=f"ask {ask.id}"),
                  source.agent)
    return result


# ---- the golden gate ------------------------------------------------------


@dataclass
class Gate:
    ok: bool
    compared: int
    failures: list[str]
    notes: list[str]

    def text(self) -> str:
        lines = [f"web reader gate: {'PASS' if self.ok else 'FAIL'} ({self.compared} asks compared)"]
        lines += [f"  FAIL {f}" for f in self.failures]
        lines += [f"  note {n}" for n in self.notes]
        return "\n".join(lines)


def gate(result: WebResult, table: Table, fixture_rows: dict[str, dict]) -> Gate:
    """For every ask gated against a row of this fixture: if the page opened and
    still carries the fixture's quote, the run must have answered the ask with a
    quote that carries most of it. A page that did not open, or no longer says what the
    fixture quoted, is reported and left out: that is the page changing, not the
    agent failing."""
    pages = {p.url: p for p in result.fetched}
    by_ask = {}
    for c in result.rows:
        if c.locator.startswith("ask "):
            by_ask[(c.url, c.locator[4:])] = c
    failures, notes, compared = [], [], 0
    for s in table.pages:
        for a in s.asks:
            f = fixture_rows.get(a.fixture) if a.fixture else None
            if f is None:
                continue
            page = pages.get(s.url)
            if page is None:
                failures.append(f"{a.fixture}: {s.url} was not read (the table did not match the job)")
                continue
            if not page.ok:
                notes.append(f"{a.fixture}: {s.url} did not open ({page.error})")
                continue
            if not web.quote_in(f["quote"], page.text[:MAX_CHARS]):
                notes.append(f"{a.fixture}: the page no longer carries the fixture's quote")
                continue
            compared += 1
            got = by_ask.get((s.url, a.id))
            if got is None:
                failures.append(f"{a.fixture}: no row for {a.id} ({a.ask})")
            elif covered(f["quote"], got.quote) < OVERLAP:
                failures.append(f"{a.fixture}: quoted {got.quote[:120]!r}, the fixture quotes {f['quote'][:120]!r}")
    return Gate(ok=not failures and compared > 0, compared=compared, failures=failures, notes=notes)


def golden(job_dir: Path, ledger_path: Path, client: ModelClient, fetcher: web.Fetcher, *, repeats: int = 2,
           table: Table | None = None):
    """Load a fixture's ledger, read the pages the table matches to it, and gate
    the answers against the fixture's own fetched rows. Live only: the pages are
    fetched now. Returns (broker, WebResult, Gate)."""
    if Path(ledger_path).exists():
        Path(ledger_path).unlink()
    broker, data = fixtures.load(Path(job_dir), Path(ledger_path))
    if hasattr(client, "bind"):
        try:
            client.bind(broker)   # a live client takes its key from the broker
        except Exception:
            broker.close()
            raise
    table = table or load()
    result = run(broker, data["job"], client, fetcher, table=table, repeats=repeats)
    fixture_rows = {r["id"]: r for r in data["rows"] if r.get("method") == "fetched" and r.get("quote")}
    return broker, result, gate(result, table, fixture_rows)
