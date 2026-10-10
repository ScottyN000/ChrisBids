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
to a ledger row, a quoted source, or a Contractor Co. phrase-library paragraph. What is
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

## After Phase 1

The readers are in [`readers/`](readers/README.md) and write through this broker
unchanged, and so does Takeoff ([`takeoff.py`](takeoff.py), Phase 3). The Scope
Writer and the Orchestrator are below. Customer Requirements, Codes & Regulations, Materials and
Pricing are still to come; `roles.py` already carries their principals and write
scopes.

## Scope Writer (Phase 3)

[`scope_writer.py`](scope_writer.py) lays out the proposal; [`proposal.py`](proposal.py)
renders it. The renderer is the one `tools/build_fixture.py` already used for the
fixtures, moved here, so a fixture's layout rendered from its ledger file is
byte-identical to its committed `proposal.md` (a test checks both jobs).

The model (Haiku, architecture p.13) is shown the ledger rows (ID, role, division,
part, method, statement; never a figure or a price) and the selectable phrases of
the phrase library. It returns a layout: sections by division plus Alternates,
tasks of rows and phrases, each task's allowance rows and concealed-conditions
close, the exclusions and the terms. It writes no sentence. Task titles are its
only words, and the schema refuses one with a digit; code numbers the tasks.

Code then refuses a layout that:

- names a row or phrase that does not exist, or a header slot that is not a header row;
- puts a row outside its own division, alternate work outside Alternates, or base-bid work under it;
- lists an exclusion, question, material or allowance row as a task item (code prints those itself);
- uses an exclusion, terms or concealed-conditions phrase in the wrong place, or lists anything twice.

Both reads must be valid; the live schema lists only the job's own row IDs and
phrase keys, so a read cannot name one that does not exist. Only what both place, in the same section, is kept
(order and titles come from the first); everything else is reported as dropped,
a header slot the reads fill differently prints FIELD, and if they share no
placement nothing is rendered. Scope and allowance rows left out, by a read or
by the agreement, are reported as unplaced, and the
Auditor's orphan check runs on the rendered text. `proposal.md` and `xref.csv`
are written to the run folder; the Scope Writer writes no ledger row.

```
python3 -m pipeline scope fixtures/nantucket --out runs/nan-scope           # replay
python3 -m pipeline scope fixtures/nantucket --out runs/nan-scope --live    # Haiku, needs the API key
```

The golden gate runs on each fixture's own ledger and compares with its
hand-made layout. It fails on a different header, a scope or allowance row
missing, or one placed in a section the fixture does not use (a base row may
also sit in its own division), and on any orphan figure. Which phrases,
exclusions, closes, quantity rows and code rows the model adds is reported but
not gated: the fixture's choice there is one reasonable layout among several.

Section titles come from the Proposal Format Example and the two test bids
(divisions 01, 02, 03, 05, 07 and 09). A row in any other division is reported
unplaced until the estimator's template names that division (architecture p.17 asks the
estimator for the template file).

## Orchestrator (Phase 3)

[`orchestrator.py`](orchestrator.py) plans a bid from the Source Register alone
and then runs it. It never opens a document and writes no ledger row
(architecture p.12).

The plan is a lookup by document kind:

- every page of a spec is a unit;
- every photo and every message is a unit;
- each drawing sheet is read in a 3 x 2 grid of tiles (`readers/tiles.py`), since a new packet has no hand-drawn view boxes.

A source that is missing, a duplicate, or of a kind no reader opens yet
(template, past bid, spreadsheet, unknown) is listed with the reason, so the plan
accounts for every register row. Customer Requirements is listed as not built.

The architecture puts the Orchestrator on Sonnet (p.13). With only the register to
go on, the plan has no choices a model would add, so it is code: the same plan
every time, at no cost. A model earns its place once Intake tags divisions and
the plan has to decide which per-division passes to run (p.7).

```
python3 -m pipeline bid <packet-dir> --job J --out runs/j --plan-only   # the plan, no model call
python3 -m pipeline bid <packet-dir> --job J --out runs/j               # live: needs the API key
```

A bid run does the following, in order:

