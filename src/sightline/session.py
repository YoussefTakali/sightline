"""Session: one target screen + the rules for acting on it reliably.

Every action follows the same contract:
  1. resolve the locator on a FRESH capture, polling until `timeout` (no sleeps);
  2. require exactly one match (strict) - or fail with every candidate drawn;
  3. require it to be STABLE: same place on two consecutive captures (no clicking a
     button while its dialog is still sliding in);
  4. act, trace (annotated frame), optionally verify an expected result.
"""
from __future__ import annotations

import time
import uuid
from collections import OrderedDict
from collections.abc import Sequence
from typing import Any

from .assets import AssetStore
from .backends.base import Backend
from .errors import Ambiguous, NotFound, SightlineError, StillVisible, Unstable, Unsupported
from .frame import Frame, region_changed
from .geometry import Point, Rect
from .locators import FindContext, Locator, Match, as_locator
from .trace import Tracer

Target = "Locator | str | Rect | Point | tuple[int, int]"


class Session:
    def __init__(self, backend: Backend, *, assets: Sequence[str] | AssetStore | None = None,
                 timeout: float = 10.0, poll: float = 0.2, similarity: float = 0.9,
                 color_similarity: float = 0.92, text_similarity: float = 0.8,
                 strict: bool = True, ambiguity_margin: float = 0.03, stable: bool = True,
                 settle: float = 0.12, screen_scale: float = 1.0, scale_search: bool = True,
                 ocr: Any = None, ocr_lang: str = "eng", theme: str | None = None,
                 trace: str = "actions", trace_dir: str = "sightline-traces",
                 park_mouse: tuple[int, int] | None = None, on_artifact=None,
                 recovery: list | None = None):
        self.backend = backend
        self.assets = assets if isinstance(assets, AssetStore) else AssetStore(
            list(assets or []), backend.target_os, theme)
        self.timeout, self.poll = float(timeout), float(poll)
        self.similarity, self.color_similarity = float(similarity), float(color_similarity)
        self.text_similarity = float(text_similarity)
        self.strict, self.ambiguity_margin = strict, float(ambiguity_margin)
        self.stable, self.settle = stable, float(settle)
        self.screen_scale, self.scale_search = float(screen_scale), scale_search
        self._ocr, self.ocr_lang = ocr, ocr_lang
        self._ocr_cache: OrderedDict = OrderedDict()
        if isinstance(park_mouse, str):                       # "x,y" from Robot / CLI
            park_mouse = tuple(int(v) for v in park_mouse.split(","))
        self.park_mouse = tuple(int(v) for v in park_mouse) if park_mouse else None
        self.tracer = Tracer(trace_dir, trace, on_artifact)
        self.report: str | None = None
        self._recovery: list = []
        if recovery:
            self.on_failure(lambda sess: _run_recovery_steps(sess, recovery))

    # ================================================================ plumbing
    @classmethod
    def from_profile(cls, name: str | None = None, config: str | None = None, **overrides):
        from .config import session_from_profile
        return session_from_profile(name, config, **overrides)

    @property
    def ocr(self):
        if self._ocr is None or isinstance(self._ocr, str):
            from .vision.ocr import make_ocr
            self._ocr = make_ocr(self._ocr or "auto", self.ocr_lang)
        return self._ocr

    def ocr_words(self, frame: Frame, area: Rect):
        key = (frame.digest, area.x, area.y, area.w, area.h)
        if key in self._ocr_cache:
            self._ocr_cache.move_to_end(key)
            return self._ocr_cache[key]
        words = self.ocr.words(frame.crop(area).pixels)
        self._ocr_cache[key] = words
        if len(self._ocr_cache) > 32:
            self._ocr_cache.popitem(last=False)
        return words

    def capture(self, region: Rect | None = None) -> Frame:
        return self.backend.capture(region)

    def close(self) -> None:
        try:
            self.backend.close()
        finally:
            self.report = self.tracer.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ================================================================ recovery
    def on_failure(self, fn):
        """Register a recovery step run after a failed test (close stray dialogs, go back
        to the home screen...) so the NEXT test starts clean. Usable as a decorator."""
        self._recovery.append(fn)
        return fn

    def recover(self) -> list[str]:
        """Run every recovery step; never raises (it runs when things already broke)."""
        problems = []
        for fn in self._recovery:
            try:
                fn(self)
            except Exception as e:
                problems.append(f"{getattr(fn, '__name__', fn)}: {str(e).splitlines()[0]}")
        self.tracer.note("recover", "; ".join(problems) or "ok")
        return problems

    def get(self, target) -> Element:
        return Element(self, as_locator(target))

    # ================================================================ finding
    def find_all(self, target, frame: Frame | None = None, region: Rect | None = None) -> list[Match]:
        """Every match right now (no waiting, no strictness), best score first."""
        frame = frame or self.capture()
        ms = as_locator(target).find(FindContext(frame, self, region))
        return sorted(ms, key=lambda m: -m.score)

    def pick(self, ms: list[Match], loc: Locator, role: str = "target",
             strict: bool | None = None) -> Match:
        ms = sorted(ms, key=lambda m: -m.score)
        if len(ms) == 1 or not (self.strict if strict is None else strict):
            return ms[0]
        if ms[1].score >= ms[0].score - self.ambiguity_margin:
            listing = "\n  ".join(str(m) for m in ms[:8])
            raise Ambiguous(
                f"{len(ms)} matches for {role} {loc.describe()} (need exactly one):\n  {listing}\n"
                "Disambiguate: .nth(i) / [i], .within(container), .right_of(label), or a "
                "more distinctive crop.", matches=ms)
        return ms[0]

    def locate(self, target, timeout: float | None = None, stable: bool | None = None,
               region: Rect | None = None, strict: bool | None = None,
               action: str = "find", quiet: bool = False) -> tuple[Match, Frame]:
        """quiet=True: a miss is an answer (exists()), not a failure: no failure trace."""
        loc = as_locator(target)
        timeout = self.timeout if timeout is None else float(timeout)
        stable = self.stable if stable is None else stable
        deadline = time.monotonic() + timeout
        prev: Match | None = None
        frame: Frame | None = None
        ctx: FindContext | None = None
        ambiguous: Ambiguous | None = None
        while True:
            frame = self.capture()
            ctx = FindContext(frame, self, region)
            m = None
            try:
                ms = loc.find(ctx)
                if ms:
                    m = self.pick(ms, loc, strict=strict)
                ambiguous = None
            except Ambiguous as e:
                ambiguous = e
            if m is not None:
                if not stable or (prev is not None and abs(prev.center.x - m.center.x) <= 2
                                  and abs(prev.center.y - m.center.y) <= 2):
                    return m, frame
                if prev is not None and time.monotonic() > deadline:
                    msg = (f"{action}: {loc.describe()} keeps moving ({prev.rect} -> {m.rect}) "
                           f"after {timeout}s")
                    art = self.tracer.failure(action, loc.describe(), msg, frame, [m.rect])
                    raise Unstable(msg, art)
                prev = m
                time.sleep(self.settle)
                continue
            prev = None
            if time.monotonic() >= deadline:
                break
            time.sleep(self.poll)
        if ambiguous is not None:
            art = self.tracer.failure(action, loc.describe(), str(ambiguous), frame,
                                      [m.rect for m in ambiguous.matches])
            raise Ambiguous(str(ambiguous), art, ambiguous.matches)
        why = "\n  ".join(ctx.diag) if ctx and ctx.diag else "no candidate at all"
        msg = f"{action}: {loc.describe()} not found within {timeout:g}s\n  {why}"
        if quiet:
            raise NotFound(msg)
        art = self.tracer.failure(action, loc.describe(), msg, frame,
                                  near=ctx.near[0] if ctx and ctx.near else None)
        raise NotFound(msg, art)

    def wait(self, target, timeout: float | None = None) -> Match:
        return self.locate(target, timeout, action="wait")[0]

    def exists(self, target, timeout: float = 0.0) -> Match | None:
        try:
            return self.locate(target, timeout, stable=False, strict=False, action="exists",
                               quiet=True)[0]
        except NotFound:
            return None

    def count(self, target, region: Rect | None = None) -> int:
        return len(self.find_all(target, region=region))

    def wait_vanish(self, target, timeout: float | None = None) -> None:
        """Gone on two consecutive captures (a redraw flicker is not 'gone')."""
        loc = as_locator(target)
        timeout = self.timeout if timeout is None else float(timeout)
        deadline = time.monotonic() + timeout
        absent = 0
        while True:
            frame = self.capture()
            ms = loc.find(FindContext(frame, self))
            absent = 0 if ms else absent + 1
            if absent >= 2:
                return
            if time.monotonic() >= deadline:
                if ms:
                    msg = f"{loc.describe()} still visible after {timeout:g}s: {ms[0]}"
                    raise StillVisible(msg, self.tracer.failure("wait_vanish", loc.describe(), msg,
                                                                frame, [m.rect for m in ms]))
                return
            time.sleep(self.poll)

    def which(self, states: dict[str, Any], timeout: float | None = None,
              region: Rect | None = None) -> tuple[str, Match]:
        """Which of several looks is on screen: {'checked': Image('cb_on', color=True),
        'unchecked': Image('cb_off', color=True)} -> ('checked', match). Best score wins."""
        locs = {k: as_locator(v) for k, v in states.items()}
        timeout = self.timeout if timeout is None else float(timeout)
        deadline = time.monotonic() + timeout
        while True:
            frame = self.capture()
            best: tuple[str, Match] | None = None
            for name, loc in locs.items():
                for m in loc.find(FindContext(frame, self, region)):
                    if best is None or m.score > best[1].score:
                        best = (name, m)
            if best or time.monotonic() >= deadline:
                break
            time.sleep(self.poll)
        if best is None:
            msg = f"none of the states {list(locs)} is visible"
            raise NotFound(msg, self.tracer.failure("which", ",".join(locs), msg, frame))
        self.tracer.step("which", f"{best[0]}", frame, best[1].rect, note=str(best[1]))
        return best

    # ================================================================ waiting on the screen
    def wait_until_stable(self, region: Rect | None = None, quiet: float = 0.4,
                          timeout: float | None = None) -> Frame:
        """Return once `region` (default: whole screen) has not changed for `quiet`
        seconds. The generic replacement for 'sleep 2' after navigation."""
        timeout = self.timeout if timeout is None else float(timeout)
        deadline = time.monotonic() + timeout
        last = self.capture()
        area = region or last.rect
        since = time.monotonic()
        while True:
            time.sleep(min(self.poll, quiet / 2))
            cur = self.capture()
            if region_changed(last, cur, area):
                since = time.monotonic()
            last = cur
            if time.monotonic() - since >= quiet:
                return cur
            if time.monotonic() >= deadline:
                raise Unstable(f"screen region {area} still changing after {timeout:g}s",
                               self.tracer.failure("wait_until_stable", str(area),
                                                   "never settled", cur, [area]))

    def wait_for_change(self, reference: Frame, region: Rect | None = None,
                        timeout: float | None = None) -> Frame:
        timeout = self.timeout if timeout is None else float(timeout)
        deadline = time.monotonic() + timeout
        area = region or reference.rect
        while True:
            cur = self.capture()
            if region_changed(reference, cur, area):
                return cur
            if time.monotonic() >= deadline:
                raise SightlineError(f"screen region {area} did not change within {timeout:g}s",
                                     self.tracer.failure("wait_for_change", str(area),
                                                         "no change", cur, [area]))
            time.sleep(self.poll)

    # ================================================================ actions
    def _point(self, target, offset: tuple[int, int], timeout, action) -> tuple[Point, Match | None, Frame | None]:
        if isinstance(target, Point):
            return target.offset(*offset), None, None
        if isinstance(target, tuple) and len(target) == 2 and all(isinstance(v, int) for v in target):
            return Point(*target).offset(*offset), None, None
        if isinstance(target, Match):
            return target.center.offset(*offset), target, None
        m, frame = self.locate(target, timeout, action=action)
        return m.center.offset(*offset), m, frame

    def _park(self) -> None:
        if self.park_mouse:
            self.backend.mouse_move(*self.park_mouse)

    def click(self, target, button: str = "left", count: int = 1, offset: tuple[int, int] = (0, 0),
              timeout: float | None = None, expect=None, expect_timeout: float | None = None,
              vanish: bool = False, retries: int = 0) -> Match | None:
        """expect=locator: verify it appears after the click. vanish=True: verify the
        clicked element disappears (dialog buttons). retries=N: click again when the
        verification fails (a click swallowed while a dialog was still drawing)."""
        action = {1: "click", 2: "double_click"}.get(count, f"click x{count}")
        if button != "left":
            action = f"{button}_click"
        desc = _desc(target)
        for attempt in range(retries + 1):
            p, m, frame = self._point(target, offset, timeout, action)
            self.backend.click(p.x, p.y, button, count)
            self.tracer.step(action, desc, frame, m.rect if m else None, p,
                             note=str(m) if m else "")
            self._park()
            if expect is None and not vanish:
                return m
            ok, why = True, ""
            et = self.timeout if expect_timeout is None else expect_timeout
            if expect is not None and self.exists(expect, et) is None:
                ok, why = False, f"{_desc(expect)} did not appear"
            if ok and vanish:
                try:
                    self.wait_vanish(target, et)
                except StillVisible:
                    ok, why = False, f"{desc} did not disappear"
            if ok:
                return m
            if attempt < retries:
                self.tracer.note("retry", f"{action} {desc}: {why}, clicking again")
        frame = self.capture()
        msg = f"{action} {desc}: {why} (after {retries + 1} click(s))"
        raise SightlineError(msg, self.tracer.failure(action, desc, msg, frame))

    def double_click(self, target, **kw) -> Match | None:
        return self.click(target, count=2, **kw)

    def right_click(self, target, **kw) -> Match | None:
        return self.click(target, button="right", **kw)

    def hover(self, target, offset=(0, 0), timeout=None) -> Match | None:
        p, m, frame = self._point(target, offset, timeout, "hover")
        self.backend.mouse_move(p.x, p.y)
        self.tracer.step("hover", _desc(target), frame, m.rect if m else None, p)
        return m

    def drag(self, source, destination, src_offset=(0, 0), dst_offset=(0, 0),
             timeout=None, steps: int = 12) -> None:
        p0, m0, frame = self._point(source, src_offset, timeout, "drag")
        p1, _, _ = self._point(destination, dst_offset, timeout, "drag-to")
        self.backend.drag(p0.x, p0.y, p1.x, p1.y, steps=steps)
        self.tracer.step("drag", f"{_desc(source)} -> {_desc(destination)}", frame,
                         m0.rect if m0 else None, p1)
        self._park()

    def type(self, text: str, into=None, clear: bool | str = False, interval: float = 0.0,
             secret: bool = False) -> None:
        """clear=True / "line": End, Shift+Home, Delete - works in single-line fields of
        every toolkit (Ctrl+A is NOT select-all everywhere: Tk and emacs-style bindings
        move to line start). clear="all": Ctrl/Cmd+A then Delete (multi-line areas)."""
        if into is not None:
            self.click(into)
        if clear:
            if clear == "all":
                self.press("mod+a", "delete")
            else:
                self.press("end", "shift+home", "delete")
        self.backend.type_text(text, interval)
        self.tracer.step("type", "*" * 8 if secret else repr(text), None)

    def press(self, *combos: str) -> None:
        for c in combos:
            self.backend.hotkey(c)
            self.tracer.step("press", c, None)

    def scroll(self, at=None, clicks: int = 3, horizontal: bool = False) -> None:
        if at is None:
            w, h = self.backend.size()
            p = Point(w // 2, h // 2)
        else:
            p, _, _ = self._point(at, (0, 0), None, "scroll")
        self.backend.scroll(p.x, p.y, clicks, horizontal)
        self.tracer.step("scroll", f"{clicks:+d} at {_desc(at) if at is not None else 'center'}", None)

    def scroll_until_visible(self, target, at=None, clicks: int = 3, max_scrolls: int = 40,
                             direction: str = "down", both_ways: bool = True,
                             horizontal: bool = False) -> Match:
        """Scroll `at` (a locator of the scrolling area, or the screen centre) until
        `target` shows up. Stops a direction as soon as scrolling no longer changes the
        area (end of list reached) instead of blindly clicking N times, then tries the
        other direction."""
        loc = as_locator(target)
        area: Rect | None = None
        if at is not None:
            am, _ = self.locate(at, action="scroll-area")
            area = am.rect
        sign = 1 if direction in ("down", "right") else -1
        for s in ([sign, -sign] if both_ways else [sign]):
            for _ in range(max_scrolls):
                m = self.exists(loc)
                if m is not None:
                    return self.locate(loc, timeout=2, action="scroll-to")[0]
                before = self.capture()
                p = area.center if area else Point(before.width // 2, before.height // 2)
                self.backend.scroll(p.x, p.y, s * clicks, horizontal)
                try:
                    after = self.wait_until_stable(area, quiet=0.25, timeout=5)
                except Unstable:
                    after = self.capture()
                if not region_changed(before, after, area or before.rect):
                    break                                  # end of this direction
        return self.locate(loc, timeout=0, action="scroll_until_visible")[0]

    # ================================================================ reading
    def read_text(self, target, timeout: float | None = None) -> str:
        """OCR of one line inside the matched rectangle (a field, a cell, a label)."""
        if isinstance(target, Rect):
            frame, rect = self.capture(), target
        else:
            m, frame = self.locate(target, timeout, action="read_text")
            rect = m.rect
        text, conf = self.ocr.line(frame.crop(rect).pixels)
        self.tracer.step("read_text", _desc(target), frame, rect, note=f"{text!r} conf={conf:.2f}")
        return text

    def read_words(self, region: Rect | None = None):
        """All phrases OCR sees (text, screen rect) - for scraping and for debugging."""
        from .vision.ocr import group_phrases
        frame = self.capture()
        area = region or frame.rect
        return [(p.text, p.rect.offset(area.x, area.y))
                for p in group_phrases(self.ocr_words(frame, area))]

    # ================================================================ clipboard
    def _need(self, cap: str) -> None:
        if cap not in self.backend.capabilities:
            raise Unsupported(f"backend '{self.backend.name}' has no {cap} access to the target")

    def get_clipboard(self) -> str:
        self._need("clipboard")
        return self.backend.get_clipboard()

    def set_clipboard(self, text: str) -> None:
        self._need("clipboard")
        self.backend.set_clipboard(text)

    def copy(self, combo: str = "mod+c", timeout: float = 5.0) -> str:
        """Press copy on the TARGET and return what arrived in its clipboard. A sentinel
        is put first, so an old clipboard content is never mistaken for the copy, and the
        value is returned only once it stopped changing (big tables arrive in pieces)."""
        self._need("clipboard")
        if getattr(self.backend, "shares_user_clipboard", False):
            # local app: the clipboard is the USER's - give their content back afterwards
            saved = self.backend.get_clipboard()
            try:
                return self._copy(combo, timeout)
            finally:
                self.backend.set_clipboard(saved)
        return self._copy(combo, timeout)

    def _copy(self, combo: str, timeout: float) -> str:
        sentinel = f"sightline-{uuid.uuid4().hex}"
        self.backend.set_clipboard(sentinel)
        time.sleep(0.1)
        self.press(combo)
        deadline = time.monotonic() + timeout
        pressed_at = time.monotonic()
        last, same, presses = None, 0, 1
        while time.monotonic() < deadline:
            time.sleep(0.15)
            cur = self.backend.get_clipboard()
            if cur and cur != sentinel:
                same = same + 1 if cur == last else 0
                last = cur
                if same >= 1:
                    return cur
            elif time.monotonic() - pressed_at > 2.5:
                # nothing came: the copy can be lost (focus not settled yet, a remote
                # clipboard bridge still starting - x11vnc needs ~10 s after connecting).
                # Copying again is harmless.
                self.press(combo)
                pressed_at, presses = time.monotonic(), presses + 1
        if last:
            return last
        raise SightlineError(f"nothing arrived in the target clipboard {timeout:g}s after {combo} "
                             f"(pressed {presses} times): is copying allowed in this app/session?")

    def screenshot(self, path: str) -> str:
        return self.capture().save(path)


def _run_recovery_steps(session: Session, steps: list) -> None:
    from .flows import run_steps
    run_steps(session, steps, {})


def _desc(t) -> str:
    if isinstance(t, Locator):
        return t.describe()
    if isinstance(t, Match):
        return str(t)
    return str(t)


class Element:
    """A locator bound to a session; every call re-resolves it on a fresh capture."""

    def __init__(self, session: Session, locator: Locator):
        self.session, self.locator = session, locator

    def __repr__(self) -> str:
        return f"Element({self.locator.describe()})"

    def match(self, timeout=None) -> Match:
        return self.session.wait(self.locator, timeout)

    def click(self, **kw):
        return self.session.click(self.locator, **kw)

    def double_click(self, **kw):
        return self.session.double_click(self.locator, **kw)

    def right_click(self, **kw):
        return self.session.right_click(self.locator, **kw)

    def hover(self, **kw):
        return self.session.hover(self.locator, **kw)

    def type(self, text: str, clear: bool = False, **kw):
        return self.session.type(text, into=self.locator, clear=clear, **kw)

    def wait(self, timeout=None) -> Match:
        return self.session.wait(self.locator, timeout)

    def wait_vanish(self, timeout=None) -> None:
        self.session.wait_vanish(self.locator, timeout)

    def exists(self, timeout: float = 0.0) -> bool:
        return self.session.exists(self.locator, timeout) is not None

    def count(self) -> int:
        return self.session.count(self.locator)

    def read_text(self, timeout=None) -> str:
        return self.session.read_text(self.locator, timeout)

    def drag_to(self, other, **kw) -> None:
        self.session.drag(self.locator, other, **kw)

    def scroll_into_view(self, at=None, **kw) -> Match:
        return self.session.scroll_until_visible(self.locator, at, **kw)
