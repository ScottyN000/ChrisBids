# ChrisBids

Bid-processing pipeline for Mersco Inc. Built in the order the architecture doc
sets out (p.16): the ledger and the auditor first, because they are what make
the rest trustworthy.

| | |
|---|---|
| [pipeline/](pipeline/) | **Phase 1**: the append-only claim ledger behind a broker, Intake, and the Auditor. [How it works](pipeline/README.md) |
| [fixtures/](fixtures/) | The two hand-made test bids as claim ledgers: the answer key every change must reproduce |
| [tools/build_fixture.py](tools/build_fixture.py) | Validates a fixture ledger against the method rules and renders its proposal |
| [tests/](tests/) | The access matrix, the method rules, intake, the auditor and the golden tests |

## Quick start

```
pip install -r requirements.txt     # pyyaml; poppler-utils for page text and images
python3 -m pipeline verify-fixtures
python3 -m unittest discover -s tests -t .
```

In Docker, which is how Phase 1 is meant to run locally:

```
docker compose run --rm pipeline verify-fixtures --out /runs/verify
CHRISBIDS_PACKET=/mnt/project-files docker compose run --rm tests
```

## The rule

No number reaches a bid document unless it points at a page, sheet, photo,
clause or field measurement that a human can open and check. A figure without a
ledger row behind it is a build failure, not a warning — `pipeline audit` finds
it and names it an orphan. Only Chris releases a bid; the pipeline cannot.
