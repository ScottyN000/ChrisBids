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
| [`vote.py`](vote.py) | Each unit is read more than once (live default two, the recordings three) and the runs are compared field by field. Disagreement keeps every reading in the order seen and picks none, not even a majority |
| [`rows.py`](rows.py) | Voted items become Claims. The method is fixed by rule from the item kind, never chosen by the model. When scaled readings disagree, code lists each one in inches and the range between them in the derivation, and gives no centre value, so nothing reads as a chosen length |
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

- Nantucket, sheet S-1, three views: all 10 dimensioned and counted rows (NAN-D-001 to D-008, D-016, D-017) reproduce exactly. The three angle legs come back as two scalings that disagree, printed `1'-8" (reading A) / 1'-10" (reading B)` and flagged conflict, which is what the fixture carries.
- Ocean Beach: all 5 photos come back as observed rows with no figure. The second run words each description differently and that does not count as a disagreement. SW p.17 comes back as 4 verbatim clause rows, and the Auditor finds every one of them on the page.
- Change one recorded count and the comparison fails. That case is in the tests.

## Live runs

```
python3 -m pipeline live fixtures/nantucket   --out runs/nan-live
python3 -m pipeline live fixtures/ocean-beach --out runs/obv-live
```

| Module | What it does |
|---|---|
| [`live.py`](live.py) | `LiveClient`: one unit per Messages API call on `claude-haiku-5-5`, the reader prompt as a cached system prompt, the reader schema as structured output, effort `high` and two reads per unit by default: on 2026-10-08 high passed the gate on both jobs with two reads and with three, where low and medium kept misreading S-1 counts. Records every response to `<out>/recordings/` in the format `ReplayClient` reads, and each call's usage (cache reads included) to `calls.jsonl` |
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

The fixture carries every figure printed on a view, including ones no
quantity uses (the 12" and 8" wall dimensions on the Partial Foundation Plan,
NAN-D-016 and D-017), so the gate fails a reader that adds a figure as well as
one that misses one (Scott's ruling, 2026-10-08). Each live run prints every
figure it produced and its token totals at the end of the log.

## Takeoff (Phase 3)

[`../takeoff.py`](../takeoff.py) runs after the readers, on the rows they wrote. It never opens a document.

| Step | Who | What |
|---|---|---|
| Inputs | code | Every dimensioned or counted row with a single agreed figure. Scaled, observed and conflicting rows are never shown, so they cannot feed a quantity |
| Formula | model | One stateless unit (the rows as a table, [`prompts/takeoff.md`](prompts/takeoff.md)) read twice live, three times in replay. Each item is a formula over `{ID}` references, the value it comes to, and a unit from `each` or `spaces` |
| Check | code | The formula is evaluated without `eval` (`schema.arith`) and must equal the item's value. A formula that uses a row it was not shown, carries a decimal (a rate or yield only a data sheet can give), or does not evaluate is discarded and reported |
| Vote | code | Items are matched across runs by what they compute: value, unit and the rows used. Two formulas written differently for the same figure are one quantity. An item not every run produced is written flagged unverified |
| Rows | code | A `counted` quantity whose derivation shows each figure in place, e.g. `3 x ((182 - 2 x 11) / 32 + 1) = 18`. The broker replays the formula again and refuses a counted or dimensioned row that rests on a scaled or observed one |
| FIELD | code | One FIELD row per scaled length and per photo condition, naming the row it comes from and repeating none of its readings. A job with no drawings gets only these, as a takeoff checklist (architecture p.16) |

The golden comparison holds Takeoff to the fixture's derived quantities (NAN-Q-001 to Q-004: 5 spaces,
6 brackets, 18 anchors, 18 bolts), identified by value, unit and the reader figures they rest on, followed
through any row they use. Ocean Beach has nothing to derive from: no model call is made, and its one photo
condition becomes a FIELD row.

Live, Takeoff runs on `claude-sonnet-5-5` at effort `high` (`--takeoff-model`, `--takeoff-effort`), since
it has to hold figures from several views at once (architecture p.13). It is replay-only on the DAG until a
live run passes the gate.

## The ruling still open

Architecture p.17 asks Chris for a ruling on scaled dimensions: never order from them, or order with a stated tolerance. Until he gives it, scaled rows are written with confidence `scaled`, carry no numeric value, and the broker refuses them in any allowance or order quantity.
