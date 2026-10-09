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
`ANTHROPIC_API_KEY` in the environment, or `BIDS_ANTHROPIC_API_KEY` when the cloud environment will not pass the first name through, read through `broker.secret` (p.12). Without the key it stops with `NOT RUN`;
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
one that misses one (the owner's ruling, 2026-10-08). Each live run prints every
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
6 brackets, 18 anchors, 18 bolts) by method, value and unit. The route is not compared: the first live run
(2026-10-08) worked out 18 anchors as 3 per bracket x the 6 bracket symbols drawn, where the fixture uses the
dimensions, and both are sound. What is checked is that every reader figure a quantity rests on is one the
fixture carries; a quantity resting on anything else is reported extra. A fixture quantity the readers
already wrote as an agreed count (6 brackets is also the 6 symbols drawn) is matched by that count, since
Takeoff is told not to derive a figure that is already a row. Ocean Beach has nothing to derive from: no
model call is made, and its one photo condition becomes a FIELD row.

Live, Takeoff runs on `claude-sonnet-5-5` at effort `high` (`--takeoff-model`, `--takeoff-effort`), since
it has to hold figures from several views at once (architecture p.13). It passed the live gate on both jobs
on 2026-10-08 (Actions run 37780360959: readers on Haiku at high effort, two reads; Takeoff 5 spaces and
18 + 18 agreed by both runs, 6 brackets matched by the 6 symbols drawn).

The spaces row depends on the drawing reader's labels. Two later runs (2026-10-08 23:20Z, run 37858877798;
2026-10-09, run 37928226366) read the same dimension strings but labelled the 11" string "Top wall segment
dimension", so Takeoff had no way to know it was the end offset and wrote only the 3 x 6 products; the gate
reported `counted | derived | 5 | spaces` missing. The drawing prompt now requires a dimension's label to say
what the string runs between, and its Foundation Plan example carries the spacing and end-offset strings
with labels that let Takeoff tell the run, the spacing and the end offset apart (the vote keys a dimension
on its text, so labels never affect the vote).
The fixes went in one per live run, in this order (all 2026-10-09, branch claude/project-thread-cz06g1):
- Run 37937707097 (labels rule) produced the spaces row from the dimensions in one run and as `6 - 1` from
  the bracket count in the other, and counted the detail's steel angle as a part. Both prompts now say so:
  spaces come from the dimension strings only, and a detail's members are not counts.
- Run 37938174100 reproduced every reader row and the spaces, and wrote 18 bolts and 18 anchors twice, as
  3 x 6 symbols in one run and 3 x the spaced brackets in the other: two flagged rows where the fixture has
  one, which the gate rightly reports. The Takeoff prompt now fixes the route: a per-assembly total
  multiplies by the number the dimensions give (spaces + 1), never by the symbol count when a run and
  spacing are listed.
- Run 37939750700 agreed on all four quantities by that route, unflagged, and failed only because one
  reader run fused the `8"` and `11"` strings at the bottom corner of the plan into a `8'-11"` that is not
  printed. The drawing prompt now says two inch strings end to end are two dimensions.
- Run 37940168055 (head e0f19e7, that rule) passed on both jobs: every dimensioned and counted reader row
  reproduced, and Takeoff wrote 5 spaces, 18 bolts and 18 anchors by the dimension route in both runs and
  6 brackets in one of them (the other left the agreed 6 symbols row to stand for it).
- Head 5b19df1 then added the check the prompt had only claimed: because the symbol count no longer
  stands in for the number of assemblies, `takeoff.symbol_notes` flags every item resting on a dimension
  route when a counted symbol row on the same view, one that counts what the per-assembly rows are per,
  disagrees with the number the dimensions give, with both figures in the derivation. Run 37941127022 on
  that head failed on Nantucket: the reader's first run labelled the `2'-8"` string as the first bracket
  from the wall face and the `11"` string as a wall segment, a label was taken from the first run that
  had it, and Takeoff saw no run and spacing and wrote `3 x 6`.
