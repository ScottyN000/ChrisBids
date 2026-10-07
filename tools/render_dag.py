#!/usr/bin/env python3
"""Render docs/pipeline-dag.md from pipeline/dag.py.

    python3 tools/render_dag.py           # write it
    python3 tools/render_dag.py --check   # fail if it is stale
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline import dag  # noqa: E402

OUT = ROOT / "docs" / "pipeline-dag.md"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()
    text = dag.document()
    if a.check:
        if not OUT.exists() or OUT.read_text() != text:
            print(f"FAIL {OUT.relative_to(ROOT)} is stale; run python3 tools/render_dag.py", file=sys.stderr)
            return 1
        print(f"OK {OUT.relative_to(ROOT)}")
        return 0
    OUT.write_text(text)
    print(f"wrote {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
