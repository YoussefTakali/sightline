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
import json
import logging
import os
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

log = logging.getLogger(__name__)

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
            (u, "GetWindowPlacement"): (wintypes.BOOL, [HW, P]),
            (u, "SetWindowPlacement"): (wintypes.BOOL, [HW, P]),
            (u, "MonitorFromWindow"): (H, [HW, wintypes.DWORD]),
            (u, "GetMonitorInfoW"): (wintypes.BOOL, [H, P]),
            (u, "IsZoomed"): (wintypes.BOOL, [HW]),
            (u, "GetAncestor"): (HW, [HW, wintypes.UINT]),
            (u, "SetLayeredWindowAttributes"): (wintypes.BOOL, [HW, wintypes.DWORD, wintypes.BYTE,
                                                                wintypes.DWORD]),
            (u, "GetLayeredWindowAttributes"): (wintypes.BOOL, [HW, P, P, P]),
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
        wide = ctypes.sizeof(P) == 8
        self.GetWindowLong = getattr(u, "GetWindowLongPtrW" if wide else "GetWindowLongW")
        self.SetWindowLong = getattr(u, "SetWindowLongPtrW" if wide else "SetWindowLongW")
        self.GetWindowLong.restype = ctypes.c_ssize_t
        self.GetWindowLong.argtypes = [HW, ctypes.c_int]
        self.SetWindowLong.restype = ctypes.c_ssize_t
        self.SetWindowLong.argtypes = [HW, ctypes.c_int, ctypes.c_ssize_t]
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