1. Intake.
2. The plan (`plan.json`).
3. Each reader on its units, prepared and hash-checked by `readers/live.prepare_units`.
4. Takeoff.
5. Codes & Regs and Materials, when the run has network (`--no-web` skips them).
6. The Scope Writer (`proposal.md`, `xref.csv`).
7. The Auditor, with the packet and the proposal.

It also writes `ledger.csv` and a summary, `bid.txt`. A test runs a small
synthetic packet through every step with fake model clients.

A new packet's readers write no header rows (project name, address, client),
so the Scope Writer may leave those slots empty and the proposal says FIELD there.

## Codes & Regs and Materials (Phase 4)

[`webread.py`](webread.py) reads the code, permit, licensing and product pages a
job needs, and writes what they say as `fetched` rows. Each row carries the URL,
the retrieval date and a verbatim quote (architecture p.4, p.6, p.16). The two
agents share the module, and each page names its agent: Codes & Regs for codes,
permits and licensing, Materials for manufacturer data sheets.

Which pages a job needs comes from [`web_sources.yaml`](web_sources.yaml). This
is the per-jurisdiction and per-manufacturer cache the architecture describes
(p.7-8). A page is read when the job's rows from its own documents name its place,
product or hazard (never fetched rows or rows citing the web, which would pick the
pages that research already found).
A two-letter state code counts only as an address writes it (", MD" or "MD 21842"),
so "10.1 fl oz" in a spec does not pull in Florida's pages.
For example, the Ocean City pages are read for a job in Ocean City, Maryland, and
the Loxon data sheets for a job whose spec names Loxon. The table was seeded from
the 57 pages the two hand-made test bids cite. Each ask is a neutral question
about what to find, with a `#` for each figure the model must read from the
page (`How long permit review takes: # weeks`). It never carries the test bid's
conclusion, since the table is the cache for every later job (p.7: evidence,
never conclusions). A jurisdiction the table does not
know gets no rows yet: the cold-cache search plan (p.13) is not built.

Every bid re-fetches its pages: a cached row is evidence, never a conclusion
(p.7-8). Code does the fetching ([`web.py`](web.py)) and checks every URL first:

- only `.gov` hosts, `.us` state portals (`*.state.xx.us`) or a domain the table
  names may be fetched (anyone may register a `.us` name, so other `.us` hosts
  are refused);
- the URL must be public (no file:, loopback or private address);
- every redirect hop is checked the same way;
- a page is at most 8 MB, and each read has a 30-second socket timeout (a
  server that trickles bytes can take longer in total).

HTML and PDF pages are turned into text.

A model (Haiku) answers each ask with a quote and one sentence. Code keeps an
answer only if all three hold:

- the quote is on the page, compared after folding case, spacing, quotes and dashes;
- the model fills the ask's `#` marks with figures (`figures`), and each is in
  the quote, a range counting as one figure (so "4" does not match "2-4") and a
  date with its month as one ("March 2024" does not match "May 2024"), or, in a
  mark the ask writes inside an identifier ("LX#", "ESR-#"), is part of one of
  the page's own identifiers, which the table lists whole (`ids`:
  ESR-4143, A24W8300) and which count only if the page carries them, so a
  product name the model recalls (HY 70 to HY 270, p.7) gets no pass;
- the sentence gives each of those figures, and none its quote lacks. The identifiers are taken out
  whole first, so "24 hours" on the A24W8300 sheet is still a figure;
- two runs pick the same option for a closed ask and fill the marks with
  exactly the same figures (identifier digits aside, in identifier marks only),
  from whichever passage;
  for an ask with neither, they quote mostly the same passage and their
  sentences give the same figures.

A product or report code the page table already knows (LX02, ESR-4143) is written
into the ask, never left as a mark for the model to fill.

An ask whose answer is a word rather than a figure (yes/no, proposed/adopted/effective)
lists its `options` in the table. Each run picks one (or "other"), code checks it
is one of them, and the runs must pick the same one, so a status is never read
from free text. The agreed option is the row's value.

