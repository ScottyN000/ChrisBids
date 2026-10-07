"""Bid pipeline, Phase 1: the claim ledger, Intake and the Auditor.

Phase 1 of the build plan in the architecture doc (p.16): the ledger as a
SQLite store per job written only through a broker that enforces the access
matrix, Intake building the Source Register with hashes and page images, and
the Auditor opening a cited page and confirming a value.

Nothing here calls a model. Everything in Phase 1 is the code half of
"models extract; code computes" (architecture p.9): hashing, page splitting,
schema checks, arithmetic replay, link liveness and orphan detection. The
readers that fill the ledger arrive in Phase 2 and write through this broker.
"""

__all__ = ["schema", "roles", "ledger", "broker", "intake", "auditor"]
