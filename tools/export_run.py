"""Export one run as a timeline the pipeline visual replays.

Everything comes from what the run already wrote: the ledger's `audit_log`
(one row per register write, model call, claim append, refusal, fetch and
audit verdict, each with its principal), the `claims` table, and
`recordings/calls.jsonl` when the run was live (one line per model call with
its token usage). Nothing is invented: a replay row bills nothing and says so;
a live row is matched to its usage line by unit and run number and priced
here, where it is tested, from the price row named like the call's model id.
A live row with no usage line, with more than one, with no model, with no
price table, or with no single row for its model is exported unpriced with
the reason, and the visual shows that instead of a figure (a figure standing
in for an unknown is what traceability forbids).

    python3 tools/export_run.py runs/nan-live --out timeline.json [--prices prices.json]

`prices.json` is what tools/fetch_prices.py wrote from the pricing page; it is
copied in with its source URL and fetch time so the visual can show both.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import re
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline import dag, roles  # noqa: E402

# Principals that write rows but have no node of their own: where the floor shows them.
OFF_FLOOR = {"field_crew": "ships"}   # the field crew's tape-measure rows come in through the door
STATION_OF_PRINCIPAL = {n.principal: n.id for n in dag.NODES if n.principal} | OFF_FLOOR

TIER = re.compile(r"\((?:for prompts )?(up to|over) ([\d,]+) tokens\)")
RUN = re.compile(r"run (\d+)")
SNAPSHOT = r"-\d{8}"   # a dated id (`claude-haiku-5-5-20260301`) is the model it is a snapshot of; any other suffix is another model


def same_model(asked: str, served: str) -> bool:
    """The ledger records the id the client asked for; calls.jsonl records the id the API says
    served the call. They name the same model when equal, or when the served id is the asked
    id plus a snapshot date."""
    return served == asked or re.fullmatch(re.escape(asked) + SNAPSHOT, served) is not None


def station_for(principal: str) -> str:
    return STATION_OF_PRINCIPAL.get(principal, principal)


def station_input(node: dag.Node) -> str:
    """What a station's model reads, for the floor: `sources` (a page from the register),
    `web` (a fetched page), `ledger` (rows other stations wrote), or nothing."""
    p = roles.PRINCIPALS.get(node.principal)
    if p is None:
        return ""
    if p.reads_sources is None or p.reads_sources:
        return "sources"
    if "fetched" in p.methods:
        return "web"
    feeds = {a for a, b, _ in dag.EDGES if b == node.id and a != "register"}
    return "ledger" if feeds and (feeds & {"ledger"} or any(dag.BY_ID[a].principal for a in feeds)) else ""


def row_key(name: str) -> str:
    """A price row's name as a model id prefix: `Claude Haiku 5.5 (for prompts up to
    100,000 tokens)` -> `claude-haiku-5-5`."""
    return re.sub(r"[^a-z0-9]+", "-", name.split("(")[0].lower()).strip("-")


def price_rows(models: dict, model: str, prompt_tokens: int) -> list[tuple[str, dict]]:
    """The price table rows for a call's model id: those whose key is the id, or the id less a
    snapshot date (`claude-haiku-5-5-20260301` is Haiku 5.5; `claude-opus-4-5` is not Opus 4,
    and gets no row when its own is missing), then, among tiers by prompt size, the one the
    call's prompt falls in. One row prices the call; none or several do not."""
    model = model.lower()
    hits = []
    for name, p in models.items():
        if not same_model(row_key(name), model):
            continue
        tier = TIER.search(name)
        if not tier:
            hits.append((name, p))
            continue
        side, limit = tier.group(1), int(tier.group(2).replace(",", ""))
        if (prompt_tokens > limit) == (side == "over"):
            hits.append((name, p))
    return hits


def priced(call: dict, prices: dict | None) -> dict:
    """The call with `cost` (USD) and `priced_as` (the row name) added, or `cost` None
    and `unpriced` saying why. Cache writes are priced at the 5-minute rate, the one
    the pipeline asks for."""
    u = call.get("usage")
    model = call.get("model") or ""
    if not prices or not prices.get("models"):
        return {**call, "cost": None, "priced_as": None, "unpriced": "no price table in this export"}
    if not model:
        return {**call, "cost": None, "priced_as": None, "unpriced": "the call names no model"}
    if not isinstance(u, dict) or not u:
        return {**call, "cost": None, "priced_as": None, "unpriced": "the usage line has no token counts"}
    prompt = sum(u.get(k) or 0 for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
    rows = price_rows(prices["models"], model, prompt)
    if len(rows) != 1:
        why = f"no price row for {model}" if not rows else f"{len(rows)} price rows match {model}"
        return {**call, "cost": None, "priced_as": None, "unpriced": why}
    name, p = rows[0]
    cost = ((u.get("input_tokens") or 0) * p["input"] + (u.get("cache_creation_input_tokens") or 0) * p["cache_write_5m"]
            + (u.get("cache_read_input_tokens") or 0) * p["cache_read"] + (u.get("output_tokens") or 0) * p["output"]) / 1e6
    return {**call, "cost": cost, "priced_as": name, "unpriced": None}


def usage_lines(run_dir: Path) -> dict[tuple[str, int], list[dict]]:
    """recordings/calls.jsonl by (unit id, run number). calls.jsonl is append-only, so a
    folder run twice holds two lines per call; the export keeps both and prices neither."""
    path = run_dir / "recordings" / "calls.jsonl"
    by_row: dict[tuple[str, int], list[dict]] = defaultdict(list)
    if path.exists():
        for line in path.read_text().splitlines():
            if line.strip():
                c = json.loads(line)
                by_row[(c.get("unit_id", ""), c.get("run"))].append(c)
    return by_row


def events(db: sqlite3.Connection, usage: dict | None = None, prices: dict | None = None) -> list[dict]:
    usage = usage or {}
    out = []
    for seq, at, principal, action, subject, detail in db.execute(
            "SELECT seq, at, principal, action, subject, detail FROM audit_log ORDER BY seq"):
        e = {"seq": seq, "at": at, "principal": principal, "station": station_for(principal),
             "action": action, "subject": subject or "", "detail": detail or ""}
        if action == "model-call":
            m = RUN.search(detail or "")
            e["run"] = int(m.group(1)) if m else None
            e["model"] = (detail or "").split(";")[0].strip()
            e["replay"] = e["model"].startswith("replay")
            e.update({"usage": None, "cost": None, "priced_as": None, "unpriced": None})
            if not e["replay"]:
                lines = usage.get((e["subject"], e["run"]), [])
                if len(lines) != 1:
                    e["unpriced"] = "no usage line in calls.jsonl" if not lines else f"{len(lines)} usage lines for this call"
                elif lines[0].get("model") and not same_model(e["model"], lines[0]["model"]):
                    # the ledger row is the record; a usage line naming another model prices nothing
                    e["unpriced"] = f"the ledger says {e['model']}, calls.jsonl says {lines[0]['model']}"
                else:
                    # priced by the id the API billed (the ledger's id, or its dated snapshot)
                    c = priced({**lines[0], "model": lines[0].get("model") or e["model"]}, prices)
                    e.update({"usage": c.get("usage"), "cost": c["cost"], "priced_as": c["priced_as"], "unpriced": c["unpriced"]})
        out.append(e)
    return out


def claims(db: sqlite3.Connection) -> list[dict]:
    cols = ("claim_id", "principal", "method", "role", "value", "unit", "flag", "source_id",
            "locator", "statement", "derivation", "written_at")
    rows = db.execute("SELECT claim_id, principal, method, role, value, unit, flag, source_id, locator,"
                      " statement, derivation, written_at FROM claims ORDER BY seq")
    return [dict(zip(cols, row)) for row in rows]


def export(run_dir: Path, prices: dict | None = None) -> dict:
    # read-only: the exporter never writes a ledger, and a folder without one is an error, not a new file
    uri = (run_dir / "ledger.db").resolve().as_uri() + "?mode=ro"
    with contextlib.closing(sqlite3.connect(uri, uri=True)) as db:
        meta = dict(db.execute("SELECT key, value FROM meta").fetchall())
        stations = [{"id": n.id, "label": n.label, "detail": n.detail, "status": n.status,
                     "principal": n.principal, "input": station_input(n)} for n in dag.NODES]
        edges = [{"from": a, "to": b, "label": label} for a, b, label in dag.EDGES]
        return {
            "job": meta.get("job", ""), "run_id": meta.get("run_id", ""), "created_at": meta.get("created_at", ""),
            "stations": stations, "edges": edges, "events": events(db, usage_lines(run_dir), prices),
            "claims": claims(db), "prices": prices,
            "draft": (run_dir / "proposal.md").exists(),   # the Scope Writer's draft, when the run rendered one
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
    calls = [e for e in data["events"] if e["action"] == "model-call" and not e["replay"]]
    unpriced = [e for e in calls if e["cost"] is None]
    if a.out:
        a.out.write_text(text)
        print(f"{a.out}: {len(data['events'])} events, {len(data['claims'])} claims, {len(calls)} billed calls"
              + (f", {len(unpriced)} unpriced" if unpriced else ""))
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
