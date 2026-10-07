"""Feet-and-inch strings to inches, in code (architecture p.9: models extract,
code computes).

A Drawing Reader copies a dimension string exactly as printed ("15'-2\"").
This module turns it into inches and writes the derivation that shows how, so
the Auditor can replay it. Anything it cannot parse is refused, not guessed.
"""
from __future__ import annotations

import re
from fractions import Fraction

_FT_IN = re.compile(
    r"""^\s*
    (?:(?P<ft>\d+)\s*'\s*)?                 # 15'
    (?:-?\s*
       (?P<in>\d+)?                         # 2
       (?:\s*(?P<num>\d+)/(?P<den>\d+))?    # 1/2
       \s*"
    )?\s*$""",
    re.VERBOSE,
)


class DimensionError(ValueError):
    pass


def to_inches(text: str) -> Fraction:
    """`15'-2"` -> 182, `11"` -> 11, `3/4"` -> 3/4, `15'-2 1/2"` -> 365/2."""
    m = _FT_IN.match(text or "")
    if not m or not any(m.group(g) for g in ("ft", "in", "num")):
        raise DimensionError(f"not a feet-and-inch dimension string: {text!r}")
    total = Fraction(int(m.group("ft") or 0) * 12)
    total += int(m.group("in") or 0)
    if m.group("num"):
        den = int(m.group("den"))
        if den == 0:
            raise DimensionError(f"zero denominator in {text!r}")
        total += Fraction(int(m.group("num")), den)
    return total


def figure(value: Fraction) -> int | float:
    """An exact inch value as the ledger stores it: int when whole."""
    return int(value) if value.denominator == 1 else float(value)


def derivation(text: str) -> str:
    """The derivation string the ledger carries for a converted dimension."""
    return f"{text.strip()} dimension string = {figure(to_inches(text))} in"
