"""The ledger store: one append-only SQLite file per job.

Append-only is enforced twice. The broker checks the caller's principal before
it writes; SQLite triggers refuse a DELETE on either table and refuse an UPDATE
of any claim column except the two audit columns, so a bug or a direct
connection cannot edit history either. A correction is a new row whose
`supersedes` points at the row it replaces.

This module is the storage layer only: it does no permission checking. Every
write goes through `pipeline.broker`.
"""
from __future__ import annotations

import csv
import io
import json
import sqlite3
from pathlib import Path

from .schema import CLAIM_FIELDS, LEDGER_FIELDS, Claim

SCHEMA_VERSION = 1

_UPDATABLE = ("audit", "audit_note")
_CLAIM_COLUMNS = [f for f in CLAIM_FIELDS]
_LOCKED_COLUMNS = [c for c in _CLAIM_COLUMNS if c not in _UPDATABLE]

DDL = f"""
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Source Register: one ID per page, sheet, photo and message (architecture p.5).
-- Write-once: Intake writes it, nobody else, and a hash change fails the run.
CREATE TABLE IF NOT EXISTS register (
    source_id    TEXT PRIMARY KEY,
    title        TEXT NOT NULL,
    file         TEXT,
    sha256       TEXT,
    pages        TEXT,
    kind         TEXT NOT NULL,
    status       TEXT NOT NULL,
    notes        TEXT,
    duplicate_of TEXT,
    principal    TEXT NOT NULL,
    written_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS claims (
    seq            INTEGER PRIMARY KEY AUTOINCREMENT,
    {", ".join(f"{c} TEXT" for c in _CLAIM_COLUMNS if c != "value_num")},
    value_num      REAL,
    principal      TEXT NOT NULL,
    written_at     TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS claims_claim_id ON claims(claim_id);

-- Every access logged with the principal (architecture p.12).
CREATE TABLE IF NOT EXISTS audit_log (
    seq         INTEGER PRIMARY KEY AUTOINCREMENT,
    at          TEXT NOT NULL,
    principal   TEXT NOT NULL,
    action      TEXT NOT NULL,
    subject     TEXT,
    detail      TEXT
);

CREATE TRIGGER IF NOT EXISTS claims_no_delete BEFORE DELETE ON claims BEGIN
    SELECT RAISE(ABORT, 'ledger is append-only: a correction is a new row with supersedes');
END;

CREATE TRIGGER IF NOT EXISTS claims_no_edit
BEFORE UPDATE OF {", ".join(["seq", *_LOCKED_COLUMNS, "principal", "written_at"])} ON claims BEGIN
    SELECT RAISE(ABORT, 'ledger is append-only: only the audit field is updated in place');
END;

CREATE TRIGGER IF NOT EXISTS register_no_delete BEFORE DELETE ON register BEGIN
    SELECT RAISE(ABORT, 'the Source Register is write-once');
END;

CREATE TRIGGER IF NOT EXISTS register_no_edit
BEFORE UPDATE ON register BEGIN
    SELECT RAISE(ABORT, 'the Source Register is write-once: re-run intake into a new ledger');
END;

CREATE TRIGGER IF NOT EXISTS log_no_delete BEFORE DELETE ON audit_log BEGIN
    SELECT RAISE(ABORT, 'the audit log is append-only');
END;

CREATE TRIGGER IF NOT EXISTS log_no_edit BEFORE UPDATE ON audit_log BEGIN
    SELECT RAISE(ABORT, 'the audit log is append-only');
END;
"""


class Ledger:
    """A job's ledger file. Open it through the broker for writes."""

    def __init__(self, path: Path | str, *, create: bool = False):
        self.path = Path(path)
        existed = self.path.exists()
        if not existed and not create:
            raise FileNotFoundError(f"no ledger at {self.path}")
        if create:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        if create:
            self.db.executescript(DDL)
            self.db.commit()

    # ---- meta -------------------------------------------------------------

    def set_meta(self, **kw) -> None:
        for k, v in kw.items():
            self.db.execute(
                "INSERT INTO meta(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (k, "" if v is None else str(v)),
            )
        self.db.commit()

    def meta(self, key: str, default: str = "") -> str:
        row = self.db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    # ---- reads ------------------------------------------------------------

    def register(self) -> dict[str, dict]:
        return {r["source_id"]: dict(r) for r in self.db.execute("SELECT * FROM register ORDER BY rowid")}

    def claims(self) -> list[Claim]:
        out = []
        for r in self.db.execute("SELECT * FROM claims ORDER BY seq"):
            d = {k: r[k] for k in CLAIM_FIELDS}
            d["value_num"] = r["value_num"]
            out.append(Claim(**d))
        return out

    def by_id(self) -> dict[str, Claim]:
        return {c.claim_id: c for c in self.claims()}

    def superseded(self) -> set[str]:
        """Claim IDs replaced by a later correction row."""
        known = {c.claim_id for c in self.claims()}
        return {
            r["supersedes"]
            for r in self.db.execute("SELECT supersedes FROM claims WHERE supersedes != ''")
            if r["supersedes"] in known
        }

    def log(self) -> list[dict]:
        return [dict(r) for r in self.db.execute("SELECT * FROM audit_log ORDER BY seq")]

    # ---- exports ----------------------------------------------------------

    def ledger_csv(self) -> str:
        """The flat ledger in the architecture doc's field order."""
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=LEDGER_FIELDS, lineterminator="\n")
        w.writeheader()
        for c in self.claims():
            w.writerow(c.as_ledger_row())
        return buf.getvalue()

    def register_csv(self) -> str:
        cols = ["source_id", "title", "file", "sha256", "pages", "kind", "status", "notes"]
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=cols, lineterminator="\n")
        w.writeheader()
        for s in self.register().values():
            w.writerow({k: s.get(k) or "" for k in cols})
        return buf.getvalue()

    def audit_json(self) -> str:
        return json.dumps(
            {
                "job": self.meta("job"),
                "run_id": self.meta("run_id"),
                "schema_version": self.meta("schema_version"),
                "claims": len(self.claims()),
                "log": self.log(),
            },
            indent=2,
        ) + "\n"

    def close(self) -> None:
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
