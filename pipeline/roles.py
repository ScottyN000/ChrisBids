"""The access matrix as data (architecture p.10-12).

Each agent is a service principal with one role and the minimum access that
role needs. The broker is the single process with write access to the ledger;
it checks the caller's principal against the method it is trying to write, so
"Photo Reader writes observed only" is enforced here rather than trusted to a
prompt.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Principal:
    name: str
    methods: frozenset[str] = frozenset()      # ledger methods it may write
    roles: frozenset[str] | None = None        # ledger roles it may write (None = any)
    writes_register: bool = False              # Source Register (Intake only)
    writes_audit: bool = False                 # the audit field (Auditor only)
    reads_sources: frozenset[str] | None = None  # register kinds it may open (None = any)
    reads_prices: bool = False
    egress: bool = False                       # outbound network, domain-allowlisted
    requires_supersedes: bool = False          # may only correct an existing row
    note: str = ""


def _p(name, **kw):
    methods = frozenset(kw.pop("methods", ()))
    roles = kw.pop("roles", None)
    reads = kw.pop("reads_sources", None)
    return Principal(
        name=name, methods=methods,
        roles=None if roles is None else frozenset(roles),
        reads_sources=None if reads is None else frozenset(reads),
        **kw,
    )


# Ledger-write scope per principal. The method column of the access matrix
# (architecture p.11) is the authority for who may write what.
PRINCIPALS: dict[str, Principal] = {p.name: p for p in [
    _p("orchestrator", reads_sources=(),
       note="Plans which agents run from the Source Register. Writes no figure of its own."),
    _p("intake", writes_register=True, reads_sources=None,
       note="The only writer of the Source Register. Hashes every file; writes no ledger row."),
    _p("drawing_reader", methods=("dimensioned", "counted", "scaled", "clause"),
       reads_sources=("drawing",),
       note="Dimensioned, scaled and counted values plus note text from plans and details."),
    _p("spec_reader", methods=("clause",), reads_sources=("spec", "proposal-template", "design", "prior-bid"),
       note="One row per clause with a page cite; conflicting clauses paired."),
    _p("photo_reader", methods=("observed",), reads_sources=("photo",),
       note="Conditions and locations only; the schema refuses a number."),
    _p("correspondence_reader", methods=("customer",), reads_sources=("correspondence",),
       note="Extracts instructions verbatim with sender and date."),
    _p("customer_requirements", methods=("customer",), reads_sources=("correspondence",),
       note="Reconciles an instruction against a spec clause: conflict or narrower scope."),
    _p("takeoff", methods=("counted", "scaled", "FIELD"), reads_sources=(),
       note="Quantities with derivation strings and FIELD placeholders; reads reader rows, not documents."),
    _p("codes", methods=("fetched",), reads_sources=(), egress=True,
       note="Code editions, permits and standards, each with a fetched URL and section."),
    _p("materials", methods=("fetched",), reads_sources=(), egress=True,
       note="Product data sheet figures and order quantities, each citing the sheet."),
    _p("scope_writer", reads_sources=(),
       note="Reads the ledger only and renders the proposal; writes no row and never sees prices."),
    _p("auditor", writes_audit=True, reads_sources=None, egress=True,
       note="The only principal that sets the audit field; opens each cited source and confirms the value."),
    _p("pricing", reads_sources=(), reads_prices=True,
       note="Phase 5. Every unit price cites a rate-book line the way a quantity cites a drawing."),
    _p("field_crew", methods=("dimensioned",), roles=("quantity",), reads_sources=(), requires_supersedes=True,
       note="Fills a FIELD row through a form: a measured row that supersedes the placeholder, nothing else."),
    _p("estimator", methods=(), reads_sources=None, reads_prices=True,
       note="Release authority and rate-book owner. Releases by hand; the pipeline cannot."),
]}

# Phase 1 loads the hand-made fixtures into a ledger. Each fixture row is routed
# to the principal that would have written it, so the access matrix is exercised
# by the golden tests rather than described in a comment.
FIXTURE_ROUTING: dict[str, str] = {
    "dimensioned": "drawing_reader",
    "scaled": "takeoff",
    "counted": "drawing_reader",
    "clause": "spec_reader",
    "observed": "photo_reader",
    "fetched": "codes",
    "customer": "customer_requirements",
    "FIELD": "takeoff",
}
# Kinds of source whose clause rows come from the Drawing Reader, not the Spec Reader.
DRAWING_KINDS = frozenset({"drawing"})


def get(name: str) -> Principal:
    try:
        return PRINCIPALS[name]
    except KeyError:
        raise KeyError(f"unknown principal {name!r}; known: {sorted(PRINCIPALS)}") from None


def route(method: str, source_kinds: frozenset[str] | set[str], *, has_calc: bool = False) -> str:
    """Which principal would have written a row with this method and sources."""
    if method in ("clause",) and source_kinds & DRAWING_KINDS:
        return "drawing_reader"
    if method == "counted" and has_calc:
        return "takeoff"
    return FIXTURE_ROUTING[method]
