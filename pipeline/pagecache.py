"""What each code or product page said the last time it was read (architecture p.7-8).

Codes are cached as evidence, never as conclusions: the cache keeps a page's
URL, retrieval date, content hash and the answers its asks agreed on, each with
the verbatim quote it rests on. Before a page is read again, the freshness rule
for its kind decides what the cache may save:

* Federal regulations (29 CFR on eCFR and OSHA) say `revalidate_days: 90` in the page table.
  Read within that many days, the page is neither fetched nor read again; its
  rows carry the date it was read. The architecture also re-reads one on a
  Federal Register hit; that change feed is not built, so an amendment inside
  the 90 days waits for the window to close.
* Every other page is fetched on every bid. When its bytes hash the same as the
  last read, and every cached quote is still on it, its answers stand without
  a model call and its rows carry today's date. On any change it is read anew.

An entry counts only for the same asks, page identifiers, prompt version and
model: a changed question is a new reading. Only a page whose asks all agreed is
cached; one with a gap or a split reading is read again next time.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path


@dataclass
class Entry:
    url: str
    key: str                    # the asks, ids, prompt version and model it was read with
    retrieved: str              # YYYY-MM-DD, the last fetch whose bytes matched
    sha256: str
    answers: dict[str, dict] = field(default_factory=dict)   # ask id -> the agreed answer


def key(source, version: str, model: str) -> str:
    asks = [[a.id, a.ask, list(a.options), a.unit] for a in source.asks]
    blob = json.dumps([asks, list(source.ids), version, model], sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()


def age(retrieved: str, today: str) -> int | None:
    """Days from `retrieved` to `today`; None when either date does not parse."""
    try:
        return (date.fromisoformat(today) - date.fromisoformat(retrieved)).days
    except ValueError:
        return None


class PageCache:
    """A JSON file of entries keyed by URL. No path: nothing is kept between bids."""

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else None
        self.entries: dict[str, Entry] = {}
        if self.path and self.path.exists():
            for e in json.loads(self.path.read_text())["pages"]:
                self.entries[e["url"]] = Entry(**e)

    def get(self, url: str, k: str) -> Entry | None:
        e = self.entries.get(url)
        return e if e is not None and e.key == k else None

    def fresh(self, e: Entry, days: int, today: str) -> bool:
        """Read recently enough that its kind skips the fetch (`days` 0: never)."""
        n = age(e.retrieved, today)
        return days > 0 and n is not None and 0 <= n < days

    def put(self, e: Entry) -> None:
        self.entries[e.url] = e
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"pages": [asdict(x) for x in self.entries.values()]}, indent=1))
            os.replace(tmp, self.path)   # a bid stopped mid-write leaves the old file whole
