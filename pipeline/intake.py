"""Intake & Classifier: build the Source Register from a packet folder.

One ID per page, sheet, photo and message; a SHA-256 for every file; duplicates
collapsed before anyone reads them; page images rendered so a reader never has
to open the original (architecture p.3, p.15 "72 photos arrived twice").

All of this is code. The Haiku pass that tags each page with its document type
and CSI divisions is Phase 2; `classify` here is the deterministic part, keyed
off extension, page count and the filename the sender chose, and it is allowed
to answer `unknown`.
"""
from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .broker import Broker

KINDS = (
    "drawing", "spec", "photo", "correspondence", "proposal-template",
    "spreadsheet", "prior-bid", "design", "unknown",
)

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".heic", ".tif", ".tiff"}
SHEET_SUFFIXES = {".csv", ".xlsx", ".xls", ".tsv"}
MESSAGE_SUFFIXES = {".eml", ".msg", ".txt", ".md"}

# Filename words that name a document type. Checked in order; the first hit wins.
NAME_HINTS = (
    ("drawing", ("s-1", "s1-", "plans", "plan-", "sheet", "detail", "drawing", "-a1", "struct")),
    ("spec", ("spec", "specification", "addendum", "rfp", "scope-and-materials")),
    ("proposal-template", ("proposal-format", "format-example", "template")),
    ("prior-bid", ("prior-bid", "past-bid", "previous-proposal")),
    ("correspondence", ("email", "message", "e-mail", "correspondence", "notes")),
    ("design", ("architecture", "design-doc")),
)

PHOTO_ID = re.compile(r"(IMG[_-]?\d{3,5})", re.IGNORECASE)
DUPLICATE_PREFIX = re.compile(r"^(?:\d+_|copy[ _-]of[ _-])", re.IGNORECASE)


@dataclass
class Source:
    source_id: str
    title: str
    file: str
    sha256: str
    kind: str
    status: str = "present"
    pages: str = ""
    notes: str = ""
    duplicate_of: str = ""
    page_images: list[str] = field(default_factory=list)

    def as_register_row(self) -> dict:
        d = {k: getattr(self, k) for k in
             ("source_id", "title", "file", "sha256", "pages", "kind", "status", "notes", "duplicate_of")}
        return d


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while block := f.read(chunk):
            h.update(block)
    return h.hexdigest()


def pdf_pages(path: Path) -> str:
    """Page count from pdfinfo. `file` misreports these PDFs; pdfinfo is right."""
    if not shutil.which("pdfinfo"):
        return ""
    try:
        out = subprocess.run(["pdfinfo", str(path)], capture_output=True, text=True, timeout=60, check=True).stdout
    except (subprocess.SubprocessError, OSError):
        return ""
    m = re.search(r"^Pages:\s+(\d+)", out, re.MULTILINE)
    return m.group(1) if m else ""


# A sheet exported from Bluebeam can carry a few stray characters (S-1's text
# layer is the nine characters of the seal date) while being entirely vector.
# A boolean would call that readable, so intake measures density instead.
TEXT_LAYER_MIN_CHARS = 200


def text_layer_chars(path: Path, pages: int = 2) -> int | None:
    """Characters in the first pages' text layer, or None if it cannot be read."""
    if not shutil.which("pdftotext"):
        return None
    try:
        out = subprocess.run(
            ["pdftotext", "-l", str(pages), str(path), "-"],
            capture_output=True, text=True, timeout=120, check=True,
        ).stdout
    except (subprocess.SubprocessError, OSError):
        return None
    return len(re.sub(r"\s+", "", out))


def has_text_layer(path: Path) -> bool | None:
    """Whether a reader can work from the text layer rather than page images."""
    n = text_layer_chars(path)
    return None if n is None else n >= TEXT_LAYER_MIN_CHARS


def classify(path: Path, pages: str = "") -> str:
    """Document kind from extension, filename and page count. May answer `unknown`."""
    suffix = path.suffix.lower()
    if suffix in IMAGE_SUFFIXES:
        return "photo"
    if suffix in SHEET_SUFFIXES:
        return "spreadsheet"
    name = path.name.lower()
    for kind, words in NAME_HINTS:
        if any(w in name for w in words):
            return kind
    if suffix in MESSAGE_SUFFIXES:
        return "correspondence"
    if suffix == ".pdf":
        # A single large sheet with no text layer is a drawing; anything else
        # needs the Phase 2 page tagger to say more than "unknown".
        if pages == "1" and has_text_layer(path) is False:
            return "drawing"
        return "unknown"
    return "unknown"


