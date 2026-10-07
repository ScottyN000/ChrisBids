"""Phase 2 readers: turn one unit of a source (a drawing tile, a spec page, a
photo, a message) into ledger rows (architecture p.3-4, p.13-14, build plan p.16).

A reader is a prompt, an output schema and a mapping from its output to ledger
rows. Everything around the model call is code, and it lives here:

* `schemas`   the strict JSON each reader must return, with enumerated fields
* `validate`  a small JSON Schema checker, so "the schema or nothing" holds
* `units`     the feet-and-inch arithmetic a model is never asked to do
* `vote`      2-3 runs per unit compared field by field; disagreement is kept
* `rows`      reader output turned into Claims with a method fixed by rule
* `clients`   the model interface, and a replay client for recorded output
* `run`       one reader over a list of units, writing through the broker
* `compare`   a produced ledger against a golden fixture

Readers get no network and no free-text channel (architecture p.10): their only
output is schema-valid JSON, and the broker only accepts the methods their
principal may write.
"""
