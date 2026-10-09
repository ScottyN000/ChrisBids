"""Export one run as a timeline the pipeline visual replays.

Everything comes from what the run already wrote: the ledger's `audit_log`
(one row per register write, model call, claim append and refusal, each with
its principal), the `claims` table, and `recordings/calls.jsonl` when the run
was live (one line per model call with its token usage). Nothing is invented:
a replay run has no calls.jsonl, so it exports no usage and the visual says so.

    python3 tools/export_run.py runs/nan-live --out timeline.json [--prices prices.json]

`prices.json` is what tools/fetch_prices.py wrote from the pricing page; it is
copied in with its source URL and fetch time so the visual can show both. Each
call's cost is worked out here, where it is tested, from the price row whose
name matches the call's model id; a call with no row, or a run with no table,
is marked unpriced with the reason, and the visual shows that instead of a
figure (a figure standing in for an unknown is what traceability forbids).
"""
from __future__ import annotations

import argparse
import contextlib
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

TIER = re.compile(r"\((?:for prompts )?(up to|over) ([\d,]+) tokens\)")


def station_for(principal: str) -> str:
    return STATION_OF_PRINCIPAL.get(principal, principal)


def row_key(name: str) -> str:
    """A price row's name as a model id prefix: `Claude Haiku 5.5 (for prompts up to
    100,000 tokens)` -> `claude-haiku-5-5`."""
    return re.sub(r"[^a-z0-9]+", "-", name.split("(")[0].lower()).strip("-")


def price_row(models: dict, model: str, prompt_tokens: int) -> tuple[str, dict] | None:
    """The price table row for a call's model id: the row whose key is the id or the id's
    longest prefix at a `-` (`claude-sonnet-4-5` is Sonnet 4.5, not Sonnet 4; a dated id
    `claude-haiku-5-5-20260301` is Haiku 5.5); among tiers by prompt size, the one the
    call's prompt falls in. None when no row matches."""
    model = model.lower()
    best = [(name, p) for name, p in models.items()
            if model == row_key(name) or model.startswith(row_key(name) + "-")]
    if not best:
        return None
    longest = max(len(row_key(name)) for name, _ in best)
    best = [(name, p) for name, p in best if len(row_key(name)) == longest]
    for name, p in best:
        tier = TIER.search(name)
        if not tier:
            return name, p
        side, limit = tier.group(1), int(tier.group(2).replace(",", ""))
        if (prompt_tokens > limit) == (side == "over"):
            return name, p
    return None


def priced(call: dict, prices: dict | None) -> dict:
    """The call with `cost` (USD) and `priced_as` (the row name) added, or `cost` None
    and `unpriced` saying why. Cache writes are priced at the 5-minute rate, the one
    the pipeline asks for."""
    u = call.get("usage") or {}
    prompt = sum(u.get(k) or 0 for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
    model = call.get("model") or ""
    if not prices or not prices.get("models"):
        return {**call, "cost": None, "priced_as": None, "unpriced": "no price table in this export"}
    if not model:
        return {**call, "cost": None, "priced_as": None, "unpriced": "the call names no model"}
    row = price_row(prices["models"], model, prompt)
    if not row:
        return {**call, "cost": None, "priced_as": None, "unpriced": f"no price row for {model}"}
    name, p = row
    cost = ((u.get("input_tokens") or 0) * p["input"] + (u.get("cache_creation_input_tokens") or 0) * p["cache_write_5m"]
            + (u.get("cache_read_input_tokens") or 0) * p["cache_read"] + (u.get("output_tokens") or 0) * p["output"]) / 1e6
    return {**call, "cost": cost, "priced_as": name, "unpriced": None}


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
            e["replay"] = e["model"].startswith("replay")
        out.append(e)
    return out


def claims(db: sqlite3.Connection) -> list[dict]:
    cols = ("claim_id", "principal", "method", "role", "value", "unit", "flag", "source_id",
            "locator", "statement", "derivation", "written_at")
    rows = db.execute("SELECT claim_id, principal, method, role, value, unit, flag, source_id, locator,"
                      " statement, derivation, written_at FROM claims ORDER BY seq")
    return [dict(zip(cols, row)) for row in rows]


def calls(run_dir: Path, prices: dict | None = None) -> list[dict]:
    path = run_dir / "recordings" / "calls.jsonl"
    if not path.exists():
        return []
    return [priced(json.loads(line), prices) for line in path.read_text().splitlines() if line.strip()]


def export(run_dir: Path, prices: dict | None = None) -> dict:
    # read-only: the exporter never writes a ledger, and a folder without one is an error, not a new file
    uri = (run_dir / "ledger.db").resolve().as_uri() + "?mode=ro"
    with contextlib.closing(sqlite3.connect(uri, uri=True)) as db:
        meta = dict(db.execute("SELECT key, value FROM meta").fetchall())
        stations = [{"id": n.id, "label": n.label, "detail": n.detail, "status": n.status,
                     "principal": n.principal} for n in dag.NODES]
        edges = [{"from": a, "to": b, "label": label} for a, b, label in dag.EDGES]
        return {
            "job": meta.get("job", ""), "run_id": meta.get("run_id", ""), "created_at": meta.get("created_at", ""),
            "stations": stations, "edges": edges, "events": events(db), "claims": claims(db),
            "calls": calls(run_dir, prices), "prices": prices,
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
