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
* the figures it fills the ask's # marks with (`figures`) are in the quote, a
  range counting as one figure, or part of one of the page's own identifiers
  (`ids` in the table: ESR-4143) that the page carries;
* the sentence gives each of those figures and none its quote lacks, the
  identifiers taken out whole;
* two runs pick the same option for a closed ask (a status, yes or no) and
  fill the marks with exactly the same figures; when the ask has neither,
  they quote mostly the same passage and their sentences give the same figures.
  A run that is discarded, or an answer that is refused, gets up to SPARES
  spare runs for the page.

Each kept answer is one `fetched` row with the URL, the retrieval date and the
quote. When the runs find differing readings on the page, code does not pick
one: each reading is written as its own row, flagged unverified. A page that does not open is one row flagged unverified, saying why, which
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
from .schema import Claim, LedgerError, sources_of

NAME = "web_reader"
AGENTS = ("codes", "materials")
SOURCES = Path(__file__).resolve().parent / "web_sources.yaml"
SOURCE_ID = "WEB"
# The text a model is shown from one page. Longer pages are cut, and the run says so.
MAX_CHARS = 80_000
QUOTE_MAX = 600
STATEMENT_MAX = 500
# Two runs agree on an answer when the shorter quote is mostly inside the longer.
OVERLAP = 0.6
# The share of gated asks the live gate needs answered (see gate()).
ANSWERED = 0.8
# Most spare runs a page gets when answers are refused (see run()).
SPARES = 2
# The choice for a closed ask whose page says something none of its options fit.
OTHER = "other"
# A figure, with a range ("2-4", "350 – 400") as one figure, so a statement
# cannot narrow a range to one end and still match the quote.
NUMBER = re.compile(r"\d+(?:[.,/]\d+)*(?:\s*[-–]\s*\d+(?:[.,/]\d+)*)?")
# A date written with its month ("March 2024", "Jan. 15, 2018") is one figure,
# its month included, so runs that read different months of one year differ.
# The month counts only before a year, so "may 2 coats" is not a date.
DATE = re.compile(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+"
                  r"(?:(\d{1,2}),?\s+)?((?:19|20)\d\d)\b", re.I)
NUMBER_WORD = re.compile(r"\b(?:two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen"
                         r"|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|sixty|seventy"
                         r"|eighty|ninety|hundred|thousand|million|dozen)\b")

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["answers"],
    "properties": {
        "answers": {"type": "array", "maxItems": 20, "items": {
            "type": "object", "additionalProperties": False,
            "required": ["ask", "found", "quote", "figures", "choice", "statement"],
            "properties": {
                "ask": {"type": "string", "maxLength": 20, "pattern": r"^[a-z0-9]+$"},
                "found": {"type": "boolean"},
                # The figures the ask's # marks stand for, in order, as the
                # quote writes them. The runs are compared on these.
                "figures": {"type": "array", "maxItems": 12, "items": {"type": "string", "maxLength": 40}},
                # For an ask with a closed answer (`options` in the table: yes/no,
                # proposed/adopted/effective), the option the quote supports;
                # "" for an ask with none. The runs are compared on this too.
                "choice": {"type": "string", "maxLength": 40},
                # Lengths are checked per answer (answer_errors), so one long
                # answer does not cost the run its other answers.
                "quote": {"type": "string"},
                "statement": {"type": "string"},
            },
        }},
    },
}


@dataclass(frozen=True)
class Ask:
    id: str
    ask: str
    fixture: str = ""           # the golden row this ask is gated against, if any
    # A closed answer's options (yes/no, a status): the model picks one, or
    # OTHER, and the runs must pick the same one. Free text is never compared.
    options: tuple[str, ...] = ()
    # The unit of the figure the first # mark stands for ("sq ft/gal", "weeks"):
    # that figure is then the row's value, with this unit, so a data sheet's
    # spread rate can feed an order quantity. Blank for an ask whose figures
    # stay in the sentence (a date, an edition).
    unit: str = ""


