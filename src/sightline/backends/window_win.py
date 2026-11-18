"""Windows 'headless local': automate a LOCALLY INSTALLED app in the background.

    s = Session(WindowBackend(title="Gestion des commandes"))
    s = Session(WindowBackend(launch=r"C:\\Program Files\\App\\app.exe", title="App"))

- SEE: every visible window of the app's process (main window, dialogs, popup menus) is
  captured with PrintWindow(PW_RENDERFULLCONTENT) - which works while the window is BEHIND
  other windows or parked off-screen - and composited into one frame in screen coordinates.
- ACT: clicks, drags, wheel and keys are POSTED to the right child control of that window
  (window messages). The real cursor does not move, the app does not come to the front,
  the user keeps working. Modifier combos (Ctrl+C...) set the target thread's key state
  for the duration of the key (AttachThreadInput + SetKeyboardState).
- No admin rights, no driver, nothing installed: plain Win32 APIs.

Limits (measure them per app with `sightline doctor`):
- a MINIMIZED window draws nothing -> keep it behind others or off-screen instead;
- some apps ignore posted input (DirectX/game engines, some Chromium/Electron widgets,
  apps reading the physical key state); some draw blank through PrintWindow;
- the clipboard is the user's own (the app is local): it is saved and restored around
  copies by the session.
"""
from __future__ import annotations

import ctypes
import re
import subprocess
import sys
import time
from ctypes import wintypes

import numpy as np

from ..errors import SightlineError
from ..frame import Frame
from ..geometry import Point, Rect
from .base import Backend

WM_MOUSEMOVE, WM_LBUTTONDOWN, WM_LBUTTONUP, WM_LBUTTONDBLCLK = 0x200, 0x201, 0x202, 0x203
WM_RBUTTONDOWN, WM_RBUTTONUP, WM_RBUTTONDBLCLK = 0x204, 0x205, 0x206
WM_MBUTTONDOWN, WM_MBUTTONUP, WM_MOUSEWHEEL, WM_MOUSEHWHEEL = 0x207, 0x208, 0x20A, 0x20E
WM_KEYDOWN, WM_KEYUP, WM_CHAR, WM_SYSKEYDOWN, WM_SYSKEYUP = 0x100, 0x101, 0x102, 0x104, 0x105
WM_NULL = 0
MK = {"left": 0x1, "right": 0x2, "middle": 0x10}
WM_NCHITTEST, HTCLIENT, HTERROR, HTTRANSPARENT = 0x84, 1, -2, -1
WM_NCMOUSEMOVE = 0xA0
# non-client (title bar, menu bar, scrollbars, borders): what real input sends there
NC_MSG = {"left": (0xA1, 0xA2, 0xA3), "right": (0xA4, 0xA5, 0xA6), "middle": (0xA7, 0xA8, 0xA9)}
BTN_MSG = {"left": (WM_LBUTTONDOWN, WM_LBUTTONUP, WM_LBUTTONDBLCLK),
           "right": (WM_RBUTTONDOWN, WM_RBUTTONUP, WM_RBUTTONDBLCLK),
           "middle": (WM_MBUTTONDOWN, WM_MBUTTONUP, 0x209)}
VK = {"enter": 0x0D, "tab": 0x09, "esc": 0x1B, "backspace": 0x08, "delete": 0x2E,
      "insert": 0x2D, "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
      "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28, "space": 0x20,
      "shift": 0x10, "ctrl": 0x11, "alt": 0x12, "win": 0x5B, "cmd": 0x5B, "capslock": 0x14,
      "menu": 0x5D, "printscreen": 0x2C, **{f"f{i}": 0x6F + i for i in range(1, 13)}}
MODIFIERS = {"shift": 0x10, "ctrl": 0x11, "alt": 0x12}
EXTENDED = {0x2D, 0x2E, 0x24, 0x23, 0x21, 0x22, 0x25, 0x26, 0x27, 0x28, 0x5B, 0x5D}
PW_RENDERFULLCONTENT = 2
CS_DBLCLKS = 0x8


def _lp(x: int, y: int) -> int:
    return ((int(y) & 0xFFFF) << 16) | (int(x) & 0xFFFF)


