"""The Auditor: pass/fail per figure, and the orphan list.

"Opens each cited source and confirms the value; list of orphan figures"
(architecture p.5). Phase 1 is the code half of that job and it is the half that
makes the system trustworthy:

* every stored row re-checked against the method rules, with no trust in the
  broker having been the one to write it;
* every `calc` expression recomputed;
* every registered file re-hashed, because sources/ is write-once;
* every cited page opened and searched for the row's quote or value, where the
  page has a text layer;
* every `fetched` URL checked for liveness when the run is allowed egress;
* every figure in the finished proposal traced back to a row, and reported as
  an orphan when it cannot be.

The Haiku pass that answers "does this quoted text support this claim, yes or
no" (architecture p.14) sits on top of this in Phase 2 and only ever sees rows
this code has already placed on a page.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from . import schema
from .broker import Broker
from .schema import Claim

# ---------------------------------------------------------------------------
# Figures in prose
# ---------------------------------------------------------------------------

# A figure is a number that could price work: a count, a length, a rate, a
# percentage, a feet-inch string, a dollar amount.
FIGURE = re.compile(
    r"""(?<![A-Za-z0-9._/-])(
        \d+'(?:\s*-?\s*\d+(?:\s+\d/\d|\.\d+)?")?   |  # 15'-2", 12'
        \d+(?:\.\d+)?/\d+"                          |  # 1/4"
        \d+(?:\.\d+)?"                              |  # 11"
        \$\s?\d[\d,]*(?:\.\d+)?                     |  # $1,200
        \d[\d,]*(?:\.\d+)?\s?%                      |  # 10%
        \d[\d,]*(?:\.\d+)?                             # 182, 2,500
    )(?![A-Za-z0-9._/-])""",
    re.VERBOSE,
)

# Numbers that are not figures. Each entry says why it is skipped, because this
# list is the one place the audit can be made to pass by looking away. `window`
# rules are matched against the text around the number, `line` rules against the
# whole line (a heading number, a list marker).
IGNORE = (
    ("window", re.compile(r"\b(?:p|pp|pg|page|sheet|rev|fig|note|item|div(?:ision)?)\.?\s*\d", re.I), "a page, sheet or item reference"),
    ("window", re.compile(r"\b[A-Z]{2,4}-[A-Z]{1,2}-\d{3}\b"), "a claim ID"),
    ("window", re.compile(
        r"(?:\b(?:IBC|FBC|ACI|AWS|SSPC|ASTM|OSHA|NFPA|COMAR|CFR|ICC-ES|ESR|AAMA|ANSI|MHIC"
        r"|F\.S\.|U\.S\.C|Rule|Chapter|Ch\.|Sec(?:tion)?\.?|Art(?:icle)?\.?)|§)[^.;]{0,24}\d"
    ), "a cited code, statute or standard"),
    ("window", re.compile(r"\b(?:19|20)\d{2}\b"), "a year"),
    ("window", re.compile(r"\b\d{1,2}/\d{1,2}/\d{2,4}\b"), "a date"),
    ("window", re.compile(r"\b\d{3}[.\-]\d{3}[.\-]\d{4}\b"), "a phone number"),
    ("window", re.compile(r"\b[A-Z]{1,2}\d{2,4}\b"), "a product code such as A89 or B50"),
    ("window", re.compile(r"\b(?:MHIC|CGC|CBC|license|lic\.?|permit no\.?|#)\s*\d"), "a licence or permit number"),
    ("window", re.compile(r"\b[A-Z]\.\d+\b"), "a spec or alternate section number"),
    ("window", re.compile(r"\b\d{5}(?:-\d{4})?\b(?=\s*$|[\s,)])"), "a ZIP code"),
    ("window", re.compile(r"#\d+\b"), "a unit number"),
    ("line", re.compile(r"^\s*#+\s*\d+(?:\.\d+)*\s"), "a numbered heading"),
    ("line", re.compile(r"^\s*\d+\.\s"), "a numbered list marker"),
)


@dataclass
class Figure:
    text: str
    line: int
    context: str


@dataclass
class Orphan:
    figure: str
    line: int
    context: str

    def __str__(self) -> str:
        return f"line {self.line}: {self.figure}  —  {self.context}"


def _normal(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip().lower()


def _squash(s: str) -> str:
    """Whitespace removed, for comparing a quote against a PDF text layer."""
    return re.sub(r"\s+", "", s or "").lower()


def _numeric_forms(text: str) -> set[str]:
    """The ways the same figure can be written, so 2,500 matches 2500."""
    forms = {text, text.replace(",", ""), text.replace(" ", "")}
    stripped = text.strip('"\'%$ ')
    forms.add(stripped)
    forms.add(stripped.replace(",", ""))
    try:
        n = float(stripped.replace(",", ""))
        forms.add(str(int(n)) if n.is_integer() else str(n))
    except ValueError:
        pass
    return {f for f in forms if f}


def ignored_reason(line: str, start: int, end: int) -> str | None:
    """Why this number on this line is not a figure, or None if it is one."""
    window = line[max(0, start - 32):end + 8]
    for scope, pattern, why in IGNORE:
        if scope == "window":
            if pattern.search(window):
                return why
        else:
            m = pattern.search(line)
            if m and m.start() <= start and end <= m.end():
                return why
    return None


def extract_figures(text: str) -> list[Figure]:
    out = []
    for i, line in enumerate(text.splitlines(), 1):
        for m in FIGURE.finditer(line):
            if ignored_reason(line, m.start(), m.end()):
                continue
            before = line[max(0, m.start() - 48):m.start()]
            after = line[m.end():m.end() + 48]
            out.append(Figure(text=m.group(1), line=i, context=f"…{before}[{m.group(1)}]{after}…".strip()))
    return out


def ledger_figures(claims: list[Claim]) -> set[str]:
    """Every figure the ledger backs: a row's own value, or a number inside the
    source text a row quotes or the derivation code replayed."""
    backed: set[str] = set()
    for c in claims:
        if c.value:
            backed |= _numeric_forms(c.value)
        for text in (c.statement, c.quote, c.derivation, c.calc, c.unit):
            for m in FIGURE.finditer(text or ""):
                backed |= _numeric_forms(m.group(1))
    return backed


def read_document(path: Path) -> str:
    """A proposal to trace figures in: markdown, plain text, or a PDF's text layer."""
    path = Path(path)
    if path.suffix.lower() != ".pdf":
        return path.read_text()
    if not shutil.which("pdftotext"):
        raise RuntimeError(f"{path} is a PDF and pdftotext is not installed")
    out = subprocess.run(
        ["pdftotext", "-layout", str(path), "-"], capture_output=True, text=True, timeout=300, check=True
    ).stdout
    if not out.strip():
        raise RuntimeError(f"{path} has no text layer; the orphan check needs readable text")
    return out


