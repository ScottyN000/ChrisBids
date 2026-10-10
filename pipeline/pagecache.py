"""What each code or product page said the last time it was read (architecture p.7-8, p.11).

Codes are cached as evidence, never as conclusions: the cache keeps a page's
URL, retrieval date, content hash and the answers its asks agreed on, each with
the verbatim quote it rests on. The savings come from skipping the search and
the reading, never the fetch (p.8, p.14): every page is fetched on every bid,
and only when its bytes hash the same as the last read, with every cached quote
still on them, do its answers stand without a model call; the rows then carry
today's date. On any change the page is read anew.

An entry counts only for the same asks, page identifiers, prompt version, model,
number of agreeing runs and code rules (`webread.RULES`): a changed question, or a
looser reading, is a new reading. Only a page whose asks all agreed is cached;
one with a gap or a split reading is read again next time.

The file is append-only (p.11: Codes & Regs and Materials append, through the
broker): one JSON line per reading or revalidation, and the last line for a URL
and key wins. A line that does not load (a write cut off, a hand edit, another
format) is skipped and counted, so the page is read anew.

Not built: the 90-day window for federal regulations, which the architecture
pairs with a Federal Register change feed (p.8); until that feed exists a
federal page is fetched every bid like any other.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

FORMAT = 1
FIELDS = {"quote", "statement", "value", "value_num", "unit"}   # an answer is a row's fields


@dataclass
class Entry:
    url: str
    key: str                    # what it was read with (`key`)
    retrieved: str              # YYYY-MM-DD, the fetch whose bytes these are
    sha256: str
    answers: dict[str, dict] = field(default_factory=dict)   # ask id -> the agreed row's fields


def key(source, version: str, model: str, repeats: int, rules: str) -> str:
    asks = [[a.id, a.ask, list(a.options), a.unit] for a in source.asks]
    blob = json.dumps([asks, list(source.ids), version, model, repeats, rules], sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()


class PageCache:
    """A JSON-lines file of entries. No path: nothing is kept between bids."""

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else None
        self.entries: dict[tuple[str, str], Entry] = {}
        self.skipped = 0        # lines that did not load
        if self.path and self.path.exists():
            for line in self.path.read_text(errors="replace").splitlines():
                try:
                    d = json.loads(line)
                    if d.pop("format") != FORMAT:
                        raise ValueError("another format")
                    e = Entry(**d)
                    if not all(isinstance(x, str) for x in (e.url, e.key, e.retrieved, e.sha256)):
                        raise ValueError("a field is not text")
                    if not all(isinstance(a, dict) and set(a) == FIELDS and isinstance(a["quote"], str)
                               for a in e.answers.values()):
                        raise ValueError("an answer is not a row")
                except (ValueError, TypeError, KeyError, AttributeError):
                    self.skipped += 1
                    continue
                self.entries[(e.url, e.key)] = e

    def get(self, url: str, k: str) -> Entry | None:
        return self.entries.get((url, k))

    def append(self, e: Entry) -> None:
        self.entries[(e.url, e.key)] = e
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a") as f:
                f.write(json.dumps({"format": FORMAT, **asdict(e)}) + "\n")
