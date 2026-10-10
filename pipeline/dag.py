"""The pipeline DAG, as data that the tests hold the code to.

The source is the "Pipeline at a glance" diagram in the architecture doc (p.2):
bid packet -> Intake -> Source Register -> the eight specialists -> the claim
ledger -> Scope Writer -> proposal draft -> Auditor -> bid ships, with an
Auditor fail sending an orphan figure back to the ledger.

Every node records how far the code has got with it. `tests/test_dag.py` checks
that record against the repository (that a node marked built has its module,
that every agent maps to a principal in the access matrix, that an agent which
writes to the ledger is one the matrix lets write), and
`tools/render_dag.py --check` fails CI when docs/pipeline-dag.md has fallen
behind this file. So the diagram changes in the same PR as the code, or the
build goes red.
"""
from __future__ import annotations

from dataclasses import dataclass

# How far the code has got with a node.
BUILT = "built"              # in the repo and covered by tests
REPLAY = "replay-only"       # built and tested on recorded model output; no live model call yet
PLANNED = "planned"          # in the architecture doc, not in the repo yet
HUMAN = "human"              # a person's step; the pipeline cannot do it
DATA = "data"                # a store or document, not an agent

STATUSES = (BUILT, REPLAY, PLANNED, HUMAN, DATA)


@dataclass(frozen=True)
class Node:
    id: str
    label: str
    detail: str                 # the subtitle on the architecture diagram
    status: str
    principal: str = ""         # roles.PRINCIPALS key, for agents
    module: str = ""            # the code that implements it, relative to the repo root
    phase: int = 0              # build-plan phase (architecture p.16-17)


NODES: tuple[Node, ...] = (
    Node("packet", "Bid packet", "email, PDFs, photos", DATA),
    Node("intake", "Intake & Classifier", "hash, dedupe, tag divisions", BUILT,
         principal="intake", module="pipeline/intake.py", phase=1),
    Node("register", "Source Register", "one ID per page and photo", DATA, module="pipeline/ledger.py"),
    Node("orchestrator", "Orchestrator", "spawns only the specialists the detected divisions need", BUILT,
         principal="orchestrator", module="pipeline/orchestrator.py", phase=3),
    Node("drawing", "Drawing Reader", "dimensioned vs scaled", BUILT,
         principal="drawing_reader", module="pipeline/readers/prompts/drawing.md", phase=2),
    Node("spec", "Spec Reader", "clause + page cite", BUILT,
         principal="spec_reader", module="pipeline/readers/prompts/spec.md", phase=2),
    Node("photo", "Photo Reader", "conditions, no numbers", BUILT,
         principal="photo_reader", module="pipeline/readers/prompts/photo.md", phase=2),
    Node("correspondence", "Correspondence", "emails, PM notes", REPLAY,
         principal="correspondence_reader", module="pipeline/readers/prompts/correspondence.md", phase=2),
    Node("takeoff", "Takeoff", "derivation or FIELD", BUILT, principal="takeoff",
         module="pipeline/takeoff.py", phase=3),
    Node("codes", "Codes & Regs", "fetched, URL + section; unchanged pages not re-read", BUILT, principal="codes",
         module="pipeline/webread.py", phase=4),
    Node("materials", "Materials", "data sheet rates and order quantities, cited", BUILT, principal="materials",
         module="pipeline/materials.py", phase=4),
    Node("customer", "Customer Reqs", "base vs alternates", PLANNED, principal="customer_requirements", phase=3),
    Node("ledger", "Claim ledger", "value, source, locator, method, derivation, audit result", DATA,
         module="pipeline/broker.py"),
    Node("scope_writer", "Scope Writer", "reads the ledger only", BUILT, principal="scope_writer",
         module="pipeline/scope_writer.py", phase=3),
    Node("draft", "Proposal draft", "Contractor Co. division format", DATA),
    Node("auditor", "Auditor", "opens every cited source", BUILT,
         principal="auditor", module="pipeline/auditor.py", phase=1),
    Node("ships", "Bid ships", "with ledger and audit log; the estimator releases by hand", HUMAN, principal="estimator"),
)

