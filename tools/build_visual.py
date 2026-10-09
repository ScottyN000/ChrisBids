"""Build the Bid Shop Floor page: the visual template with run exports embedded.

    python3 tools/build_visual.py --out bid-shop.html nantucket=runs/nan.json ocean-beach=runs/obv.json

Each `name=timeline.json` is a tools/export_run.py export; the page lists them
by name and can also load more at view time. The template is
tools/visual/bid_shop.html; `__RUNS__` is replaced with the JSON object. The
page is self-contained: no network, no library, so it can be opened from a file.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

TEMPLATE = Path(__file__).resolve().parent / "visual" / "bid_shop.html"


def build(runs: dict[str, dict]) -> str:
    payload = json.dumps(runs, separators=(",", ":")).replace("</", "<\\/")   # never close the script tag early
    return TEMPLATE.read_text().replace("__RUNS__", payload)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("runs", nargs="+", help="name=timeline.json")
    a = ap.parse_args(argv)
    runs = {}
    for spec in a.runs:
        name, _, path = spec.partition("=")
        data = json.loads(Path(path).read_text())
        data["title"] = f"{data.get('job', name)} · {name}"
        runs[name] = data
    a.out.write_text(build(runs))
    print(f"{a.out}: {len(runs)} runs, {a.out.stat().st_size} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
