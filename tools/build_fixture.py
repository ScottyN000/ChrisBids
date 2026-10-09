#!/usr/bin/env python3
"""Validate a golden-fixture ledger and render its outputs.

Usage:
    python3 tools/build_fixture.py fixtures/nantucket          # write outputs
    python3 tools/build_fixture.py fixtures/nantucket --check  # fail if outputs are stale

Inputs (hand-authored, per job folder):
    register.csv   Source Register: one row per source document / photo / message
    ledger.yaml    Claim ledger rows plus the proposal layout

Outputs (generated, never hand-edited):
    ledger.csv     Flat ledger in the architecture doc's field order (p.5-6)
    proposal.md    Proposal rendered from the phrase library + ledger rows only
    xref.csv       Every proposal paragraph with the claim and phrase IDs it draws on
    corrections.md What changed from the hand-made test bid, generated from `supersedes`

Models extract; code computes. Any row with a `calc` expression is re-evaluated
here and must equal its stated value, or the build fails.
"""
import argparse
import csv
import io
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
PHRASES = ROOT / "fixtures" / "phrase-library.yaml"
sys.path.insert(0, str(ROOT))
from pipeline.schema import arith  # noqa: E402  (the one arithmetic evaluator; no eval)
from pipeline.proposal import fmt_value, render, xref_csv  # noqa: E402  (shared with the Scope Writer)

METHODS = {"dimensioned", "counted", "scaled", "clause", "observed", "fetched", "customer", "FIELD"}
CONFIDENCE = {"exact", "scaled", "inferred", "missing"}
FLAGS = {"", "unverified", "conflict"}
ROLES = {"header", "scope", "quantity", "allowance", "material", "code", "exclusion", "question", "note"}
# Methods that may feed a Contractor Co. allowance or an order quantity (architecture p.6).
ALLOWANCE_OK = {"dimensioned", "counted", "clause", "FIELD"}

LEDGER_FIELDS = [
    "claim_id", "value", "unit", "statement", "source_id", "locator", "method",
    "derivation", "confidence", "agent", "timestamp", "audit",
    "division", "role", "part", "flag", "question", "url", "retrieved", "quote",
    "supersedes",
]


class BuildError(Exception):
    pass


def load(job_dir: Path):
    data = yaml.safe_load((job_dir / "ledger.yaml").read_text())
    with open(job_dir / "register.csv", newline="") as f:
        register = {r["source_id"]: r for r in csv.DictReader(f)}
    phrases = yaml.safe_load(PHRASES.read_text())["phrases"]
    return data, register, phrases


def validate(data, register, phrases):
    errors = []
    rows = data["rows"]
    by_id = {}
    for r in rows:
        rid = r.get("id", "?")
        if rid in by_id:
            errors.append(f"{rid}: duplicate claim_id")
        by_id[rid] = r
        for k in ("statement", "source", "method", "role", "confidence"):
            if not r.get(k):
                errors.append(f"{rid}: missing {k}")
        m = r.get("method")
        if m not in METHODS:
            errors.append(f"{rid}: method {m!r} not in {sorted(METHODS)}")
        if r.get("confidence") not in CONFIDENCE:
            errors.append(f"{rid}: confidence {r.get('confidence')!r} not in {sorted(CONFIDENCE)}")
        if r.get("flag", "") not in FLAGS:
            errors.append(f"{rid}: flag {r.get('flag')!r} not in {sorted(FLAGS)}")
        if r.get("role") not in ROLES:
            errors.append(f"{rid}: role {r.get('role')!r} not in {sorted(ROLES)}")
        if r.get("audit"):
            errors.append(f"{rid}: audit is set; only the Auditor writes that field")

        for src in str(r.get("source", "")).split("+"):
            src = src.strip()
            if src == "none":
                continue
            if src not in register:
                errors.append(f"{rid}: source {src!r} not in register.csv")
            elif register[src]["status"] != "present" and r.get("flag") != "unverified":
                errors.append(f"{rid}: cites {src} ({register[src]['status']}) but is not flagged unverified")

        v = r.get("value")
        if m == "observed" and v is not None:
            errors.append(f"{rid}: observed rows may not carry a number")
        if m == "FIELD" and v is not None:
            errors.append(f"{rid}: FIELD rows have a blank value")
        if m == "scaled" and r.get("confidence") != "scaled":
            errors.append(f"{rid}: scaled rows must have confidence scaled")
        if m == "fetched":
            if not r.get("url") or not r.get("retrieved"):
                errors.append(f"{rid}: fetched rows need url and retrieved")
            if not r.get("quote") and r.get("flag") != "unverified":
                errors.append(f"{rid}: fetched row without a quote must be flagged unverified")
        if m == "customer" and not r.get("quote"):
            errors.append(f"{rid}: customer rows carry the instruction verbatim in quote")
        if r.get("role") in ("allowance", "material") and m not in ALLOWANCE_OK:
            errors.append(f"{rid}: {m} value may not feed an allowance or order quantity")
        if r.get("question") and r["question"] not in by_id and r["question"] not in {x.get("id") for x in rows}:
            errors.append(f"{rid}: question {r['question']} not found")

    # calc replay: code recomputes every derived figure.
    for r in rows:
        expr = r.get("calc")
        if not expr:
            continue
        rid = r["id"]
        refs = re.findall(r"\{([A-Z0-9-]+)\}", expr)
        env_expr = expr
        for ref in refs:
            src = by_id.get(ref)
            if src is None:
                errors.append(f"{rid}: calc references unknown {ref}")
                break
            if src.get("value") is None or not isinstance(src.get("value"), (int, float)):
                errors.append(f"{rid}: calc input {ref} has no numeric value")
                break
            if r.get("role") in ("allowance", "material") and src["method"] not in ALLOWANCE_OK:
                errors.append(f"{rid}: calc input {ref} is {src['method']}; cannot feed an order quantity")
            env_expr = env_expr.replace("{" + ref + "}", repr(src["value"]))
        else:
            if not re.fullmatch(r"[0-9.+\-*/() ]+", env_expr):
                errors.append(f"{rid}: calc {expr!r} is not plain arithmetic")
                continue
            try:
                got = arith(env_expr)
            except (ValueError, ZeroDivisionError) as e:
                errors.append(f"{rid}: calc {expr!r} does not evaluate: {e}")
                continue
            if abs(got - (r.get("value") or 0)) > 1e-9:
                errors.append(f"{rid}: calc {expr} = {got}, ledger says {r.get('value')}")

    for sec in data["proposal"]["sections"]:
        for task in sec.get("tasks", []):
            for it in task.get("items", []) + [{"row": a} for a in task.get("allowance", [])]:
                if "row" in it and it["row"] not in by_id:
                    errors.append(f"layout: unknown row {it['row']}")
                if "phrase" in it and it["phrase"] not in phrases:
                    errors.append(f"layout: unknown phrase {it['phrase']}")
            if task.get("close") and task["close"] not in phrases:
                errors.append(f"layout: unknown phrase {task['close']}")
            for a in task.get("allowance", []):
                if a in by_id and by_id[a]["role"] != "allowance":
                    errors.append(f"layout: {a} used as allowance but role is {by_id[a]['role']}")
    for key in data["proposal"].get("exclusion_phrases", []) + data["proposal"].get("terms", []):
        if key not in phrases:
            errors.append(f"layout: unknown phrase {key}")
    if errors:
        raise BuildError("\n".join(errors))
    return by_id


