"""Golden replay: run the readers on a fixture job and compare with its ledger.

With recorded responses this tests everything around the model: the schemas,
the vote, the method rules, the broker's scoping and the comparison itself.
With a live client the same function is the deployment gate the architecture
doc asks for (p.10): a prompt or model change ships only when this reproduces
the fixture's dimensioned and counted rows.
"""
from __future__ import annotations

from pathlib import Path

from .. import fixtures
from ..broker import Broker
from . import compare, run
from .clients import ModelClient, ReplayClient

READERS = ("drawing", "spec", "photo", "correspondence")


def replay(job_dir: Path, ledger_path: Path, client: ModelClient | None = None, *, repeats: int = 3):
    """Returns (broker, results by reader, Comparison)."""
    job_dir = Path(job_dir)
    data, register = fixtures.read_fixture(job_dir)
    if ledger_path.exists():
        ledger_path.unlink()
    broker = Broker.open_job(ledger_path, "intake", job=data["job"], run_id=f"replay-{data['job']}", create=True)
    broker.write_register(register)

    client = client or ReplayClient(job_dir / "recordings", model_id="replay (recorded expected output)")
    results = {}
    for reader in READERS:
        units = client.units(reader) if isinstance(client, ReplayClient) else []
        if units:
            results[reader] = run.read(broker, reader, data["job"], units, client, repeats=repeats)

    fixture_claims = [fixtures.to_claim(r, data["timestamp"], data["agent"]) for r in data["rows"]]
    present = {r["source_id"] for r in register if r["status"] == "present"}
    produced = [c for res in results.values() for c in res.rows]
    return broker, results, compare.compare(produced, fixture_claims, present)