class WINDOWPLACEMENT(ctypes.Structure):
    _fields_ = [("length", wintypes.UINT), ("flags", wintypes.UINT), ("showCmd", wintypes.UINT),
                ("ptMinPosition", wintypes.POINT), ("ptMaxPosition", wintypes.POINT),
                ("rcNormalPosition", wintypes.RECT)]


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]


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
    _ladder = ("post", "send", "real")       # input modes, from least to most intrusive

    _private_desktop = False                 # True: every window on the desktop is the app's

    @staticmethod
    def _launch(launch, cwd):
        cmd, flags = shell_command(launch)
        return subprocess.Popen(cmd, cwd=cwd, creationflags=flags)

    def __init__(self, title: str | None = None, launch: str | list[str] | None = None,
                 cwd: str | None = None, timeout: float = 30.0, regex: bool = False,
                 modifier_hold: float = 0.15, target_os: str = "windows",
                 restore_minimized: bool | str = False, reminimize: bool = True,
                 input_mode: str = "auto", idle_ms: int = 800, match=None, abort_if=None,
                 hide_launched: bool = False, close_launched: bool = False):
        """A minimized window draws nothing (with Citrix seamless the server even stops sending
        it), so to be seen it must not stay minimized:
        restore_minimized="invisible": made fully TRANSPARENT first, then restored at the
            bottom of the window stack - it draws (sightline sees it), you never see it;
        restore_minimized=True / "behind": restored behind your windows (visible behind them).
        Real input in invisible mode puts the window on top at opacity 1/255 for the click -
        it never visibly appears. reminimize=True: at the end it is minimized again and its
        opacity put back - your taskbar looks as before the run."""
        # match(window) -> bool: extra window filter; abort_if() -> bool: give up waiting
        # (AppElsewhere); hide_launched: an app WE launch on the user's desktop is made
        # invisible at once; close_launched: closed with the session
        super().__init__(target_os)
        self.close_launched = close_launched
        if restore_minimized not in (False, True, "behind", "invisible"):
            raise SightlineError("restore_minimized must be False, True/'behind' or 'invisible'")
        self._ghost_saved = None
        if input_mode != "auto" and input_mode not in self._ladder:
            raise SightlineError(f"input_mode must be 'auto' or one of {self._ladder}")
        # post: queued window messages (never interrupts; some apps ignore them)
        # send: synchronous messages + the activation messages real input generates
        # real: polite REAL input - waits for the user to be idle, brings the app forward for
        #       a fraction of a second, clicks/types, gives the user back window and cursor
        # auto: learns which one THIS app obeys (see _auto) and remembers it per app
        self.requested_mode = input_mode
        self.idle_ms = idle_ms
        self._real_engine = None
        self._real = None
        self._confirmed = input_mode != "auto"
        self._use_mode("post" if input_mode == "auto" else input_mode)
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
        self.process = self._launch(launch, cwd) if launch else None
        pid = self.process.pid if self.process else None
        pat = re.compile(title if regex else re.escape(title), re.I) if title else None
        deadline = time.monotonic() + timeout
        while True:
            wins = [w for w in list_windows(self.api)
                    if (pat is None or pat.search(w["title"])) and w["rect"].w > 0
                    and (match(w) if match else
                         (pat is not None or w["pid"] == pid
                          or (self._private_desktop and w["class"] != "ConsoleWindowClass")))]
            if pid is not None and any(w["pid"] == pid for w in wins):
                # we launched it: bind to OUR process, not another window with the same title
                wins = [w for w in wins if w["pid"] == pid]
            if wins:
                break
            if abort_if is not None and abort_if():
                raise AppElsewhere("the app opened somewhere else")
            if time.monotonic() > deadline:
                what = f"matching {title!r}" if title else "of the launched process"
                hint = (" - if a launcher/shortcut starts the app as ANOTHER process, give its "
                        "window title=" if not title and not self._private_desktop else "")
                raise SightlineError(f"no visible window {what} within {timeout:g}s "
                                     f"(see `sightline windows`){hint}")
            time.sleep(0.3)
        self.hwnd, self.pid = wins[0]["hwnd"], wins[0]["pid"]
        self.title = wins[0]["title"]
        if hide_launched and self.process is not None:
            # we just started it on the user's desktop: out of sight at once, still drawing
            self.restore_minimized = self.restore_minimized or "invisible"
            if self._make_invisible():
                self.api.u.SetWindowPos(self.hwnd, 1, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)
        self.app_key = f"{_exe_name(self.pid)}|{wins[0]['class']}"
        if self.requested_mode == "auto":
            learned = _load_modes().get(self.app_key)
            if learned in self._ladder:
                self._use_mode(learned)
                self._confirmed = True                    # known: never escalate (no double clicks)
        if self.api.dpi_ctx:            # speak the app's own coordinate space (DPI-unaware apps)
            ctx = self.api.u.GetWindowDpiAwarenessContext(self.hwnd)
            if ctx:
                self.api.u.SetThreadDpiAwarenessContext(ctx)

    # ================================================================== windows of the app
    def _app_windows(self) -> list[dict]:
        wins = [w for w in list_windows(self.api)
                if (self._private_desktop or w["pid"] == self.pid) and w["rect"].w > 1
                and w["rect"].h > 1]
        if not wins:
            raise SightlineError("the application has no visible window any more (closed?)")
        if self._private_desktop and not self.api.u.IsWindow(self.hwnd):
            # the window we bound to is gone (a "Connecting..." dialog, a splash screen):
            # every window here is the app's - follow its biggest one
            big = max(wins, key=lambda w: w["rect"].w * w["rect"].h)
            self.hwnd, self.pid = big["hwnd"], big["pid"]
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
        wp = WINDOWPLACEMENT(length=ctypes.sizeof(WINDOWPLACEMENT))
        u.GetWindowPlacement(self.hwnd, ctypes.byref(wp))
        self._placement = wp                                     # to put it back exactly
        self._restored_by_us = True
        if self.restore_minimized == "invisible":
            self._make_invisible()                               # BEFORE it appears: no flash
        u.ShowWindow(self.hwnd, 4)                               # SW_SHOWNOACTIVATE
        if wp.flags & 0x2:                                       # WPF_RESTORETOMAXIMIZED
            # it was FULL SCREEN (maximized) before being minimized. Windows has no "maximize
            # without activating": give it exactly the monitor's work area instead, so the
            # app (and Citrix, which mirrors the size) lays out exactly as when maximized
            mi = MONITORINFO(cbSize=ctypes.sizeof(MONITORINFO))
            u.GetMonitorInfoW(u.MonitorFromWindow(self.hwnd, 2), ctypes.byref(mi))  # NEAREST
            r = mi.rcWork
            u.SetWindowPos(self.hwnd, 1, r.left, r.top, r.right - r.left, r.bottom - r.top,
                           0x0010)                               # HWND_BOTTOM, NOACTIVATE
        else:
            u.SetWindowPos(self.hwnd, 1, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)  # HWND_BOTTOM,
        time.sleep(0.6)                                          # NOSIZE|NOMOVE|NOACTIVATE

    # ================================================================== invisible mode
    def _make_invisible(self) -> bool:
        """Opacity 0 (layered window), keeping any colour key the app already uses (Citrix
        seamless windows can be layered). False if the window manages its own layering."""
        if self._ghost_saved is not None:
            return True
        u, h = self.api.u, self.hwnd
        ex = self.api.GetWindowLong(h, -20)                      # GWL_EXSTYLE
        layered = bool(ex & 0x80000)                             # WS_EX_LAYERED
        key, alpha, flags = wintypes.DWORD(), wintypes.BYTE(), wintypes.DWORD()
        if layered and not u.GetLayeredWindowAttributes(h, ctypes.byref(key), ctypes.byref(alpha),
                                                        ctypes.byref(flags)):
            log.warning("window uses per-pixel layering: cannot make it invisible")
            return False
        self._ghost_saved = (ex, layered, key.value, alpha.value if flags.value & 2 else 255,
                             flags.value)
        if not layered:
            self.api.SetWindowLong(h, -20, ex | 0x80000)
        self._set_opacity(0)
        return True

    def _set_opacity(self, a: int) -> None:
        ex, layered, key, _alpha, flags = self._ghost_saved
        self.api.u.SetLayeredWindowAttributes(self.hwnd, key, a, (flags & 0x1) | 0x2)  # +LWA_ALPHA

    def _restore_opacity(self) -> None:
        if self._ghost_saved is None:
            return
        ex, layered, key, alpha, flags = self._ghost_saved
        if layered:
            self.api.u.SetLayeredWindowAttributes(self.hwnd, key, alpha, flags or 0x2)
        else:
            self._set_opacity(255)
            self.api.SetWindowLong(self.hwnd, -20, ex)
        self._ghost_saved = None

    def _ghost_on_top(self) -> None:
        """For a real click on an invisible window: on top, at opacity 1/255 (receives the
        click, cannot be seen)."""
        self._set_opacity(1)
        self.api.u.SetWindowPos(self.hwnd, -1, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)  # TOPMOST

    def _ghost_back(self) -> None:
        u = self.api.u
        u.SetWindowPos(self.hwnd, -2, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)   # NOTOPMOST
        u.SetWindowPos(self.hwnd, 1, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)    # BOTTOM
        self._set_opacity(0)

    def _burst(self, hwnd):
        """Real-input burst; invisible windows are clicked without ever being seen."""
        if self._ghost_saved is not None:
            return self._real.burst(self.hwnd, before=self._ghost_on_top, after=self._ghost_back)
        return self._real.burst(hwnd)

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
        if self.input_mode == "send":
            res = ctypes.c_size_t()
            if not self.api.u.SendMessageTimeoutW(hwnd, msg, wp, lp, 0x2, 2000, ctypes.byref(res)):
                raise SightlineError(f"the application did not process message 0x{msg:x} "
                                     f"(error {ctypes.get_last_error()})")
            return
        if not self.api.u.PostMessageW(hwnd, msg, wp, lp):
            raise SightlineError(f"could not post message 0x{msg:x} to the application "
                                 f"(error {ctypes.get_last_error()})")

    # ================================================================== input mode learning
    def _use_mode(self, mode: str) -> None:
        self.input_mode = mode
        if mode == "real" and self._real_engine is None:
            from ._wininput import RealInput
            self._real_engine = RealInput(idle_ms=self.idle_ms)
        self._real = self._real_engine if mode == "real" else None

    def _auto(self, action, what: str) -> None:
        """Do `action` with the current mode; if the app shows NO reaction, redo it with the
        next mode (post -> send -> real). The first mode the app obeys is kept for this app
        and saved, so next runs start with it and never escalate again (no double clicks)."""
        ladder = list(self._ladder)
        start = ladder.index(self.input_mode)
        for mode in ladder[start:]:
            self._use_mode(mode)
            before, noise = self._quiet_reference()
            action()
            if self._reacted(before, noise):
                self._confirmed = True
                _save_mode(self.app_key, mode)
                log.info("input mode for %s: %s (learned on %s)", self.app_key, mode, what)
                return
            log.info("%s: no reaction to %s with input mode %s", self.app_key, what, mode)
        self._use_mode(ladder[start])                    # nothing reacted: maybe a no-op action
        self._no_reaction(what)

    def _no_reaction(self, what: str) -> None:
        log.warning("%s: no visible reaction to %s in any input mode", self.app_key, what)

    def _quiet_reference(self):
        """A capture of the app plus the pixels that change ON THEIR OWN (blinking caret,
        animations), so they are not taken for a reaction."""
        import cv2
        a = self.capture()
        time.sleep(0.6)
        b = self.capture()
        if a.pixels.shape != b.pixels.shape:
            return b, None
        noise = (cv2.absdiff(a.gray, b.gray) > 24).astype(np.uint8)
        return b, cv2.dilate(noise, np.ones((7, 7), np.uint8))

    def _reacted(self, before, noise, timeout: float = 2.0) -> bool:
        import cv2
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            time.sleep(0.25)
            now = self.capture()
            if (now.pixels.shape != before.pixels.shape
                    or (now.origin_x, now.origin_y) != (before.origin_x, before.origin_y)):
                return True                                  # a dialog/menu opened, the app moved
            diff = (cv2.absdiff(now.gray, before.gray) > 24).astype(np.uint8)
            if noise is not None:
                diff[noise > 0] = 0
            if int(diff.sum()) > 20:
                return True
        return False

    def forget_input_mode(self) -> None:
        """Forget what was learned for this app (it will be learned again)."""
        modes = _load_modes()
        modes.pop(self.app_key, None)
        _write_modes(modes)
        self._confirmed = self.requested_mode != "auto"
        self._use_mode("post")

    def _top_at(self, x: int, y: int):
        """The app's top-level window under screen (x, y) (a dialog or the main window)."""
        for w in self._app_windows():
            if w["rect"].contains(Point(x, y)):
                return w["hwnd"]
        return self.hwnd

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
        if self.input_mode == "send":
            # what Windows sends before a real click reaches a window
            msg = BTN_MSG[button][0]
            self._post(h, 0x0021, self._top_at(*self._pos), (msg << 16) | HTCLIENT)  # MOUSEACTIVATE
            self._post(h, 0x0020, h, (msg << 16) | HTCLIENT)                       # SETCURSOR
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
        if not self._confirmed:
            return self._auto(lambda: self._click_once(x, y, button, count, interval, hold),
                              f"click at ({x},{y})")
        self._click_once(x, y, button, count, interval, hold)

    def _click_once(self, x: int, y: int, button: str = "left", count: int = 1,
                    interval: float = 0.08, hold: float = 0.03) -> None:
        if self._real is not None:
            with self._burst(self._top_at(x, y)):
                self._real.click(x, y, button, count)
            return
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

    def drag(self, x0: int, y0: int, x1: int, y1: int, button: str = "left",
             steps: int = 12, hold: float = 0.15) -> None:
        if self._real is None:
            return super().drag(x0, y0, x1, y1, button, steps, hold)
        with self._burst(self._top_at(x0, y0)):
            self._real.move(x0, y0)
            time.sleep(0.05)
            self._real.button(button, True)
            time.sleep(hold)
            for i in range(1, steps + 1):
                self._real.move(round(x0 + (x1 - x0) * i / steps), round(y0 + (y1 - y0) * i / steps))
                time.sleep(0.015)
            time.sleep(hold)
            self._real.button(button, False)

    def scroll(self, x: int, y: int, clicks: int, horizontal: bool = False) -> None:
        if self._real is not None:
            with self._burst(self._top_at(x, y)):
                self._real.wheel(x, y, clicks, horizontal)
            return
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
        if self._real is not None:
            with self._burst(self._focus_top()):
                for m in mods:
                    self._real.vk(MODIFIERS[m], True)
                for k in rest:
                    vk = self._vk(k)
                    self._real.vk(vk, True, vk in EXTENDED)
                    self._real.vk(vk, False, vk in EXTENDED)
                for m in reversed(mods):
                    self._real.vk(MODIFIERS[m], False)
            return
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

    def _focus_top(self):
        """Top-level window that holds the keyboard focus (a dialog, or the main window)."""
        h = self._focus()
        return (self.api.u.GetAncestor(h, 2) or h) if h else self.hwnd   # GA_ROOT

    def type_text(self, text: str, interval: float = 0.0) -> None:
        if not self._confirmed and text.strip():
            return self._auto(lambda: self._type_once(text, interval), f"typing {len(text)} chars")
        self._type_once(text, interval)

    def _type_once(self, text: str, interval: float = 0.0) -> None:
        if self._real is not None:
            with self._burst(self._focus_top()):
                self._real.text(text, interval)
            return
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
            u = self.api.u
            wp = getattr(self, "_placement", None)
            if wp is not None:
                # the EXACT original state: minimized, normal size, and "reopens maximized"
                wp.length = ctypes.sizeof(WINDOWPLACEMENT)
                wp.showCmd = 7                                   # SW_SHOWMINNOACTIVE
                u.SetWindowPlacement(self.hwnd, ctypes.byref(wp))
            if not u.IsIconic(self.hwnd):                        # placement refused: still minimize
                u.ShowWindow(self.hwnd, 7)
            self._restored_by_us = False
        if self.close_launched and self.process is not None:
            _kill(self.pid)                                      # the app (maybe via a launcher)
            try:
                self.process.terminate()
            except OSError:
                pass
            return
        if self.api.u.IsWindow(self.hwnd):
            self._restore_opacity()                              # minimized first: no flash