def source_id_for(path: Path, kind: str, taken: set[str]) -> str:
    """A stable, human-readable register ID: IMG_8316, S-1, SW, PFE, or a slug."""
    if kind == "photo":
        m = PHOTO_ID.search(path.name)
        if m:
            base = m.group(1).upper().replace("-", "_")
            if not base.startswith("IMG_"):
                base = "IMG_" + base[3:].lstrip("_")
            return _unique(base, taken)
    stem = DUPLICATE_PREFIX.sub("", path.stem)
    # A sheet designator: S-1, A-101, M-2. The hyphen is required so a year or a
    # street number in the filename is not mistaken for one.
    m = re.search(r"(?:^|[^A-Za-z0-9])([A-Z]{1,2}-\d{1,3})(?:[^A-Za-z0-9]|$)", stem)
    if m:
        return _unique(m.group(1).upper(), taken)
    words = [w for w in re.split(r"[^A-Za-z]+", stem) if w]
    initials = "".join(w[0] for w in words[:3]).upper() or "SRC"
    return _unique(initials, taken)


def _unique(base: str, taken: set[str]) -> str:
    if base not in taken:
        taken.add(base)
        return base
    for n in range(2, 100):
        cand = f"{base}-{n}"
        if cand not in taken:
            taken.add(cand)
            return cand
    raise RuntimeError(f"cannot make a unique source ID from {base!r}")


def render_pages(pdf: Path, out_dir: Path, *, dpi: int = 200, max_pages: int = 40) -> list[str]:
    """Rasterise a PDF so readers work from page images, not the original.

    Drawings are rendered at a higher DPI than text pages because the Drawing
    Reader tiles them; a sheet with no text layer has nothing else to read.
    """
    if not shutil.which("pdftoppm"):
        return []
    out_dir.mkdir(parents=True, exist_ok=True)
    prefix = out_dir / "p"
    try:
        subprocess.run(
            ["pdftoppm", "-png", "-r", str(dpi), "-l", str(max_pages), str(pdf), str(prefix)],
            capture_output=True, timeout=900, check=True,
        )
    except (subprocess.SubprocessError, OSError):
        return []
    return sorted(p.name for p in out_dir.glob("p*.png"))


def scan(packet: Path, *, images: bool = False, image_root: Path | None = None) -> list[Source]:
    """Walk a packet folder and build the Source Register rows.

    Files with the same SHA-256 collapse onto the first one seen; the later copy
    is registered as a duplicate so the register still accounts for it and no
    reader opens it twice.
    """
    packet = Path(packet)
    files = sorted(p for p in packet.rglob("*") if p.is_file())
    by_hash: dict[str, str] = {}
    taken: set[str] = set()
    sources: list[Source] = []
    for path in files:
        if path.name in ("SHA256SUMS", ".DS_Store"):
            continue
        digest = sha256_file(path)
        rel = str(path.relative_to(packet))
        pages = pdf_pages(path) if path.suffix.lower() == ".pdf" else ""
        kind = classify(path, pages)
        if digest in by_hash:
            first = by_hash[digest]
            sources.append(Source(
                source_id=_unique(f"{first}-dup", taken), title=f"Duplicate of {first}: {path.name}",
                file=rel, sha256=digest, kind=kind, status="duplicate", pages=pages,
                notes=f"identical bytes to {first}; collapsed at intake", duplicate_of=first,
            ))
            continue
        sid = source_id_for(path, kind, taken)
        by_hash[digest] = sid
        notes = []
        if path.suffix.lower() == ".pdf":
            chars = text_layer_chars(path)
            if chars is not None and chars < TEXT_LAYER_MIN_CHARS:
                notes.append(
                    f"text layer holds {chars} characters in the first pages; read from page rasters"
                )
        src = Source(source_id=sid, title=path.name, file=rel, sha256=digest, kind=kind,
                     pages=pages, notes="; ".join(notes))
        if images and path.suffix.lower() == ".pdf" and image_root is not None:
            dpi = 300 if kind == "drawing" else 150
            src.page_images = render_pages(path, Path(image_root) / sid, dpi=dpi)
            if src.page_images:
                src.notes = "; ".join(filter(None, [src.notes, f"{len(src.page_images)} page images rendered"]))
        sources.append(src)
    return sources


def run(packet: Path, broker: Broker, *, images: bool = False, image_root: Path | None = None) -> list[Source]:
    """Scan a packet and write the Source Register through the broker."""
    sources = scan(packet, images=images, image_root=image_root)
    broker.write_register([s.as_register_row() for s in sources])
    return sources


def verify(broker: Broker, packet: Path) -> list[str]:
    """Re-hash every registered file. Any hash change fails the run (architecture p.10)."""
    problems = []
    for sid, s in broker.ledger.register().items():
        if not s.get("file") or s.get("status") not in ("present", "duplicate"):
            continue
        path = Path(packet) / s["file"]
        if not path.exists():
            problems.append(f"{sid}: {s['file']} is in the register but not in the packet")
            continue
        got = sha256_file(path)
        if got != s["sha256"]:
            problems.append(f"{sid}: {s['file']} hash changed (register {s['sha256'][:12]}, packet {got[:12]})")
    return problems
