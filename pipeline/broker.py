"""The ledger broker: the only thing with write access to a job's ledger.

"A single broker process that is the only thing with write access to the ledger
and checks the caller's role against the method it is trying to write"
(architecture p.12). An agent holds a broker handle bound to its principal; it
cannot widen its own scope, cannot edit or delete a row, and cannot set the
audit field unless it is the Auditor.

Versions are stamped here, not supplied by the caller, so every row can be
traced to the exact software that produced it (architecture p.9).
"""
from __future__ import annotations

import os
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from . import guard, roles, schema
from .ledger import SCHEMA_VERSION, Ledger
from .schema import Claim, LedgerError

# Pinned per release and written on every row (architecture p.9).
PROMPT_VERSION = "phase1-no-model-calls"
TOOL_VERSIONS = "pipeline=0.1.0"
# Phase 1 makes no model calls; readers in Phase 2 pass their own model ID.
DEFAULT_MODEL_ID = "none (code only)"

# Secrets live in the broker's environment, never in a prompt or a tool result
# (architecture p.12). Phase 1 needs none; the names are fixed here so a later
# phase does not invent its own.
SECRET_ENV = ("CHRISBIDS_RATE_BOOK", "CHRISBIDS_API_KEY", "ANTHROPIC_API_KEY")
# A secret may also arrive under another name, read only when its own is unset.
# The cloud environment does not pass ANTHROPIC_API_KEY through to sessions (it
# is the name Claude Code itself authenticates with), so the pipeline's key is
# stored as MERSCO_ANTHROPIC_API_KEY there.
SECRET_FALLBACK = {"ANTHROPIC_API_KEY": ("MERSCO_ANTHROPIC_API_KEY",)}


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Broker:
    """A ledger handle bound to one principal.

    `clock` is injectable so golden runs are reproducible: the fixtures stamp
    their own timestamp rather than the wall clock.
    """

    def __init__(
        self,
        ledger: Ledger,
        principal: str,
        *,
        run_id: str = "",
        model_id: str = DEFAULT_MODEL_ID,
        prompt_version: str = PROMPT_VERSION,
        tool_versions: str = TOOL_VERSIONS,
        agent_label: str = "",
        clock=utcnow,
    ):
        self.ledger = ledger
        self.principal = roles.get(principal)
        self.run_id = run_id or ledger.meta("run_id")
        self.model_id = model_id
        self.prompt_version = prompt_version
        self.tool_versions = tool_versions
        self.agent_label = agent_label or principal
        self.clock = clock

    # ---- construction -----------------------------------------------------

    @classmethod
    def open_job(
        cls, path: Path | str, principal: str, *, job: str = "", run_id: str = "", create: bool = False, **kw
    ) -> "Broker":
        ledger = Ledger(path, create=create)
        if create:
            ledger.set_meta(job=job, run_id=run_id, schema_version=SCHEMA_VERSION, created_at=utcnow())
        return cls(ledger, principal, run_id=run_id, **kw)

    def as_principal(self, principal: str, **kw) -> "Broker":
        """A second handle on the same ledger for a different principal."""
        return Broker(
            self.ledger, principal, run_id=self.run_id,
            model_id=kw.pop("model_id", self.model_id),
            prompt_version=kw.pop("prompt_version", self.prompt_version),
            tool_versions=kw.pop("tool_versions", self.tool_versions),
            clock=kw.pop("clock", self.clock), **kw,
        )

    # ---- logging ----------------------------------------------------------

    def _log(self, action: str, subject: str = "", detail: str = "") -> None:
        self.ledger.db.execute(
            "INSERT INTO audit_log(at, principal, action, subject, detail) VALUES(?,?,?,?,?)",
            (self.clock(), self.principal.name, action, subject, detail),
        )

    def log_call(self, subject: str, detail: str) -> None:
        """Record one model call: input hash, model ID, prompt version, output hash."""
        self._log("model-call", subject, f"{self.model_id}; {self.prompt_version}; {detail}")
        self.ledger.db.commit()

    def _deny(self, what: str) -> None:
        self._log("denied", what, f"principal {self.principal.name}")
        self.ledger.db.commit()
        raise LedgerError(f"{self.principal.name} may not {what}")

    # ---- Source Register (Intake only) ------------------------------------

    def write_register(self, sources: list[dict]) -> int:
        if not self.principal.writes_register:
            self._deny("write the Source Register")
        existing = self.ledger.register()
        at = self.clock()
        for s in sources:
            sid = s["source_id"]
            if sid in existing:
                old = existing[sid]
                if (old.get("sha256") or "") != (s.get("sha256") or ""):
                    self._deny(f"change the hash of {sid} (sources/ is write-once)")
                continue
            self.ledger.db.execute(
                "INSERT INTO register(source_id, title, file, sha256, pages, kind, status, notes,"
                " duplicate_of, principal, written_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    sid, s.get("title", ""), s.get("file", ""), s.get("sha256", ""),
                    "" if s.get("pages") in (None, "") else str(s["pages"]),
                    s.get("kind", "unknown"), s.get("status", "present"), s.get("notes", ""),
                    s.get("duplicate_of", ""), self.principal.name, at,
                ),
            )
            self._log("register", sid, s.get("kind", ""))
        self.ledger.db.commit()
        return len(self.ledger.register())

    # ---- claims -----------------------------------------------------------

    def append(self, claim: Claim | dict) -> Claim:
        """Append one ledger row, or refuse it."""
        c = claim if isinstance(claim, Claim) else Claim(**claim)
        errors = self._check(c)
        if errors:
            self._log("rejected", c.claim_id, "; ".join(errors))
            self.ledger.db.commit()
            raise LedgerError("\n".join(errors))

        # Who wrote a row and when is the broker's to say, not the caller's: a
        # reader cannot label its row as another agent's or backdate it.
        c = replace(
            c,
            agent=self.agent_label,
            timestamp=self.clock(),
            run_id=self.run_id,
            model_id=self.model_id,
            prompt_version=self.prompt_version,
            tool_versions=self.tool_versions,
            audit="",
            audit_note="",
        )
        cols = list(schema.CLAIM_FIELDS)
        values = [getattr(c, col) for col in cols]
        self.ledger.db.execute(
            # Column names come from schema.CLAIM_FIELDS, never from the caller;
            # every value is a bound parameter.
            f"INSERT INTO claims({', '.join(cols)}, principal, written_at) "  # nosec B608
            f"VALUES({', '.join('?' * len(cols))}, ?, ?)",
            (*values, self.principal.name, self.clock()),
        )
        self._log("append", c.claim_id, f"{c.method}/{c.role}")
        self.ledger.db.commit()
        return c

    def append_many(self, claims) -> list[Claim]:
        return [self.append(c) for c in claims]

    def _check(self, c: Claim) -> list[str]:
        if not self.principal.methods:
            self._deny("write ledger rows")
        if c.method not in self.principal.methods:
            self._deny(f"write a {c.method} row (it may write {sorted(self.principal.methods)})")
        if self.principal.roles is not None and c.role not in self.principal.roles:
            self._deny(f"write a {c.role} row")
        if self.principal.requires_supersedes and not c.supersedes:
            self._deny("write a row that does not supersede an existing one")
        if c.audit or c.audit_note:
            self._deny("set the audit field (only the Auditor does)")

        errors = schema.check_vocabulary(c) + schema.check_method_rules(c)
        by_id = self.ledger.by_id()
        if c.claim_id in by_id:
            errors.append(f"{c.claim_id}: already in the ledger; a correction is a new ID with supersedes")
        errors += schema.replay_calc(c, by_id)

        register = self.ledger.register()
        for sid in schema.sources_of(c.source_id):
            if sid not in register:
                errors.append(f"{c.claim_id}: source {sid!r} is not in the Source Register")
            elif register[sid]["status"] != "present" and c.flag != "unverified":
                errors.append(
                    f"{c.claim_id}: cites {sid} ({register[sid]['status']}) but is not flagged unverified"
                )
        if c.supersedes and c.supersedes not in by_id and not c.supersedes.startswith(("p.", "page ")):
            # A correction may supersede a row in this ledger or a passage of the
            # prior hand-made bid, which is a register source rather than a row.
            if not any(c.supersedes.startswith(sid) for sid in register):
                errors.append(f"{c.claim_id}: supersedes {c.supersedes!r}, which is neither a claim nor a source")
        # A row may point at an open question that has not been written yet: the
        # ledger is append-only, so the broker cannot see the future. The Auditor
        # checks that every question target resolved.
        return errors

    # ---- the audit field (Auditor only) -----------------------------------

    def set_audit(self, claim_id: str, verdict: str, note: str = "") -> None:
        if not self.principal.writes_audit:
            self._deny("set the audit field")
        if verdict not in schema.AUDIT:
            raise LedgerError(f"audit verdict {verdict!r} not in {list(schema.AUDIT)}")
        cur = self.ledger.db.execute(
            "UPDATE claims SET audit = ?, audit_note = ? WHERE claim_id = ?", (verdict, note, claim_id)
        )
        if cur.rowcount == 0:
            raise LedgerError(f"no claim {claim_id} to audit")
        self._log("audit", claim_id, f"{verdict}: {note}" if note else verdict)
        self.ledger.db.commit()

    # ---- egress -----------------------------------------------------------

    def may_fetch(self, url: str) -> bool:
        """Outbound network is for Codes & Regs, Materials and the Auditor only,
        and only to a public http(s) address. The per-domain allowlist is the
        egress proxy's job (hosting plan); this refuses what no allowlist should
        ever pass: file:, loopback, private and cloud-metadata addresses."""
        if not self.principal.egress:
            return False
        try:
            guard.public_url(url, resolve=False)
        except guard.UnsafeInput as e:
            self._log("denied", "fetch", f"{url[:200]}: {e}")
            self.ledger.db.commit()
            return False
        return True

    def secret(self, name: str) -> str:
        """Secrets come from the broker's environment, never from a prompt."""
        if name not in SECRET_ENV:
            raise LedgerError(f"{name} is not a broker secret; known: {list(SECRET_ENV)}")
        if not self.principal.reads_prices and name == "CHRISBIDS_RATE_BOOK":
            self._deny("read the rate book")
        for env in (name,) + SECRET_FALLBACK.get(name, ()):
            if os.environ.get(env):
                return os.environ[env]
        return ""

    def close(self) -> None:
        self.ledger.close()