class _Api:
    """ctypes prototypes, built once (64-bit safe handle types)."""

    def __init__(self):
        u, g, k = (ctypes.WinDLL(n, use_last_error=True) for n in ("user32", "gdi32", "kernel32"))
        self.u, self.g, self.k = u, g, k
        H, HW, P = wintypes.HANDLE, wintypes.HWND, ctypes.c_void_p
        sig = {
            (u, "GetDC"): (H, [HW]), (u, "ReleaseDC"): (ctypes.c_int, [HW, H]),
            (u, "PrintWindow"): (wintypes.BOOL, [HW, H, wintypes.UINT]),
            (u, "PostMessageW"): (wintypes.BOOL, [HW, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]),
            (u, "SendMessageTimeoutW"): (wintypes.LPARAM, [HW, wintypes.UINT, wintypes.WPARAM,
                                                         wintypes.LPARAM, wintypes.UINT,
                                                         wintypes.UINT, P]),
            (u, "ChildWindowFromPointEx"): (HW, [HW, wintypes.POINT, wintypes.UINT]),
            (u, "ScreenToClient"): (wintypes.BOOL, [HW, P]),
            (u, "GetWindowRect"): (wintypes.BOOL, [HW, P]),
            (u, "GetClientRect"): (wintypes.BOOL, [HW, P]),
            (u, "GetWindowThreadProcessId"): (wintypes.DWORD, [HW, P]),
            (u, "IsWindowVisible"): (wintypes.BOOL, [HW]), (u, "IsIconic"): (wintypes.BOOL, [HW]),
            (u, "IsWindow"): (wintypes.BOOL, [HW]),
            (u, "ShowWindow"): (wintypes.BOOL, [HW, ctypes.c_int]),
            (u, "SetWindowPos"): (wintypes.BOOL, [HW, HW, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                                  ctypes.c_int, wintypes.UINT]),
            (u, "GetWindowTextLengthW"): (ctypes.c_int, [HW]),
            (u, "GetWindowTextW"): (ctypes.c_int, [HW, P, ctypes.c_int]),
            (u, "GetClassNameW"): (ctypes.c_int, [HW, P, ctypes.c_int]),
            (u, "GetClassLongPtrW" if ctypes.sizeof(P) == 8 else "GetClassLongW"):
                (ctypes.c_size_t, [HW, ctypes.c_int]),
            (u, "GetGUIThreadInfo"): (wintypes.BOOL, [wintypes.DWORD, P]),
            (u, "AttachThreadInput"): (wintypes.BOOL, [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]),
            (u, "GetKeyboardState"): (wintypes.BOOL, [P]), (u, "SetKeyboardState"): (wintypes.BOOL, [P]),
            (u, "MapVirtualKeyW"): (wintypes.UINT, [wintypes.UINT, wintypes.UINT]),
            (u, "VkKeyScanW"): (ctypes.c_short, [wintypes.WCHAR]),
            (g, "CreateCompatibleDC"): (H, [H]), (g, "CreateCompatibleBitmap"): (H, [H, ctypes.c_int, ctypes.c_int]),
            (g, "SelectObject"): (H, [H, H]), (g, "DeleteObject"): (wintypes.BOOL, [H]),
            (g, "DeleteDC"): (wintypes.BOOL, [H]),
            (g, "BitBlt"): (wintypes.BOOL, [H, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                            H, ctypes.c_int, ctypes.c_int, wintypes.DWORD]),
            (u, "GetWindowDC"): (H, [HW]),
            (g, "GetDIBits"): (ctypes.c_int, [H, H, wintypes.UINT, wintypes.UINT, P, P, wintypes.UINT]),
            (k, "GetCurrentThreadId"): (wintypes.DWORD, []),
        }
        for (dll, name), (res, args) in sig.items():
            f = getattr(dll, name)
            f.restype, f.argtypes = res, args
        self.GetClassLong = getattr(u, "GetClassLongPtrW" if ctypes.sizeof(P) == 8 else "GetClassLongW")
        try:
            self.dwm = ctypes.WinDLL("dwmapi")
            self.dwm.DwmGetWindowAttribute.argtypes = [HW, wintypes.DWORD, P, wintypes.DWORD]
        except OSError:
            self.dwm = None
        try:                                    # Windows 10 1607+: per-window DPI context
            u.GetWindowDpiAwarenessContext.restype = H
            u.GetWindowDpiAwarenessContext.argtypes = [HW]
            u.SetThreadDpiAwarenessContext.restype = H
            u.SetThreadDpiAwarenessContext.argtypes = [H]
            self.dpi_ctx = True
        except AttributeError:
            self.dpi_ctx = False
        self.EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, HW, wintypes.LPARAM)


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD),
                ("biCompression", wintypes.DWORD), ("biSizeImage", wintypes.DWORD),
                ("biXPelsPerMeter", wintypes.LONG), ("biYPelsPerMeter", wintypes.LONG),
                ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]


class GUITHREADINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("flags", wintypes.DWORD),
                ("hwndActive", wintypes.HWND), ("hwndFocus", wintypes.HWND),
                ("hwndCapture", wintypes.HWND), ("hwndMenuOwner", wintypes.HWND),
                ("hwndMoveSize", wintypes.HWND), ("hwndCaret", wintypes.HWND),
                ("rcCaret", wintypes.RECT)]


def list_windows(api: _Api | None = None) -> list[dict]:
    """Visible top-level windows: hwnd, pid, title, class, rect, minimized (z-order, top first)."""
    api = api or _Api()
    u, out = api.u, []

    def cb(h, _):
        if u.IsWindowVisible(h):
            n = u.GetWindowTextLengthW(h)
            t = ctypes.create_unicode_buffer(n + 1)
            u.GetWindowTextW(h, t, n + 1)
            c = ctypes.create_unicode_buffer(256)
            u.GetClassNameW(h, c, 256)
            pid = wintypes.DWORD()
            u.GetWindowThreadProcessId(h, ctypes.byref(pid))
            r = wintypes.RECT()
            u.GetWindowRect(h, ctypes.byref(r))
            out.append({"hwnd": h, "pid": pid.value, "title": t.value, "class": c.value,
                        "rect": Rect.from_corners(r.left, r.top, r.right, r.bottom),
                        "minimized": bool(u.IsIconic(h))})
        return True
    u.EnumWindows(api.EnumProc(cb), 0)
    return out


