# ChrisBids

Bid-processing pipeline for Contractor Co. Built in the order the architecture doc
sets out (p.16): the ledger and the auditor first, because they are what make
the rest trustworthy.

| | |
|---|---|
| [pipeline/](pipeline/) | **Phase 1**: the append-only claim ledger behind a broker, Intake, and the Auditor. [How it works](pipeline/README.md) |
| [pipeline/readers/](pipeline/readers/) | **Phase 2**: drawing, spec, photo and correspondence readers: schemas, prompts, the three-run vote and the golden replay. [How it works](pipeline/readers/README.md) |
| [pipeline/takeoff.py](pipeline/takeoff.py) | **Phase 3**, first agent: Takeoff. The model writes formulas over the readers' dimensioned and counted rows; code evaluates each one and writes the quantity with its derivation. Scaled lengths and photo conditions become FIELD rows. [How it works](pipeline/readers/README.md#takeoff-phase-3) |
| [pipeline/scope_writer.py](pipeline/scope_writer.py) | **Phase 3**, second agent: the Scope Writer. The model selects rows and library phrases per division; code checks the layout against the ledger and renders the proposal and its cross-reference. [How it works](pipeline/README.md#scope-writer-phase-3) |
| [pipeline/orchestrator.py](pipeline/orchestrator.py) | **Phase 3**: the Orchestrator. Plans a bid from the Source Register (every spec page, photo and message, drawing sheets in grid tiles) and runs it end to end: `python -m pipeline bid <packet> --job J --out runs/j`. [How it works](pipeline/README.md#orchestrator-phase-3) |
| [pipeline/webread.py](pipeline/webread.py) | **Phase 4**: Codes & Regs and Materials. Fetches the pages the page table matches to the job, from allowlisted domains only. Writes each verbatim quote as a `fetched` row with its URL and date; code checks every quote against the page. [How it works](pipeline/README.md#codes--regs-and-materials-phase-4) |
| [fixtures/](fixtures/) | The two hand-made test bids as claim ledgers: the answer key every change must reproduce |
| [docs/pipeline-dag.md](docs/pipeline-dag.md) | The pipeline DAG from the architecture doc, with what is built, replay-only and planned. Generated from [`pipeline/dag.py`](pipeline/dag.py); CI fails if it falls behind the code |
| [tools/build_fixture.py](tools/build_fixture.py) | Validates a fixture ledger against the method rules and renders its proposal |
| [tools/visual/](tools/visual/) | The Bid Shop Floor: a page that replays a run as workers carrying the job between the stations, with a meter for model spend and who is backed up. `tools/export_run.py` turns a run folder (its ledger's audit log and claims, and `calls.jsonl` on a live run) into one JSON; `tools/fetch_prices.py` reads the price table from the pricing page with its URL and time; `tools/build_visual.py` embeds the exports in the page. Every move on the floor is an audit-log row; the cat, the couriers' beer and the closing scene (no row records a release: the estimator walks the bid out by hand, and only when every Scope Writer run was valid, the draft is in the run folder and the audited rows include no fail; the auditor's orphan-figure and source-hash checks are not in the log, and the page says so) are the only inventions, and the page says so. A replay run bills no tokens; a live call with no usage line or no single price row for its model is counted, not priced |
| [docs/advisor.md](docs/advisor.md) | The advisor: an independent design and code review of every PR against the architecture doc ([text](docs/architecture.txt)) and the standing rules. CI runs it on each push to a PR that is ready for review, not a draft ([`advisor.yml`](.github/workflows/advisor.yml)); a blocking finding fails the check. Run it locally first with the `advisor` subagent ([`.claude/agents/advisor.md`](.claude/agents/advisor.md)) |
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

Mutation testing ([`.github/workflows/mutation.yml`](.github/workflows/mutation.yml)). On a
PR, CI runs only the mutants in the `pipeline/` modules the PR changes, and in the modules
its changed test files import, and gates on their combined score; a weekly run on main
runs them all:

```
pip install pytest mutmut
python3 -m mutmut run                     # small edits to pipeline/, the tests run against each
python3 tools/changed_mutants.py origin/main | grep -q . && python3 -m mutmut run $(python3 tools/changed_mutants.py origin/main)   # this branch's modules; nothing printed means nothing to run (bare `mutmut run` is everything)
python3 -m mutmut results                 # the mutants no test caught
python3 -m mutmut export-cicd-stats && python3 tools/mutation_gate.py
```

The gate fails below the floor in [`tools/mutation_gate.py`](tools/mutation_gate.py). The floor
only goes up: a PR that adds code adds the tests that kill its mutants. A picked module
that gives mutmut nothing to mutate stops the run ("Filtered for specific mutants, but
nothing matches"); list such a module in `do_not_mutate` in `pyproject.toml`.

## The rule

No number reaches a bid document unless it points at a page, sheet, photo,
clause or field measurement that a human can open and check. A figure without a
ledger row behind it is a build failure, not a warning — `pipeline audit` finds
it and names it an orphan. Only the estimator releases a bid; the pipeline cannot.