So is the figure of an ask whose table entry names its `unit` (`# sq ft/gal`,
`# weeks`): the figure the answer fills the first mark with, a range as one
figure, with that unit. A data sheet's spread rate then reaches the ledger as a
value, and a `material` row's calc may rest on it ([`schema.py`](schema.py)
replays a calc at both ends of a range and the row's value is the range the
ends give, each range input named once and every corner positive; a calc that
rests on a flagged row is refused unless the row carries a flag itself, and a
derived row claims no firmer confidence than its weakest input. An allowance or
an order quantity is held to its method rule down the whole chain of calcs it
rests on: a scaled area that passes through a fetched or clause row with its own
calc is still scaled, and feeds neither). An ask with no unit (a date, an edition) keeps its
figures in the sentence; a unit ask has no `options`, and its first mark never
fills one of the page's identifiers.

When the runs find differing readings on the page (different passages and
figures, or one run finds it and one does not), code does not pick one. Each
reading is written as its own row, flagged unverified, for the estimator to settle.

A run that is discarded, or an answer that is refused, gets up to two spare
runs for that page, so one slip does not cost the ask.

A page that does not open is written as one row flagged unverified, saying why,
and so is an ask with no agreed answer and no reading kept, so the gap reaches
the bid.
The test bids record a dead link the same way. Each fetch is logged with its
date, its content hash and, after a redirect, the address that served it.

Golden gate, live only: `python -m pipeline web fixtures/<job> --out runs/x`.
For every ask gated against one of the fixture's own fetched rows, the run's
quote must carry most of the fixture's quote, or every figure of the fixture's
statement, and a closed ask's option must be the one the fixture's row gives
(kept beside the fixture in `web_choices.yaml`, never in the page table). A row
that does not is a wrong answer and fails the gate. An
unverified reading does not count as an answer. An ask
with no row is a miss: safe, since the bid then has no verified row for it, but
incomplete, so at least 80% of the compared asks must have a row. Two cases are
reported and left out:

- a page that no longer opens, or no longer carries the fixture's quote (the
  page changed, not the agent);
- a page the job's own documents do not lead to: the test bid found it by
  searching, which is not built (p.13). The offline golden test pins how many
  asks each job compares (19 and 24), so a table edit that stops a page
  matching still fails CI.

The gate runs in `live.yml` (`part: web`).

### Order rows