@dataclass(frozen=True)
class Source:
    url: str
    title: str
    agent: str
    when: tuple[tuple[str, ...], ...]   # every group must match; any term in a group will do
    asks: tuple[Ask, ...]
    # The page's own identifiers (a product, report or section number). A
    # statement may name one although the quote does not; its digits are not
    # figures read from the page.
    ids: tuple[str, ...] = ()


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
            asks=tuple(Ask(a["id"], a["ask"], a.get("fixture", ""), tuple(a.get("options", ())), a.get("unit", ""))
                       for a in p["asks"]),
            ids=tuple(p.get("ids", ())),
        ))
    return Table(named=frozenset(data["named_domains"]), pages=pages)


def _term(term: str, text: str) -> bool:
    # A short term must be a whole word; a longer one may start a word. A
    # two-letter state code counts only as an address writes it (", MD" or
    # "MD 21842"), so "10.1 fl oz" in a spec does not pull in Florida's pages.
    t = re.escape(term)
    if len(term) == 2:
        return re.search(r",\s*" + t + r"(?![a-z0-9])|(?<![a-z0-9])" + t + r"\s+\d{5}\b", text) is not None
    end = r"(?![a-z0-9])" if len(term) <= 3 else ""
    return re.search(r"(?<![a-z0-9])" + t + end, text) is not None


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
    options = sorted({o for a in source.asks for o in a.options})
    answers["items"]["properties"]["choice"] = {"enum": ["", *options, OTHER] if options else [""]}
    return out


def choice_errors(answer: dict, ask: Ask) -> list[str]:
    """What the ask itself requires of a found answer: one entry of `figures`
    per # mark (so the runs are compared on the figures asked for), and a
    choice that is one of the ask's options (or OTHER), or empty for an ask
    with none."""
    if not answer["found"]:
        return []
    marks = ask.ask.count("#")
    if len(answer.get("figures", ())) != marks:
        return [f"figures has {len(answer.get('figures', ()))} entries for the ask's {marks} # marks"]
    got = answer.get("choice", "")
    if ask.options and got not in (*ask.options, OTHER):
        return [f"the choice {got!r} is not one of {', '.join(ask.options)}, {OTHER}"]
    if not ask.options and got:
        return [f"the ask has no options, but the choice is {got!r}"]
    return []


def brief(source: Source) -> str:
    lines = [f"The page: {source.title}"]
    if source.ids:
        lines.append(f"Its identifiers: {', '.join(source.ids)}")
    lines.append("Answer each ask from the page:")
    lines += [f"{a.id}: {a.ask}" + (f" [choice: {' | '.join((*a.options, OTHER))}]" if a.options else "")
              for a in source.asks]
    return "\n".join(lines)


def unit_for(job: str, n: int, source: Source, page: web.Page) -> tuple[Unit, bool]:
    text, cut = page.text, len(page.text) > MAX_CHARS
    if cut:
        text = text[:MAX_CHARS]
    return Unit(unit_id=f"{job}#web{n}", source_id=SOURCE_ID, locator=source.url, tag=source.title,
                text=text, brief=brief(source)), cut


def on_page(ids: tuple[str, ...], page_text: str) -> tuple[str, ...]:
    """The identifiers the page carries. Only these count (run, answer_errors and the gate alike)."""
    return tuple(i for i in ids if web.quote_in(i, page_text))


def figures(text: str, ids: tuple[str, ...] = ()) -> set[str]:
    """The figures in a text, each range as one ("2-4") and each date with its
    month as one ("mar 2024"), after taking out the
    page's own identifiers whole (ESR-4143, A24W8300), so their digits are not
    counted. Only whole identifiers come out: "24 hours" on the A24W8300 sheet
    is still a figure."""
    for i in sorted(ids, key=len, reverse=True):
        text = re.sub(r"(?<![\w.])" + re.escape(i) + r"(?![\w])", " ", text or "", flags=re.I)
    dates = {" ".join(x for x in (m[1].lower(), m[2], m[3]) if x) for m in DATE.finditer(text or "")}
    text = DATE.sub(" ", text or "")
    return dates | {re.sub(r"\s*[-–]\s*", "-", n) for n in NUMBER.findall(text)}


