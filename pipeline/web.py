"""Fetching a web page for Codes & Regs and Materials, and checking a quote against it.

Only those two agents (and the Auditor) have outbound network (architecture
p.12), and only to an allowlisted domain: `.gov` and `.us` hosts, plus the
domains a page table names (manufacturers, code publishers). Every hop of a
redirect is checked again, so an allowed page cannot hand the fetch to a host
that is not.

Code fetches and turns the page into text; a model only reads that text. A
quote a model returns counts only if it is in the text: `quote_in` compares the
two after normalising case, whitespace, quotes and dashes, and a quote that
joins two passages with "..." must have both, in order.
"""
from __future__ import annotations

import hashlib
import html.parser
import re
# pdftotext runs on a fetched PDF, by absolute argument list and with no shell.
import subprocess  # nosec B404
import tempfile
import unicodedata
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from . import guard

# The allowlist the architecture names (p.12): .gov, .us state portals, and
# named manufacturer domains. The named domains come from the page table.
ALWAYS = (".gov", ".us")
MAX_BYTES = 8_000_000
MAX_REDIRECTS = 5
TIMEOUT = 30
USER_AGENT = "ChrisBids/0.1 (Mersco bid pipeline; reads code and product pages)"
# Words HTML puts in elements that are not page text.
SKIP_TAGS = {"script", "style", "noscript", "svg", "template", "head"}
BLOCK_TAGS = {"p", "div", "br", "li", "tr", "td", "th", "h1", "h2", "h3", "h4", "h5", "h6",
              "section", "article", "table", "ul", "ol", "dd", "dt", "blockquote", "pre", "hr"}
GAP = re.compile(r"\s*(?:\.\.\.|…)\s*")


class FetchRefused(Exception):
    pass


def host_of(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    return host[4:] if host.startswith("www.") else host


def allowed(url: str, named: frozenset[str] | set[str]) -> str:
    """Why this URL may not be fetched, or "" if it may."""
    try:
        guard.public_url(url, resolve=False)
    except guard.UnsafeInput as e:
        return str(e)
    host = host_of(url)
    if host.endswith(ALWAYS) or any(host == d or host.endswith("." + d) for d in named):
        return ""
    return f"{host} is not on the allowlist"


class _Text(html.parser.HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in SKIP_TAGS:
            self.skip += 1
        elif tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in SKIP_TAGS:
            self.skip = max(0, self.skip - 1)
        elif tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def html_text(markup: str) -> str:
    p = _Text()
    p.feed(markup)
    p.close()
    lines = (re.sub(r"[ \t\r\f\v\xa0]+", " ", line).strip() for line in "".join(p.parts).split("\n"))
    return "\n".join(line for line in lines if line)


def pdf_text(data: bytes) -> str:
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "page.pdf"
        path.write_bytes(data)
        out = subprocess.run(["pdftotext", "-enc", "UTF-8", str(path), "-"],
                             capture_output=True, timeout=120, check=True)
    return out.stdout.decode("utf-8", "replace")


def normalize(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "")
    s = s.translate(str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"', "–": "-", "—": "-",
                                   "‐": "-", "‑": "-", "\xad": ""}))
    return re.sub(r"\s+", " ", s).strip().lower()


def quote_in(quote: str, text: str) -> bool:
    """Is every passage of the quote in the text, in order?"""
    parts = [normalize(p) for p in GAP.split(quote or "")]
    parts = [p for p in parts if p]
    if not parts:
        return False
    hay, at = normalize(text), 0
    for p in parts:
        i = hay.find(p, at)
        if i < 0:
            return False
        at = i + len(p)
    return True


@dataclass
class Page:
    url: str
    final_url: str = ""
    retrieved: str = ""         # YYYY-MM-DD
    status: int = 0
    content_type: str = ""
    sha256: str = ""            # of the bytes fetched
    text: str = ""
    error: str = ""             # why there is no text

    @property
    def ok(self) -> bool:
        return not self.error


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None             # each hop is checked by Fetcher.fetch


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


class Fetcher:
    """GET one page, following redirects only to allowed hosts, and turn it into text."""

    def __init__(self, named: frozenset[str] | set[str], *, opener=None, clock=_today, resolve: bool = True):
        self.named = frozenset(named)
        self.opener = opener or urllib.request.build_opener(_NoRedirect)
        self.clock = clock
        self.resolve = resolve  # tests pass False: no DNS

    def _open(self, url: str):
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
        try:
            # http(s) only: fetch() checks every URL with allowed() before it gets here.
            return self.opener.open(req, timeout=TIMEOUT)  # nosec B310
        except urllib.error.HTTPError as e:
            return e

    def fetch(self, url: str) -> Page:
        page = Page(url=url, retrieved=self.clock())
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            why = allowed(current, self.named)
            if not why and self.resolve:
                try:
                    guard.public_url(current)
                except guard.UnsafeInput as e:
                    why = str(e)
            if why:
                page.error = f"not fetched: {why}"
                return page
            try:
                resp = self._open(current)
            except (urllib.error.URLError, OSError, ValueError) as e:
                page.error = f"did not open: {getattr(e, 'reason', e)}"
                return page
            status = resp.getcode() or 0
            if status in (301, 302, 303, 307, 308) and resp.headers.get("Location"):
                current = urljoin(current, resp.headers["Location"])
                continue
            return self._read(page, current, status, resp)
        page.error = f"more than {MAX_REDIRECTS} redirects"
        return page

    def _read(self, page: Page, final: str, status: int, resp) -> Page:
        page.final_url, page.status = final, status
        page.content_type = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        data = resp.read(MAX_BYTES + 1)
        if status != 200:
            page.error = f"HTTP {status}"
            return page
        if len(data) > MAX_BYTES:
            page.error = f"larger than {MAX_BYTES} bytes"
            return page
        page.sha256 = hashlib.sha256(data).hexdigest()
        try:
            if "pdf" in page.content_type or data[:5] == b"%PDF-":
                page.text = pdf_text(data)
            elif "html" in page.content_type or b"<html" in data[:2000].lower():
                page.text = html_text(data.decode(resp.headers.get_content_charset() or "utf-8", "replace"))
            elif page.content_type.startswith("text/"):
                page.text = data.decode(resp.headers.get_content_charset() or "utf-8", "replace")
            else:
                page.error = f"cannot read {page.content_type or 'unknown content'}"
        except (subprocess.SubprocessError, OSError) as e:
            page.error = f"could not extract text: {e}"
        if not page.error and not page.text.strip():
            page.error = "no text on the page (a scan or a script-only page)"
        return page