class AppElsewhere(SightlineError):
    """The app's window appeared somewhere else than where it was launched."""


class NeedsRealInput(SightlineError):
    """The app reacted to no window message: it needs real input (visible desktop)."""


def _kill(pid: int) -> None:
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.OpenProcess.restype = wintypes.HANDLE
    k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    h = k.OpenProcess(0x0001, False, pid)
    if h:
        k.TerminateProcess(h, 1)
        k.CloseHandle(h)


DIRECT = (".exe", ".com")


def shell_command(launch):
    """(command, creation flags). Programs start directly; anything else Windows can open by
    double-click (.lnk shortcut, .bat/.cmd, .jar, .py, .appref-ms, .msc, a document...) goes
    through the shell file associations ('start'), from a console-less cmd.exe."""
    parts = [launch] if isinstance(launch, str) else list(launch)
    target = parts[0].strip().strip('"')
    ext = os.path.splitext(target)[1].lower()
    if isinstance(launch, str) and not os.path.isfile(target):
        return launch, 0                      # "prog.exe arg" or a name found on PATH
    if not ext or ext in DIRECT:
        return launch, 0
    if ext == ".ica":                                    # Citrix: the client engine itself
        from ..citrix import find_wfica
        exe = find_wfica()
        if exe:
            return [exe, *parts], 0
    if ext in (".bat", ".cmd"):                          # in a console nobody sees
        return ["cmd.exe", "/d", "/c", *parts], 0x08000000
    if ext == ".ps1":
        return ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", *parts], 0x08000000
    return ["cmd.exe", "/d", "/c", "start", "", *parts], 0x08000000   # CREATE_NO_WINDOW


def _exe_name(pid: int) -> str:
    """Executable name of a process (e.g. 'wfica32.exe'); no admin rights needed."""
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.OpenProcess.restype = wintypes.HANDLE
    k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_wchar_p,
                                             ctypes.POINTER(wintypes.DWORD)]
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    h = k.OpenProcess(0x1000, False, pid)                     # QUERY_LIMITED_INFORMATION
    if not h:
        return "unknown.exe"
    try:
        buf = ctypes.create_unicode_buffer(1024)
        n = wintypes.DWORD(1024)
        if k.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)):
            return os.path.basename(buf.value).lower()
        return "unknown.exe"
    finally:
        k.CloseHandle(h)


def _modes_file() -> str:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "sightline", "input_modes.json")


def _load_modes() -> dict:
    try:
        with open(_modes_file(), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _write_modes(modes: dict) -> None:
    path = _modes_file()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(modes, f, indent=2, sort_keys=True)


def _save_mode(key: str, mode: str) -> None:
    modes = _load_modes()
    modes[key] = mode
    _write_modes(modes)


def _utf16_units(ch: str) -> list[int]:
    b = ch.encode("utf-16-le")
    return [int.from_bytes(b[i:i + 2], "little") for i in range(0, len(b), 2)]