def audit_proposal(text: str, claims: list[Claim], extra_text: str = "") -> list[Orphan]:
    """Figures in the proposal that no ledger row accounts for.

    `extra_text` is the Mersco phrase library: a figure inside a library
    paragraph traces to the Proposal Format Example page the phrase cites, not
    to a ledger row.
    """
    backed = ledger_figures(claims)
    for m in FIGURE.finditer(extra_text or ""):
        backed |= _numeric_forms(m.group(1))
    orphans = []
    for fig in extract_figures(text):
        if not (_numeric_forms(fig.text) & backed):
            orphans.append(Orphan(fig.text, fig.line, fig.context))
    return orphans


# ---------------------------------------------------------------------------
# Opening a cited page
# ---------------------------------------------------------------------------

PAGE_IN_LOCATOR = re.compile(r"\bp\.?\s?(\d{1,3})", re.I)


def page_text(pdf: Path, page: int) -> str | None:
    """The text layer of one page, or None when there is none to read."""
    if not shutil.which("pdftotext") or not pdf.exists():
        return None
    try:
        out = subprocess.run(
            ["pdftotext", "-layout", "-f", str(page), "-l", str(page), str(pdf), "-"],
            capture_output=True, text=True, timeout=120, check=True,
        ).stdout
    except (subprocess.SubprocessError, OSError):
        return None
    return out if out.strip() else None


@dataclass
class RowVerdict:
    claim_id: str
    verdict: str          # pass | fail | unverified
    note: str


