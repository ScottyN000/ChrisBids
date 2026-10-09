"""tools/changed_mutants.py: which mutants a PR runs."""
import tempfile
import unittest
from pathlib import Path

from tools import changed_mutants


def make_root(d: str) -> Path:
    root = Path(d)
    (root / "pyproject.toml").write_text('[tool.mutmut]\ndo_not_mutate = ["pipeline/cli*.py"]\n')
    (root / "pipeline" / "readers").mkdir(parents=True)
    (root / "tests").mkdir()
    for f, text in {"pipeline/web.py": "def f():\n    return 1\n", "pipeline/readers/live.py": "class A:\n    def g(self): pass\n",
                    "pipeline/readers/__init__.py": "", "pipeline/ledger.py": "def h(): pass\n",
                    "pipeline/cli.py": "def main(): pass\n", "pipeline/__init__.py": "",
                    "pipeline/consts.py": "X = 1\n"}.items():
        (root / f).write_text(text)
    return root


class ChangedMutantsCase(unittest.TestCase):
    def test_only_changed_pipeline_modules_with_a_function(self):
        with tempfile.TemporaryDirectory() as d:
            root = make_root(d)
            changed = ["pipeline/web.py", "pipeline/readers/live.py", "pipeline/readers/live.py", "pipeline/cli.py",
                       "pipeline/__init__.py", "pipeline/consts.py", "pipeline/gone.py", "pipeline/web_sources.yaml",
                       "tools/x.py"]
            self.assertEqual(changed_mutants.patterns(changed, root), ["pipeline.readers.live.*", "pipeline.web.*"])
            self.assertEqual(changed_mutants.patterns(["README.md"], root), [])

    def test_do_not_mutate_entries_are_glob_patterns(self):
        with tempfile.TemporaryDirectory() as d:
            root = make_root(d)
            self.assertEqual(changed_mutants.patterns(["pipeline/cli.py"], root), [])

    def test_a_changed_test_stands_for_the_modules_it_imports(self):
        with tempfile.TemporaryDirectory() as d:
            root = make_root(d)
            (root / "tests" / "test_web.py").write_text(
                "import os\nimport pipeline.ledger\nfrom pipeline import web, consts\nfrom pipeline.readers import live\n"
                "from pipeline.cli import main\nfrom tests.helpers import x\n")
            self.assertEqual(changed_mutants.patterns(["tests/test_web.py"], root),
                             ["pipeline.ledger.*", "pipeline.readers.live.*", "pipeline.web.*"])
            self.assertEqual(changed_mutants.patterns(["tests/test_gone.py", "tests/data.json"], root), [])

    def test_nothing_is_printed_when_there_is_no_pattern(self):
        self.assertEqual(changed_mutants.render([]), "")
        self.assertEqual(changed_mutants.render(["pipeline.web.*"]), "pipeline.web.*\n")


if __name__ == "__main__":
    unittest.main()
