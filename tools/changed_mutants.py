"""Name the mutants a pull request needs: those in the pipeline functions it changes.

Per-PR mutation testing runs locally, in the session that builds the PR, not
in Actions (Scott, 2026-10-10: the CI runs took too long and cost Actions
minutes); the weekly run on main (.github/workflows/mutation.yml) still runs
every mutant. mutmut takes mutant name patterns (`pipeline.coats.x_coat_count__mutmut_*`),
so a PR that edits two functions runs only their mutants. Given the lines a PR
changes, a module is narrowed to the top-level functions and methods those
lines fall in; a change to module- or class-level code (a constant, a regex
a function reads) picks the whole module, since mutmut does not mutate it but
every function reading it may now behave differently. Comments, blank lines,
docstrings and imports outside functions pick nothing. Without line numbers a
changed module is picked whole. A module with no function in it has no mutants
and is left out, because mutmut 3.8 stops with "Filtered for specific mutants,
but nothing matches" when no pattern matches a mutant. A changed test file
stands for the pipeline modules it imports only in a PR that changes no
pipeline code, and only when the PR deletes lines from it (a test only added
to cannot lower a score); it is read from the base when the PR deletes the
file, and every test file that imports it counts too. A weakened test in a PR
that also changes pipeline code is left to the weekly run. A PR that
touches the mutation tooling itself runs every module, since the picker cannot
vouch for its own change.

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


def imports(source: str, package: str, relative_to: str = "") -> list[str]:
    """The modules of `package` a file's source imports, as repository paths.

    A relative import (`from .test_broker import x`) is resolved against `relative_to`.
    """
    out = []
    for node in ast.walk(ast.parse(source)):
        names = []
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level and relative_to:
                module = ".".join(x for x in (relative_to, module) if x)
            if module.split(".")[0] == package:
                names.append(module)
                names.extend(f"{module}.{a.name}" for a in node.names)   # `from pipeline import web`
        elif isinstance(node, ast.Import):
            names.extend(a.name for a in node.names if a.name.split(".")[0] == package)
        out.extend(n.replace(".", "/") + ".py" for n in names)
    return out


def with_dependents(tests: set[str], root: Path) -> set[str]:
    """The changed test files plus every test file that imports one of them, and so on.

    A changed helper (`tests/webfake.py`) or a test another test imports then
    stands for the pipeline modules those tests cover.
    """
    out = set(tests)
    while True:
        more = set()
        for path in (root / "tests").glob("*.py"):
            f = str(path.relative_to(root))
            if f not in out and set(imports(path.read_text(), "tests", "tests")) & out:
                more.add(f)
        if not more:
            return out
        out |= more


def functions(source: str, lines: set[int]) -> list[str] | None:
    """The mutmut name stems of the top-level functions and methods holding `lines`
    (`x_f`, `xǁAǁg`), in source order; None when a line falls in module- or
    class-level code, which picks the whole module."""
    tree = ast.parse(source)
    found = []

    def span(n: ast.AST) -> range:
        first = min([n.lineno] + [d.lineno for d in getattr(n, "decorator_list", [])])
        return range(first, n.end_lineno + 1)

    for node in tree.body:
        here = lines & set(span(node))
        if not here:
            continue
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            found.append(f"x_{node.name}")
        elif isinstance(node, ast.ClassDef):
            for item in node.body:
                inside = lines & set(span(item))
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if inside:
                        found.append(f"x\u01c1{node.name}\u01c1{item.name}")
                elif inside and not (isinstance(item, ast.Expr) and isinstance(item.value, ast.Constant)):
                    return None   # a class attribute every method may read
            if lines & set(range(node.lineno, node.body[0].lineno)):
                return None   # the class line itself (its bases)
        elif isinstance(node, (ast.Import, ast.ImportFrom)) or (
                isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)):
            continue   # an import or a docstring holds no mutant and changes no function's mutants
        else:
            return None   # a module-level constant the functions read
    return found


def patterns(changed: list[str], root: Path = ROOT, at_base=lambda f: None,
             lines: dict[str, set[int]] | None = None, weakened: set[str] | None = None) -> list[str]:
    """at_base(f) gives a file's text at the base commit when the PR deleted it, else None.
    `lines` gives the head line numbers a PR changes in each file (a file it leaves
    out is picked whole); `weakened` the test files it deletes lines from (None: every
    changed test)."""
    cfg = tomllib.loads((root / "pyproject.toml").read_text())["tool"]["mutmut"]
    skip = cfg.get("do_not_mutate", [])
    files = set(changed)
    whole = set()
    if files & set(TOOLING):
        whole.update(str(p.relative_to(root)) for p in (root / "pipeline").rglob("*.py"))
    # a weakened test widens the pick only in a PR that changes no pipeline code; otherwise the weekly run covers it
    code = any(f.startswith("pipeline/") and f.endswith(".py") for f in changed)
    tests = {f for f in changed if f.startswith("tests/") and f.endswith(".py")
             and (weakened is None or (f in weakened and not code))}
    for f in with_dependents(tests, root) if (root / "tests").is_dir() else tests:
        source = (root / f).read_text() if (root / f).exists() else at_base(f)
        if source is not None:
            whole.update(imports(source, "pipeline"))
    out = []
    for f in sorted(files | whole):
        path = root / f
        if (not f.startswith("pipeline/") or not f.endswith(".py") or any(fnmatch.fnmatch(f, p) for p in skip)
                or not path.exists() or not re.search(r"^\s*def ", path.read_text(), re.M)):
            continue
        module = f[:-3].replace("/", ".")
        picked = None if f in whole or lines is None or f not in lines else functions(path.read_text(), lines[f])
        if picked is None:
            out.append(module + ".*")
        else:
            out += [f"{module}.{stem}__mutmut_*" for stem in picked]
    return out


def changed_lines(diff: str) -> dict[str, set[int]]:
    """The head line numbers each file's `git diff -U0` hunks touch; a pure deletion
    marks the lines on either side of where it was."""
    out: dict[str, set[int]] = {}
    current = None
    for line in diff.splitlines():
        if line.startswith("+++ "):
            current = line[6:] if line.startswith("+++ b/") else None
            if current:
                out.setdefault(current, set())
        elif line.startswith("@@") and current:
            m = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", line)
            start, count = int(m.group(1)), int(m.group(2) or 1)
            out[current].update(range(start, start + count) if count else (start, start + 1))
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

    def git(*args: str) -> str:
        return subprocess.run(["git", *args, f"{base}...HEAD"], cwd=ROOT, capture_output=True, text=True,
                              check=True).stdout

    lines = changed_lines(git("diff", "-U0"))
    weakened = {row.split("\t")[2] for row in git("diff", "--numstat").splitlines()
                if row.split("\t")[1] not in ("0", "-")}
    sys.stdout.write(render(patterns(diff, at_base=at_base, lines=lines, weakened=weakened)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