def confirm_on_page(c: Claim, register: dict[str, dict], packet: Path) -> RowVerdict | None:
    """Open the page a row cites and look for its quote or its value.

    Returns None when the row cites no openable page, which is not a failure:
    a drawing has no text layer and a web page is checked by link liveness.
    """
    page_no = PAGE_IN_LOCATOR.search(c.locator or "")
    if not page_no:
        return None
    sources = [register[s] for s in schema.sources_of(c.source_id) if s in register]
    pdfs = [s for s in sources if (s.get("file") or "").lower().endswith(".pdf") and s.get("status") == "present"]
    if not pdfs:
        return None
    needle = (_normal(c.quote) or _normal(c.value))[:120]
    page = int(page_no.group(1))
    opened = []
    for s in pdfs:
        text = page_text(Path(packet) / s["file"], page)
        if text is None:
            return RowVerdict(c.claim_id, "unverified",
                              f"{s['source_id']} p.{page} has no text layer; needs the page image")
        opened.append((s["source_id"], text))
        # Spacing differs between a quote and the page's text layer ("320 - 400"
        # against "320-400"), so the comparison ignores whitespace. It never
        # ignores a digit.
        if needle and (needle in _normal(text) or _squash(needle) in _squash(text)):
            return RowVerdict(c.claim_id, "pass", f"found on {s['source_id']} p.{page}")
    if not needle:
        # The page opened, but the row paraphrases the clause rather than quoting
        # it, so code cannot confirm it. "Does this text support this claim, yes
        # or no" is the one question the Phase 2 Haiku pass answers, and it only
        # ever sees a page this code has already opened.
        sid, text = opened[0]
        return RowVerdict(c.claim_id, "unverified",
                          f"{sid} p.{page} opened ({len(text.split())} words) but the row quotes nothing; "
                          "needs the yes/no support check")
    return RowVerdict(c.claim_id, "fail",
                      f"not found on {pdfs[0]['source_id']} p.{page}: {(c.quote or c.value)[:60]!r}")


def link_live(url: str, *, timeout: int = 20) -> tuple[bool, str]:
    """A dead link at audit time is a fail (architecture p.6)."""
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "ChrisBids-Auditor/0.1"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return (200 <= r.status < 400), f"HTTP {r.status}"
    except urllib.error.HTTPError as e:
        if e.code in (403, 405):  # HEAD refused; the page may still be there
            return True, f"HTTP {e.code} on HEAD, not checked further"
        return False, f"HTTP {e.code}"
    except (urllib.error.URLError, OSError, ValueError) as e:
        return False, f"{type(e).__name__}: {e}"


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------

@dataclass
class AuditReport:
    job: str = ""
    rows: int = 0
    verdicts: dict[str, int] = field(default_factory=dict)
    failures: list[str] = field(default_factory=list)
    unverified: list[str] = field(default_factory=list)
    orphans: list[Orphan] = field(default_factory=list)
    hash_problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failures and not self.orphans and not self.hash_problems

    def text(self) -> str:
        lines = [f"Audit of {self.job}: {self.rows} rows",
                 "  " + ", ".join(f"{k} {v}" for k, v in sorted(self.verdicts.items()))]
        for title, items in (("source hashes", self.hash_problems),
                             ("failures", self.failures),
                             ("unverified", self.unverified)):
            if items:
                lines.append(f"\n{title} ({len(items)}):")
                lines += [f"  - {i}" for i in items]
        if self.orphans:
            lines.append(f"\norphan figures ({len(self.orphans)}): a figure with no ledger row behind it")
            lines += [f"  - {o}" for o in self.orphans]
        lines.append("\nPASS" if self.ok else "\nFAIL")
        return "\n".join(lines)


