"""Fail CI when the mutation score drops below the floor.

Reads mutants/mutmut-cicd-stats.json (written by `mutmut export-cicd-stats`
after `mutmut run`). The score is killed / (killed + survived + no tests):
a mutant no test even runs counts against it, the same as one that survives.
Timeouts count as killed, since the mutant broke the code badly enough to hang.

Mutant names shift whenever code moves, so the gate is a score rather than a
list. Raise FLOOR as survivors are killed; never lower it to get a PR green.

    python3 tools/mutation_gate.py            # print the score, fail below FLOOR
    python3 tools/mutation_gate.py --floor 80
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

FLOOR = 0.0  # percent; set from the first full run


def score(stats: dict) -> float:
    killed = stats["killed"] + stats["timeout"]
    counted = killed + stats["survived"] + stats["no_tests"]
    return 100.0 * killed / counted if counted else 0.0  # nothing measured is not a pass


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--stats", type=Path, default=Path("mutants/mutmut-cicd-stats.json"))
    ap.add_argument("--floor", type=float, default=FLOOR)
    a = ap.parse_args(argv)
    if not a.stats.exists():
        print(f"{a.stats} not found; run `mutmut run` and `mutmut export-cicd-stats` first", file=sys.stderr)
        return 2
    stats = json.loads(a.stats.read_text())
    s = score(stats)
    print(f"mutation score {s:.1f}% (floor {a.floor:.1f}%): {stats['killed']} killed, {stats['timeout']} timed out, "
          f"{stats['survived']} survived, {stats['no_tests']} with no test, of {stats['total']}")
    if s < a.floor:
        print("FAIL: more mutants survive than the floor allows; see `mutmut results`", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
