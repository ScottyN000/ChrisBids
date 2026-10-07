"""Redundancy, not judgment (architecture p.9).

Each unit is read two or three times and the runs are compared field by field.
Agreement passes. Disagreement is kept, every reading in the order it first
appeared, and the row is flagged for a human; no reading is chosen, not even a
majority. An item that only some runs produced is kept and flagged unverified.

Fields that are allowed to vary between runs (a photo's description wording)
are excluded from the comparison and taken from the first run that has them.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field


def _norm(s) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip().lower()


# key: what identifies the same item across runs.
# compared: the fields that must agree for the item to pass.
RULES = {
    "drawing": {"key": ("kind", "label"), "compared": ("text", "count", "unit")},
    "spec": {"key": ("clause", "requirement"), "compared": ("division", "product")},
    "photo": {"key": ("location", "condition"), "compared": ("severity",)},
    "correspondence": {"key": ("instruction",), "compared": ("sender", "date")},
}


@dataclass
class Voted:
    item: dict                                  # the first run's item
    seen: int                                   # runs that produced this key
    runs: int                                   # runs made
    readings: dict[str, list] = field(default_factory=dict)  # field -> distinct values, if they differ

    @property
    def status(self) -> str:
        if self.readings:
            return "conflict"
        if self.seen < self.runs:
            return "partial"
        return "agree"


def vote(reader: str, runs: list[list[dict]]) -> list[Voted]:
    rule = RULES[reader]
    order: list[tuple] = []
    by_key: dict[tuple, list[dict]] = {}
    for items in runs:
        seen_this_run = set()
        for item in items:
            k = tuple(_norm(item.get(f)) for f in rule["key"])
            if k in seen_this_run:
                continue  # a run that repeats itself counts once
            seen_this_run.add(k)
            if k not in by_key:
                by_key[k] = []
                order.append(k)
            by_key[k].append(item)
    out = []
    for k in order:
        items = by_key[k]
        readings = {}
        for f in rule["compared"]:
            values = []
            for it in items:
                v = it.get(f)
                if v not in values:
                    values.append(v)
            if len(values) > 1:
                readings[f] = values
        out.append(Voted(item=items[0], seen=len(items), runs=len(runs), readings=readings))
    return out
