"""Read a data grid by OCR - for grids that do not allow copy-paste.

    table = read_grid(s, ["Numéro", "Client", "Montant", "Statut"])

Prefer `read_table_via_clipboard` (exact values) whenever the grid supports Ctrl+C; this
is the fallback, built to be generic:

1. HEADER: the column titles are found by OCR on one row (all of them, same row).
2. COLUMNS: borders come from the separator lines drawn in the header strip (titles are
   often CENTRED, so text edges are not borders); midpoints between titles otherwise.
3. BODY: from under the header down to the grid's bottom border (a long horizontal line)
   or the given region.
4. ROWS: text lines of the body (horizontal ink profile), so row height is measured,
   never configured.
5. CELLS: one OCR call per COLUMN (not per cell), words assigned to rows by position.
6. PAGES: scroll inside the body, read again, merge on the key column; stop when a scroll
   no longer changes the body or brings no new key.

Values cut by the grid ("Lefè…", "Lefè...") are reported in `GridResult.truncated` -
widen the column (or use the clipboard) rather than trusting them.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import numpy as np

from ..errors import NotFound, SightlineError
from ..frame import Frame, region_changed
from ..geometry import Rect
from ..vision.ocr import fold, ink, search_text

if TYPE_CHECKING:
    from ..session import Session


@dataclass
class GridGeometry:
    header: Rect
    columns: list[tuple[int, int]]          # screen x ranges
    body: Rect


@dataclass
class GridResult:
    header: list[str]
    rows: list[list[str]]
    truncated: list[tuple[int, str, str]] = field(default_factory=list)   # (row, column, value)
    pages: int = 1

    def table(self) -> list[list[str]]:
        return [self.header] + self.rows


# ------------------------------------------------------------------ geometry
def _header_row(words, titles: Sequence[str]) -> list[Rect]:
    """Rects of every title, all on the same row (topmost such row)."""
    hits = {t: search_text(words, t, exact=False, threshold=0.75) for t in titles}
    missing = [t for t, h in hits.items() if not h]
    if missing:
        raise NotFound(f"grid header: title(s) {missing} not found by OCR")
    best = None
    for anchor in sorted(hits[titles[0]], key=lambda h: h.rect.y):
        row = [anchor.rect]
        for t in titles[1:]:
            same = [h for h in hits[t] if h.rect.vertical_overlap(anchor.rect) >= 0.5]
            if not same:
                break
            row.append(min(same, key=lambda h: abs(h.rect.center.y - anchor.rect.center.y)).rect)
        else:
            if all(row[i].x < row[i + 1].x for i in range(len(row) - 1)):
                best = row
                break
    if best is None:
        raise NotFound(f"grid header: titles {list(titles)} are not on one row, left to right")
    return best


def _vertical_lines(gray: np.ndarray, min_frac: float = 0.7) -> list[int]:
    """x of vertical lines crossing (most of) a strip."""
    med = np.median(gray, axis=1, keepdims=True).astype(np.int16)
    diff = np.abs(gray.astype(np.int16) - med) > 25
    frac = diff.mean(axis=0)
    xs = np.where(frac >= min_frac)[0]
    lines, run = [], []
    for x in xs:
        if run and x != run[-1] + 1:
            lines.append(int(np.mean(run)))
            run = []
        run.append(x)
    if run:
        lines.append(int(np.mean(run)))
    return lines


def _bottom_border(gray: np.ndarray, start: int, min_frac: float = 0.9) -> int | None:
    """y (relative) of the first long horizontal line below `start`."""
    meds = np.median(gray, axis=1).astype(np.int16)
    for y in range(max(start, 2), gray.shape[0] - 3):
        row = gray[y].astype(np.int16)
        if (np.abs(row - meds[y]) <= 12).mean() < min_frac:
            continue
        # a BORDER is thin: different from just above AND from a few px below
        # (a block of highlighted rows is uniform too, but thick)
        if abs(meds[y] - meds[y - 2]) > 25 and abs(meds[y] - meds[y + 3]) > 12:
            return y
    return None


def measure_grid(frame: Frame, words, titles: Sequence[str], region: Rect | None = None,
                 area: Rect | None = None) -> GridGeometry:
    area = area or frame.rect
    rects = _header_row(words, titles)
    rects = [r.offset(area.x, area.y) for r in rects]
    top = min(r.y for r in rects) - 4
    bottom = max(r.bottom for r in rects) + 4
    lo = region.x if region else max(frame.origin_x, rects[0].x - 400)
    hi = region.right if region else min(frame.rect.right, rects[-1].right + 400)
    strip = frame.crop(Rect(lo, top, hi - lo, bottom - top)).gray
    lines = [lo + x for x in _vertical_lines(strip)]
    # header row borders: refine top/bottom to the strip's horizontal lines if any
    bounds = []
    for i in range(len(rects) - 1):
        a, b = rects[i], rects[i + 1]
        between = [x for x in lines if a.right <= x <= b.x]
        mid = (a.center.x + b.center.x) // 2
        bounds.append(min(between, key=lambda x: abs(x - mid)) if between else mid)
    left = [x for x in lines if x < rects[0].x]
    right = [x for x in lines if x > rects[-1].right]
    first = max(left) if left else rects[0].x - 6
    last = min(right) if right else (region.right if region else
                                     rects[-1].right + (rects[-1].center.x - bounds[-1]
                                                        if bounds else 40))
    edges = [first] + bounds + [last]
    columns = [(edges[i], edges[i + 1]) for i in range(len(edges) - 1)]
    body_top = bottom + 1
    if region is not None:
        body = Rect.from_corners(edges[0], body_top, edges[-1], region.bottom)
    else:
        col = frame.crop(Rect.from_corners(edges[0], frame.origin_y, edges[-1], frame.rect.bottom)).gray
        b = _bottom_border(col, body_top - frame.origin_y + 6)
        end = frame.origin_y + b if b is not None else frame.rect.bottom
        body = Rect.from_corners(edges[0], body_top, edges[-1], end)
    return GridGeometry(Rect.from_corners(edges[0], top, edges[-1], bottom), columns, body)


def row_bands(frame: Frame, body: Rect, key: tuple[int, int], min_gap: int = 2) -> list[tuple[int, int]]:
    """Screen y-ranges of the text lines in the key column."""
    img = ink(frame.crop(Rect.from_corners(key[0] + 3, body.y, key[1] - 3, body.bottom)).pixels)
    rows = (img < 140).sum(axis=1) >= 2
    bands, start, gap = [], None, 0
    for y, on in enumerate(rows):
        if on:
            if start is None:
                start = y
            gap = 0
        elif start is not None:
            gap += 1
            if gap >= min_gap:
                bands.append((start, y - gap + 1))
                start, gap = None, 0
    if start is not None:
        bands.append((start, len(rows)))
    bands = [b for b in bands if b[1] - b[0] >= 5]           # text lines, not dust
    if not bands:
        return []
    pitch = int(np.median(np.diff([b[0] for b in bands]))) if len(bands) > 1 else 0
    pad = max((pitch - int(np.median([b[1] - b[0] for b in bands]))) // 2, 2) if pitch else 3
    return [(body.y + max(a - pad, 0), body.y + min(b + pad, body.h)) for a, b in bands]


def _read_page(s: Session, frame: Frame, geo: GridGeometry, key_idx: int):
    bands = row_bands(frame, geo.body, geo.columns[key_idx])
    cells = [[""] * len(geo.columns) for _ in bands]
    for c, (x0, x1) in enumerate(geo.columns):
        crop = frame.crop(Rect.from_corners(x0 + 3, geo.body.y, x1 - 2, geo.body.bottom))
        words = s.ocr.words(crop.pixels)
        for w in words:
            cy = crop.origin_y + w.rect.center.y
            for r, (a, b) in enumerate(bands):
                if a <= cy < b:
                    cells[r][c] = (cells[r][c] + " " + w.text).strip()
                    break
    return [[_clean_cell(v) for v in row] for row in cells]


def _clean_cell(value: str) -> str:
    """Drop tokens made only of symbols at the START or END of a cell ('——— 87,01',
    '124,02 _'): fragments of borders/separators OCR picks up through a compressed remote
    picture. Symbols inside a value ('12-03', 'A/B', '-15,00') are data and are kept."""
    toks = value.split()
    while toks and not any(c.isalnum() for c in toks[0]):
        toks.pop(0)
    while toks and not any(c.isalnum() for c in toks[-1]):
        toks.pop()
    return " ".join(toks)


def _same_key(a: str, b: str) -> bool:
    fa, fb = fold(a).replace(" ", ""), fold(b).replace(" ", "")
    if fa == fb:
        return True
    # O/0, l/1... OCR slips on the same row read twice
    table = str.maketrans({"o": "0", "l": "1", "i": "1", "s": "5", "b": "8"})
    return fa.translate(table) == fb.translate(table)


def read_grid(s: Session, columns: Sequence[str], key_column: int | str = 0,
              region: Rect | None = None, scroll: bool = True, scroll_clicks: int = 3,
              max_pages: int = 100) -> GridResult:
    """OCR the grid whose header shows `columns` (titles, left to right). See module doc."""
    key_idx = key_column if isinstance(key_column, int) else list(columns).index(key_column)
    frame = s.capture()
    area = region or frame.rect
    words = s.ocr_words(frame, area)
    geo = measure_grid(frame, words, columns, region, area)
    s.tracer.step("grid", f"{len(columns)} columns", frame, geo.body,
                  others=[Rect.from_corners(a, geo.header.y, b, geo.header.bottom)
                          for a, b in geo.columns])
    rows: list[list[str]] = []
    pages = 0
    for _ in range(max_pages):
        pages += 1
        page = [r for r in _read_page(s, frame, geo, key_idx) if r[key_idx]]
        new = [r for r in page if not any(_same_key(r[key_idx], o[key_idx]) for o in rows)]
        if pages > 1 and page and len(new) == len(page):
            raise SightlineError("grid: the scroll skipped rows (no overlap between pages) - "
                                 "use fewer scroll_clicks")
        rows += new
        if not scroll or (pages > 1 and not new):
            break
        before = frame
        s.backend.scroll(geo.body.center.x, geo.body.center.y, scroll_clicks)
        s._park()          # web clients draw the pointer INTO the picture: move it off the rows
        try:
            frame = s.wait_until_stable(geo.body, quiet=0.3, timeout=5)
        except SightlineError:
            frame = s.capture()
        if not region_changed(before, frame, geo.body):
            break                                              # end of the grid
    truncated = [(i, columns[c], v) for i, r in enumerate(rows) for c, v in enumerate(r)
                 if v.rstrip().endswith(("...", "…", ".."))]
    s.tracer.note("grid", f"{len(rows)} rows x {len(columns)} columns, {pages} page(s)"
                  + (f", {len(truncated)} truncated value(s)" if truncated else ""))
    return GridResult(list(columns), rows, truncated, pages)


__all__ = ["GridGeometry", "GridResult", "measure_grid", "read_grid", "row_bands"]
