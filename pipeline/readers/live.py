"""The live model client, and the units a live run shows it.

`LiveClient` implements `ModelClient.complete` against the Anthropic API:
one unit per call, the reader's prompt as a cached system prompt, the reader's
schema as structured output, no conversation. Every response is recorded in
the `recordings/` format `ReplayClient` reads, so a live run can be replayed
and diffed later without the network, and every call's usage goes to
`calls.jsonl` beside it.

Structured output cannot carry every constraint our schemas state (string
lengths, patterns, numeric bounds). The API is sent the subset it accepts, via
the SDK's own `transform_schema`; `run.read` still checks each response against
the full schema and discards any that fail, so a constraint the API could not
enforce is never silently dropped.

`units_for(job_dir, packet, work)` turns `fixtures/<job>/units.yaml` into the
input files: drawing views cropped from the sheet (`tiles.py`), spec pages as
their text layer, photos as they are.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import subprocess
from dataclasses import asdict
from pathlib import Path

import yaml

from ..intake import sha256_file
from . import tiles
from .clients import prompt_version
from .rows import Unit

MODEL = "claude-haiku-5-5"
# Where the key is sent. Pinned rather than taken from ANTHROPIC_BASE_URL, which
# cloud sessions set to their own endpoint; a different URL has to be passed to
# LiveClient explicitly.
API_URL = "https://api.anthropic.com"
# An explicit request timeout, in seconds. The SDK refuses a non-streaming call
# whose max_tokens it expects to run past its default 10 minutes unless the
# client sets its own timeout (anthropic 1.12 _base_client
# _calculate_nonstreaming_timeout); the Scope Writer asks for more than that.
API_TIMEOUT = 900.0
MEDIA = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}


class LiveRunError(RuntimeError):
    pass


def _refused(e: Exception) -> LiveRunError:
    return LiveRunError(
        f"the API refused ANTHROPIC_API_KEY ({e.__class__.__name__}); check the key in the cloud environment")


# ---- units -------------------------------------------------------------------

def units_for(job_dir: Path, packet: Path, work: Path) -> dict[str, list[Unit]]:
    """The units a live run reads, by reader, with each input file prepared under `work`."""
    job_dir = Path(job_dir)
    spec = yaml.safe_load((job_dir / "units.yaml").read_text()) or {}
    from .. import fixtures
    _, register = fixtures.read_fixture(job_dir)
    return prepare_units(job_dir.name, spec, register, packet, work)


def prepare_units(label: str, spec: dict[str, list[dict]], register: list[dict], packet: Path,
                  work: Path) -> dict[str, list[Unit]]:
    """Units from a spec in the units.yaml shape (a fixture's, or the Orchestrator's plan),
    each input file checked against its register hash and prepared under `work`."""
    packet, work = Path(packet), Path(work)
    files = {r["source_id"]: r["file"] for r in register if r["status"] == "present" and r["file"]}
    hashes = {r["source_id"]: r["sha256"] for r in register}

    out: dict[str, list[Unit]] = {}
    for reader, entries in spec.items():
        for e in entries:
            if e["source_id"] not in files:
                raise LiveRunError(f"{label} {e['unit_id']}: {e['source_id']} is not a present source in register.csv")
            src = packet / files[e["source_id"]]
            if not src.exists():
                raise LiveRunError(f"{e['unit_id']}: {src} not found under the packet root {packet}")
            # The file read must be the file registered (architecture p.10), wherever the packet came from.
            if sha256_file(src) != hashes[e["source_id"]]:
                raise LiveRunError(f"{e['unit_id']}: {files[e['source_id']]} does not match its register hash")
            path = _prepare(reader, e, src, work)
            # A text unit carries the exact text the model is shown, so run.read
            # can hold every verbatim field to it. Rasters carry none.
            text = path.read_text() if path.suffix.lower() in (".txt", ".eml", ".md") else ""
            out.setdefault(reader, []).append(Unit(
                unit_id=e["unit_id"], source_id=e["source_id"], locator=e.get("locator", ""),
                tag=e.get("tag", ""), path=str(path), scale=e.get("scale", ""), text=text,
            ))
    return out


def _safe(unit_id: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in unit_id)


def _prepare(reader: str, e: dict, src: Path, work: Path) -> Path:
    page = int(e.get("page", 1))
    if reader == "drawing":
        if "box" not in e:
            raise LiveRunError(f"{e['unit_id']}: a drawing unit needs a box (or list grid tiles as units)")
        return tiles.render_box(src, work / "units" / f"{_safe(e['unit_id'])}.png",
                                tiles.Box.of(e["box"]), page=page)
    if reader == "spec" and src.suffix.lower() == ".pdf":
        out = work / "units" / f"{_safe(e['unit_id'])}.txt"
        out.parent.mkdir(parents=True, exist_ok=True)
        text = subprocess.run(["pdftotext", "-layout", "-f", str(page), "-l", str(page), str(src), "-"],
                              capture_output=True, text=True, check=True, env=tiles.tool_env()).stdout
        if not text.strip():
            raise LiveRunError(f"{e['unit_id']}: page {page} of {src.name} has no text layer")
        out.write_text(text)
        return out
    return src


# ---- the API schema ----------------------------------------------------------

def api_schema(schema: dict) -> dict:
    """The part of a reader schema structured output accepts.

    Our schemas use two shorthands the SDK transform does not take: a list of
    types (`["string", "null"]`) and an `enum` with no `type`. Both become
    `anyOf`; the SDK transform then moves unsupported keywords into the
    description. The full schema is still enforced locally by `validate`.
    """
    import anthropic

    def norm(s):
        s = copy.copy(s)
        if "properties" in s:
            s["properties"] = {k: norm(v) for k, v in s["properties"].items()}
        if "items" in s:
            s["items"] = norm(s["items"])
        if "enum" in s and "type" not in s:
            values = s.pop("enum")
            kinds = []
            strs = [v for v in values if isinstance(v, str)]
            if strs:
                kinds.append({"type": "string", "enum": strs})
            if None in values:
                kinds.append({"type": "null"})
            return {"anyOf": kinds, **s} if len(kinds) > 1 else {**kinds[0], **s}
        if isinstance(s.get("type"), list):
            types = s.pop("type")
            rest = {k: s.pop(k) for k in list(s) if k not in ("description", "title")}
            return {"anyOf": [{"type": t, **(rest if t != "null" else {})} for t in types], **s}
        return s

    return anthropic.transform_schema(norm(schema))


# ---- the client --------------------------------------------------------------

def content_for(reader: str, unit: Unit) -> list[dict]:
    """The user turn for one unit: the unit itself, and one line saying what it is."""
    path = Path(unit.path)
    where = " ".join(filter(None, [unit.source_id, unit.locator]))
    if path.suffix.lower() in MEDIA:
        data = base64.standard_b64encode(path.read_bytes()).decode()
        head = {"drawing": f"Sheet {where}" + (f", stated scale {unit.scale}" if unit.scale else ""),
                "photo": f"Photo {unit.source_id}"}.get(reader, where)
        return [{"type": "image", "source": {"type": "base64", "media_type": MEDIA[path.suffix.lower()], "data": data}},
                {"type": "text", "text": f"{head}. Return the JSON for this {reader} unit."}]
    text = unit.text or path.read_text()
    # The delimiter is named after a hash of the text it wraps, so no text can
    # contain its own closing tag. Escaping would change what the reader copies
    # verbatim, and a random name would make the runs of one unit differ.
    tag = "unit-" + hashlib.sha256(text.encode()).hexdigest()[:16]
    source = where.replace("<", "").replace(">", "").replace('"', "'")
    return [{"type": "text", "text": f"<{tag} source=\"{source}\">\n{text}\n</{tag}>\n"
                                     f"The page is between the {tag} tags. Return the JSON for this {reader} unit."
                                     + (f"\n\n{unit.brief}" if unit.brief else "")}]


USAGE_KEYS = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")


def usage_totals(calls_log: Path) -> str:
    """One line totalling the tokens in a run's calls.jsonl, so the run log shows what it used."""
    lines = [json.loads(l) for l in Path(calls_log).read_text().splitlines() if l.strip()]
    total = {k: sum(c["usage"][k] or 0 for c in lines) for k in USAGE_KEYS}
    return f"usage: {len(lines)} calls, " + ", ".join(f"{v} {k}" for k, v in total.items())