def id_marks(ask: str, ids: tuple[str, ...]) -> frozenset[int]:
    """Which of the ask's # marks fill part of one of the page's identifiers
    ("LX#", "ESR-#", "HIT-HY #", "A#W#"): the text the ask writes before the
    mark, with earlier marks as wildcards, begins one of the identifiers."""
    out, parts = set(), ask.split("#")
    for k in range(len(parts) - 1):
        before = "#".join(parts[:k + 1])
        last = re.search(r"[\w#.-]*$", before).group(0)
        word = re.search(r"([\w#.-]+ )?$", before[:len(before) - len(last)]).group(0)
        for token in {last, word + last}:   # "LX#", "ESR-#"; "HIT-HY #"
            if not re.search(r"[A-Za-z]", token):
                continue
            pattern = re.compile(re.escape(token).replace(r"\#", r"[\w.]*"), re.I)
            if any(pattern.match(i) for i in ids):
                out.add(k)
    return frozenset(out)


def slots(answer: dict, ids: tuple[str, ...] = (), marks: frozenset[int] = frozenset()) -> tuple[tuple[str, ...], ...]:
    """The answer's `figures`, each as the figures it holds (a range is one).
    In a mark that fills an identifier (`marks`, from id_marks), figures that
    are part of the page's own identifiers are left out, so a run that fills
    "LX#" with "02W0050" and one that fills it with "LX02W0050" agree. Every
    other mark keeps all its figures."""
    named = {n for i in ids for n in figures(i)}
    return tuple(tuple(sorted(figures(f) - (named if k in marks else set())))
                 for k, f in enumerate(answer.get("figures", ())))


def figure_of(answer: dict, ask: Ask, ids: tuple[str, ...] = ()) -> tuple[str, str]:
    """The row's value and unit from an ask that names its unit: the one figure
    the answer fills the first # mark with, as the quote writes it (a range is
    one figure: "320-400"), and the table's unit. An ask with no unit, an answer
    not found, or a first mark filled with no figure or several gives none."""
    if not ask.unit or not answer.get("found"):
        return "", ""
    filled = slots(answer, ids, id_marks(ask.ask, ids))
    if not filled or len(filled[0]) != 1:
        return "", ""
    return filled[0][0], ask.unit


def answer_errors(answer: dict, page_text: str, ids: tuple[str, ...] = (), ask: str = "") -> list[str]:
    """Why code will not keep this answer. Empty means it may become a row.
    Each entry of `figures` must be in the quote or, in a mark the ask places
    in an identifier, be part of one of the page's own identifiers (`ids`,
    from the page table: "ESR-#" is 4143; this goes by digit group), and the
    statement must give each of them. The statement may give only figures its
    quote carries; there the identifiers are taken out whole, so "24 hours" on
    the A24W8300 sheet is still a figure."""
    if not answer["found"]:
        return []
    # an identifier stands in for a figure only if the page carries it: a
    # product name the model recalled (HY 70 -> HY 270, p.7) gets no pass
    ids = on_page(ids, page_text)
    errs = []
    if len(answer["quote"]) > QUOTE_MAX:
        errs.append(f"the quote is longer than {QUOTE_MAX} characters")
    if len(answer["statement"]) > STATEMENT_MAX:
        errs.append(f"the statement is longer than {STATEMENT_MAX} characters")
    if not answer["quote"].strip():
        errs.append("found, but no quote")
    elif not web.quote_in(answer["quote"], page_text):
        errs.append("the quote is not on the page")
    if not answer["statement"].strip():
        errs.append("found, but no statement")
    quoted = figures(answer["quote"])
    slot_figures = {f for slot in slots(answer, ids, id_marks(ask, ids)) for f in slot}
    loose = sorted(slot_figures - quoted)
    if loose:
        errs.append(f"figures the quote does not carry: {', '.join(loose)}")
    unstated = sorted(slot_figures - figures(answer["statement"], ids))
    if unstated:
        errs.append(f"the statement does not give its figures: {', '.join(unstated)}")
    extra = sorted(figures(answer["statement"], ids) - quoted)
    # a figure written in words counts too ("one" is left out: it is mostly not a figure)
    extra += sorted(set(NUMBER_WORD.findall(answer["statement"].lower()))
                    - set(NUMBER_WORD.findall(answer["quote"].lower())))
    if extra:
        errs.append(f"the statement has figures not among its quoted figures: {', '.join(extra)}")
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