[`materials.py`](materials.py) is the Materials agent's second job: "product
data sheet figures (spread rate, yield, pack size), order quantities" (p.4). It
reads ledger rows, never documents or pages. After the pages are read, the model
is shown the job's current rows (what the documents say, the figures, the FIELD
rows for what will be measured, the fetched data sheets; never a scaled or
observed row, p.6) and says, for each product the rows name, which rows its
order rests on: the data-sheet row, the quantity row it covers (a takeoff area,
length, volume or count, the allowance it goes into, or the FIELD row for one),
the clause row that states the coat count (a coating only) and the spec's own
coverage row when there is one, since the spec's stated rate takes precedence
over the data sheet's (p.16). The model writes no number: code reads the coat
count from the clause it names, and when the clause names a product code it
reads the count for the code the data-sheet row carries: the clause is cut into
stretches at semicolons, sentence ends, "or" (not "or approved equal"), "then", "and", "over", "followed by", "after", "before" and "prior to" (outside parentheses), and a
count belongs to the one code its own stretch names ("A89 (coats not stated),
or K62, 1 coat" gives K62 one coat and leaves A89 open; "B53 over one coat of
primer" cuts at "over", so the primer's coat is not B53's); a count whose
stretch names no code, or two, is tied to nothing, and the clause then settles
no product's count; so is a count written after its code in a stretch opened by
a sequence word ("then", "over", "followed by", "after", "before", "prior to")
that continues anything, or by "and" after a product ("B53 over B66 primer, 2
coats" and "Base coat as needed, then B53, 2 coats" may give the system two
coats: two readings, neither picked, though "Walls and ceilings: B53, 2 coats"
continues no product). A stretch that names both a primer and a finish ("B53
finish with primer, 2 coats") holds two products' wording, so its count is
tied to nothing. A clause naming no code settles a count only
when no stretch names a product (a primer or finish word) the count could be
for ("primer; finish coats: two coats" gives the primer nothing) and the
stretches that state counts agree. A data-sheet row naming no product code is
read as the clause's one code when every stretch names it or names no product;
when a stretch names a product without a code, or the clause names several
codes, it settles nothing for that sheet; a sheet row that names a second
code (a recommended primer, a system) is read for neither; and a codeless
clause that speaks of a primer only is read for a sheet that names a primer,
one that speaks of a finish only is no primer sheet's. A second count said
without the word "coat", or an added coat ("one coat; two at patched areas",
"1 coat; 2 at patched areas", "a second coat at repairs", "plus 1 coat at
repairs", "double coat at repairs", "recoat patched areas"), is a second
reading, so the clause settles nothing; a hyphenated count ("2-coat system")
is a count. A customer row sets no count (it never overrides a spec clause). A
recoat time ("between coats: 24 hours") or a coverage line ("finish coat: 400
sq ft/gal") is no count: the "coats:" form counts only when "coats" is the label
itself (the label starts a stretch, after punctuation or a cut word, and may
be led by "number of" or one role word; "mils per coat: 4" and
"between finish coats: 24" state none), and the number after the colon must
end the clause or be followed by punctuation, "and" or "coat(s)"; "2/3 coats"
is a range. A clause that
states none, states several, states a range ("1-2 coats"), says the count is
not stated or speaks of another product settles nothing, and the order is
written without a figure saying so. Every row the figure rests on is named
with it in the derivation (`1200 sq ft (X-TK-Q-01) x 2 coats (X-SP-010) /
300-350 sq ft/gal (X-R-001) = ...`) and cited in the row's
locator and tag, so the Auditor can see where the literal came from. Code checks that
the quantity row's unit is what the rate covers (an area for a rate per gallon
over square feet, a length for a rate per tube over linear feet) and that a part
in `each` rests on a counted row in each, works the quantity out, `{AREA} *
coats / {RATE}` for a coating and `{Q} / {RATE}` for a mortar, sealant or
adhesive, at both ends of a rate stated as a range, and writes it as a `fetched`
material row citing the sheet (URL, date, quote), the only method the access
matrix lets Materials write (p.11). The row is named after the sheet's page (the
page table's title), never in the model's words, and a product code the model
writes must appear in the rows it cites. A count (anchors, bolts) cites its
count row and says so (`count from NAN-Q-003 (counted)`); an open question or a
flag on any row the order cites is carried onto the order. A product with no
data-sheet row in the ledger gets no row, and the run says so; an order that
waits on a FIELD row, a coat count no clause settles (none named, or one that
leaves it open, see above) or a rate nobody stated is
written with no figure, flagged unverified, naming what is missing; so is one
whose page did not show the product (a fetched row with no quote). Items are
compared across the valid runs by the rows they rest on and their unit, not the
product's wording (a second wording of one order in a run is noted, not written);
when the runs name different orders, or a run was discarded, one spare run is
made (as the page reader does); an order not every valid run saw is written
flagged unverified, never dropped or chosen. The step runs on the client Takeoff
uses (Sonnet): it reconciles rows from the spec, Takeoff and the page reader,
which the frugality rule puts on Sonnet, while the page reads stay on Haiku
(p.13). A rate, a yield, a coat count, a waste factor or a spare count is never
a number from the model: it is a row or it is nothing, and the schema and
[`schema.py`](schema.py) (`MATERIAL_OK`) hold the model and the broker to it.

Golden gate, live only, in `python -m pipeline web` after the pages: for every
material row of the fixture that cites a fetched page, the run must have an order
row in the fixture's unit (a tube is not a cartridge) citing a page the fixture
holds for the job (that page first; a product has several pages and the hand bid
cited one of them, so a row citing another is noted), with a figure exactly when
the fixture has one and equal to it; a firm run row where the hand bid flags its
order is noted; 80% of the compared orders must have a row. A fixture order that
cites no page (the hand-made bid's angle footage, its bolts) is noted and left
out: Materials writes fetched rows only.

Not built yet: the cold-cache search, the 90-day cache for federal regulations,
and the edition, effective-date, discontinuation and ESR-expiry comparisons
between bids (p.8). The page hash is in the fetch log, not on the row. An agreed
figure with no unit in the table (a date) is checked by code but stored only in
the row's sentence, not in `value`.
