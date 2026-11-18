"""A remote session rendered inside a HEADLESS browser.

Remote-desktop products have web clients that draw the remote screen into a page:
Citrix Workspace app for HTML5 ("light version"), VMware Horizon HTML Access, Azure
Virtual Desktop / Windows 365 web client, Apache Guacamole, noVNC... Opened in a headless
browser, the session:

  - is drawn with NO window on your screen (nothing to lock, nothing to hide);
  - receives mouse/keyboard events through the browser's DevTools protocol, delivered to
    that page only - your own mouse, keyboard and clipboard are never touched;
  - needs NO admin rights: pip packages + a browser already on the machine
    (Edge on every Windows: channel="msedge"), running as your normal user.

The session itself is still pixels, so every locator, OCR and component works unchanged.

Login (SSO/MFA) is done ONCE by a human in a visible window with the same
`user_data_dir` (`sightline browser-login`), then headless runs reuse the cookies until
the portal's session expires.
"""
from __future__ import annotations

import os
import time
from collections.abc import Callable

import cv2
import numpy as np

from ..errors import SightlineError, Unsupported
from ..frame import Frame
from ..geometry import Rect
from .base import Backend

PW_KEYS = {
    "ctrl": "Control", "shift": "Shift", "alt": "Alt", "cmd": "Meta", "win": "Meta",
    "enter": "Enter", "tab": "Tab", "esc": "Escape", "backspace": "Backspace",
    "delete": "Delete", "insert": "Insert", "home": "Home", "end": "End",
    "pageup": "PageUp", "pagedown": "PageDown", "up": "ArrowUp", "down": "ArrowDown",
    "left": "ArrowLeft", "right": "ArrowRight", "space": " ", "capslock": "CapsLock",
    "menu": "ContextMenu", "printscreen": "PrintScreen",
    **{f"f{i}": f"F{i}" for i in range(1, 13)},
}

# How each web client exposes the REMOTE clipboard to the page.
CLIPBOARD_JS = {
    # async clipboard API of the page (Citrix HTML5, AVD web client, Guacamole...; permission
    # granted). After writing, a window 'focus' event: web clients push the local clipboard
    # to the remote session on focus (what a user switching back to the tab triggers). Without
    # it the remote clipboard keeps its old owner, and an app that copies again while it
    # already owns the clipboard (Tk, other X11 apps) never notifies the gateway: only the
    # first copy would arrive - measured with Guacamole over both VNC and RDP.
    "navigator": ("() => navigator.clipboard.readText()",
                  "t => navigator.clipboard.writeText(t)"
                  ".then(() => window.dispatchEvent(new Event('focus')))"),
    # noVNC: its clipboard panel mirrors the remote clipboard
    "novnc": ("() => document.getElementById('noVNC_clipboard_text').value",
              """t => { const e = document.getElementById('noVNC_clipboard_text');
                        e.value = t; e.dispatchEvent(new Event('change')); }"""),
}


