"""Export one run as a timeline the pipeline visual replays.

Everything comes from what the run already wrote: the ledger's `audit_log`
(one row per register write, model call, claim append, refusal, fetch and
audit verdict, each with its principal), the `claims` table, and
`recordings/calls.jsonl` when the run was live (one line per model call with
its token usage). Nothing is invented: a replay row bills nothing and says so;
a live row is matched to its usage line by unit and run number and priced
here, where it is tested, from the price row named like the call's model id.
A live row with no usage line, with more than one, with a line naming
another model, with a line that has no token counts, with no model, with no
price table, or with no single row for its model is exported unpriced with
the reason, and the visual shows that instead of a figure (a figure standing
in for an unknown is what traceability forbids). The export also says
whether the run folder holds the Scope Writer's draft, what the last line of
bid.txt says when the run was a whole bid (a file older than the ledger was
left by an earlier run into the folder and does not count, which is told by
file times, so export the folder the run happened in or a copy that keeps
them), how each fetch ended and which host was refused, and, from those and
the log, how the run ended (`ending`): whether the finished bid is on the
floor and, if not, why. The page draws that; it decides nothing.

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
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline import dag, roles  # noqa: E402

# Principals that write rows but have no node of their own: where the floor shows them.
OFF_FLOOR = {"field_crew": "ships"}   # the field crew's tape-measure rows come in through the door
STATION_OF_PRINCIPAL = {n.principal: n.id for n in dag.NODES if n.principal} | OFF_FLOOR

TIER = re.compile(r"\((?:for prompts )?(up to|over) ([\d,]+) tokens\)")
RUN = re.compile(r"run (\d+)")
SNAPSHOT = r"-\d{8}"   # a dated id (`claude-haiku-5-5-20260301`) is the model it is a snapshot of; any other suffix is another model
OFF_LIST = re.compile(r"^not fetched: \S+ is not on the allowlist(;|$)")   # web.allowed's refusal, as web.Fetcher logs it
HOST_IN = re.compile(r"^not fetched: (\S+) (?:is not on the allowlist|is the local machine|is a non-public address|resolves to non-public address|does not resolve)")
CREATED = "%Y-%m-%dT%H:%M:%SZ"   # the ledger's created_at, as Broker.open_job writes it


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


def fetch_kind(action: str, detail: str) -> str:
    """How a fetch row ended, for the floor. A `fetch` row's detail is the fetcher's: the
    retrieval date, then `sha256 ...` of the page read or the error. `read`: a page hash.
    `off-list`: the allowlist refused the host. `unsafe`: the address guard refused the URL (a
    non-http scheme, credentials in it, no host, a loopback, private or non-public address),
    which the fetcher logs as `not fetched: ...` and the broker, checking before any fetch, as
    a `denied` row on `fetch`. `failed`: anything else (did not open, an HTTP status, too large,
    nothing readable, a host that does not resolve)."""
    if action == "denied":
        return "unsafe"
    note = re.sub(r"^[^;]*;\s*", "", detail)
    if note.startswith("sha256 "):
        return "read"
    if OFF_LIST.match(note):
        return "off-list"
    if note.startswith("not fetched: ") and "does not resolve" not in note:
        return "unsafe"
    return "failed"


def refused_host(action: str, detail: str) -> str:
    """What a refused fetch names, for the floor: the host the allowlist or the address guard
    refused, which on a redirect is that hop's host and not the requested page's (the fetcher
    checks every hop), or the host of the URL the broker refused; the refusal's own words when
    it names no host (a non-http scheme, credentials, no host). Empty for a read or a failed one."""
    if action == "denied":
        url, _, why = detail.partition(": ")
        return urlsplit(url).hostname or why or detail
    note = re.sub(r"^[^;]*;\s*", "", detail)
    m = HOST_IN.match(note)
    if m:
        return m.group(1)
    return note[len("not fetched: "):].split("; served by")[0] if note.startswith("not fetched: ") else ""


def this_runs(path: Path, created_at: str | None) -> bool:
    """Whether a file in the run folder is this run's: it exists and was written at or after the
    ledger's `created_at`. Every command that rebuilds the ledger sets that, and none removes an
    older proposal.md or bid.txt, so an older file is an earlier run's and does not count; nor
    does any file when the ledger has no readable `created_at`. File times are all the pipeline
    records for this, so a copy of the folder made without keeping them (cp -r, an archive that
    drops them) makes every file look like this run's: export where the run happened, or from a
    copy that keeps times."""
    try:
        since = datetime.strptime(created_at or "", CREATED).replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return False
    return path.exists() and path.stat().st_mtime >= since


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
    if not isinstance(u, dict) or not all(isinstance(u.get(k), int) for k in ("input_tokens", "output_tokens")):
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
        elif action == "fetch" or (action == "denied" and subject == "fetch"):
            e["fetch"] = fetch_kind(action, e["detail"])
            e["fetch_host"] = refused_host(action, e["detail"]) if e["fetch"] in ("off-list", "unsafe") else ""
        out.append(e)
    return out


def claims(db: sqlite3.Connection) -> list[dict]:
    cols = ("claim_id", "principal", "method", "role", "value", "unit", "flag", "source_id",
            "locator", "statement", "derivation", "written_at")
    rows = db.execute("SELECT claim_id, principal, method, role, value, unit, flag, source_id, locator,"
                      " statement, derivation, written_at FROM claims ORDER BY seq")
    return [dict(zip(cols, row)) for row in rows]


def bid_verdict(run_dir: Path, meta: dict) -> str | None:
    """The verdict of a whole-bid run (`pipeline bid`, run id `bid-<job>` in the ledger's meta):
    `OK` or `NOT OK` from the last line of its bid.txt, which folds in the auditor's orphan-figure
    and source-hash checks, or `missing` when the run wrote none (it stopped before the verdict,
    and a bid.txt older than the ledger is an earlier run's) or the line is not one. None for any
    other run: `pipeline scope` and `pipeline replay` rebuild the ledger in place and leave an
    older bid.txt where it was, so that file says nothing about them."""
    if meta.get("run_id") != f"bid-{meta.get('job', '')}":
        return None
    path = run_dir / "bid.txt"
    if not this_runs(path, meta.get("created_at")):
        return "missing"
    last = path.read_text().rstrip().rsplit("\n", 1)[-1]
    return last[len("bid: "):] if last.startswith("bid: ") else "missing"


def ending(evs: list[dict], draft: bool, verdict: str | None) -> dict:
    """What the log and the run folder show the run reached, and whether the finished bid is on
    the floor: every Scope Writer run valid (the call row's detail ends `; valid`), a draft in
    the folder, audit rows with no `fail`, and on a whole bid a bid.txt that says OK. `why` names
    the first of those that fails, or is empty when the bid ships; `drafted` is the first two
    together (this run wrote the draft on the tray)."""
    sw = [e for e in evs if e["action"] == "model-call" and e["station"] == "scope_writer"]
    verdicts = [e["detail"].split(":")[0].strip() for e in evs if e["action"] == "audit"]
    r = {"scope_runs": len(sw), "valid": sum(1 for e in sw if re.search(r";\s*valid$", e["detail"])),
         "draft": bool(draft), "audits": len(verdicts), "passed": verdicts.count("pass"),
         "unverified": verdicts.count("unverified"), "failed": verdicts.count("fail"), "verdict": verdict}
    if not r["scope_runs"]:
        why = "the Scope Writer did not run"
    elif r["valid"] < r["scope_runs"]:
        why = "a Scope Writer run was discarded"
    elif not r["draft"]:
        why = "no draft in the run folder"
    elif not r["audits"]:
        why = "no audit rows"
    elif r["failed"]:
        why = f"{r['failed']} row{'' if r['failed'] == 1 else 's'} failed"
    elif verdict == "missing":
        why = "the bid run wrote no verdict"
    elif verdict is not None and verdict != "OK":
        why = f"bid.txt says {verdict}"
    else:
        why = ""
    return {**r, "drafted": bool(r["scope_runs"] and r["valid"] == r["scope_runs"] and r["draft"]), "why": why, "ships": not why}


def export(run_dir: Path, prices: dict | None = None) -> dict:
    # read-only: the exporter never writes a ledger, and a folder without one is an error, not a new file
    uri = (run_dir / "ledger.db").resolve().as_uri() + "?mode=ro"
    with contextlib.closing(sqlite3.connect(uri, uri=True)) as db:
        meta = dict(db.execute("SELECT key, value FROM meta").fetchall())
        stations = [{"id": n.id, "label": n.label, "detail": n.detail, "status": n.status,
                     "principal": n.principal, "input": station_input(n)} for n in dag.NODES]
        edges = [{"from": a, "to": b, "label": label} for a, b, label in dag.EDGES]
        evs = events(db, usage_lines(run_dir), prices)
        draft = this_runs(run_dir / "proposal.md", meta.get("created_at"))   # this run's draft (an older one does not count)
        verdict = bid_verdict(run_dir, meta)         # bid.txt's `bid: OK` / `bid: NOT OK` / `missing`, on a whole bid only
        return {
            "job": meta.get("job", ""), "run_id": meta.get("run_id", ""), "created_at": meta.get("created_at", ""),
            "stations": stations, "edges": edges, "events": evs, "claims": claims(db), "prices": prices,
            "draft": draft, "verdict": verdict, "ending": ending(evs, draft, verdict),
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
