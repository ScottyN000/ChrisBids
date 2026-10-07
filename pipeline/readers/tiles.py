"""Drawing views and tiles: one image per Drawing Reader call.

A sheet is never sent whole (architecture p.13: one view or tile per call).
Each unit is a region of one page, given as fractions of the page as it is
displayed (rotation applied), so a layout written once holds at any DPI:

- a named view, cropped to a box a person drew once per sheet
  (`fixtures/<job>/units.yaml`), or
- a tile of a fixed grid, for a sheet nobody has laid out yet.

Rendering is pdftoppm's own crop, so the bytes depend only on the PDF, the box
and the DPI. The DPI is chosen so the tile's long edge fits the size the model
is shown, rather than letting the API downscale it to something we never saw.
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

# Long edge in pixels. Images larger than this are downscaled by the API, so we
# render at the size the model actually reads instead.
MAX_EDGE = 1568
MAX_DPI = 300


@dataclass(frozen=True)
class Box:
    """A region of a page as fractions of its displayed width and height."""

    left: float
    top: float
    right: float
    bottom: float

    def __post_init__(self):
        if not (0 <= self.left < self.right <= 1 and 0 <= self.top < self.bottom <= 1):
            raise ValueError(f"box must lie inside the page with left<right, top<bottom: {self}")

    @classmethod
    def of(cls, seq) -> "Box":
        return cls(*(float(v) for v in seq))


def page_size_in(pdf: Path, page: int = 1) -> tuple[float, float]:
    """Displayed width and height of a page in inches, rotation applied."""
    out = subprocess.run(
        ["pdfinfo", "-f", str(page), "-l", str(page), str(pdf)],
        capture_output=True, text=True, check=True,
    ).stdout
    m = re.search(rf"Page\s+{page} size:\s+([\d.]+) x ([\d.]+) pts", out) or \
        re.search(r"Page size:\s+([\d.]+) x ([\d.]+) pts", out)
    r = re.search(rf"Page\s+{page} rot:\s+(\d+)", out) or re.search(r"Page rot:\s+(\d+)", out)
    if not m:
        raise ValueError(f"{pdf}: pdfinfo gave no page size for page {page}")
    w, h = float(m.group(1)) / 72, float(m.group(2)) / 72
    if r and int(r.group(1)) % 180 == 90:
        w, h = h, w
    return w, h


def fit_dpi(page_in: tuple[float, float], box: Box, *, max_edge: int = MAX_EDGE, max_dpi: int = MAX_DPI) -> int:
    """The highest DPI (up to max_dpi) at which the box's long edge fits max_edge."""
    long_in = max((box.right - box.left) * page_in[0], (box.bottom - box.top) * page_in[1])
    return max(1, min(max_dpi, int(max_edge / long_in)))


def render_box(pdf: Path, out: Path, box: Box, *, page: int = 1,
               max_edge: int = MAX_EDGE, max_dpi: int = MAX_DPI) -> Path:
    """Render one region of one page to `out` (.png). Returns the path."""
    w_in, h_in = page_size_in(pdf, page)
    dpi = fit_dpi((w_in, h_in), box, max_edge=max_edge, max_dpi=max_dpi)
    x, y = round(box.left * w_in * dpi), round(box.top * h_in * dpi)
    W, H = round((box.right - box.left) * w_in * dpi), round((box.bottom - box.top) * h_in * dpi)
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["pdftoppm", "-png", "-singlefile", "-r", str(dpi), "-f", str(page), "-l", str(page),
         "-x", str(x), "-y", str(y), "-W", str(W), "-H", str(H), str(pdf), str(out.with_suffix(""))],
        check=True, capture_output=True,
    )
    return out.with_suffix(".png")


def grid(cols: int, rows: int, *, overlap: float = 0.05) -> list[tuple[str, Box]]:
    """A fixed grid over the whole page, row by row, each tile padded by `overlap`
    of a cell on every side so a dimension string on a seam appears whole in one tile."""
    if cols < 1 or rows < 1 or not 0 <= overlap < 0.5:
        raise ValueError("grid needs cols, rows >= 1 and 0 <= overlap < 0.5")
    tiles = []
    for r in range(rows):
        for c in range(cols):
            pad_x, pad_y = overlap / cols, overlap / rows
            tiles.append((f"r{r + 1}c{c + 1}", Box(
                max(0.0, c / cols - pad_x), max(0.0, r / rows - pad_y),
                min(1.0, (c + 1) / cols + pad_x), min(1.0, (r + 1) / rows + pad_y),
            )))
    return tiles
