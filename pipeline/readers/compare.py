"""A reader-produced ledger against a golden fixture.

"Any prompt or model change must reproduce their ledgers exactly on
dimensioned and counted fields before deployment" (architecture p.10). A figure
is identified by what a human would check: the source, the place on it, the
method, the value and the unit. Claim IDs and wording are not compared.

Scaled and observed rows are judgment calls and allowed to vary between runs
(p.10), so they are reported but do not fail the comparison.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from ..schema import CALC_REF, Claim

EXACT = ("dimensioned", "counted")
REPORTED = ("scaled", "observed")


def reader_rows(claims: list[Claim], present_sources: set[str]) -> list[Claim]:
    """The fixture rows a reader, rather than Takeoff, would have written:
    no `calc`, one source that is in the packet, and a quantity or note role."""
    out = []
    for c in claims:
        if c.method not in EXACT + REPORTED or c.calc:
            continue
        if "+" in c.source_id or c.source_id not in present_sources:
            continue
        if c.method != "observed" and c.role not in ("quantity", "note"):
            continue
        out.append(c)
    return out


def key(c: Claim) -> tuple:
    if c.method == "observed":
        return (c.method, c.source_id)
    return (c.method, c.source_id, c.locator, c.value, c.unit)


@dataclass
class Comparison:
    matched: list[tuple] = field(default_factory=list)
    missing: list[tuple] = field(default_factory=list)   # in the fixture, not produced
    extra: list[tuple] = field(default_factory=list)     # produced, not in the fixture

    @property
    def exact_ok(self) -> bool:
        return not any(k[0] in EXACT for k in self.missing + self.extra)

    def text(self) -> str:
        lines = [f"{len(self.matched)} matched, {len(self.missing)} missing, {len(self.extra)} extra; "
                 f"dimensioned and counted {'reproduce exactly' if self.exact_ok else 'DO NOT reproduce'}"]
        for title, items in (("missing", self.missing), ("extra", self.extra)):
            for k in items:
                note = "" if k[0] in EXACT else "  (allowed to vary)"
                lines.append(f"  {title}: {' | '.join(map(str, k))}{note}")
        return "\n".join(lines)


def compare(produced: list[Claim], fixture: list[Claim], present_sources: set[str],
            sources: set[str] | None = None) -> Comparison:
    """Compare on the sources the produced run actually read (or `sources`)."""
    want_rows = reader_rows(fixture, present_sources)
    got_rows = [c for c in produced if c.method in EXACT + REPORTED]
    scope = sources if sources is not None else {c.source_id for c in got_rows}
    # Multisets, not sets: two anchors-per-bracket counts in one view are two
    # figures, and a reader that produced only one of them has missed one.
    want = Counter(key(c) for c in want_rows if c.source_id in scope)
    got = Counter(key(c) for c in got_rows)
    return Comparison(
        matched=sorted((want & got).elements()),
        missing=sorted((want - got).elements()),
        extra=sorted((got - want).elements()),
    )


def fixture_derived(fixture: list[Claim], present_sources: set[str]) -> list[Claim]:
    """The fixture rows Takeoff would have written: a dimensioned or counted
    quantity with a `calc`, resting only on sources in the packet."""
    return [c for c in fixture
            if c.calc and c.method in EXACT and c.role == "quantity"
            and all(s in present_sources for s in c.source_id.replace("+", " ").split())]


def add_derived(comparison: Comparison, produced_by_id: dict[str, Claim], produced: list[Claim],
                fixture: list[Claim], present_sources: set[str]) -> Comparison:
    """Takeoff's derived quantities against the fixture's, in the same comparison.

    A derived figure is identified by its method, value and unit. The same
    figure can be reached from more than one set of rows (18 anchors is 3 per
    bracket x 6 bracket symbols, or 3 x the brackets the dimensions space out),
    and both are sound, so the route is not compared. What is checked instead:

    * every reader figure a derived row rests on is one the fixture carries,
      or the row is reported extra with the figures it used;
    * a fixture quantity that the readers already wrote as a counted figure
      (6 brackets is also the 6 symbols drawn) is matched by that figure, since
      Takeoff is told not to derive a number that is already a row.
    """
    known = {key(c) for c in reader_rows(fixture, present_sources)}
    want = Counter(derived_key(c) for c in fixture_derived(fixture, present_sources))
    got, unsourced = Counter(), []
    for c in produced:
        if not c.calc:
            continue
        stray = [leaf for leaf in leaves(c, produced_by_id) if leaf not in known]
        if stray:
            unsourced.append(derived_key(c) + (tuple(stray),))
        else:
            got[derived_key(c)] += 1
    counted = Counter(("counted", "derived", c.value, c.unit) for c in produced_by_id.values()
                      if c.method == "counted" and not c.calc and not c.flag and key(c) in known)
    missing = want - got
    covered = missing & counted
    comparison.matched = sorted(comparison.matched + list((want & got).elements()) + list(covered.elements()))
    comparison.missing = sorted(comparison.missing + list((missing - covered).elements()))
    comparison.extra = sorted(comparison.extra + list((got - want).elements()) + unsourced)
    return comparison


def derived_key(c: Claim) -> tuple:
    return (c.method, "derived", c.value, c.unit)


def leaves(c: Claim, by_id: dict[str, Claim], _depth: int = 0) -> tuple:
    """The reader figures a derived row rests on, by what a human would check,
    followed through any derived row it uses in turn."""
    if not c.calc or _depth > 20:
        return (key(c),)
    out = set()
    for r in CALC_REF.findall(c.calc):
        if r in by_id:
            out.update(leaves(by_id[r], by_id, _depth + 1))
    return tuple(sorted(out))
