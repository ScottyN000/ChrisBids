"""Load a golden fixture into a ledger through the broker.

The two hand-made bids are the answer key (architecture p.10). Phase 1 proves
the foundation against them in two ways:

* every fixture row is routed to the principal that would have written it, so
  the access matrix is exercised rather than described — a row the matrix would
  refuse cannot be loaded at all;
* the ledger exported back out must byte-match the committed `ledger.csv`, so
  the store, the broker and `tools/build_fixture.py` agree field for field.

The fixture's own `agent` and `timestamp` are stamped on every row instead of
the wall clock, which is what makes the export reproducible.
"""
from __future__ import annotations

import csv
from pathlib import Path

import yaml

from . import roles, schema
from .broker import Broker
from .schema import Claim

ROOT = Path(__file__).resolve().parent.parent
PHRASE_LIBRARY = ROOT / "fixtures" / "phrase-library.yaml"

# Keys ledger.yaml may carry on a row. Anything else is a typo, which is worth
# failing on: an unquoted locator once turned `p.10, p.15, p.16` into three keys
# and silently dropped two page cites.
ROW_KEYS = {
    "id", "role", "method", "confidence", "source", "locator", "tag", "value", "unit",
    "statement", "derivation", "calc", "division", "part", "flag", "question",
    "url", "retrieved", "quote", "supersedes", "reason",
}


class FixtureError(Exception):
    pass


def read_fixture(job_dir: Path) -> tuple[dict, list[dict]]:
    data = yaml.safe_load((Path(job_dir) / "ledger.yaml").read_text())
    with open(Path(job_dir) / "register.csv", newline="") as f:
        register = list(csv.DictReader(f))
    bad = [(r.get("id", "?"), sorted(set(r) - ROW_KEYS)) for r in data["rows"] if set(r) - ROW_KEYS]
    if bad:
        raise FixtureError(
            "ledger.yaml rows carry keys the schema does not know (usually an unquoted value "
            "that YAML read as a mapping):\n"
            + "\n".join(f"  {rid}: {keys}" for rid, keys in bad)
        )
    return data, register


def to_claim(row: dict, timestamp: str, agent: str) -> Claim:
    value, value_num = schema.format_value(row.get("value"))
    return Claim(
        claim_id=row["id"], statement=row["statement"], source_id=str(row["source"]),
        method=row["method"], role=row["role"], confidence=row["confidence"],
        value=value, value_num=value_num, unit=row.get("unit", "") or "",
        locator=row.get("locator", "") or "", tag=row.get("tag", "") or "",
        derivation=row.get("derivation", "") or "", calc=row.get("calc", "") or "",
        division=str(row.get("division", "") or ""), part=row.get("part", "") or "",
        flag=row.get("flag", "") or "", question=row.get("question", "") or "",
        url=row.get("url", "") or "", retrieved=str(row.get("retrieved", "") or ""),
        quote=row.get("quote", "") or "", supersedes=row.get("supersedes", "") or "",
        reason=row.get("reason", "") or "", agent=agent, timestamp=timestamp,
    )


def load(job_dir: Path, ledger_path: Path) -> tuple[Broker, dict]:
    """Build a ledger file from a fixture folder. Returns an auditor-bound broker."""
    job_dir = Path(job_dir)
    data, register = read_fixture(job_dir)
    run_id = f"fixture-{data['job']}"
    intake_broker = Broker.open_job(
        ledger_path, "intake", job=data["job"], run_id=run_id, create=True,
        clock=lambda: data["timestamp"],
    )
    intake_broker.ledger.set_meta(
        job_name=data["job_name"], original=data["original"],
        status_line=data["status_line"], fixture=str(job_dir),
    )
    intake_broker.write_register(register)
    kinds = {r["source_id"]: r["kind"] for r in register}

    writers: dict[str, Broker] = {}
    for row in data["rows"]:
        source_kinds = {kinds.get(s, "unknown") for s in schema.sources_of(row["source"])}
        principal = roles.route(row["method"], source_kinds, has_calc=bool(row.get("calc")))
        if principal not in writers:
            writers[principal] = intake_broker.as_principal(
                principal, agent_label=data["agent"], clock=lambda: data["timestamp"]
            )
        writers[principal].append(to_claim(row, data["timestamp"], data["agent"]))
    return intake_broker.as_principal("auditor", agent_label=data["agent"]), data
