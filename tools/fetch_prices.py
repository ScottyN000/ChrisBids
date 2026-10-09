"""Fetch the model price table from the pricing page and write it as JSON.

Prices are never typed in by hand: this reads the page's own table and records
the URL it was read from and the time, so a figure shown anywhere downstream
cites both. The URL is fixed (SECURITY.md); a redirect leaving the pricing host
is refused, and the page is read up to the pipeline's own size cap.

    python3 tools/fetch_prices.py --out prices.json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.web import MAX_BYTES  # noqa: E402

HOST = "https://platform.claude.com/"
URL = HOST + "docs/en/about-claude/pricing.md"
COLUMNS = ("input", "cache_write_5m", "cache_write_1h", "cache_read", "output")
PRICE = re.compile(r"\$([\d.]+)\s*/\s*MTok")


class OnHost(urllib.request.HTTPRedirectHandler):
    """Follow a redirect only to the pricing host over https; anything else is an error."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not newurl.startswith(HOST):
            raise urllib.error.HTTPError(newurl, code, f"redirect off {HOST} refused", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def parse(markdown: str) -> dict[str, dict[str, float]]:
    """Rows of the model table: name -> prices in dollars per million tokens."""
    models = {}
    for line in markdown.splitlines():
        if not line.startswith("| Claude "):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        name = re.sub(r"\s*\(\[.*?\]\(.*?\)\)", "", cells[0]).strip()   # drop "(retired ...)" links
        figures = [PRICE.search(c) for c in cells[1:]]
        if len(figures) == 5 and all(figures):
            models[name] = {k: float(m.group(1)) for k, m in zip(COLUMNS, figures)}
    return models


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    req = urllib.request.Request(URL, headers={"User-Agent": "chrisbids-fetch-prices/1 (python-urllib)"})
    with urllib.request.build_opener(OnHost()).open(req, timeout=30) as r:  # nosec B310 - one fixed https URL, redirects held to its host
        data = r.read(MAX_BYTES + 1)
        read_from = r.geturl()
    if len(data) > MAX_BYTES:
        print(f"the pricing page is larger than {MAX_BYTES} bytes; not read", file=sys.stderr)
        return 1
    models = parse(data.decode())
    if not models:
        print("no model table found on the page; its layout may have changed", file=sys.stderr)
        return 1
    out = {"source_url": read_from, "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
           "unit": "USD per million tokens", "models": models}
    with open(a.out, "w") as f:
        json.dump(out, f, indent=1)
    print(f"{a.out}: {len(models)} models from {read_from}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