# (from, to, label). Read-only flows are edges too: the Scope Writer reads the
# ledger and writes nothing to it.
EDGES: tuple[tuple[str, str, str], ...] = (
    ("packet", "intake", ""),
    ("intake", "register", ""),
    ("register", "orchestrator", ""),
    ("orchestrator", "drawing", "spawns"),
    ("orchestrator", "spec", "spawns"),
    ("orchestrator", "photo", "spawns"),
    ("orchestrator", "correspondence", "spawns"),
    ("drawing", "takeoff", ""),
    # Not drawn on p.2, but the roster (p.4) has Takeoff read the Photo Reader's
    # rows too: each photo condition becomes a FIELD row saying what to measure.
    ("photo", "takeoff", ""),
    ("spec", "codes", ""),
    ("photo", "materials", ""),
    ("spec", "materials", "products"),
    ("takeoff", "materials", "figures"),
    ("correspondence", "customer", ""),
    ("drawing", "ledger", "rows"),
    ("spec", "ledger", "rows"),
    ("photo", "ledger", "rows"),
    ("correspondence", "ledger", "rows"),
    ("takeoff", "ledger", "rows"),
    ("codes", "ledger", "rows"),
    ("materials", "ledger", "rows"),
    ("customer", "ledger", "rows"),
    ("ledger", "scope_writer", "reads"),
    ("scope_writer", "draft", ""),
    ("draft", "auditor", ""),
    ("ledger", "auditor", "reads"),
    ("auditor", "ledger", "fail: orphan figure, back to ledger"),
    ("auditor", "ships", "pass"),
)

BY_ID = {n.id: n for n in NODES}


def writers_to_ledger() -> set[str]:
    """Agent nodes with a `rows` edge into the ledger."""
    return {a for a, b, label in EDGES if b == "ledger" and label == "rows"}


def mermaid() -> str:
    """The DAG as a Mermaid flowchart, styled by build status."""
    shape = {DATA: ("[(", ")]"), HUMAN: ("([", "])")}
    lines = ["flowchart TD"]
    for n in NODES:
        open_, close = shape.get(n.status, ("[", "]"))
        tag = "" if n.status in (DATA,) else f"<br/><i>{n.status}</i>"
        lines.append(f'    {n.id}{open_}"<b>{n.label}</b><br/>{n.detail}{tag}"{close}')
    for a, b, label in EDGES:
        arrow = "-.->" if label == "reads" else "-->"
        lines.append(f"    {a} {arrow}|{label}| {b}" if label else f"    {a} {arrow} {b}")
    lines += [
        "    classDef built fill:#dff3e4,stroke:#2e7d32",
        "    classDef replay fill:#fff4d6,stroke:#b8860b",
        "    classDef planned fill:#f2f2f2,stroke:#9e9e9e,stroke-dasharray: 4 3",
        "    classDef human fill:#e3edf9,stroke:#1f5fa8",
        "    classDef data fill:#ffffff,stroke:#555",
    ]
    css = {BUILT: "built", REPLAY: "replay", PLANNED: "planned", HUMAN: "human", DATA: "data"}
    for status, cls in css.items():
        ids = [n.id for n in NODES if n.status == status]
        if ids:
            lines.append(f"    class {','.join(ids)} {cls}")
    return "\n".join(lines)


def document() -> str:
    """docs/pipeline-dag.md, generated from this file."""
    rows = "\n".join(
        f"| {n.label} | {n.status} | {n.phase or '—'} | {n.principal or '—'} | "
        f"{f'[`{n.module}`](../{n.module})' if n.module else '—'} |"
        for n in NODES
    )
    return f"""# Pipeline DAG

Generated from [`pipeline/dag.py`](../pipeline/dag.py) by `python3 tools/render_dag.py`. Do not edit by hand:
CI runs `tools/render_dag.py --check` and `tests/test_dag.py`, so a change to the pipeline that does not
update the DAG fails the build.

Source: "Pipeline at a glance", architecture doc p.2. Status says how far the code has got with each node.

```mermaid
{mermaid()}
```

| Node | Status | Phase | Principal | Code |
|---|---|---|---|---|
{rows}

Status key: **built** is in the repo and tested; **replay-only** is built and tested on recorded model output
with no live model call yet; **planned** is in the architecture doc but not built; **human** is a person's
step the pipeline cannot take; **data** is a store or a document.
"""
