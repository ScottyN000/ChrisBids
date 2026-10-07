"""Run one reader over a list of units and write its rows through the broker.

Per unit: call the model `repeats` times, discard any response that is not
schema-valid, vote the rest field by field, map the voted items to Claims with
the method fixed by rule, and append them as the reader's own principal. Every
call is logged with its input hash, model ID, prompt version and output hash
(architecture p.12).

On a text unit (a spec page's text layer, an email body) every field the
reader must copy verbatim is checked against the text it was shown, and an item
whose quote is not there is dropped and reported (rows.not_in_source).

A unit with no valid response writes nothing. It is reported back as unread, so
a human or a later run deals with it; a reader never fills a gap itself.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace

from ..broker import Broker
from ..schema import Claim, LedgerError
from . import rows, schemas, validate
from .clients import ModelClient, prompt, prompt_version
from .vote import vote

PRINCIPAL = {
    "drawing": "drawing_reader",
    "spec": "spec_reader",
    "photo": "photo_reader",
    "correspondence": "correspondence_reader",
}


@dataclass
class ReadResult:
    reader: str
    units: int = 0
    calls: int = 0
    discarded: list[str] = field(default_factory=list)   # "unit run N: why"
    unread: list[str] = field(default_factory=list)      # units with no valid response
    rows: list[Claim] = field(default_factory=list)
    refused: list[str] = field(default_factory=list)     # rows the broker refused

    def text(self) -> str:
        flagged = sum(1 for r in self.rows if r.flag)
        lines = [f"{self.reader}: {self.units} units, {self.calls} calls, {len(self.rows)} rows "
                 f"({flagged} flagged), {len(self.discarded)} responses discarded, {len(self.unread)} units unread"]
        lines += [f"  discarded {d}" for d in self.discarded]
        lines += [f"  unread {u}" for u in self.unread]
        lines += [f"  refused {r}" for r in self.refused]
        return "\n".join(lines)


def _hash(obj) -> str:
    text = obj if isinstance(obj, str) else json.dumps(obj, sort_keys=True)
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def read(
    broker: Broker,
    reader: str,
    job: str,
    units: list[rows.Unit],
    client: ModelClient,
    *,
    repeats: int = 3,
) -> ReadResult:
    schema = schemas.BY_READER[reader]
    system = prompt(reader)
    writer = broker.as_principal(
        PRINCIPAL[reader], model_id=client.model_id, prompt_version=prompt_version(reader),
        agent_label=f"{reader} reader",
    )
    result = ReadResult(reader=reader, units=len(units))
    for unit in units:
        responses = []
        for run in range(repeats):
            result.calls += 1
            raw = client.complete(reader, unit, system, schema, run)
            try:
                data = json.loads(raw) if isinstance(raw, str) else raw
            except json.JSONDecodeError as e:
                data, errs = None, [f"not JSON: {e}"]
            else:
                errs = validate.errors(data, schema)
                if not errs:
                    errs = rows.semantic_errors(reader, data)
            writer.log_call(
                unit.unit_id,
                f"run {run + 1}; input {_hash([unit.unit_id, unit.path, unit.text])}; output {_hash(raw)}; "
                f"{'discarded: ' + errs[0] if errs else 'valid'}",
            )
            if errs:
                result.discarded.append(f"{unit.unit_id} run {run + 1}: {'; '.join(errs[:3])}")
                continue
            kept = []
            for i, item in enumerate(data["items"]):
                missing = rows.not_in_source(reader, unit, item)
                if missing:
                    result.discarded.append(
                        f"{unit.unit_id} run {run + 1}: items[{i}] {', '.join(missing)} not in the source text")
                else:
                    kept.append(item)
            responses.append(kept)
        if not responses:
            result.unread.append(unit.unit_id)
            continue
        for claim in rows.to_claims(reader, job, unit, vote(reader, responses)):
            if len(responses) < repeats and not claim.flag:
                # Discarded runs mean there was less to compare: agreement among
                # the survivors is not the agreement the redundancy rule asks for.
                note = f"only {len(responses)} of {repeats} runs were schema-valid"
                claim = replace(claim, flag="unverified",
                                derivation="; ".join(filter(None, [claim.derivation, note])))
            try:
                result.rows.append(writer.append(claim))
            except LedgerError as e:
                result.refused.append(f"{claim.claim_id}: {e}")
    return result