def same_facts(fixture_statement: str, got: str, ids: tuple[str, ...] = ()) -> bool:
    """Does `got` carry every figure the fixture's statement gives? The page's
    own identifiers are not figures. A statement with no figure is never the
    same facts by this test."""
    want = figures(fixture_statement, ids)
    return bool(want) and want <= figures(got, ids)


@dataclass
class WebResult:
    pages: int = 0
    calls: int = 0
    fetched: list[web.Page] = field(default_factory=list)
    rows: list[Claim] = field(default_factory=list)
    discarded: list[str] = field(default_factory=list)   # runs or answers code refused
    unanswered: list[str] = field(default_factory=list)  # asks with no agreed answer, and why
    readings: list[str] = field(default_factory=list)    # what each kept unverified reading read
    unopened: list[str] = field(default_factory=list)    # pages that did not open: each is an unverified row
    unread: list[str] = field(default_factory=list)      # pages that opened but gave no valid run
    blocked: list[str] = field(default_factory=list)     # URLs the broker refused to fetch: each is an unverified row
    refused: list[str] = field(default_factory=list)     # rows the broker refused
    notes: list[str] = field(default_factory=list)

    def text(self) -> str:
        opened = sum(1 for p in self.fetched if p.ok)
        lines = [f"{NAME}: {self.pages} pages ({opened} opened), {self.calls} calls, {len(self.rows)} rows, "
                 f"{len(self.unanswered)} asks unanswered, {len(self.discarded)} answers or runs discarded"]
        for name, items in (("note", self.notes), ("unopened", self.unopened), ("blocked", self.blocked),
                            ("unread", self.unread),
                            ("unanswered", self.unanswered), ("reading", self.readings),
                            ("discarded", self.discarded), ("refused", self.refused)):
            lines += [f"  {name} {x}" for x in items]
        return "\n".join(lines)


def _row(job: str, n: int, source: Source, page: web.Page, **kw) -> Claim:
    # Both agents write role "code": a data-sheet row is the evidence an order
    # quantity rests on (p.4) through a `material` row's calc (schema.MATERIAL_OK),
    # which the Materials order step writes, not the reader.
    return Claim(claim_id=f"{job}-WEB-{n:03d}", source_id=SOURCE_ID, method="fetched", role="code",
                 tag=source.title, url=source.url, retrieved=page.retrieved, **kw)


def _gap(job: str, n: int, source: Source, page: web.Page, ask: Ask, why: str) -> Claim:
    """An ask with no answer is still a row, flagged unverified, so the gap reaches the bid."""
    return _row(job, n, source, page, flag="unverified", confidence="missing", quote="", locator=f"ask {ask.id}",
                statement=f"{source.title}: no answer for \"{ask.ask}\" ({why}); nothing on it is verified")


