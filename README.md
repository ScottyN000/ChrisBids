# ChrisBids

Bid-processing pipeline for Mersco Inc. Built in the order the architecture doc
sets out (p.16): the ledger and the auditor first, because they are what make
the rest trustworthy.

| | |
|---|---|
| [pipeline/](pipeline/) | **Phase 1**: the append-only claim ledger behind a broker, Intake, and the Auditor. [How it works](pipeline/README.md) |
| [pipeline/readers/](pipeline/readers/) | **Phase 2**: drawing, spec, photo and correspondence readers: schemas, prompts, the three-run vote and the golden replay. [How it works](pipeline/readers/README.md) |
| [pipeline/takeoff.py](pipeline/takeoff.py) | **Phase 3**, first agent: Takeoff. The model writes formulas over the readers' dimensioned and counted rows; code evaluates each one and writes the quantity with its derivation. Scaled lengths and photo conditions become FIELD rows. [How it works](pipeline/readers/README.md#takeoff-phase-3) |
| [pipeline/scope_writer.py](pipeline/scope_writer.py) | **Phase 3**, second agent: the Scope Writer. The model selects rows and library phrases per division; code checks the layout against the ledger and renders the proposal and its cross-reference. [How it works](pipeline/README.md#scope-writer-phase-3) |
| [pipeline/orchestrator.py](pipeline/orchestrator.py) | **Phase 3**: the Orchestrator. Plans a bid from the Source Register (every spec page, photo and message, drawing sheets in grid tiles) and runs it end to end: `python -m pipeline bid <packet> --job J --out runs/j`. [How it works](pipeline/README.md#orchestrator-phase-3) |
| [fixtures/](fixtures/) | The two hand-made test bids as claim ledgers: the answer key every change must reproduce |
| [docs/pipeline-dag.md](docs/pipeline-dag.md) | The pipeline DAG from the architecture doc, with what is built, replay-only and planned. Generated from [`pipeline/dag.py`](pipeline/dag.py); CI fails if it falls behind the code |
| [tools/build_fixture.py](tools/build_fixture.py) | Validates a fixture ledger against the method rules and renders its proposal |
| [SECURITY.md](SECURITY.md) | What keeps document content from steering the pipeline, and what CI scans for |
| [tests/](tests/) | The access matrix, the method rules, intake, the auditor and the golden tests |

## Quick start

```
pip install -r requirements.txt     # pyyaml; poppler-utils for page text and images
python3 -m pipeline verify-fixtures
python3 -m pipeline replay fixtures/nantucket --out runs/nan-replay
python3 -m unittest discover -s tests -t .
```

In Docker, which is how Phase 1 is meant to run locally:

```
docker compose run --rm pipeline verify-fixtures --out /runs/verify
CHRISBIDS_PACKET=/mnt/project-files docker compose run --rm tests
```

Mutation testing, which CI runs on every PR that touches `pipeline/` or `tests/`
([`.github/workflows/mutation.yml`](.github/workflows/mutation.yml)):

```
pip install pytest mutmut
python3 -m mutmut run                     # small edits to pipeline/, the tests run against each
python3 -m mutmut results                 # the mutants no test caught
python3 -m mutmut export-cicd-stats && python3 tools/mutation_gate.py
```

The gate fails below the floor in [`tools/mutation_gate.py`](tools/mutation_gate.py). The floor
only goes up: a PR that adds code adds the tests that kill its mutants.

## The rule

No number reaches a bid document unless it points at a page, sheet, photo,
clause or field measurement that a human can open and check. A figure without a
ledger row behind it is a build failure, not a warning — `pipeline audit` finds
it and names it an orphan. Only Chris releases a bid; the pipeline cannot.