def ledger_csv(data):
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=LEDGER_FIELDS, lineterminator="\n")
    w.writeheader()
    for r in data["rows"]:
        w.writerow({
            "claim_id": r["id"], "value": "" if r.get("value") is None else r["value"],
            "unit": r.get("unit", ""), "statement": r["statement"], "source_id": r["source"],
            "locator": r.get("locator", ""), "method": r["method"],
            "derivation": r.get("derivation", "") or (r.get("calc") or ""),
            "confidence": r["confidence"], "agent": data["agent"], "timestamp": data["timestamp"],
            "audit": "", "division": r.get("division", ""), "role": r["role"],
            "part": r.get("part", ""), "flag": r.get("flag", ""), "question": r.get("question", ""),
            "url": r.get("url", ""), "retrieved": r.get("retrieved", ""), "quote": r.get("quote", ""),
            "supersedes": r.get("supersedes", ""),
        })
    return buf.getvalue()


def corrections_md(data):
    lines = [f"# Corrections to the hand-made {data['job_name']} test bid\n",
             f"Generated from the `supersedes` field of [ledger.yaml](ledger.yaml). The original PDF "
             f"({data['original']}) is unchanged in the project's source folder.\n",
             "| Hand bid said | Now | Why | Claim |", "|---|---|---|---|"]
    for r in data["rows"]:
        if r.get("supersedes"):
            if r.get("value") is None and r["method"] != "FIELD":
                now = r["statement"]
            else:
                now = f"{fmt_value(r.get('value'))} {r.get('unit', '')}".strip() + f" — {r['statement']}"
            lines.append(f"| {r['supersedes']} | {now} | {r.get('reason', '')} | {r['id']} |"
                         .replace("\n", " "))
    for d in data.get("dropped", []):
        lines.append(f"| {d['was']} | removed | {d['why']} | — |")
    return "\n".join(lines) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("job_dir", type=Path)
    ap.add_argument("--check", action="store_true", help="fail if generated files differ from disk")
    args = ap.parse_args()
    data, register, phrases = load(args.job_dir)
    try:
        by_id = validate(data, register, phrases)
    except BuildError as e:
        print(f"FAIL {args.job_dir}\n{e}", file=sys.stderr)
        return 1
    proposal, xref = render(data, by_id, phrases, register)
    outputs = {
        "ledger.csv": ledger_csv(data),
        "proposal.md": proposal,
        "xref.csv": xref_csv(xref),
        "corrections.md": corrections_md(data),
    }
    stale = []
    for name, text in outputs.items():
        path = args.job_dir / name
        if args.check:
            if not path.exists() or path.read_text() != text:
                stale.append(name)
        else:
            path.write_text(text)
    if stale:
        print(f"FAIL {args.job_dir}: stale outputs {stale}; re-run without --check", file=sys.stderr)
        return 1
    n = len(data["rows"])
    print(f"OK {args.job_dir}: {n} rows, "
          f"{sum(r['method'] == 'FIELD' for r in data['rows'])} FIELD, "
          f"{sum(bool(r.get('flag')) for r in data['rows'])} flagged, "
          f"{sum(r['role'] == 'question' for r in data['rows'])} open questions")
    return 0


if __name__ == "__main__":
    sys.exit(main())