def _same_reading(a: dict, b: dict, ids: tuple[str, ...] = (), marks: frozenset[int] = frozenset()) -> bool:
    """Do two answers give the same reading? A closed answer (a status, yes
    or no) is a choice, and the two must pick the same option. The figures
    then decide: each run fills the ask's # marks, and the two must fill them
    exactly alike, from whichever passage (a data sheet gives its spread rate
    in a table and again in the text). What a passage says beyond its figures
    is never read from free text: an ask whose answer is a word has options,
    and a shared option with no marks to fill is one reading from any passage.
    Answers with no figure and no options must quote mostly the same passage
    and give the same figures in their statements."""
    if a.get("choice", "") != b.get("choice", "") or a.get("choice") == OTHER:
        return False        # a closed answer: the same option, and one of the ask's own
    x, y = slots(a, ids, marks), slots(b, ids, marks)
    if any(x) or any(y):
        return x == y
    if a.get("choice"):
        return True         # a closed ask with no marks: the shared option is the reading, from any passage
    return (_same_passage(a["quote"], b["quote"])
            and figures(a["statement"], ids) == figures(b["statement"], ids))


def _same_passage(a: str, b: str) -> bool:
    """Do two quotes come mostly from one passage (either may be the longer, or joined with ...)?"""
    return max(overlap(a, b), covered(a, b), covered(b, a)) >= OVERLAP


def _agree(runs: list[dict[str, dict]], ask: Ask, need: int | None = None,
           ids: tuple[str, ...] = ()) -> tuple[dict | None, str, list[dict]]:
    """The one answer the first `need` kept answers (default: one from every
    run) all give to this ask, or why there is none. When there is none but runs
    found differing readings on the page, those readings come back too: code
    never picks between them (the determinism rule), it keeps each one,
    flagged unverified."""
    answers = [r[ask.id] for r in runs if ask.id in r]
    need = len(runs) if need is None else need
    if len(answers) < need:
        return None, "a run's answer was refused", []
    answers = answers[:need]    # the first kept answers; a spare run only stands in
    found = [a for a in answers if a["found"]]
    if not found:
        return None, "not on the page", []
    if len(found) < len(answers):
        return None, "the runs disagree on whether the page says it", _distinct(found)
    first = answers[0]
    marks = id_marks(ask.ask, ids)
    if not all(_same_reading(first, a, ids, marks) for a in answers[1:]):
        return None, "the runs give different readings", _distinct(found)
    return first, "", []


def _distinct(answers: list[dict]) -> list[dict]:
    """The readings, once each (two runs may give the same quote and sentence)."""
    out, seen = [], set()
    for a in answers:
        key = (web.normalize(a["quote"]), web.normalize(a["statement"]))
        if key not in seen:
            seen.add(key)
            out.append(a)
    return out


def from_packet(c: Claim) -> bool:
    """Does this row rest only on the job's own documents?"""
    cited = sources_of(c.source_id)
    return bool(cited) and SOURCE_ID not in cited


