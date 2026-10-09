"""Redundancy, not judgment (architecture p.9).

Each unit is read two or three times and the runs are compared field by field.
Agreement passes. Disagreement is kept, every reading in the order it first
appeared, and the row is flagged for a human; no reading is chosen, not even a
majority. An item that only some runs produced is kept and flagged unverified.

Fields that are allowed to vary between runs (a photo's description wording)
are excluded from the comparison and taken from the first run that has them.
A dimension's label is the exception: every run's wording is kept on the row,
in first-seen order, because Takeoff reads the label to learn what the string
spans and one run's wording can miss that (live run 37941127022, 2026-10-09).

A drawing figure is identified by the figure itself, not by the label the model
gives it, since labels are worded differently on every run (live run 1,
2026-10-08). A dimension string is one figure however many times it is printed,
as the fixtures record it. Counts are matched by value and unit in the order
each run lists them, so two "3 per bracket" counts on one detail stay two. A
figure one run read differently is then a separate row that not every run
produced, flagged unverified with the rest.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field


def _norm(s) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip().lower()


# key: what identifies the same item across runs.
# compared: the fields that must agree for the item to pass.
RULES = {
    "drawing": {"key": ("kind", "label"), "compared": ("text", "count", "unit")},   # scaled, note, load, standard
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
    labels: list[str] = field(default_factory=list)  # a drawing dimension's label from every run, first seen first

    @property
    def label(self) -> str:
        """The wordings the runs gave this figure, side by side when they differ."""
        return " / ".join(self.labels or [(self.item.get("label") or "").strip()])

    @property
    def status(self) -> str:
        if self.readings:
            return "conflict"
        if self.seen < self.runs:
            return "partial"
        return "agree"


# Drawing kinds identified by their figure: the fields that make up the key.
FIGURE_KEY = {"dimension": ("text",), "count": ("count", "unit")}


def _key(reader: str, item: dict, seen: dict) -> tuple:
    fields = FIGURE_KEY.get(item.get("kind")) if reader == "drawing" else None
    if fields is None:
        return tuple(_norm(item.get(f)) for f in RULES[reader]["key"])
    k = (item["kind"], *(_norm(item.get(f)) for f in fields))
    if item["kind"] == "count":
        # The n-th count of this value in a run is matched with the n-th in the others.
        n = seen.get(k, 0)
        seen[k] = n + 1
        k = (*k, n)
    return k


def vote(reader: str, runs: list[list[dict]]) -> list[Voted]:
    rule = RULES[reader]
    order: list[tuple] = []
    by_key: dict[tuple, list[dict]] = {}
    for items in runs:
        seen_this_run = set()
        counts: dict[tuple, int] = {}
        for item in items:
            k = _key(reader, item, counts)
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
        labels = []
        if reader == "drawing" and items[0].get("kind") == "dimension":
            seen_wordings = set()
            for it in items:
                label = (it.get("label") or "").strip()
                wording = re.sub(r"[\s.,;:]+$", "", label).lower()
                if wording and wording not in seen_wordings:
                    seen_wordings.add(wording)
                    labels.append(label)
        out.append(Voted(item=items[0], seen=len(items), runs=len(runs), readings=readings, labels=labels))
    return out
