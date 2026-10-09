"""Export one run as a timeline the pipeline visual replays.

Everything comes from what the run already wrote: the ledger's `audit_log`
(one row per register write, model call, claim append and refusal, each with
its principal), the `claims` table, and `recordings/calls.jsonl` when the run
was live (one line per model call with its token usage). Nothing is invented:
a replay run has no calls.jsonl, so it exports no usage and the visual says so.

    python3 tools/export_run.py runs/nan-live --out timeline.json [--prices prices.json]

`prices.json` is what tools/fetch_prices.py wrote from the pricing page; it is
copied in with its source URL and fetch time so the visual can show both.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline import dag  # noqa: E402

STATION_OF_PRINCIPAL = {n.principal: n.id for n in dag.NODES if n.principal}
# Principals that write without a node of their own.
STATION_OF_PRINCIPAL.setdefault("ledger", "ledger")

MODEL_CALL = re.compile(r"(?P<model>[\w.\-]+)?;?\s*(?P<prompt>\w+@[0-9a-f]+)?.*?run (?P<run>\d+)")


def station_for(principal: str) -> str:
    return STATION_OF_PRINCIPAL.get(principal, principal)


def events(db: sqlite3.Connection) -> list[dict]:
    out = []
    for seq, at, principal, action, subject, detail in db.execute(
            "SELECT seq, at, principal, action, subject, detail FROM audit_log ORDER BY seq"):
        e = {"seq": seq, "at": at, "principal": principal, "station": station_for(principal),
             "action": action, "subject": subject or "", "detail": detail or ""}
        if action == "model-call":
            m = re.search(r"run (\d+)", detail or "")
            e["run"] = int(m.group(1)) if m else None
            e["model"] = (detail or "").split(";")[0].strip()
            e["replay"] = "replay" in (detail or "")
        out.append(e)
    return out


def claims(db: sqlite3.Connection) -> list[dict]:
    cols = ("claim_id", "principal", "method", "role", "value", "unit", "flag", "source_id",
            "locator", "statement", "derivation", "written_at")
    rows = db.execute("SELECT claim_id, principal, method, role, value, unit, flag, source_id, locator,"
                      " statement, derivation, written_at FROM claims ORDER BY seq")
    return [dict(zip(cols, row)) for row in rows]


def calls(run_dir: Path) -> list[dict]:
    path = run_dir / "recordings" / "calls.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def export(run_dir: Path, prices: dict | None = None) -> dict:
    db = sqlite3.connect(run_dir / "ledger.db")
    meta = dict(db.execute("SELECT key, value FROM meta").fetchall())
    stations = [{"id": n.id, "label": n.label, "detail": n.detail, "status": n.status,
                 "principal": n.principal} for n in dag.NODES]
    edges = [{"from": a, "to": b, "label": label} for a, b, label in dag.EDGES]
    return {
        "job": meta.get("job", ""), "run_id": meta.get("run_id", ""), "created_at": meta.get("created_at", ""),
        "stations": stations, "edges": edges, "events": events(db), "claims": claims(db),
        "calls": calls(run_dir), "prices": prices,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("run_dir", type=Path)
    ap.add_argument("--out", type=Path)
    ap.add_argument("--prices", type=Path, help="tools/fetch_prices.py output")
    a = ap.parse_args(argv)
    prices = json.loads(a.prices.read_text()) if a.prices else None
    data = export(a.run_dir, prices)
    text = json.dumps(data, indent=1)
    if a.out:
        a.out.write_text(text)
        print(f"{a.out}: {len(data['events'])} events, {len(data['claims'])} claims, {len(data['calls'])} calls")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