def run(broker: Broker, job: str, client: ModelClient, fetcher: web.Fetcher, *, table: Table | None = None,
        agents: tuple[str, ...] = AGENTS, repeats: int = 2) -> WebResult:
    table = table or load()
    gone = broker.ledger.superseded()
    claims = [c for c in broker.ledger.claims() if c.claim_id not in gone]
    # Matched on what the readers and Takeoff wrote from the packet: not on
    # fetched rows, which are this agent's own output from this run or an
    # earlier one, nor on any row that cites the web or no source at all (a
    # question or FIELD row written from web research would pick the very pages
    # that research found).
    sources = pick(table, [c for c in claims if c.method != "fetched" and from_packet(c)], agents)
    result = WebResult(pages=len(sources))
    if not sources:
        result.notes.append("no page in the table matches this job's rows (a cold-cache search is not built)")
        return result
    system = prompt(NAME)
    writers = {a: broker.as_principal(a, model_id=client.model_id, prompt_version=prompt_version(NAME))
               for a in agents}
    taken = {c.claim_id for c in broker.ledger.claims()}    # superseded rows keep their IDs too
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
            # Like a page that did not open, the refusal is a row, so the gap reaches the bid.
            result.blocked.append(source.url)
            page = web.Page(url=source.url, retrieved=fetcher.clock(),
                            error="the broker refused the fetch")
            write(_row(job, next_id(), source, page, flag="unverified", confidence="missing", quote="",
                       statement=f"{source.title}: the broker refused the fetch; nothing on it is verified"),
                  source.agent)
            continue
        page = fetcher.fetch(source.url)
        result.fetched.append(page)
        served = f"; served by {page.final_url}" if page.final_url and page.final_url != source.url else ""
        writer.log_fetch(source.url, f"{page.retrieved}; {page.error or 'sha256 ' + page.sha256}{served}")
        if not page.ok:
            result.unopened.append(f"{source.url}: {page.error}")
            write(_row(job, next_id(), source, page, flag="unverified", confidence="missing", quote="",
                       statement=f"{source.title}: the page did not open ({page.error}); nothing on it is verified"),
                  source.agent)
            continue
        unit, cut = unit_for(job, i, source, page)
        ids = on_page(source.ids, unit.text)
        if cut:
            result.notes.append(f"{source.url}: page cut to its first {MAX_CHARS} characters")
        schema = schema_for(source)
        runs = []
        # Spare runs (up to SPARES), made only while a run was discarded or an
        # answer refused, so a slip does not cost the ask: each ask still needs
        # `repeats` kept answers, and every kept answer must agree.
        for r in range(repeats + SPARES):
            if r >= repeats and len(runs) >= repeats and all(
                    sum(a.id in k for k in runs) >= repeats for a in source.asks):
                break
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
            by_id = {x.id: x for x in source.asks}
            for a in data["answers"]:
                why = answer_errors(a, unit.text, ids, by_id[a["ask"]].ask) + choice_errors(a, by_id[a["ask"]])
                if why:
                    result.discarded.append(f"{unit.unit_id} run {r + 1} {a['ask']}: {'; '.join(why)}")
                else:
                    kept[a["ask"]] = a
            runs.append(kept)
        if len(runs) < repeats:
            why = f"{len(runs)} of {repeats} runs valid"
            result.unread.append(f"{source.url}: {why}")
            for ask in source.asks:
                write(_gap(job, next_id(), source, page, ask, why), source.agent)
            continue
        for ask in source.asks:
            agreed, why, readings = _agree(runs, ask, repeats, ids)
            if agreed is None:
                kept = f"; {len(readings)} readings kept, flagged unverified" if readings else ""
                result.unanswered.append(f"{source.url} {ask.id} ({ask.ask}): {why}{kept}")
                # what each kept reading read, so a split can be judged from the run log alone
                result.readings += [f"{unit.unit_id} {ask.id}: figures {r.get('figures', [])}, "
                                    f"choice {r.get('choice', '')!r}, quote {r['quote'][:100]!r}" for r in readings]
                for a in readings:
                    value, unit = figure_of(a, ask, ids)
                    write(_row(job, next_id(), source, page, confidence="inferred", flag="unverified",
                               quote=a["quote"].strip(), statement=a["statement"].strip(), value=value, unit=unit,
                               locator=f"ask {ask.id}"), source.agent)
                if not readings:
                    write(_gap(job, next_id(), source, page, ask, why), source.agent)
                continue
            value, unit = figure_of(agreed, ask, ids)
            write(_row(job, next_id(), source, page, confidence="exact", quote=agreed["quote"].strip(),
                       statement=agreed["statement"].strip(), value=agreed.get("choice", "") or value, unit=unit,
                       locator=f"ask {ask.id}"),
                  source.agent)
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
        answered = self.compared - len(self.misses)
        lines = [f"web reader gate: {'PASS' if self.ok else 'FAIL'} ({self.compared} asks compared, "
                 f"{answered} answered; {ANSWERED:.0%} needed, and no wrong answer)"]
        lines += [f"  FAIL {f}" for f in self.failures]
        lines += [f"  miss {m}" for m in self.misses]
        lines += [f"  note {n}" for n in self.notes]
        return "\n".join(lines)


