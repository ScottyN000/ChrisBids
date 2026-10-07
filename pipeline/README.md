# Phase 1: the ledger, Intake and the Auditor

> Build the ledger and the auditor first; they are what make the system
> trustworthy, and every other agent is replaceable once they exist.
> — architecture doc p.16

Nothing in this package calls a model. Phase 1 is the code half of "models
extract; code computes": hashing, page splitting, schema checks, arithmetic
replay, link liveness and orphan detection. The readers arrive in Phase 2 and
write through the same broker, under the same rules.

| Module | What it is |
|---|---|
| [`schema.py`](schema.py) | The ledger row, the enumerated vocabularies and the method rules (p.5–6) as pure functions |
| [`roles.py`](roles.py) | The access matrix (p.10–12) as data: who may write which method, who has egress, who sees prices |
| [`ledger.py`](ledger.py) | One append-only SQLite file per job, with triggers that refuse an edit or a delete |
| [`broker.py`](broker.py) | The only writer. Checks the caller's principal, stamps versions, logs every access |
| [`intake.py`](intake.py) | The Source Register: hashes, duplicate collapsing, page counts, page images, document kind |
| [`auditor.py`](auditor.py) | Pass/fail per figure, and the orphan list |
| [`fixtures.py`](fixtures.py) | Loads a golden fixture into a ledger, routing each row to the principal that would have written it |
| [`cli.py`](cli.py) | `python3 -m pipeline …` |

## Running it

```
pip install -r requirements.txt            # pyyaml; poppler-utils for page text and images

python3 -m pipeline verify-fixtures        # the golden test
python3 -m pipeline intake /mnt/project-files/source --job NAN --out runs/nan --images
python3 -m pipeline load fixtures/nantucket --out runs/nan-fixture
python3 -m pipeline audit runs/nan-fixture/ledger.db \
    --packet /mnt/project-files --proposal fixtures/nantucket/proposal.md
python3 -m unittest discover -s tests -t .
```

Or in Docker, which is how this is meant to run locally:

```
docker compose run --rm pipeline verify-fixtures --out /runs/verify
CHRISBIDS_PACKET=/mnt/project-files docker compose run --rm tests
```

## Append-only, twice over

The broker refuses a write its caller's principal is not allowed. SQLite
triggers refuse a `DELETE` on the claims, register or log tables, and refuse an
`UPDATE` of any claim column except `audit` and `audit_note`, so a bug or a
direct `sqlite3` connection cannot edit history either. A correction is a new
row whose `supersedes` points at the row it replaces; the superseded row stays
in the file and the Auditor stops counting it.

The audit field is the one field ever updated in place, and only the Auditor
principal can do it.

## What a verdict means

A pass has to rest on something the code actually did:

| Verdict | When |
|---|---|
| `pass` | the row's quote or value was found on the page it cites; or its `calc` was replayed from rows that were themselves checked; or it is a FIELD placeholder, which carries no figure |
| `unverified` | the cited source is not in the packet; the agent flagged it; two readings were kept; the page has no text layer; the page opened but the row paraphrases rather than quotes; a `fetched` URL was not re-fetched this run |
| `fail` | a method rule broken, arithmetic that does not reproduce, a quote not on the page it cites, a dead link, a source hash that changed |

"Does this quoted text support this claim, yes or no" is the one question left
for the Phase 2 Haiku pass, and it only ever sees a page this code has already
opened.

## Orphan figures

`audit --proposal` extracts every number from a finished proposal and traces it
to a ledger row, a quoted source, or a Mersco phrase-library paragraph. What is
left over is an orphan: a figure with nothing behind it.

The two rendered fixture proposals have none. Fed the original hand-made bids,
the Auditor reports 43 orphans on Nantucket and 140 on Ocean Beach — the
scaled leg lengths, the bag yields, the spread rates and the "~" figures that
were the reason for building the ledger. That comparison is the Phase 1
acceptance test (p.16: "feed it the two hand-written bids above and have it find
every orphan") and it runs in `tests/test_golden.py`.

The list of numbers that are *not* figures — page cites, claim IDs, statute
numbers, years, phone numbers, product codes — is `IGNORE` in
[`auditor.py`](auditor.py). Each entry says why it is skipped, because that list
is the one place the audit could be made to pass by looking away.

## Not in Phase 1

Readers, Takeoff, Customer Requirements, the Scope Writer, Codes & Regulations,
Materials and Pricing. `roles.py` already carries their principals and their
write scopes, so a Phase 2 reader is a prompt and an output schema, not a change
to the store.
