"""Command line for Phase 1.

    python3 -m pipeline intake  <packet-dir> --job NAN --out runs/nan [--images]
    python3 -m pipeline load    fixtures/nantucket --out runs/nan-fixture
    python3 -m pipeline export  runs/nan-fixture/ledger.db [--out ledger.csv]
    python3 -m pipeline audit   runs/nan-fixture/ledger.db [--packet fixtures/packet]
                                [--proposal fixtures/nantucket/proposal.md] [--links]
    python3 -m pipeline verify-fixtures
    python3 -m pipeline replay  fixtures/nantucket --out runs/nan-replay [--repeats 3]
    python3 -m pipeline live    fixtures/nantucket --out runs/nan-live [--packet fixtures/packet]
                                [--model claude-haiku-5-5] [--effort low]   (needs ANTHROPIC_API_KEY)
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import auditor, fixtures, intake
from .broker import Broker
from .ledger import Ledger
from .schema import LedgerError

ROOT = Path(__file__).resolve().parent.parent


def cmd_intake(a) -> int:
    out = Path(a.out)
    broker = Broker.open_job(out / "ledger.db", "intake", job=a.job, run_id=a.run_id or f"intake-{a.job}", create=True)
    sources = intake.run(Path(a.packet), broker, images=a.images, image_root=out / "pages")
    (out / "register.csv").write_text(broker.ledger.register_csv())
    kinds: dict[str, int] = {}
    for s in sources:
        kinds[s.kind] = kinds.get(s.kind, 0) + 1
    dups = sum(1 for s in sources if s.status == "duplicate")
    print(f"OK {out/'ledger.db'}: {len(sources)} sources, {dups} duplicates collapsed")
    print("   " + ", ".join(f"{k} {v}" for k, v in sorted(kinds.items())))
    if a.images:
        print(f"   page images under {out/'pages'}")
    return 0


def cmd_load(a) -> int:
    out = Path(a.out)
    db = out / "ledger.db"
    if db.exists():
        db.unlink()
    try:
        broker, _data = fixtures.load(Path(a.fixture), db)
    except (fixtures.FixtureError, LedgerError) as e:
        print(f"FAIL {a.fixture}\n{e}", file=sys.stderr)
        return 1
    csv_text = broker.ledger.ledger_csv()
    (out / "ledger.csv").write_text(csv_text)
    committed = Path(a.fixture) / "ledger.csv"
    same = committed.exists() and committed.read_text() == csv_text
    print(f"OK {db}: {len(broker.ledger.claims())} rows from {a.fixture}")
    print(f"   export {'matches' if same else 'DIFFERS FROM'} {committed}")
    return 0 if same or not committed.exists() else 1


def cmd_export(a) -> int:
    with Ledger(a.db) as led:
        text = led.ledger_csv()
    if a.out:
        Path(a.out).write_text(text)
        print(f"OK wrote {a.out}")
    else:
        sys.stdout.write(text)
    return 0


def cmd_audit(a) -> int:
    broker = Broker(Ledger(a.db), "auditor")
    report = auditor.run(
        broker,
        packet=Path(a.packet) if a.packet else None,
        proposal=Path(a.proposal) if a.proposal else None,
        phrase_library=fixtures.PHRASE_LIBRARY if a.proposal else None,
        check_links=a.links,
    )
    print(report.text())
    return 0 if report.ok else 1


def cmd_verify_fixtures(a) -> int:
    """The golden test: both fixtures load through the broker and export byte-identically."""
    rc = 0
    for job in ("nantucket", "ocean-beach"):
        fixture = ROOT / "fixtures" / job
        db = Path(a.out or (ROOT / "runs" / "verify")) / job / "ledger.db"
        if db.exists():
            db.unlink()
        try:
            broker, _ = fixtures.load(fixture, db)
        except (fixtures.FixtureError, LedgerError) as e:
            print(f"FAIL {job}: {e}", file=sys.stderr)
            rc = 1
            continue
        got, want = broker.ledger.ledger_csv(), (fixture / "ledger.csv").read_text()
        if got == want:
            print(f"OK   {job}: {len(broker.ledger.claims())} rows reproduce {fixture.name}/ledger.csv")
        else:
            print(f"FAIL {job}: export differs from {fixture.name}/ledger.csv", file=sys.stderr)
            rc = 1
        broker.close()
    return rc


def cmd_replay(a) -> int:
    """Run the readers on a fixture's recorded responses and compare with its ledger."""
    from .readers import golden
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    broker, results, comparison = golden.replay(Path(a.fixture), out / "ledger.db", repeats=a.repeats)
    for res in results.values():
        print(res.text())
    print(comparison.text())
    (out / "ledger.csv").write_text(broker.ledger.ledger_csv())
    broker.close()
    failed = any(r.refused or r.unread for r in results.values())
    return 0 if comparison.exact_ok and not failed else 1


