# Phase 2: the readers

A reader turns one unit of a source into ledger rows: one drawing view or tile,
one spec page, one photo, one message per call (architecture p.13–14). The
model's only job is to copy what is printed or say what is visible, into a
strict schema. Everything else is code, and it is all here.

| Module | What it does |
|---|---|
| [`schemas.py`](schemas.py) | The JSON each reader must return. Closed vocabularies; "not stated" is null; the photo schema has no field that can hold a number and refuses digits in its description |
| [`prompts/`](prompts/) | One system prompt per reader, with the ambiguity-removal rules and golden-job examples, including the empty case. Every example is checked against its schema in the tests |
| [`validate.py`](validate.py) | A small JSON Schema checker. A response that fails is discarded, never repaired |
| `rows.not_in_source` | On a text unit, every field the reader must copy verbatim (spec requirement and product, correspondence instruction, drawing note text) has to appear in the text it was shown, whitespace aside. An item that does not is dropped in code and reported as discarded, so made-up or injected text never becomes a row |
| [`units.py`](units.py) | Feet-and-inch strings to inches. The reader copies `15'-2"`; code writes `182 in` and the derivation the Auditor replays |
| [`vote.py`](vote.py) | Each unit is read three times and the runs are compared field by field. Disagreement keeps every reading in the order seen and picks none, not even a majority |
| [`rows.py`](rows.py) | Voted items become Claims. The method is fixed by rule from the item kind, never chosen by the model |
| [`run.py`](run.py) | One reader over a list of units, writing through the broker as that reader's own principal, logging every call |
| [`clients.py`](clients.py) | The model boundary, plus `ReplayClient`, which answers from recorded responses |
| [`compare.py`](compare.py) | A produced ledger against a golden fixture: dimensioned and counted rows must match exactly |
| [`golden.py`](golden.py) | The deployment gate: run the readers on a fixture job and compare |

## Golden replay

```
python3 -m pipeline replay fixtures/nantucket --out runs/nan-replay
python3 -m pipeline replay fixtures/ocean-beach --out runs/obv-replay
```

`fixtures/<job>/recordings/` holds the answer a correct reader should give for
each unit, three runs per unit. Every figure in a recording is a row in that
job's `ledger.yaml`; nothing is added. Replaying them tests the code around the
model (schemas, vote, method rules, broker scope, comparison), not the model.

What the replay shows today:

- Nantucket, sheet S-1, three views: all 8 dimensioned and counted rows (NAN-D-001 to D-008) reproduce exactly. The three angle legs come back as two scalings that disagree, printed `1'-8" (reading A) / 1'-10" (reading B)` and flagged conflict, which is what the fixture carries.
- Ocean Beach: all 5 photos come back as observed rows with no figure. The second run words each description differently and that does not count as a disagreement. SW p.17 comes back as 4 verbatim clause rows, and the Auditor finds every one of them on the page.
- Change one recorded count and the comparison fails. That case is in the tests.

## What a live run adds

The readers are written against `ModelClient.complete(reader, unit, system, schema, run)`.
A live run needs three things this branch does not have:

1. **An Anthropic API key** in the environment as `ANTHROPIC_API_KEY`, read only by the broker's process (architecture p.12).
2. **A live client** that sends one unit per call to Haiku with the reader's prompt and its schema as structured output, with prompt caching on the system prompt, and returns the parsed object. Each response is recorded to `recordings/` so the run can be replayed and diffed later.
3. **Drawing tiles.** Intake already renders page images; the Drawing Reader needs each view cropped or the sheet tiled on a fixed grid, one tile per call.

The gate is then the same function: `golden.replay(job, ledger, client=<live client>)` must reproduce the fixture's dimensioned and counted rows before a prompt or model change ships.

## The ruling still open

Architecture p.17 asks Chris for a ruling on scaled dimensions: never order from them, or order with a stated tolerance. Until he gives it, scaled rows are written with confidence `scaled`, carry no numeric value, and the broker refuses them in any allowance or order quantity.