def run(
    broker: Broker,
    *,
    packet: Path | None = None,
    proposal: Path | None = None,
    phrase_library: Path | None = None,
    check_links: bool = False,
    write: bool = True,
) -> AuditReport:
    """Audit a ledger. The broker must be the auditor principal to write verdicts."""
    ledger = broker.ledger
    claims = ledger.claims()
    register = ledger.register()
    by_id = {c.claim_id: c for c in claims}
    superseded = ledger.superseded()
    report = AuditReport(job=ledger.meta("job") or str(ledger.path), rows=len(claims))

    if packet:
        from . import intake
        report.hash_problems = intake.verify(broker, packet)

    for c in claims:
        if c.claim_id in superseded:
            _record(report, broker, c, "unverified", "superseded by a correction row", write)
            continue

        errors = schema.check_vocabulary(c) + schema.check_method_rules(c) + schema.replay_calc(c, by_id)
        for sid in schema.sources_of(c.source_id):
            if sid not in register:
                errors.append(f"{c.claim_id}: source {sid} is not in the Source Register")
        if c.question and c.question not in by_id:
            errors.append(f"{c.claim_id}: open question {c.question} was never written")
        if errors:
            _record(report, broker, c, "fail", "; ".join(e.split(': ', 1)[-1] for e in errors), write)
            continue

        missing = [s for s in schema.sources_of(c.source_id) if register[s]["status"] not in ("present", "duplicate")]
        if missing:
            _record(report, broker, c, "unverified", f"cites {', '.join(missing)}, not in the packet", write)
            continue
        if c.flag == "unverified":
            _record(report, broker, c, "unverified", "flagged unverified by the agent that wrote it", write)
            continue
        if c.flag == "conflict":
            _record(report, broker, c, "unverified", "two readings kept; a human resolves it", write)
            continue

        if c.method == "fetched" and check_links and c.url:
            live, why = link_live(c.url)
            if not live:
                _record(report, broker, c, "fail", f"dead link at audit time ({why}): {c.url}", write)
                continue

        if packet:
            v = confirm_on_page(c, register, packet)
            if v is not None:
                _record(report, broker, c, v.verdict, v.note, write)
                continue

        # Nothing left to open. A pass here has to rest on something the code
        # actually did, so only two kinds of row earn one: a FIELD placeholder,
        # which has no figure to confirm, and a derived figure whose arithmetic
        # was replayed from rows that were themselves confirmed.
        if c.method == "FIELD":
            _record(report, broker, c, "pass", "FIELD placeholder; says what to measure, carries no figure", write)
        elif c.calc:
            _record(report, broker, c, "pass", f"arithmetic replayed: {c.calc}", write)
        elif c.method == "fetched":
            _record(report, broker, c, "unverified",
                    f"URL stored with its retrieval date but not re-fetched this run: {c.url}", write)
        elif c.method == "dimensioned" and (conv := _replay_conversion(c)) is not None:
            # The feet-and-inch conversion is code and can be replayed; the
            # dimension string itself still has to be read off the page.
            if conv:
                _record(report, broker, c, "fail", conv, write)
            else:
                _record(report, broker, c, "unverified",
                        f"conversion replayed ({c.derivation.split(';')[0]}); the dimension string "
                        "still needs the yes/no check against the page image", write)
        else:
            _record(report, broker, c, "unverified",
                    "schema and sources check out, but no cited page could be opened here; "
                    "needs the yes/no check against the page image", write)

    if proposal:
        extra = Path(phrase_library).read_text() if phrase_library else ""
        report.orphans = audit_proposal(read_document(Path(proposal)), claims, extra)
    return report


_CONVERSION = re.compile(r"^(?P<text>.+?) dimension string = (?P<inches>[\d.]+) in\b")


def _replay_conversion(c: Claim) -> str | None:
    """Re-run a reader's feet-and-inch conversion. None if the row has none to
    replay, "" if it reproduces, otherwise why it does not."""
    m = _CONVERSION.match(c.derivation or "")
    if not m:
        return None
    from .readers import units
    try:
        got = units.figure(units.to_inches(m.group("text")))
    except units.DimensionError as e:
        return f"derivation does not parse: {e}"
    if c.value_num is None or abs(float(got) - c.value_num) > 1e-9:
        return f"{m.group('text')} is {got} in, ledger says {c.value or '(blank)'}"
    return ""


def _record(report: AuditReport, broker: Broker, c: Claim, verdict: str, note: str, write: bool) -> None:
    report.verdicts[verdict] = report.verdicts.get(verdict, 0) + 1
    if verdict == "fail":
        report.failures.append(f"{c.claim_id}: {note}")
    elif verdict == "unverified":
        report.unverified.append(f"{c.claim_id}: {note}")
    if write:
        broker.set_audit(c.claim_id, verdict, note)
