"""Ready-made high-level actions for classic desktop UIs.

    menu(s, "Fichier > Exporter > CSV")
    select(s, Text("Pays :").area("right", 180), "France")
    fill_form(s, {"Utilisateur :": "Jean", "Mot de passe :": secret})

The trick that makes them robust: after opening a menu or a drop-down, the next item is
searched ONLY in the screen area that just changed (the popup) - so "Exporter" in a
menu is never confused with an "Exporter" button elsewhere on screen.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from .errors import NotFound, SightlineError
from .frame import changed_bbox
from .locators import Locator, Text, as_locator

if TYPE_CHECKING:
    from .session import Session


def _open_and_get_popup(s: Session, target, timeout) -> tuple:
    before = s.capture()
    m = s.click(target, timeout=timeout)
    try:
        after = s.wait_for_change(before, timeout=5)
        after = s.wait_until_stable(quiet=0.25, timeout=5)
    except SightlineError:
        after = s.capture()
    return m, changed_bbox(before, after)


def menu(s: Session, path: str, timeout: float | None = None, separator: str = ">") -> None:
    """Open a menu path: 'Fichier > Exporter > CSV'. Items are OCR text (fuzzy), each
    searched inside the popup the previous click opened."""
    items = [p.strip() for p in path.split(separator) if p.strip()]
    if not items:
        raise SightlineError("menu: empty path")
    target: Locator = Text(items[0])
    for i in range(len(items)):
        if i == len(items) - 1:
            s.click(target, timeout=timeout)
            return
        _, popup = _open_and_get_popup(s, target, timeout)
        nxt = Text(items[i + 1])
        if popup is not None:
            nxt_in = nxt.within(popup.expand(4))
            if s.exists(nxt_in, timeout=2) is not None:
                target = nxt_in
                continue
        target = nxt                                   # popup not detected: whole screen


def select(s: Session, combo, option: str, timeout: float | None = None,
           max_scrolls: int = 15) -> None:
    """Choose `option` in a drop-down: open it, find the option in the list that appeared
    (scrolling the list if needed), click it."""
    _, popup = _open_and_get_popup(s, as_locator(combo), timeout)
    opt = Text(option)
    where = opt.within(popup.expand(2)) if popup is not None else opt
    if s.exists(where, timeout=1) is None and popup is not None:
        try:
            s.scroll_until_visible(where, at=popup, max_scrolls=max_scrolls)
        except NotFound:
            s.press("esc")
            raise NotFound(f"select: option '{option}' not in the drop-down list "
                           f"{popup}") from None
    s.click(where, timeout=timeout)


def fill_form(s: Session, fields: Mapping[str, str], width: int = 220, gap: int = 6,
              clear: bool = True) -> None:
    """Type into the field right of each label: {"Utilisateur :": "Jean", ...}."""
    for label, value in fields.items():
        s.type(str(value), into=Text(label).area("right", width, gap=gap), clear=clear)
