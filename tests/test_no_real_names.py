"""The repository is public: no real person, client contact or the contractor's own name may come back.

The names are stored only as hash prefixes, so this file does not republish them. Stand-ins
("Contractor Co.", "the estimator", "SW Rep", 555 numbers) are used everywhere instead.

Limits: a word matches only on a prefix of 6 to 12 letters, so a name of 5 letters or fewer is
not guarded. Photos are not read: their EXIF carries no artist, copyright or description text
(checked 2026-10-09), and the guard would need an image library to keep that true.
"""
import hashlib
import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parent.parent
# sha256 prefixes of the lowercase word (or of its first N letters) and of 10-digit phone numbers
WORDS = {"8e552b7f99a0c760", "d4d6f82a278b65e2", "e2a3927dcf03f69a", "9b7ea83f484fd62f", "31f6410360d73372",
         "0551cc564af1d114", "acde34b1375da100", "4d0bedec0782c846"}
PHONES = {"380096d12d00192a", "9af6688fb882aa4f", "bce2b9218ec8ca50", "72c4ffaf2a83ac64"}


def _h(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()[:16]


def hits(text: str) -> list[str]:
    found = []
    for w in re.findall(r"[a-z0-9]+", text.lower()):
        if any(_h(w[:n]) in WORDS for n in range(6, min(len(w), 12) + 1)):
            found.append(w)
    for run in re.findall(r"\d[\d.\- ()]{8,}\d", text):
        digits = re.sub(r"\D", "", run)
        if len(digits) >= 10 and _h(digits[-10:]) in PHONES:
            found.append(run)
    return found


class NoRealNamesCase(unittest.TestCase):
    def test_the_guard_catches_a_hashed_name(self):
        # a made-up word, so no guarded name appears here in any form; the match is on its 6-letter prefix
        with mock.patch.object(sys.modules[__name__], "WORDS", WORDS | {_h("zqxwvb")}):
            self.assertEqual(hits("Signed, Zqxwvbtest"), ["zqxwvbtest"])
        self.assertEqual(hits("call 1-555-555-0100, Contractor Co."), [])

    @unittest.skipUnless(shutil.which("pdftotext"), "needs poppler-utils")
    def test_no_tracked_file_carries_a_real_name_or_number(self):
        files = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.split()
        bad = {}
        for f in files:
            path = ROOT / f
            if not path.is_file():
                continue
            text = f
            if path.suffix.lower() == ".pdf":
                # the text layer, the info dictionary and the XMP packet (CI has poppler)
                for cmd in (["pdftotext", str(path), "-"], ["pdfinfo", str(path)], ["pdfinfo", "-meta", str(path)]):
                    text += subprocess.run(cmd, capture_output=True, text=True, check=True).stdout
            elif path.suffix.lower() not in (".jpg", ".jpeg", ".png"):
                text += path.read_text(encoding="utf-8", errors="replace")
            if hits(text):
                bad[f] = hits(text)[:3]
        self.assertEqual(bad, {})


if __name__ == "__main__":
    unittest.main()