- Head b8aa6fb: the vote keeps every run's wording of a dimension label on the row (`A or B`; ` / ` means conflicting readings and ` | ` is the Takeoff table's column mark), so Takeoff
  sees each run's reading of what the string spans, and the drawing prompt says the string printed again
  and again along a row of symbols is the spacing and the short string from the wall face to the first
  symbol is the end offset. Run 37941692004 passed on both jobs; the `11"` row carried two wordings and
  all four quantities agreed in both Takeoff runs.
- Head 964fa1a: the label carries no count of the printings (a count belongs in a voted counted row).
  Run 37941971164 passed on both jobs.
- Head after that: an item resting on a dimension whose runs worded the label differently is flagged
  unverified (`takeoff.label_notes`), since the model chose which wording to follow, unless a symbol row
  on the view agrees with the number the dimensions give. Run 37942423549 (head 669c6b1) passed on both jobs.
- Head after 669c6b1 (code and tests only, no prompt change): a symbol row is matched on the first word of
  the item's own per-assembly unit (`per bracket assembly` names the bracket), an item is compared with its
  own kind's symbols when a view has two kinds, every symbol count on the view is compared when none names
  the assembly, and label wordings that differ only in case or trailing punctuation count as one. A
  wording carrying a bare count the dimension string lacks (`printed 5 times`) is dropped by the vote while
  another wording survives (a count belongs in a counted row; sheet and detail references such as `1/S-1`
  are not counts); when none survives the first is kept as printed, never rewritten, the row's derivation
  says the label carries a count with no counted row, and Takeoff flags every item resting on it. A spaces or
  assembly-count item is compared only with the symbols its own dimension rows name when a view has two kinds of assembly. The symbol check reads every current row, so a symbol count the
  reader runs disagreed on (a conflict row no formula may use) is still compared and both readings go on the
  note; a count made of the string's own digits (`2 places` on `2'-8"`) is still a count.
- After PR #17 (the advisor's five deferred items, code and tests only): (a) a wording the vote drops for
  carrying a count still marks the row (`a run put a count in the label with no counted row`), so Takeoff
  flags what rests on it whether the count was dropped or kept. (b) Measured on run 37942423549 (head
  669c6b1, the first with the differing-labels marker): 3 of the 5 dimensioned rows carried `runs word the
  label differently` (12", 11" and 8"; the run and spacing did not) and 0 of the 4 Takeoff items were
  flagged, because the 6 bracket symbols on the view agreed with the number the dimensions give; the trigger
  is left as it is, since a narrower one would need code to read the labels for a run, a spacing and an
  offset. (c) `#4 bar`, `2 x 4 blocking` and `4x4 posts` are sizes, and `11 inches`, `8 inch CMU`, `6 mil
  poly` are figures, not counts; a count of spacings is a count however it is written (`5 x 2'-8"`, arch
  p.4's `11 + 5 x 32 + 11`, `5 x 2 ft`), as are `max 5 spaces` and `2 in each bay`. A count kept in a label
  and used in a formula as a plain number (`{PER} * (5 + 1)`) is noted on the item too, since that number
  cites no row. (d) An item that rests on a symbol count where the symbols' view carries dimension strings
  naming the assembly counted is flagged unverified whatever else it uses (`takeoff.route_notes`, those rows
  on the note; a fastener total that takes the anchors from the symbols and the bolts from the dimensions is
  on that route too, and the symbol check passes such a sum by). The dimension strings that count are those
  naming what the item's per-assembly rows are per, else what the job's are per, as `symbol_rows` matches
  them, so window tags beside a room width (the prompt's own route when no run and spacing are listed) are
  not flagged; a job with no per-assembly row at all is held to every dimension string on the view. (e) The
  per-PR mutation score goes in the PR description with the run it came from (now in the Every PR bar of
  `docs/advisor.md`).

## The ruling still open

Architecture p.17 asks the estimator for a ruling on scaled dimensions: never order from them, or order with a stated tolerance. Until that ruling is given, scaled rows are written with confidence `scaled`, carry no numeric value, and the broker refuses them in any allowance or order quantity.
