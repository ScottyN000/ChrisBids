"""The model boundary.

A reader never talks to a model directly; it hands one unit, one system prompt
and one schema to a client and gets back one JSON object. Every call is
stateless (architecture p.14: "no accumulated conversation").

`ReplayClient` answers from recorded responses on disk. It is how the golden
tests run without a network or an API key, and how a live run is re-checked
later: record once, replay forever. The live client that calls Haiku is added
when the project has an API key; it implements the same `complete` method.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Protocol

from .rows import Unit

PROMPTS = Path(__file__).resolve().parent / "prompts"


def prompt(reader: str) -> str:
    return (PROMPTS / f"{reader}.md").read_text()


def prompt_version(reader: str) -> str:
    """Pinned per release and stamped on every row (architecture p.9)."""
    return f"{reader}@{hashlib.sha256(prompt(reader).encode()).hexdigest()[:12]}"


class ModelClient(Protocol):
    model_id: str

    def complete(self, reader: str, unit: Unit, system: str, schema: dict, run: int) -> dict | str:
        """One stateless call. Returns the parsed JSON object, or raw text to be parsed."""
        ...


class ReplayClient:
    """Answers from `<dir>/<reader>.json`: {"units": [{"unit_id": ..., "runs": [...]}]}."""

    def __init__(self, directory: Path, *, model_id: str = "replay"):
        self.directory = Path(directory)
        self.model_id = model_id
        self._cache: dict[str, dict] = {}

    def recording(self, reader: str) -> dict:
        if reader not in self._cache:
            path = self.directory / f"{reader}.json"
            self._cache[reader] = json.loads(path.read_text()) if path.exists() else {"units": []}
        return self._cache[reader]

    def units(self, reader: str) -> list[Unit]:
        fields = ("unit_id", "source_id", "locator", "tag", "path", "scale", "text")
        return [Unit(**{k: u.get(k, "") for k in fields}) for u in self.recording(reader)["units"]]

    def complete(self, reader: str, unit: Unit, system: str, schema: dict, run: int) -> dict | str:
        for u in self.recording(reader)["units"]:
            if u["unit_id"] == unit.unit_id:
                runs = u["runs"]
                if run >= len(runs):
                    raise LookupError(f"{reader} {unit.unit_id}: no recorded run {run + 1}")
                return runs[run]
        raise LookupError(f"{reader}: no recording for unit {unit.unit_id}")
