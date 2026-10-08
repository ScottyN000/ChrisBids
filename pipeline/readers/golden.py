"""Golden replay: run the readers on a fixture job and compare with its ledger.

With recorded responses this tests everything around the model: the schemas,
the vote, the method rules, the broker's scoping and the comparison itself.
With a live client the same function is the deployment gate the architecture
doc asks for (p.10): a prompt or model change ships only when this reproduces
the fixture's dimensioned and counted rows.
"""
from __future__ import annotations

from pathlib import Path

from .. import fixtures, takeoff
from ..broker import Broker
from . import compare, run
from .clients import ModelClient, ReplayClient

READERS = ("drawing", "spec", "photo", "correspondence")


def replay(job_dir: Path, ledger_path: Path, client: ModelClient | None = None, *, repeats: int = 3,
           units: dict[str, list] | None = None, takeoff_client: ModelClient | None = None,
           takeoff_repeats: int | None = None):
    """Returns (broker, results by reader and "takeoff", Comparison).

    `units` is what each reader is shown, by reader. Replay takes it from the
    recordings; a live run passes `live.units_for(...)`. Takeoff then runs on
    the rows the readers wrote, with `takeoff_client` (replay: the same
    recordings), and its derived quantities join the same comparison.
    """
    job_dir = Path(job_dir)
    data, register = fixtures.read_fixture(job_dir)
    if ledger_path.exists():
        ledger_path.unlink()
    broker = Broker.open_job(ledger_path, "intake", job=data["job"], run_id=f"replay-{data['job']}", create=True)
    broker.write_register(register)
    for c in (client, takeoff_client):
        if hasattr(c, "bind"):
            try:
                c.bind(broker)   # a live client takes its key from the broker
            except Exception:
                broker.close()
                raise

    client = client or ReplayClient(job_dir / "recordings", model_id="replay (recorded expected output)")
    results = {}
    for reader in READERS:
        if units is not None:
            todo = units.get(reader, [])
        else:
            todo = client.units(reader) if isinstance(client, ReplayClient) else []
        if todo:
            results[reader] = run.read(broker, reader, data["job"], todo, client, repeats=repeats)

    fixture_claims = [fixtures.to_claim(r, data["timestamp"], data["agent"]) for r in data["rows"]]
    present = {r["source_id"] for r in register if r["status"] == "present"}
    produced = [c for res in results.values() for c in res.rows]
    comparison = compare.compare(produced, fixture_claims, present)

    if takeoff_client is None and isinstance(client, ReplayClient):
        takeoff_client = client
    tk = takeoff.run(broker, data["job"], takeoff_client,
                     repeats=takeoff_repeats if takeoff_repeats is not None else repeats)
    results["takeoff"] = tk
    compare.add_derived(comparison, broker.ledger.by_id(), tk.rows, fixture_claims, present)
    return broker, results, comparison
