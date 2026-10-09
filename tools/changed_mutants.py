"""Name the mutants a pull request needs: those in the pipeline modules it changes.

mutmut takes mutant name patterns (`pipeline.webread.*`), so a PR that touches
two modules runs only their mutants instead of all ~9,400. The tests still run
in full against each mutant, so a module's score is measured the same way as in
a full run. A module with no function in it has no mutants and is left out
(mutmut refuses a pattern list that matches nothing).

    python3 tools/changed_mutants.py origin/main      # one pattern per line; none means skip
"""
from __future__ import annotations

import re
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def patterns(changed: list[str], root: Path = ROOT) -> list[str]:
    cfg = tomllib.loads((root / "pyproject.toml").read_text())["tool"]["mutmut"]
    skip = set(cfg.get("do_not_mutate", []))
    out = []
    for f in sorted(set(changed)):
        path = root / f
        if (not f.startswith("pipeline/") or not f.endswith(".py") or f in skip or not path.exists()
                or not re.search(r"^\s*def ", path.read_text(), re.M)):
            continue
        out.append(f[:-3].replace("/", ".") + ".*")
    return out


def main(argv: list[str]) -> int:
    base = argv[1] if len(argv) > 1 else "origin/main"
    diff = subprocess.run(["git", "diff", "--name-only", f"{base}...HEAD"], cwd=ROOT,
                          capture_output=True, text=True, check=True).stdout.split()
    print("\n".join(patterns(diff)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
