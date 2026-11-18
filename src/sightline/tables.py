"""Reading tables: exact values through the clipboard first, OCR only as a fallback.

Most grids (Java JTable, .NET DataGridView, Excel, web tables, Delphi grids...) copy
the selection as tab-separated text with full values - never truncated with '...',
never OCR typos. This is a generic step any grid component can reuse, with the safety
checks that make it trustworthy:

- a sentinel in the clipboard first (an old copy is never taken for this one);
- a consistent column count on every row (else the copy is not a table);
- optional cross-check: values read on screen (OCR) must be found in the copy, so a
  copy of the WRONG grid (focus elsewhere) is rejected.
"""
from __future__ import annotations

import csv
import difflib
import io
from collections.abc import Sequence
from typing import TYPE_CHECKING

from .errors import SightlineError

if TYPE_CHECKING:
    from .session import Session


def parse_tsv(text: str, quoted: bool = False) -> list[list[str]]:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if quoted:                       # Excel quotes cells that contain tabs/newlines
        rows = list(csv.reader(io.StringIO(text), delimiter="\t", quotechar='"'))
    else:                            # JTable & co: plain, a quote is just a character
        rows = [line.split("\t") for line in text.split("\n")]
    rows = [[" ".join(c.split()) for c in r] for r in rows]
    rows = [r for r in rows if any(c for c in r)]
    if not rows:
        return rows
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    while width > 1 and all(not r[width - 1] for r in rows):   # Excel's trailing empties
        rows = [r[:-1] for r in rows]
        width -= 1
    return rows


def read_table_via_clipboard(session: Session, cell, select_all: str = "mod+a",
                             copy: str = "mod+c", columns: Sequence[str] | None = None,
                             has_header: bool | None = None, quoted: bool = False,
                             must_contain: Sequence[str] = (), restore_click: bool = True,
                             timeout: float = 15.0) -> list[list[str]]:
    """Click `cell` (any data cell of the grid), select all, copy, parse.

    columns: expected titles; the copy must have exactly that many columns, and a copied
      title row equal to them is dropped. Returns [columns] + rows when given.
    has_header: True if the copy includes a title row to drop (None = auto when
      `columns` is given).
    must_contain: values visible on screen that must be in the copy (wrong-grid guard).
    restore_click: click the cell again afterwards so the grid is not left fully selected.
    """
    session.click(cell)
    session.press(select_all)
    text = session.copy(copy, timeout)
    if restore_click:
        session.click(cell)
    rows = parse_tsv(text, quoted)
    if not rows:
        raise SightlineError("the grid copied nothing (Ctrl+C disabled on it?)")
    widths = {len(r) for r in rows}
    if columns is not None:
        if widths != {len(columns)}:
            raise SightlineError(f"copied {sorted(widths)} columns, expected {len(columns)} "
                                 f"({list(columns)[:6]}...). First row: {rows[0][:6]}")
        norm = [c.casefold().strip() for c in columns]
        if has_header or (has_header is None and [c.casefold() for c in rows[0]] == norm):
            rows = rows[1:]
    elif has_header:
        rows = rows[1:]
    flat = {c.casefold() for r in rows for c in r if c}
    for v in must_contain:
        vv = v.casefold().rstrip(".… ")
        ok = any(c.startswith(vv) for c in flat) if v.rstrip().endswith(("..", "…")) else (
            vv in flat or any(difflib.SequenceMatcher(None, vv, c).ratio() >= 0.85 for c in flat))
        if not ok:
            raise SightlineError(f"'{v}' is visible on screen but not in the copied data: the "
                                 "copy is not from this grid (focus elsewhere?)")
    session.tracer.note("read_table_via_clipboard", f"{len(rows)} rows x {max(widths)} cols")
    return ([list(columns)] if columns is not None else []) + rows