def _states(fixture: dict, statement: str, ids: tuple[str, ...]) -> bool:
    """Does the run's statement give every figure of the fixture's statement
    that the fixture's own quote carries? (A figure the fixture's statement
    adds from elsewhere, such as a table number, no quote could support.)"""
    want = figures(fixture.get("statement", ""), ids) & figures(fixture["quote"])
    return want <= figures(statement, ids)


def gate(result: WebResult, table: Table, fixture_rows: dict[str, dict],
         choices: dict[str, str] | None = None) -> Gate:
    """For every ask gated against a row of this fixture: if the page opened and
    still carries the fixture's quote, the run must have answered the ask with a
    quote that carries most of it, or that carries every figure the fixture's
    statement gives (the same facts from another passage of the page), and its
    statement must give every figure of the fixture's statement that the
    fixture's quote carries (so a narrowed range or a swapped date fails). A row that does not is a wrong
    answer and fails the gate. An ask left without
    a row is a miss: the bid then has no verified row for it, which is safe but
    incomplete, so the gate passes only while at least ANSWERED of the compared
    asks have a row. A page that did not open, or no longer says what the
    fixture quoted, is reported and left out: that is the page changing, not the
    agent failing. So is a page the job's packet rows do not lead to (the test
    bid found it by searching, which is not built)."""
    pages = {p.url: p for p in result.fetched}
    by_ask = {}
    for c in result.rows:
        if c.locator.startswith("ask ") and c.flag != "unverified":   # an unverified reading is not an answer
            by_ask[(c.url, c.locator[4:])] = c
    failures, notes, misses, compared = [], [], [], 0
    for s in table.pages:
        for a in s.asks:
            f = fixture_rows.get(a.fixture) if a.fixture else None
            if f is None:
                continue
            page = pages.get(s.url)
            if s.url in result.blocked:
                failures.append(f"{a.fixture}: the broker refused to fetch {s.url}")
                continue
            if page is None:
                # The job's own rows do not lead to this page: the test bid found it
                # by its own search, which is not built (p.13). Reported, not compared;
                # the offline golden test pins how many asks each job compares.
                notes.append(f"{a.fixture}: {s.url} not matched by the job's packet rows (cold-cache search not built)")
                continue
            if not page.ok:
                notes.append(f"{a.fixture}: {s.url} did not open ({page.error})")
                continue
            if not web.quote_in(f["quote"], page.text[:MAX_CHARS]):
                notes.append(f"{a.fixture}: the page no longer carries the fixture's quote")
                continue
            compared += 1
            ids = on_page(s.ids, page.text[:MAX_CHARS])
            got = by_ask.get((s.url, a.id))
            if got is None:
                misses.append(f"{a.fixture}: no row for {a.id} ({a.ask})")
            elif covered(f["quote"], got.quote) < OVERLAP and not same_facts(f.get("statement", ""), got.quote, ids):
                failures.append(f"{a.fixture}: quoted {got.quote[:120]!r}, the fixture quotes {f['quote'][:120]!r}")
            elif not _states(f, got.statement, ids):
                failures.append(f"{a.fixture}: states {got.statement[:120]!r}, the fixture states "
                                f"{f['statement'][:120]!r}")
            elif a.options and got.value != (choices or {}).get(a.fixture):
                failures.append(f"{a.fixture}: chose {got.value!r}, the fixture's answer is "
                                f"{(choices or {}).get(a.fixture)!r}")
    ok = not failures and compared > 0 and compared - len(misses) >= ANSWERED * compared
    return Gate(ok=ok, compared=compared, failures=failures, notes=notes, misses=misses)


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
    # The closed answers the fixture's rows give, kept beside the fixture and
    # out of the page table, which holds what to look for, never what was found.
    path = Path(job_dir) / "web_choices.yaml"
    choices = yaml.safe_load(path.read_text()) if path.exists() else {}
    return broker, result, gate(result, table, fixture_rows, choices)
