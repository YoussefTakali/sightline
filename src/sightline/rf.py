"""Robot Framework keywords.

    *** Settings ***
    Library    sightline.rf.SightlineLibrary    profile=headless

    *** Test Cases ***
    Login
        Type Text       Jean           into=text:Utilisateur : @right:250    clear=True
        Click           text:Valider || image:buttons/valider    expect=text:Bienvenue
        ${state}=       Which State    checked=image:cb_on;color=true    unchecked=image:cb_off;color=true
        ...             within=exact:Actif @right:80

Locators are strings (see sightline.spec): image:, text:, exact:, regex:, at:,
`A || B` fallback, `C >> T` inside, `[n]` n-th (1-based), `L @right:200` area next to L.
Every failure embeds the annotated screenshot in log.html.
"""
from __future__ import annotations

import os
from typing import Any

from .errors import SightlineError
from .geometry import Rect
from .locators import Locator
from .session import Session
from .spec import parse_locator


def _rf_bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def _opt_float(v):
    return None if v in (None, "", "None") else float(v)


class SightlineLibrary:
    ROBOT_LIBRARY_SCOPE = "SUITE"

    def __init__(self, profile: str | None = None, config: str | None = None, **overrides):
        self._profile, self._config, self._overrides = profile, config, overrides
        self._s: Session | None = None

    # ---------------------------------------------------------------- plumbing
    def _out_dir(self) -> str:
        try:
            from robot.libraries.BuiltIn import BuiltIn
            return BuiltIn().get_variable_value("${OUTPUT DIR}") or os.getcwd()
        except Exception:
            return os.getcwd()

    def _embed(self, path: str, ok: bool, text: str) -> None:
        try:
            from robot.api import logger
        except ImportError:
            return
        rel = os.path.relpath(path, self._out_dir()).replace(os.sep, "/")
        if ok:
            logger.debug(f'<a href="{rel}"><img src="{rel}" width="480"></a>', html=True)
        else:
            logger.info(f'<b>{text.splitlines()[0]}</b><br><a href="{rel}">'
                        f'<img src="{rel}" style="max-width:900px"></a>', html=True)

    @property
    def s(self) -> Session:
        if self._s is None:
            self.open_session(self._profile, self._config, **self._overrides)
        return self._s

    def _loc(self, spec) -> Locator:
        return spec if isinstance(spec, Locator) else parse_locator(str(spec))

    def open_session(self, profile: str | None = None, config: str | None = None, **overrides):
        """Connect using a profile of sightline.toml (or SIGHTLINE_PROFILE)."""
        self.close_session()
        overrides.setdefault("trace_dir", os.path.join(self._out_dir(), "sightline"))
        overrides.setdefault("on_artifact", self._embed)
        self._s = Session.from_profile(profile, config, **overrides)

    def open_vnc_session(self, host: str, port: int = 5900, password: str | None = None,
                         target_os: str = "windows", assets: str = "", **settings):
        """Connect straight to a VNC server, without a profile."""
        from .backends.vnc import VNCBackend
        self.close_session()
        b = VNCBackend(host, int(port), password, target_os=target_os)
        self._s = Session(b, assets=[a for a in assets.split(",") if a],
                          trace_dir=os.path.join(self._out_dir(), "sightline"),
                          on_artifact=self._embed, **settings)

    def close_session(self) -> None:
        if self._s is not None:
            self._s.close()
            self._s = None

    def set_default_timeout(self, seconds) -> float:
        old, self.s.timeout = self.s.timeout, float(seconds)
        return old

    def set_default_similarity(self, value) -> float:
        old, self.s.similarity = self.s.similarity, float(value)
        return old

    # ---------------------------------------------------------------- actions
    def click(self, locator, timeout=None, expect=None, vanish=False, retries=0,
              offset_x=0, offset_y=0, button="left"):
        """Click the unique match. expect=<locator> must appear afterwards; vanish=True:
        the clicked element must disappear; retries=N clicks again if not."""
        self.s.click(self._loc(locator), button=button, timeout=_opt_float(timeout),
                     expect=self._loc(expect) if expect else None, vanish=_rf_bool(vanish),
                     retries=int(retries), offset=(int(offset_x), int(offset_y)))

    def double_click(self, locator, timeout=None, offset_x=0, offset_y=0):
        self.s.double_click(self._loc(locator), timeout=_opt_float(timeout),
                            offset=(int(offset_x), int(offset_y)))

    def right_click(self, locator, timeout=None, offset_x=0, offset_y=0):
        self.s.right_click(self._loc(locator), timeout=_opt_float(timeout),
                           offset=(int(offset_x), int(offset_y)))

    def hover(self, locator, timeout=None):
        self.s.hover(self._loc(locator), timeout=_opt_float(timeout))

    def click_nearest(self, target, anchor, direction="right", max_distance=None, timeout=None):
        """Click the `target` nearest to `anchor` on its `direction` side (right/left/above/
        below/near), aligned on the same row/column: the checkbox of a label, the button
        of a line..."""
        md = int(max_distance) if max_distance not in (None, "", "None") else None
        t, a = self._loc(target), self._loc(anchor)
        loc = t.near(a, md or 200) if direction == "near" else getattr(
            t, {"right": "right_of", "left": "left_of", "above": "above", "below": "below"}[direction])(a, md)
        self.s.click(loc, timeout=_opt_float(timeout))

    def click_next_to(self, anchor, side="right", size=200, gap=4, timeout=None):
        """Click the area next to a label (an input field right of 'Email :')."""
        self.s.click(self._loc(anchor).area(side, int(size), int(gap)), timeout=_opt_float(timeout))

    def drag_and_drop(self, source, target, timeout=None):
        self.s.drag(self._loc(source), self._loc(target), timeout=_opt_float(timeout))

    def type_text(self, text, into=None, clear=False, interval=0.0):
        self.s.type(str(text), into=self._loc(into) if into else None, clear=_rf_bool(clear),
                    interval=float(interval))

    def type_secret(self, text, into=None, clear=False):
        """Like Type Text, never written to the logs/trace."""
        try:
            from robot.api import logger
            logger.info("Typing a secret", also_console=False)
        except ImportError:
            pass
        self.s.type(str(text), into=self._loc(into) if into else None, clear=_rf_bool(clear),
                    secret=True)

    def press_keys(self, *combos):
        """Press Keys    ctrl+a    delete    mod+s     (mod = Ctrl, or Cmd on macOS)"""
        self.s.press(*combos)

    def scroll(self, clicks=3, at=None, horizontal=False):
        self.s.scroll(self._loc(at) if at else None, int(clicks), _rf_bool(horizontal))

    def scroll_until_visible(self, locator, area=None, direction="down", clicks=3, max_scrolls=40):
        self.s.scroll_until_visible(self._loc(locator), self._loc(area) if area else None,
                                    int(clicks), int(max_scrolls), direction)

    # ---------------------------------------------------------------- waits / checks
    def wait_until_visible(self, locator, timeout=None):
        m = self.s.wait(self._loc(locator), _opt_float(timeout))
        return [m.rect.x, m.rect.y, m.rect.w, m.rect.h]

    def wait_until_gone(self, locator, timeout=None):
        self.s.wait_vanish(self._loc(locator), _opt_float(timeout))

    def element_should_be_visible(self, locator, timeout=None):
        self.s.wait(self._loc(locator), _opt_float(timeout))

    def element_should_not_be_visible(self, locator, timeout=0):
        m = self.s.exists(self._loc(locator), float(timeout or 0))
        if m is not None:
            raise SightlineError(f"{locator} is visible: {m}")

    def element_exists(self, locator, timeout=0) -> bool:
        return self.s.exists(self._loc(locator), float(timeout or 0)) is not None

    def count_matches(self, locator) -> int:
        return self.s.count(self._loc(locator))

    def wait_until_screen_is_stable(self, quiet=0.5, timeout=None, region=None):
        r = Rect(*[int(v) for v in str(region).split(",")]) if region else None
        self.s.wait_until_stable(r, float(quiet), _opt_float(timeout))

    def which_state(self, within=None, timeout=None, **states):
        """${s}=  Which State  checked=image:cb_on;color=true  unchecked=image:cb_off;color=true
        Returns the name of the state that matches best (optionally inside `within`)."""
        region = None
        if within:
            region = self.s.wait(self._loc(within), _opt_float(timeout)).rect
        name, _ = self.s.which({k: self._loc(v) for k, v in states.items()},
                               _opt_float(timeout), region)
        return name

    # ---------------------------------------------------------------- reading
    def read_text(self, locator, timeout=None) -> str:
        return self.s.read_text(self._loc(locator), _opt_float(timeout))

    def read_table_via_clipboard(self, cell, columns=None, must_contain=None, quoted=False):
        """Exact grid content: click a data cell, select all, copy, parse. `columns`:
        list (or 'A|B|C') of expected titles. Returns [header?] + rows."""
        from .tables import read_table_via_clipboard
        cols = columns.split("|") if isinstance(columns, str) else columns
        must = must_contain.split("|") if isinstance(must_contain, str) else (must_contain or ())
        return read_table_via_clipboard(self.s, self._loc(cell), columns=cols, must_contain=must,
                                        quoted=_rf_bool(quoted))

    def read_grid(self, columns, key_column=0, scroll=True):
        """OCR a grid that cannot be copied: `columns` = its titles (list or 'A|B|C').
        Returns [header] + rows. Prefer Read Table Via Clipboard when Ctrl+C works."""
        from .components import read_grid
        cols = columns.split("|") if isinstance(columns, str) else list(columns)
        key = int(key_column) if str(key_column).isdigit() else str(key_column)
        return read_grid(self.s, cols, key_column=key, scroll=_rf_bool(scroll)).table()

    def select_menu(self, path):
        """Select Menu    Fichier > Exporter > CSV"""
        from .actions import menu
        menu(self.s, str(path))

    def select_option(self, combo, option):
        """Select Option    text:Pays : @right:180    France"""
        from .actions import select
        select(self.s, self._loc(combo), str(option))

    def get_clipboard(self) -> str:
        return self.s.get_clipboard()

    def handle_dialogs(self, required=None, optional=None, timeout=20, quiet=3):
        """Answer dialogs in any order. required/optional: lists of BUTTON locators."""
        from .dialogs import handle_dialogs
        as_list = lambda v: [] if not v else (list(v) if isinstance(v, (list, tuple)) else [v])
        return handle_dialogs(self.s, [self._loc(x) for x in as_list(required)],
                              [self._loc(x) for x in as_list(optional)], float(timeout),
                              float(quiet))

    def take_screenshot(self, name="screenshot") -> str:
        d = os.path.join(self._out_dir(), "sightline")
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, f"{name}.png")
        self.s.screenshot(path)
        self._embed(path, False, name)
        return path
