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

    def test_a_deleted_test_is_read_from_the_base(self):
        with tempfile.TemporaryDirectory() as d:
            root = make_root(d)
            base = {"tests/test_gone.py": "from pipeline import ledger\n"}
            self.assertEqual(changed_mutants.patterns(["tests/test_gone.py"], root, at_base=base.get), ["pipeline.ledger.*"])
            self.assertEqual(changed_mutants.patterns(["tests/test_never.py"], root, at_base=base.get), [])

    def test_a_change_to_the_mutation_tooling_runs_every_module(self):
        with tempfile.TemporaryDirectory() as d:
            root = make_root(d)
            for f in changed_mutants.TOOLING:
                with self.subTest(f):
                    self.assertEqual(changed_mutants.patterns([f], root),
                                     ["pipeline.ledger.*", "pipeline.readers.live.*", "pipeline.web.*"])

    def test_a_changed_helper_stands_for_the_tests_that_import_it(self):
        with tempfile.TemporaryDirectory() as d:
            root = make_root(d)
            (root / "tests" / "webfake.py").write_text("import json\n")
            (root / "tests" / "test_web.py").write_text("from tests.webfake import Sites\nfrom pipeline import web\n")
            (root / "tests" / "test_broker.py").write_text("from pipeline import ledger\n")
            (root / "tests" / "test_security.py").write_text("from .test_broker import make\nfrom pipeline.readers import live\n")
            (root / "tests" / "test_other.py").write_text("from pipeline import consts\n")
            self.assertEqual(changed_mutants.patterns(["tests/webfake.py"], root), ["pipeline.web.*"])
            self.assertEqual(changed_mutants.patterns(["tests/test_broker.py"], root),
                             ["pipeline.ledger.*", "pipeline.readers.live.*"])

    def test_changed_lines_narrow_a_module_to_its_functions(self):
        src = ('"""Doc."""\nimport re\n\nX = 1\n\n\ndef f():\n    return X\n\n\n@dec\ndef g():\n    return 2\n\n\n'
               'class A:\n    """Doc."""\n    def m(self):\n        return 3\n    Y = 4\n')
        self.assertEqual(changed_mutants.functions(src, {7, 8}), ["x_f"])
        self.assertEqual(changed_mutants.functions(src, {11, 12}), [])          # mutmut makes no mutant of a decorated g
        self.assertEqual(changed_mutants.functions(src, {7, 18, 19}), ["x_f", "x\u01c1A\u01c1m"])
        self.assertEqual(changed_mutants.functions(src, {1, 2, 5, 17}), [])     # docstrings, an import, a blank line
        self.assertIsNone(changed_mutants.functions(src, {4}))                  # a constant a function reads
        self.assertIsNone(changed_mutants.functions(src, {20}))                 # a class attribute
        self.assertIsNone(changed_mutants.functions(src, {16}))                 # the class line (its bases)
        dataclass = "@dataclass(frozen=True)\nclass B:\n    x: int = 0\n\n    def m(self):\n        return self.x\n"
        self.assertIsNone(changed_mutants.functions(dataclass, {1}))           # a class decorator changes every method
        stubs = ("def p():\n    pass\n\n\ndef d():\n    \"\"\"Doc.\"\"\"\n\n\nclass C:\n    @staticmethod\n    def s():\n"
                 "        return 1\n\n    @property\n    def q(self):\n        return 2\n")
        self.assertEqual(changed_mutants.functions(stubs, {2, 6, 12, 16}), ["x\u01c1C\u01c1s"])
        with tempfile.TemporaryDirectory() as d:
            root = make_root(d)
            (root / "pipeline" / "web.py").write_text("X = 1\n\n\ndef f():\n    return X\n\n\ndef h():\n    return 2\n")
            self.assertEqual(changed_mutants.patterns(["pipeline/web.py"], root, lines={"pipeline/web.py": {9}}),
                             ["pipeline.web.x_h__mutmut_*"])
            self.assertEqual(changed_mutants.patterns(["pipeline/web.py"], root, lines={"pipeline/web.py": {1}}),
                             ["pipeline.web.*"])
            self.assertEqual(changed_mutants.patterns(["pipeline/web.py", "pipeline/ledger.py"], root,
                                                      lines={"pipeline/web.py": {9}}),
                             ["pipeline.ledger.*", "pipeline.web.x_h__mutmut_*"])   # no line numbers: the whole module
            # a tooling change still picks every module whole
            self.assertEqual(changed_mutants.patterns(["pipeline/web.py", "pyproject.toml"], root,
                                                      lines={"pipeline/web.py": {9}}),
                             ["pipeline.ledger.*", "pipeline.readers.live.*", "pipeline.web.*"])

    def test_a_deletion_is_read_in_the_base(self):
        base = "X = 1\n\n\ndef f():\n    return X\n\n\nclass A:\n    def m(self):\n        return 1\n\n    def n(self):\n        return 2\n"
        self.assertIsNone(changed_mutants.functions(base, {12, 13}, removed=True))      # a whole method deleted
        self.assertIsNone(changed_mutants.functions(base, {4}, removed=True))           # f renamed
        self.assertIsNone(changed_mutants.functions(base, {1}, removed=True))           # a constant deleted
        self.assertEqual(changed_mutants.functions(base, {5}, removed=True), ["x_f"])   # a line inside f
        self.assertEqual(changed_mutants.functions(base, {4}), ["x_f"])                 # on the head side, a def line is f's
        with tempfile.TemporaryDirectory() as d:
            root = make_root(d)
            head = "X = 1\n\n\ndef f():\n    return X\n\n\nclass A:\n    def m(self):\n        return 1\n"
            (root / "pipeline" / "web.py").write_text(head)
            at_base = {"pipeline/web.py": base}.get
            # deleting n leaves only blank head lines either side; the base shows a method went
            self.assertEqual(changed_mutants.patterns(["pipeline/web.py"], root, at_base, lines={"pipeline/web.py": {10, 11}},
                                                      removed={"pipeline/web.py": {11, 12, 13}}), ["pipeline.web.*"])
            self.assertEqual(changed_mutants.patterns(["pipeline/web.py"], root, at_base, lines={"pipeline/web.py": {5}},
                                                      removed={"pipeline/web.py": {5}}), ["pipeline.web.x_f__mutmut_*"])

    def test_a_weakened_test_widens_the_pick(self):
        with tempfile.TemporaryDirectory() as d:
            root = make_root(d)
            (root / "tests" / "test_web.py").write_text("from pipeline import web, ledger\n")
            self.assertEqual(changed_mutants.patterns(["tests/test_web.py"], root, weakened={"tests/test_web.py"}),
                             ["pipeline.ledger.*", "pipeline.web.*"])
            self.assertEqual(changed_mutants.patterns(["tests/test_web.py"], root, weakened=set()), [])   # only added to
            self.assertEqual(changed_mutants.patterns(["tests/test_web.py", "pipeline/web.py"], root,
                                                      lines={"pipeline/web.py": {1}}, weakened={"tests/test_web.py"}),
                             ["pipeline.ledger.*", "pipeline.web.*"])   # code changes too: still widened

    def test_changed_lines_from_a_zero_context_diff(self):
        diff = ("diff --git a/pipeline/web.py b/pipeline/web.py\n--- a/pipeline/web.py\n+++ b/pipeline/web.py\n"
                "@@ -3,2 +3,3 @@ def f():\n-a\n+b\n@@ -10 +11 @@\n-c\n+d\n@@ -20,2 +21,0 @@\n-e\n-f\n"
                "--- a/gone.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-x\n")
        self.assertEqual(changed_mutants.changed_lines(diff), {"pipeline/web.py": {3, 4, 5, 11, 21, 22}})
        self.assertEqual(changed_mutants.changed_lines(diff, "-"), {"pipeline/web.py": {3, 4, 10, 20, 21}})

    def test_nothing_is_printed_when_there_is_no_pattern(self):
        self.assertEqual(changed_mutants.render([]), "")
        self.assertEqual(changed_mutants.render(["pipeline.web.*"]), "pipeline.web.*\n")


if __name__ == "__main__":
    unittest.main()
