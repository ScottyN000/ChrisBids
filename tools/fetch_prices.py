"""Fetch the model price table from the pricing page and write it as JSON.

Prices are never typed in by hand: this reads the page's own table and records
the URL and the time, so a figure shown anywhere downstream cites both.

    python3 tools/fetch_prices.py --out prices.json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from datetime import datetime, timezone

URL = "https://platform.claude.com/docs/en/about-claude/pricing.md"
COLUMNS = ("input", "cache_write_5m", "cache_write_1h", "cache_read", "output")
PRICE = re.compile(r"\$([\d.]+)\s*/\s*MTok")


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
    ap.add_argument("--url", default=URL)
    a = ap.parse_args(argv)
    if not a.url.startswith("https://"):
        print("the pricing page is read over https only", file=sys.stderr)
        return 2
    req = urllib.request.Request(a.url, headers={"User-Agent": "chrisbids-fetch-prices/1 (python-urllib)"})
    with urllib.request.urlopen(req, timeout=30) as r:  # nosec B310 - https only, checked above
        text = r.read().decode()
    models = parse(text)
    if not models:
        print("no model table found on the page; its layout may have changed", file=sys.stderr)
        return 1
    out = {"source_url": a.url, "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
           "unit": "USD per million tokens", "models": models}
    with open(a.out, "w") as f:
        json.dump(out, f, indent=1)
    print(f"{a.out}: {len(models)} models from {a.url}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
