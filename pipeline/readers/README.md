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

## Live runs

```
python3 -m pipeline live fixtures/nantucket   --out runs/nan-live
python3 -m pipeline live fixtures/ocean-beach --out runs/obv-live
```

| Module | What it does |
|---|---|
| [`live.py`](live.py) | `LiveClient`: one unit per Messages API call on `claude-haiku-5-5`, the reader prompt as a cached system prompt, the reader schema as structured output, effort `low` by default. Records every response to `<out>/recordings/` in the format `ReplayClient` reads, and each call's usage (cache reads included) to `calls.jsonl` |
| [`tiles.py`](tiles.py) | Crops one view (or one tile of a fixed grid) from a drawing page with pdftoppm, at the DPI where its long edge fits 1568 px, the size the model is shown |
| `fixtures/<job>/units.yaml` | What a live run reads: the three S-1 views by box, SW p.17 as its text layer, the five photos as uploaded |

A live run needs the packet at `--packet` (default `fixtures/packet`) and
`ANTHROPIC_API_KEY` in the environment, or `MERSCO_ANTHROPIC_API_KEY` when the cloud environment will not pass the first name through, read through `broker.secret` (p.12). Without the key it stops with `NOT RUN`;
with one, a 1-token call checks the key before any unit is read.

Structured output does not take every constraint our schemas state (string
lengths, patterns, numeric bounds). The API is sent the subset it accepts,
through the SDK's `transform_schema`, with the rest written into the field
descriptions; `run.read` still checks each response against the full schema and
discards any that fail.

In GitHub Actions the same check is the manual `live` workflow
(`.github/workflows/live.yml`): Actions > live > Run workflow. It needs the
repository secret `ANTHROPIC_API_KEY`. The 7 source files the golden jobs read
are committed under `fixtures/packet/`, and every file a unit reads is checked
against its register hash first. Each job uploads
its comparison, ledger, recordings and the view images it sent. Those images
(`units/`) are crops of the customer's drawings: if this repository ever goes
public, drop `units/` from the uploaded artifacts first.

The gate is the same function as replay: `golden.replay(job, ledger, client=LiveClient(...), units=...)`
must reproduce the fixture's dimensioned and counted rows. `tests/test_live.py`
runs the whole live path with the model replaced by the expected answers; the
real gate there runs only with `CHRISBIDS_LIVE=1`, since it spends money.

A live read may list figures that are printed on a view but that the fixture
leaves out (the Partial Foundation Plan also prints 12" and 8" wall
dimensions). The comparison counts those as extra dimensioned rows and fails.
Whether the fixture should carry them or the gate should only fail on missing
rows is not decided here.

## The ruling still open

Architecture p.17 asks Chris for a ruling on scaled dimensions: never order from them, or order with a stated tolerance. Until he gives it, scaled rows are written with confidence `scaled`, carry no numeric value, and the broker refuses them in any allowance or order quantity.
