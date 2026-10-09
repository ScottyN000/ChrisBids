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
    counted_label: bool = False                 # every wording carried a count the string lacks; the first is kept as printed

    @property
    def label(self) -> str:
        """The wordings the runs gave this figure, side by side when they differ."""
        return " | ".join(self.labels or [(self.item.get("label") or "").strip()])

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


# A bare count in a label: a number standing on its own before a word (`5 spaces`,
# `printed 5 times`, `(2 places)`). A number joined to a mark, a slash, a hyphen or a
# letter (`2'-8"`, `1/S-1`, `S-1`, `2x4`), or named by the word before it (`detail 1`,
# `sheet 2`, `type 3`), is a figure or a reference and is left alone.
BARE_COUNT = re.compile(r"(?<![\w\-/.'\"])(\d+)(?=\s+[A-Za-z])")
REFERENCE_WORDS = {"det", "detail", "dtl", "sheet", "sht", "ref", "note", "type", "no", "mark", "section", "sect", "view", "plan", "elev", "elevation", "grid", "line", "level", "lvl", "step", "phase", "unit", "bldg", "building", "item", "typ"}


def bare_counts(label: str) -> list[str]:
    """The numbers in a label that are counts, as `BARE_COUNT` says, with those a
    reference word names left out."""
    out = []
    for m in BARE_COUNT.finditer(label):
        before = re.findall(r"[A-Za-z]+", label[: m.start()])
        if not before or before[-1].lower() not in REFERENCE_WORDS:
            out.append(m.group(1))
    return out


def dimension_labels(items: list[dict]) -> tuple[list[str], bool]:
    """Every run's wording of a dimension's label, first seen first, and whether a count
    had to be let through. Wordings that differ only in case or trailing punctuation are
    one. A wording carrying a bare count (`printed 5 times`, with the dimension string
    itself taken out first) is a count with no counted row behind it (traceability), so
    it is dropped while another wording survives; when none does, the first is kept as
    printed, never rewritten, and the second value is True so the row can say so. Sheet
    and detail references (`REF. DET. 1/S-1`) are not counts."""
    text = (items[0].get("text") or "").strip()
    labels, seen, counted = [], set(), []
    for it in items:
        label = (it.get("label") or "").strip()
        wording = re.sub(r"[\s.,;:]+$", "", label).lower()
        if not wording or wording in seen:
            continue
        seen.add(wording)
        (counted if bare_counts(label.replace(text, " ") if text else label) else labels).append(label)
    if labels:
        return labels, False
    return counted[:1], bool(counted)


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
        labels, counted_label = [], False
        if reader == "drawing" and items[0].get("kind") == "dimension":
            labels, counted_label = dimension_labels(items)
        out.append(Voted(item=items[0], seen=len(items), runs=len(runs), readings=readings, labels=labels,
                         counted_label=counted_label))
    return out