def cmd_live(a) -> int:
    """Run the readers live on a fixture job's source packet and compare with its ledger."""
    from .readers import golden, live
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    try:
        client = live.LiveClient(model=a.model, effort=a.effort or None, record=out / "recordings")
        units = live.units_for(Path(a.fixture), Path(a.packet), out)
        broker, results, comparison = golden.replay(Path(a.fixture), out / "ledger.db", client,
                                                    repeats=a.repeats, units=units)
    except live.LiveRunError as e:
        print(f"NOT RUN: {e}", file=sys.stderr)
        return 3
    for res in results.values():
        print(res.text())
    print(comparison.text())
    if (out / "recordings" / "calls.jsonl").exists():
        print(live.usage_totals(out / "recordings" / "calls.jsonl"))
    (out / "ledger.csv").write_text(broker.ledger.ledger_csv())
    (out / "comparison.txt").write_text(comparison.text() + "\n")
    broker.close()
    failed = any(r.refused or r.unread for r in results.values())
    return 0 if comparison.exact_ok and not failed else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="pipeline", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("intake", help="build the Source Register from a packet folder")
    p.add_argument("packet")
    p.add_argument("--job", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--run-id", default="")
    p.add_argument("--images", action="store_true", help="render page images with pdftoppm")
    p.set_defaults(func=cmd_intake)

    p = sub.add_parser("load", help="load a golden fixture into a ledger through the broker")
    p.add_argument("fixture")
    p.add_argument("--out", required=True)
    p.set_defaults(func=cmd_load)

    p = sub.add_parser("export", help="write ledger.csv from a ledger file")
    p.add_argument("db")
    p.add_argument("--out", default="")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("audit", help="audit a ledger and list orphan figures")
    p.add_argument("db")
    p.add_argument("--packet", default="", help="packet root, to re-hash sources and open cited pages")
    p.add_argument("--proposal", default="", help="proposal to trace figures in")
    p.add_argument("--links", action="store_true", help="check fetched URLs for liveness (needs egress)")
    p.set_defaults(func=cmd_audit)

    p = sub.add_parser("verify-fixtures", help="golden test: both fixtures load and export byte-identically")
    p.add_argument("--out", default="")
    p.set_defaults(func=cmd_verify_fixtures)

    p = sub.add_parser("replay", help="run the readers on recorded responses and compare with the fixture")
    p.add_argument("fixture")
    p.add_argument("--out", required=True)
    p.add_argument("--repeats", type=int, default=3)
    p.set_defaults(func=cmd_replay)

    p = sub.add_parser("live", help="run the readers live on Haiku against a fixture's packet and compare")
    p.add_argument("fixture")
    p.add_argument("--packet", default="fixtures/packet", help="packet root the register's paths are relative to")
    p.add_argument("--out", required=True)
    p.add_argument("--repeats", type=int, default=3)
    p.add_argument("--model", default="claude-haiku-5-5")
    p.add_argument("--effort", default="low", choices=["low", "medium", "high", ""],
                   help="empty for the model default")
    p.set_defaults(func=cmd_live)

    a = ap.parse_args(argv)
    try:
        return a.func(a)
    except LedgerError as e:
        print(f"REFUSED: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