class BrowserBackend(Backend):
    name = "browser"
    capabilities = frozenset({"mouse", "keyboard", "scroll", "clipboard"})

    def __init__(self, url: str | None = None, headless: bool = True, channel: str | None = None,
                 executable_path: str | None = None, user_data_dir: str | None = None,
                 viewport: tuple[int, int] | list[int] = (1280, 800), target_os: str = "windows",
                 clipboard: str | tuple[str, str] | None = "navigator",
                 browser: str = "chromium", launch_args: list[str] | None = None,
                 on_page: Callable | None = None, follow_new_pages: bool = True):
        super().__init__(target_os)
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise SightlineError("Browser backend needs: pip install playwright") from exc
        self._pw = sync_playwright().start()
        kind = getattr(self._pw, browser)
        w, h = (int(v) for v in viewport)
        opts = dict(headless=headless, args=launch_args or [])
        if channel:
            opts["channel"] = channel                  # "msedge" / "chrome": installed browser
        if executable_path:
            opts["executable_path"] = executable_path
        try:
            if user_data_dir:                          # persistent profile: keeps SSO cookies
                self._browser = None
                self.context = kind.launch_persistent_context(
                    os.path.expanduser(user_data_dir), viewport={"width": w, "height": h}, **opts)
                self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
            else:
                self._browser = kind.launch(**opts)
                self.context = self._browser.new_context(viewport={"width": w, "height": h})
                self.page = self.context.new_page()
        except Exception:
            self._pw.stop()
            raise
        if browser == "chromium":
            try:
                self.context.grant_permissions(["clipboard-read", "clipboard-write"])
            except Exception:
                pass
        self._clip = CLIPBOARD_JS.get(clipboard, clipboard) if isinstance(clipboard, str) else clipboard
        if self._clip is None:
            self.capabilities = frozenset({"mouse", "keyboard", "scroll"})
        self._w, self._h = w, h
        if follow_new_pages:
            # portals (Citrix StoreFront...) open the session in a NEW tab: drive the newest
            self.context.on("page", self._adopt)
        if on_page:
            on_page(self.page)                         # e.g. DOM steps of a login portal
        if url:
            self.page.goto(url)

    def _adopt(self, page) -> None:
        try:
            page.set_viewport_size({"width": self._w, "height": self._h})
            page.wait_for_load_state("domcontentloaded")
        except Exception:
            pass
        self.page = page
        page.on("close", lambda _p: self._fallback_page(_p))

    def _fallback_page(self, closed) -> None:
        if self.page is closed:
            alive = [p for p in self.context.pages if not p.is_closed()]
            if alive:
                self.page = alive[-1]

    # ------------------------------------------------------------------ screen
    def size(self) -> tuple[int, int]:
        return self._w, self._h

    def capture(self, region: Rect | None = None) -> Frame:
        clip = None
        ox = oy = 0
        if region is not None:
            r = region.intersect(Rect(0, 0, self._w, self._h)) or Rect(0, 0, 1, 1)
            clip, ox, oy = {"x": r.x, "y": r.y, "width": r.w, "height": r.h}, r.x, r.y
        png = self.page.screenshot(clip=clip, type="png", animations="allow", caret="initial")
        img = cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_COLOR)
        return Frame(img, ox, oy, time.time())

    # ------------------------------------------------------------------ input (page only)
    def mouse_move(self, x: int, y: int) -> None:
        self.page.mouse.move(x, y)

    def mouse_down(self, button: str = "left") -> None:
        self.page.mouse.down(button=button)

    def mouse_up(self, button: str = "left") -> None:
        self.page.mouse.up(button=button)

    def scroll(self, x: int, y: int, clicks: int, horizontal: bool = False) -> None:
        self.page.mouse.move(x, y)
        for _ in range(abs(int(clicks))):
            d = 100 if clicks > 0 else -100              # one wheel notch
            self.page.mouse.wheel(d if horizontal else 0, 0 if horizontal else d)
            time.sleep(0.03)

    def _key(self, key: str) -> str:
        return PW_KEYS.get(key, key)

    def key_down(self, key: str) -> None:
        self.page.keyboard.down(self._key(key))

    def key_up(self, key: str) -> None:
        self.page.keyboard.up(self._key(key))

    def type_text(self, text: str, interval: float = 0.0) -> None:
        # one real keydown/keyup per character (not insertText): remote-desktop web clients
        # translate KEY events into remote keystrokes, they ignore text insertion.
        for ch in text:
            self.page.keyboard.press({"\n": "Enter", "\t": "Tab"}.get(ch, ch))
            if interval:
                time.sleep(interval)

    # ------------------------------------------------------------------ remote clipboard
    def get_clipboard(self) -> str:
        if self._clip is None:
            raise Unsupported("browser backend: no clipboard adapter configured")
        return str(self.page.evaluate(self._clip[0]) or "").replace("\r\n", "\n")

    def set_clipboard(self, text: str) -> None:
        if self._clip is None:
            raise Unsupported("browser backend: no clipboard adapter configured")
        self.page.evaluate(self._clip[1], text)

    def close(self) -> None:
        try:
            self.context.close()
            if self._browser is not None:
                self._browser.close()
        finally:
            self._pw.stop()
