"""tools/changed_mutants.py: which mutants a PR runs."""
import tempfile
import unittest
from pathlib import Path

from tools import changed_mutants


class ChangedMutantsCase(unittest.TestCase):
    def test_only_changed_pipeline_modules_with_a_function(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "pyproject.toml").write_text('[tool.mutmut]\ndo_not_mutate = ["pipeline/cli.py"]\n')
            (root / "pipeline" / "readers").mkdir(parents=True)
            for f, text in {"pipeline/web.py": "def f():\n    return 1\n", "pipeline/readers/live.py": "class A:\n    def g(self): pass\n",
                            "pipeline/cli.py": "def main(): pass\n", "pipeline/__init__.py": "",
                            "pipeline/consts.py": "X = 1\n"}.items():
                (root / f).write_text(text)
            changed = ["pipeline/web.py", "pipeline/readers/live.py", "pipeline/readers/live.py", "pipeline/cli.py",
                       "pipeline/__init__.py", "pipeline/consts.py", "pipeline/gone.py", "pipeline/web_sources.yaml",
                       "tests/test_web.py", "tools/x.py"]
            self.assertEqual(changed_mutants.patterns(changed, root), ["pipeline.readers.live.*", "pipeline.web.*"])
            self.assertEqual(changed_mutants.patterns(["README.md"], root), [])


if __name__ == "__main__":
    unittest.main()