class WindowBackend(Backend):
    name = "window"
    capabilities = frozenset({"mouse", "keyboard", "scroll", "clipboard"})
    shares_user_clipboard = True             # the session saves/restores the user's clipboard

    def __init__(self, title: str | None = None, launch: str | list[str] | None = None,
                 cwd: str | None = None, timeout: float = 30.0, regex: bool = False,
                 modifier_hold: float = 0.15, target_os: str = "windows",
                 restore_minimized: bool = False, reminimize: bool = True):
        """restore_minimized=True: if the app is minimized, restore it BEHIND all other windows
        without giving it focus (a minimized window draws nothing - with Citrix seamless the
        server even stops sending it - so it must be restored to be seen; you keep working).
        reminimize=True: when the session closes, minimize it again (without activating it)
        if sightline was the one that restored it - your taskbar looks as before the run."""
        super().__init__(target_os)
        self.restore_minimized = restore_minimized
        self.reminimize = reminimize
        self._restored_by_us = False
        if sys.platform != "win32":
            raise SightlineError("the 'window' backend is Windows-only (Linux: 'display' backend)")
        if not title and not launch:
            raise SightlineError("window backend needs title= (and/or launch=)")
        self.api = _Api()
        self.modifier_hold = modifier_hold
        self.fallback_capture = False   # True once an app needed the "uncovered only" capture
        self._mods: set[str] = set()
        self._buttons: set[str] = set()
        self._pos = (0, 0)
        self._down: tuple[int, str] | None = None        # (hwnd, button) of the pressed button
        self._last_target = None
        self.process = subprocess.Popen(launch, cwd=cwd) if launch else None
        pid = self.process.pid if self.process else None
        pat = re.compile(title if regex else re.escape(title), re.I) if title else None
        deadline = time.monotonic() + timeout
        while True:
            wins = [w for w in list_windows(self.api)
                    if (pat is None or pat.search(w["title"])) and w["rect"].w > 0
                    and (pat is not None or w["pid"] == pid)]
            if pid is not None and any(w["pid"] == pid for w in wins):
                # we launched it: bind to OUR process, not another window with the same title
                wins = [w for w in wins if w["pid"] == pid]
            if wins:
                break
            if time.monotonic() > deadline:
                what = f"matching {title!r}" if title else "of the launched process"
                raise SightlineError(f"no visible window {what} within {timeout:g}s "
                                     "(see `sightline windows`)")
            time.sleep(0.3)
        self.hwnd, self.pid = wins[0]["hwnd"], wins[0]["pid"]
        if self.api.dpi_ctx:            # speak the app's own coordinate space (DPI-unaware apps)
            ctx = self.api.u.GetWindowDpiAwarenessContext(self.hwnd)
            if ctx:
                self.api.u.SetThreadDpiAwarenessContext(ctx)

    # ================================================================== windows of the app
    def _app_windows(self) -> list[dict]:
        wins = [w for w in list_windows(self.api) if w["pid"] == self.pid and w["rect"].w > 1
                and w["rect"].h > 1]
        if not wins:
            raise SightlineError("the application has no visible window any more (closed?)")
        main = [w for w in wins if w["hwnd"] == self.hwnd]
        if main and main[0]["minimized"]:
            if not self.restore_minimized:
                raise SightlineError(
                    "the application window is MINIMIZED, so it draws nothing. Restore it (it can "
                    "stay behind other windows or be moved off-screen), or use "
                    "restore_minimized=True to let sightline restore it behind your windows.")
            self.restore_behind()
            return self._app_windows_after_restore()
        return [w for w in wins if not w["minimized"]]

    def restore_behind(self) -> None:
        """Un-minimize the app WITHOUT activating it and put it at the bottom of the window
        stack: it draws again, your current window keeps the focus and stays on top."""
        u = self.api.u
        self._restored_by_us = True
        u.ShowWindow(self.hwnd, 4)                               # SW_SHOWNOACTIVATE
        u.SetWindowPos(self.hwnd, 1, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)  # HWND_BOTTOM,
        time.sleep(0.6)                                          # NOSIZE|NOMOVE|NOACTIVATE

    def _app_windows_after_restore(self) -> list[dict]:
        wins = [w for w in list_windows(self.api) if w["pid"] == self.pid and w["rect"].w > 1
                and w["rect"].h > 1 and not w["minimized"]]
        if not wins:
            raise SightlineError("the application window could not be restored")
        return wins

    def _visible_rect(self, hwnd, rect: Rect) -> Rect:
        """Without Windows 10's invisible resize borders (they would paint black margins)."""
        if self.api.dwm is not None:
            r = wintypes.RECT()
            if self.api.dwm.DwmGetWindowAttribute(hwnd, 9, ctypes.byref(r), ctypes.sizeof(r)) == 0:
                vis = Rect.from_corners(r.left, r.top, r.right, r.bottom)
                return vis.intersect(rect) or rect
        return rect

    def _print(self, hwnd, w: int, h: int) -> np.ndarray:
        u, g = self.api.u, self.api.g
        screen = u.GetDC(None)
        mem = g.CreateCompatibleDC(screen)
        bmp = g.CreateCompatibleBitmap(screen, w, h)
        old = g.SelectObject(mem, bmp)
        bi = BITMAPINFOHEADER(40, w, -h, 1, 32, 0, 0, 0, 0, 0, 0)       # top-down rows
        buf = np.zeros((h, w, 4), np.uint8)

        def read():
            g.SelectObject(mem, old)
            g.GetDIBits(mem, bmp, 0, h, buf.ctypes.data, ctypes.byref(bi), 0)
            g.SelectObject(mem, bmp)
            return bool(buf[:, :, :3].any())
        try:
            # 1. compositor copy: works behind other windows / off-screen
            if u.PrintWindow(hwnd, mem, PW_RENDERFULLCONTENT) and read():
                return buf[:, :, :3].copy()
            if u.PrintWindow(hwnd, mem, 0) and read():
                return buf[:, :, :3].copy()
            # 2. the window's own pixels (GPU-drawn apps): only correct while UNCOVERED
            wdc = u.GetWindowDC(hwnd)
            if wdc:
                g.BitBlt(mem, 0, 0, w, h, wdc, 0, 0, 0x00CC0020)       # SRCCOPY
                u.ReleaseDC(hwnd, wdc)
                if read():
                    self.fallback_capture = True
                    return buf[:, :, :3].copy()
            return buf[:, :, :3].copy()
        finally:
            g.SelectObject(mem, old)
            g.DeleteObject(bmp)
            g.DeleteDC(mem)
            u.ReleaseDC(None, screen)

    def size(self) -> tuple[int, int]:
        b = self._bbox(self._app_windows())
        return b.w, b.h

    @staticmethod
    def _bbox(wins) -> Rect:
        r = wins[0]["rect"]
        for w in wins[1:]:
            r = r.union(w["rect"])
        return r

    def capture(self, region: Rect | None = None) -> Frame:
        wins = self._app_windows()
        box = self._bbox(wins)
        canvas = np.zeros((box.h, box.w, 3), np.uint8)
        drawn = False
        for w in reversed(wins):                         # bottom of the z-order first
            r = w["rect"]
            img = self._print(w["hwnd"], r.w, r.h)
            vis = self._visible_rect(w["hwnd"], r)
            sx, sy = vis.x - r.x, vis.y - r.y
            dx, dy = vis.x - box.x, vis.y - box.y
            canvas[dy:dy + vis.h, dx:dx + vis.w] = img[sy:sy + vis.h, sx:sx + vis.w]
            drawn = drawn or bool(img.any())
        if not drawn:
            raise SightlineError("the application draws nothing for background capture "
                                 "(PrintWindow returned blank pages): it cannot run headless "
                                 "with this backend - use 'local' (foreground) for it.")
        f = Frame(canvas, box.x, box.y, time.time())
        return f.crop(region) if region else f

    # ================================================================== input routing
    def _hit(self, x: int, y: int):
        """(control hwnd, client x, client y) under SCREEN point (x, y) among the app's
        windows, honouring their z-order."""
        u = self.api.u
        for w in self._app_windows():
            if not w["rect"].contains(Point(x, y)):
                continue
            h = w["hwnd"]
            while True:
                pt = wintypes.POINT(x, y)
                u.ScreenToClient(h, ctypes.byref(pt))
                child = u.ChildWindowFromPointEx(h, pt, 0x1 | 0x4)   # skip invisible/transparent
                if not child or child == h:
                    return h, pt.x, pt.y
                h = child
        return None

    def _nchit(self, hwnd, x: int, y: int) -> int:
        """WM_NCHITTEST at SCREEN (x, y): HTCLIENT (1) or a non-client part (menu bar 5,
        scrollbars 6/7, caption 2...)."""
        res = ctypes.c_size_t()
        ok = self.api.u.SendMessageTimeoutW(hwnd, WM_NCHITTEST, 0, _lp(x, y), 0x2, 1000,
                                            ctypes.byref(res))
        return ctypes.c_int16(res.value & 0xFFFF).value if ok else HTCLIENT

    def _client(self, hwnd, x: int, y: int) -> tuple[int, int]:
        pt = wintypes.POINT(x, y)
        self.api.u.ScreenToClient(hwnd, ctypes.byref(pt))
        return pt.x, pt.y

    def _wparam_buttons(self) -> int:
        v = 0
        for b in self._buttons:
            v |= MK[b]
        if "shift" in self._mods:
            v |= 0x4
        if "ctrl" in self._mods:
            v |= 0x8
        return v

    def _post(self, hwnd, msg, wp, lp) -> None:
        if not self.api.u.PostMessageW(hwnd, msg, wp, lp):
            raise SightlineError(f"could not post message 0x{msg:x} to the application "
                                 f"(error {ctypes.get_last_error()})")

    def _sync(self, hwnd) -> None:
        """Let the app process what was posted before the next step (best effort)."""
        res = ctypes.c_size_t()
        self.api.u.SendMessageTimeoutW(hwnd, WM_NULL, 0, 0, 0x2, 1000, ctypes.byref(res))

    # ================================================================== mouse
    def mouse_move(self, x: int, y: int) -> None:
        self._pos = (int(x), int(y))
        if self._down is not None:                     # dragging: the pressed control "captures"
            h = self._down[0]
            cx, cy = self._client(h, x, y)
        else:
            hit = self._hit(x, y)
            if hit is None:
                return
            h, cx, cy = hit
            nc = self._nchit(h, x, y)
            if nc != HTCLIENT:
                self._post(h, WM_NCMOUSEMOVE, nc & 0xFFFF, _lp(x, y))
                return
        self._post(h, WM_MOUSEMOVE, self._wparam_buttons(), _lp(cx, cy))

    def mouse_down(self, button: str = "left") -> None:
        hit = self._hit(*self._pos)
        if hit is None:
            raise SightlineError(f"no window of the application at {self._pos}")
        h, cx, cy = hit
        self._buttons.add(button)
        nc = self._nchit(h, *self._pos)
        self._down = (h, button, nc)
        self._last_target = h
        if nc != HTCLIENT:                       # menu bar, scrollbar, caption...
            self._post(h, NC_MSG[button][0], nc & 0xFFFF, _lp(*self._pos))
            return
        self._post(h, BTN_MSG[button][0], self._wparam_buttons(), _lp(cx, cy))

    def mouse_up(self, button: str = "left") -> None:
        h = self._down[0] if self._down else (self._hit(*self._pos) or (None,))[0]
        nc = self._down[2] if self._down else HTCLIENT
        self._buttons.discard(button)
        self._down = None
        if h:
            if nc != HTCLIENT:
                self._post(h, NC_MSG[button][1], nc & 0xFFFF, _lp(*self._pos))
            else:
                cx, cy = self._client(h, *self._pos)
                self._post(h, BTN_MSG[button][1], self._wparam_buttons(), _lp(cx, cy))
            self._sync(h)

    def click(self, x: int, y: int, button: str = "left", count: int = 1,
              interval: float = 0.08, hold: float = 0.03) -> None:
        self.mouse_move(x, y)
        self.mouse_down(button)
        self.mouse_up(button)
        if count >= 2:
            h = self._last_target
            nc = self._nchit(h, x, y)
            if nc != HTCLIENT:
                for _ in range(count - 1):
                    self._post(h, NC_MSG[button][2], nc & 0xFFFF, _lp(x, y))
                    self._post(h, NC_MSG[button][1], nc & 0xFFFF, _lp(x, y))
                self._sync(h)
                return
            cx, cy = self._client(h, x, y)
            dbl = bool(self.api.GetClassLong(h, -26) & CS_DBLCLKS)     # GCL_STYLE
            time.sleep(0.02)
            for _ in range(count - 1):
                # what Windows itself generates for a real double click
                self._buttons.add(button)
                self._post(h, BTN_MSG[button][2] if dbl else BTN_MSG[button][0],
                           self._wparam_buttons(), _lp(cx, cy))
                self._buttons.discard(button)
                self._post(h, BTN_MSG[button][1], self._wparam_buttons(), _lp(cx, cy))
            self._sync(h)

    def scroll(self, x: int, y: int, clicks: int, horizontal: bool = False) -> None:
        hit = self._hit(x, y)
        if hit is None:
            return
        h = hit[0]
        for _ in range(abs(int(clicks))):
            delta = -120 if clicks > 0 else 120
            if horizontal:
                delta = -delta
            wp = ((delta & 0xFFFF) << 16) | self._wparam_buttons()
            self._post(h, WM_MOUSEHWHEEL if horizontal else WM_MOUSEWHEEL, wp, _lp(x, y))
            time.sleep(0.02)
        self._sync(h)

    # ================================================================== keyboard
    def _focus(self):
        u, tid = self.api.u, self.api.u.GetWindowThreadProcessId(self.hwnd, None)
        gi = GUITHREADINFO(cbSize=ctypes.sizeof(GUITHREADINFO))
        if u.GetGUIThreadInfo(tid, ctypes.byref(gi)) and gi.hwndFocus:
            return gi.hwndFocus
        return self._last_target or self.hwnd

    def _key_lparam(self, vk: int, up: bool) -> int:
        scan = self.api.u.MapVirtualKeyW(vk, 0) & 0xFF
        lp = 1 | (scan << 16) | ((1 << 24) if vk in EXTENDED else 0)
        if "alt" in self._mods:
            lp |= 1 << 29
        if up:
            lp |= (1 << 30) | (1 << 31)
        return lp

    def _vk(self, key: str) -> int:
        if key in VK:
            return VK[key]
        if len(key) == 1:
            r = self.api.u.VkKeyScanW(key)
            if r == -1:
                raise SightlineError(f"no key for '{key}' on this keyboard layout")
            return r & 0xFF
        raise SightlineError(f"unknown key {key!r}")

    def _with_modifiers(self, fn) -> None:
        """Make the app's thread see the held modifiers (GetKeyState) while it handles fn's
        messages: share input state with it and set the key state, then restore."""
        if not self._mods:
            fn()
            return
        u, k = self.api.u, self.api.k
        mine, theirs = k.GetCurrentThreadId(), u.GetWindowThreadProcessId(self.hwnd, None)
        attached = bool(u.AttachThreadInput(mine, theirs, True))
        state = (ctypes.c_ubyte * 256)()
        u.GetKeyboardState(state)
        saved = bytes(state)
        for m in self._mods:
            state[MODIFIERS[m]] |= 0x80
            state[{"shift": 0xA0, "ctrl": 0xA2, "alt": 0xA4}[m]] |= 0x80
        u.SetKeyboardState(state)
        try:
            fn()
            time.sleep(self.modifier_hold)
        finally:
            u.SetKeyboardState((ctypes.c_ubyte * 256).from_buffer_copy(saved))
            if attached:
                u.AttachThreadInput(mine, theirs, False)

    def key_down(self, key: str) -> None:
        if key in MODIFIERS:
            self._mods.add(key)
            return
        vk, h = self._vk(key), self._focus()
        msg = WM_SYSKEYDOWN if "alt" in self._mods else WM_KEYDOWN
        self._with_modifiers(lambda: (self._post(h, msg, vk, self._key_lparam(vk, False)),
                                      self._sync(h)))

    def key_up(self, key: str) -> None:
        if key in MODIFIERS:
            self._mods.discard(key)
            return
        vk, h = self._vk(key), self._focus()
        msg = WM_SYSKEYUP if "alt" in self._mods else WM_KEYUP
        self._with_modifiers(lambda: self._post(h, msg, vk, self._key_lparam(vk, True)))

    def hotkey(self, combo: str) -> None:
        from .base import parse_combo
        keys = parse_combo(combo, self.target_os)
        mods = [k for k in keys if k in MODIFIERS]
        rest = [k for k in keys if k not in MODIFIERS]
        self._mods.update(mods)
        try:
            for k in rest:
                vk, h = self._vk(k), self._focus()
                down, up = (WM_SYSKEYDOWN, WM_SYSKEYUP) if "alt" in self._mods else (WM_KEYDOWN, WM_KEYUP)

                def send(vk=vk, h=h, down=down, up=up):
                    self._post(h, down, vk, self._key_lparam(vk, False))
                    self._post(h, up, vk, self._key_lparam(vk, True))
                    self._sync(h)
                self._with_modifiers(send)
        finally:
            self._mods.difference_update(mods)

    def type_text(self, text: str, interval: float = 0.0) -> None:
        h = self._focus()
        for ch in text:
            if ch in "\n\r":
                self.hotkey("enter")
                continue
            if ch == "\t":
                self.hotkey("tab")
                continue
            for unit in _utf16_units(ch):              # surrogate pairs: 2 messages
                self._post(h, WM_CHAR, unit, 1)
            if interval:
                time.sleep(interval)
        self._sync(h)

    # ================================================================== clipboard (local)
    def get_clipboard(self) -> str:
        from .local import _win_clipboard_get
        return _win_clipboard_get()

    def set_clipboard(self, text: str) -> None:
        from .local import _win_clipboard_set
        _win_clipboard_set(text)

    def close(self) -> None:
        # never closes the user's application; puts it back the way it was found
        if self._restored_by_us and self.reminimize and self.api.u.IsWindow(self.hwnd):
            self.api.u.ShowWindow(self.hwnd, 7)                  # SW_SHOWMINNOACTIVE
            self._restored_by_us = False


def _utf16_units(ch: str) -> list[int]:
    b = ch.encode("utf-16-le")
    return [int.from_bytes(b[i:i + 2], "little") for i in range(0, len(b), 2)]
