"""Name the mutants a pull request needs: those in the pipeline modules it changes.

mutmut takes mutant name patterns (`pipeline.webread.*`), so a PR that touches
two modules runs only their mutants instead of all ~9,400. Each mutant is
tested exactly as in a full run, so a module's score is the same number. A
module with no function in it has no mutants and is left out (mutmut refuses a
pattern list that matches nothing). A changed test file stands for the pipeline
modules it imports, read from the base when the PR deletes it, so a PR that
only weakens or removes a test is still measured against the code that test
covers. A PR that touches the mutation tooling itself runs every module, since
the picker cannot vouch for its own change.

    python3 tools/changed_mutants.py origin/main      # one pattern per line; none means skip
"""
from __future__ import annotations

import ast
import fnmatch
import re
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOLING = ("pyproject.toml", "tools/mutation_gate.py", "tools/changed_mutants.py", ".github/workflows/mutation.yml")


def imported_modules(source: str) -> list[str]:
    """The pipeline modules a test file's source imports, as repository paths."""
    out = []
    for node in ast.walk(ast.parse(source)):
        names = []
        if isinstance(node, ast.ImportFrom) and node.module and node.module.split(".")[0] == "pipeline":
            names.append(node.module)
            names.extend(f"{node.module}.{a.name}" for a in node.names)   # `from pipeline import web`
        elif isinstance(node, ast.Import):
            names.extend(a.name for a in node.names if a.name.split(".")[0] == "pipeline")
        out.extend(n.replace(".", "/") + ".py" for n in names)
    return out


def patterns(changed: list[str], root: Path = ROOT, at_base=lambda f: None) -> list[str]:
    """at_base(f) gives a file's text at the base commit when the PR deleted it, else None."""
    cfg = tomllib.loads((root / "pyproject.toml").read_text())["tool"]["mutmut"]
    skip = cfg.get("do_not_mutate", [])
    files = set(changed)
    if files & set(TOOLING):
        files.update(str(p.relative_to(root)) for p in (root / "pipeline").rglob("*.py"))
    for f in changed:
        if f.startswith("tests/") and f.endswith(".py"):
            source = (root / f).read_text() if (root / f).exists() else at_base(f)
            if source is not None:
                files.update(imported_modules(source))
    out = []
    for f in sorted(files):
        path = root / f
        if (not f.startswith("pipeline/") or not f.endswith(".py") or any(fnmatch.fnmatch(f, p) for p in skip)
                or not path.exists() or not re.search(r"^\s*def ", path.read_text(), re.M)):
            continue
        out.append(f[:-3].replace("/", ".") + ".*")
    return out


def render(found: list[str]) -> str:
    """One pattern per line; nothing at all when there is none, so a size check means something."""
    return "".join(p + "\n" for p in found)


def main(argv: list[str]) -> int:
    base = argv[1] if len(argv) > 1 else "origin/main"
    diff = subprocess.run(["git", "diff", "--name-only", f"{base}...HEAD"], cwd=ROOT,
                          capture_output=True, text=True, check=True).stdout.split()

    def at_base(f: str) -> str | None:
        shown = subprocess.run(["git", "show", f"{base}:{f}"], cwd=ROOT, capture_output=True, text=True)
        return shown.stdout if shown.returncode == 0 else None

    sys.stdout.write(render(patterns(diff, at_base=at_base)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