class Recorder:
    """Writes responses as `<dir>/<reader>.json` in the ReplayClient format, after every call."""

    def __init__(self, directory: Path, *, model_id: str, note: str = ""):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.model_id = model_id
        self.note = note
        self._data: dict[str, dict] = {}

    def add(self, reader: str, unit: Unit, run: int, response):
        rec = self._data.setdefault(reader, {
            "reader": reader, "model": self.model_id, "prompt_version": prompt_version(reader),
            "note": self.note, "units": [],
        })
        entry = next((u for u in rec["units"] if u["unit_id"] == unit.unit_id), None)
        if entry is None:
            entry = {**{k: v for k, v in asdict(unit).items() if v}, "runs": []}
            rec["units"].append(entry)
        while len(entry["runs"]) <= run:
            entry["runs"].append(None)
        entry["runs"][run] = response
        (self.directory / f"{reader}.json").write_text(json.dumps(rec, indent=1, ensure_ascii=False) + "\n")


class LiveClient:
    """One stateless Messages API call per unit and run."""

    def __init__(self, *, model: str = MODEL, effort: str | None = "high", record: Path | None = None,
                 api: object | None = None, max_tokens: int = 16000, base_url: str = API_URL):
        self.api = api   # None until bind() gets the key from the broker
        self.model_id = model
        self.effort = effort
        self.max_tokens = max_tokens
        self.base_url = base_url
        self.recorder = Recorder(record, model_id=model, note=(
            "Live responses, recorded as returned. Replay with ReplayClient to re-check this run offline."
        )) if record else None
        self._schemas: dict[str, dict] = {}

    def request(self, reader: str, unit: Unit, system: str, schema: dict) -> dict:
        """The request body for one call. Identical for every run of a unit, so the
        three runs differ only by sampling, and the system prompt prefix is cached."""
        key = json.dumps(schema, sort_keys=True)   # a per-job schema is a new object each run
        if key not in self._schemas:
            self._schemas[key] = api_schema(schema)
        output_config = {"format": {"type": "json_schema", "schema": self._schemas[key]}}
        if self.effort:
            output_config["effort"] = self.effort
        return {
            "model": self.model_id,
            "max_tokens": self.max_tokens,
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            "messages": [{"role": "user", "content": content_for(reader, unit)}],
            "output_config": output_config,
        }

    def bind(self, broker) -> None:
        """Take the API key from the broker, which alone reads secrets
        (architecture p.12), and check it before any unit is read."""
        if self.api is not None:
            return
        key = broker.secret("ANTHROPIC_API_KEY")
        if not key:
            raise LiveRunError("neither ANTHROPIC_API_KEY nor MERSCO_ANTHROPIC_API_KEY is set; "
                               "add the key to the cloud environment and start a new session")
        import anthropic
        self.api = anthropic.Anthropic(api_key=key, base_url=self.base_url, timeout=API_TIMEOUT)
        self.check()

    def _create(self, **body):
        """One Messages API call; a refused key becomes a LiveRunError that says so."""
        try:
            return self.api.messages.create(**body)
        except Exception as e:
            if type(e).__name__ in ("AuthenticationError", "PermissionDeniedError"):
                raise _refused(e) from e
            raise

    def check(self) -> None:
        """One tiny call, so a missing key stops the run before any unit is read."""
        self._create(model=self.model_id, max_tokens=1, messages=[{"role": "user", "content": "ok"}])

    def complete(self, reader: str, unit: Unit, system: str, schema: dict, run: int) -> dict | str:
        body = self.request(reader, unit, system, schema)
        msg = self._create(**body)
        text = "".join(b.text for b in msg.content if b.type == "text")
        if msg.stop_reason != "end_turn":
            # Cut off or refused: kept as text, so run.read discards it as not JSON.
            response = text or f"(no output: stop_reason {msg.stop_reason})"
        else:
            try:
                response = json.loads(text)
            except json.JSONDecodeError:
                response = text
        if self.recorder:
            self.recorder.add(reader, unit, run, response)
            usage = msg.usage
            line = {
                "reader": reader, "unit_id": unit.unit_id, "run": run + 1, "model": msg.model,
                "request_id": msg._request_id, "stop_reason": msg.stop_reason,
                "input_sha256": hashlib.sha256(json.dumps(body["messages"], sort_keys=True).encode()).hexdigest()[:16],
                "usage": {k: getattr(usage, k) for k in USAGE_KEYS},
            }
            with open(self.recorder.directory / "calls.jsonl", "a") as f:
                f.write(json.dumps(line) + "\n")
        return response
