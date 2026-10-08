# Pipeline DAG

Generated from [`pipeline/dag.py`](../pipeline/dag.py) by `python3 tools/render_dag.py`. Do not edit by hand:
CI runs `tools/render_dag.py --check` and `tests/test_dag.py`, so a change to the pipeline that does not
update the DAG fails the build.

Source: "Pipeline at a glance", architecture doc p.2. Status says how far the code has got with each node.

```mermaid
flowchart TD
    packet[("<b>Bid packet</b><br/>email, PDFs, photos")]
    intake["<b>Intake & Classifier</b><br/>hash, dedupe, tag divisions<br/><i>built</i>"]
    register[("<b>Source Register</b><br/>one ID per page and photo")]
    orchestrator["<b>Orchestrator</b><br/>spawns only the specialists the detected divisions need<br/><i>built</i>"]
    drawing["<b>Drawing Reader</b><br/>dimensioned vs scaled<br/><i>built</i>"]
    spec["<b>Spec Reader</b><br/>clause + page cite<br/><i>built</i>"]
    photo["<b>Photo Reader</b><br/>conditions, no numbers<br/><i>built</i>"]
    correspondence["<b>Correspondence</b><br/>emails, PM notes<br/><i>replay-only</i>"]
    takeoff["<b>Takeoff</b><br/>derivation or FIELD<br/><i>built</i>"]
    codes["<b>Codes & Regs</b><br/>fetched, URL + section<br/><i>built</i>"]
    materials["<b>Materials</b><br/>data sheet rates, cited<br/><i>built</i>"]
    customer["<b>Customer Reqs</b><br/>base vs alternates<br/><i>planned</i>"]
    ledger[("<b>Claim ledger</b><br/>value, source, locator, method, derivation, audit result")]
    scope_writer["<b>Scope Writer</b><br/>reads the ledger only<br/><i>built</i>"]
    draft[("<b>Proposal draft</b><br/>Mersco division format")]
    auditor["<b>Auditor</b><br/>opens every cited source<br/><i>built</i>"]
    ships(["<b>Bid ships</b><br/>with ledger and audit log; Chris releases by hand<br/><i>human</i>"])
    packet --> intake
    intake --> register
    register --> orchestrator
    orchestrator -->|spawns| drawing
    orchestrator -->|spawns| spec
    orchestrator -->|spawns| photo
    orchestrator -->|spawns| correspondence
    drawing --> takeoff
    photo --> takeoff
    spec --> codes
    photo --> materials
    correspondence --> customer
    drawing -->|rows| ledger
    spec -->|rows| ledger
    photo -->|rows| ledger
    correspondence -->|rows| ledger
    takeoff -->|rows| ledger
    codes -->|rows| ledger
    materials -->|rows| ledger
    customer -->|rows| ledger
    ledger -.->|reads| scope_writer
    scope_writer --> draft
    draft --> auditor
    ledger -.->|reads| auditor
    auditor -->|fail: orphan figure, back to ledger| ledger
    auditor -->|pass| ships
    classDef built fill:#dff3e4,stroke:#2e7d32
    classDef replay fill:#fff4d6,stroke:#b8860b
    classDef planned fill:#f2f2f2,stroke:#9e9e9e,stroke-dasharray: 4 3
    classDef human fill:#e3edf9,stroke:#1f5fa8
    classDef data fill:#ffffff,stroke:#555
    class intake,orchestrator,drawing,spec,photo,takeoff,codes,materials,scope_writer,auditor built
    class correspondence replay
    class customer planned
    class ships human
    class packet,register,ledger,draft data
```

| Node | Status | Phase | Principal | Code |
|---|---|---|---|---|
| Bid packet | data | — | — | — |
| Intake & Classifier | built | 1 | intake | [`pipeline/intake.py`](../pipeline/intake.py) |
| Source Register | data | — | — | [`pipeline/ledger.py`](../pipeline/ledger.py) |
| Orchestrator | built | 3 | orchestrator | [`pipeline/orchestrator.py`](../pipeline/orchestrator.py) |
| Drawing Reader | built | 2 | drawing_reader | [`pipeline/readers/prompts/drawing.md`](../pipeline/readers/prompts/drawing.md) |
| Spec Reader | built | 2 | spec_reader | [`pipeline/readers/prompts/spec.md`](../pipeline/readers/prompts/spec.md) |
| Photo Reader | built | 2 | photo_reader | [`pipeline/readers/prompts/photo.md`](../pipeline/readers/prompts/photo.md) |
| Correspondence | replay-only | 2 | correspondence_reader | [`pipeline/readers/prompts/correspondence.md`](../pipeline/readers/prompts/correspondence.md) |
| Takeoff | built | 3 | takeoff | [`pipeline/takeoff.py`](../pipeline/takeoff.py) |
| Codes & Regs | built | 4 | codes | [`pipeline/webread.py`](../pipeline/webread.py) |
| Materials | built | 4 | materials | [`pipeline/webread.py`](../pipeline/webread.py) |
| Customer Reqs | planned | 3 | customer_requirements | — |
| Claim ledger | data | — | — | [`pipeline/broker.py`](../pipeline/broker.py) |
| Scope Writer | built | 3 | scope_writer | [`pipeline/scope_writer.py`](../pipeline/scope_writer.py) |
| Proposal draft | data | — | — | — |
| Auditor | built | 1 | auditor | [`pipeline/auditor.py`](../pipeline/auditor.py) |
| Bid ships | human | — | chris | — |

Status key: **built** is in the repo and tested; **replay-only** is built and tested on recorded model output
with no live model call yet; **planned** is in the architecture doc but not built; **human** is a person's
step the pipeline cannot take; **data** is a store or a document.
