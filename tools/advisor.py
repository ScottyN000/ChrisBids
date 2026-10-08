"""The advisor's verdict: check its findings, render the PR comment, count what blocks.

The advisor (docs/advisor.md) is a model; this is the code that decides. A reply
that does not match docs/advisor-schema.json is a failed review, never repaired.
A blocking finding stands only when its basis names an architecture page, a
standing rule or a concrete failing input; otherwise it is demoted to advice.

    python3 tools/advisor.py review.json --out comment.md

Exit 0 when nothing blocks, 1 when something does, 2 when the reply is unusable.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline.readers.validate import errors  # noqa: E402

SCHEMA = json.loads((ROOT / "docs" / "advisor-schema.json").read_text())
MARKER = "<!-- chrisbids-advisor -->"
# "arch p.6", "rule: Determinism, ...", "input <X> gives <Y>, should be <Z>"
BASIS = re.compile(r"^(arch p\.\d+|rule: \S|input \S.* gives \S)", re.IGNORECASE)


def check(review: dict) -> list[dict]:
    """The findings with severity settled by rule. Raises ValueError on a bad reply."""
    problems = errors(review, SCHEMA)
    if problems:
        raise ValueError("; ".join(problems))
    settled = []
    for f in review["findings"]:
        f = dict(f, demoted=False)
        if f["severity"] == "blocking" and not BASIS.match(f["basis"] or ""):
            f.update(severity="advice", demoted=True)
        settled.append(f)
    return settled


def _where(f: dict) -> str:
    if not f["file"]:
        return "whole PR"
    return f"`{f['file']}:{f['line']}`" if f["line"] else f"`{f['file']}`"


def render(summary: str, findings: list[dict]) -> str:
    blocking = [f for f in findings if f["severity"] == "blocking"]
    advice = [f for f in findings if f["severity"] == "advice"]
    head = f"{len(blocking)} blocking, {len(advice)} advice" if blocking else f"nothing blocking, {len(advice)} advice"
    out = [MARKER, f"## Advisor: {head}", "", summary.strip(), ""]
    for n, f in enumerate(blocking + advice, 1):
        basis = f" ({f['basis']})" if f["basis"] else ""
        out.append(f"**{n}. {f['severity']} · {f['area']}** {_where(f)}{basis}")
        if f["demoted"]:
            out.append("_Demoted from blocking: the basis names no architecture page, rule or failing input._")
        out += ["", f["finding"].strip(), "", f"Fix: {f['fix'].strip()}", ""]
    out.append("Brief: `docs/advisor.md`. A blocking finding fails the `advisor` check.")
    return "\n".join(out) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("review", type=Path, help="the advisor's JSON reply")
    ap.add_argument("--out", type=Path, help="write the PR comment here (default: stdout)")
    a = ap.parse_args(argv)
    try:
        review = json.loads(a.review.read_text())
        findings = check(review)
    except ValueError as e:  # json.JSONDecodeError is a ValueError
        print(f"advisor reply unusable: {e}", file=sys.stderr)
        return 2
    text = render(review["summary"], findings)
    if a.out:
        a.out.write_text(text)
    else:
        sys.stdout.write(text)
    return 1 if any(f["severity"] == "blocking" for f in findings) else 0


if __name__ == "__main__":
    sys.exit(main())
