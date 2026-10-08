"""The advisor's verdict: check its findings, render the PR comment, count what blocks.

The advisor (docs/advisor.md) is a model; this is the code that decides. A reply
that does not match docs/advisor-schema.json is a failed review, never repaired.
A blocking finding stands only when its basis names an architecture page, a
standing rule or a concrete failing input; otherwise it is demoted to advice.

    python3 tools/advisor.py review.json --out comment.md
    python3 tools/advisor.py review.json --usage execution.json   # adds what the review used

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


USAGE_KEYS = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")


def usage_line(execution: str) -> str:
    """What the review used, from the result message in Claude Code's execution output.

    The output is a JSON array of messages or one message per line; the last
    `result` message carries the totals. The cost is Claude Code's own figure.
    """
    text = execution.strip()
    try:
        messages = json.loads(text) if text.startswith("[") else [json.loads(x) for x in text.splitlines() if x.strip()]
    except ValueError:
        return "usage: not reported (execution output unreadable)"
    results = [m for m in messages if isinstance(m, dict) and m.get("type") == "result"]
    if not results:
        return "usage: not reported (no result message)"
    r = results[-1]
    failed = f"review failed: {str(r.get('result') or 'no reason given')[:300]}; " if r.get("is_error") else ""
    u = r.get("usage") or {}
    tokens = ", ".join(f"{u.get(k) or 0} {k}" for k in USAGE_KEYS)
    cost = r.get("total_cost_usd")
    cost = f", ${cost:.2f} as Claude Code reports it" if isinstance(cost, (int, float)) else ""
    return f"{failed}usage: {r.get('num_turns', '?')} turns, {tokens}{cost}"


def render(summary: str, findings: list[dict], usage: str | None = None) -> str:
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
    if usage:
        out.append(f"<sub>{usage}</sub>")
        out.append("")
    out.append("Brief: `docs/advisor.md`. A blocking finding fails the `advisor` check.")
    return "\n".join(out) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("review", type=Path, help="the advisor's JSON reply")
    ap.add_argument("--out", type=Path, help="write the PR comment here (default: stdout)")
    ap.add_argument("--usage", type=Path, help="Claude Code's execution output, to report what the review used")
    a = ap.parse_args(argv)
    usage = None
    if a.usage:
        usage = usage_line(a.usage.read_text()) if a.usage.exists() else "usage: not reported (no execution output)"
        print(usage)
    try:
        review = json.loads(a.review.read_text())
        findings = check(review)
    except ValueError as e:  # json.JSONDecodeError is a ValueError
        print(f"advisor reply unusable: {e}", file=sys.stderr)
        if a.out:
            a.out.write_text(f"{MARKER}\n## Advisor: no review\n\nThe advisor returned no usable reply ({e}).\n\n"
                             f"<sub>{usage or 'usage: not reported'}</sub>\n")
        return 2
    text = render(review["summary"], findings, usage)
    if a.out:
        a.out.write_text(text)
    else:
        sys.stdout.write(text)
    return 1 if any(f["severity"] == "blocking" for f in findings) else 0


if __name__ == "__main__":
    sys.exit(main())
