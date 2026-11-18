"""Drive the machine this code runs on (headed mode).

Capture: mss (Windows GDI, macOS CoreGraphics, Linux X11). Input: pynput.
HiDPI: on Windows the process is made per-monitor DPI aware, so capture and input both
use physical pixels. On macOS Retina mss captures 2x pixels while input uses points:
the measured ratio is applied in ONE place (_to_input).

Linux Wayland: compositors forbid global capture/injection by design. Run the app in
an X11/XWayland session, or use the headless profile (Xvfb + VNC) - which is also what
CI does, so the tests you write locally run unchanged there.
"""
from __future__ import annotations

import os
import platform
import subprocess
import sys
import time

import numpy as np

from ..errors import SightlineError, Unsupported
from ..frame import Frame
from ..geometry import Rect
from .base import Backend


def current_os() -> str:
    s = platform.system().lower()
    return {"darwin": "macos"}.get(s, s)


def windows_session_locked() -> bool:
    """True when the Windows session is locked (or on the secure desktop: UAC prompt,
    Ctrl+Alt+Del): the visible screen is then not the user's desktop. No admin needed."""
    if sys.platform != "win32":
        return False
    import ctypes
    from ctypes import wintypes
    u, _ = _win32()
    h = u.OpenInputDesktop(0, False, 0x0001)              # DESKTOP_READOBJECTS
    if not h:
        return True                                       # access denied: Winlogon desktop
    try:
        buf = ctypes.create_unicode_buffer(256)
        n = wintypes.DWORD()
        if u.GetUserObjectInformationW(h, 2, buf, ctypes.sizeof(buf), ctypes.byref(n)):  # UOI_NAME
            return buf.value.lower() != "default"
        return False
    finally:
        u.CloseDesktop(h)


def _make_dpi_aware() -> None:
    if sys.platform != "win32":
        return
    import ctypes
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)        # per-monitor
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


class LocalBackend(Backend):
    name = "local"
    capabilities = frozenset({"mouse", "keyboard", "scroll", "clipboard"})

    def __init__(self, monitor: int = 1, target_os: str | None = None):
        super().__init__(target_os or current_os())
        if sys.platform.startswith("linux") and os.environ.get("WAYLAND_DISPLAY") \
                and not os.environ.get("DISPLAY"):
            raise SightlineError(
                "Pure Wayland session: global screen capture and input injection are "
                "blocked. Use an X11 session, run the app under XWayland with DISPLAY set, "
                "or use the headless profile (Xvfb + VNC).")
        _make_dpi_aware()
        try:
            import mss
            from pynput import keyboard, mouse
        except ImportError as exc:
            raise SightlineError("Local backend needs: pip install 'sightline[local]'") from exc
        self._mss = mss.mss()
        mons = self._mss.monitors
        if not 0 <= monitor < len(mons):
            raise SightlineError(f"monitor={monitor} but only {len(mons) - 1} monitor(s)")
        self._mon = mons[monitor]
        self._mouse = mouse.Controller()
        self._kb = keyboard.Controller()
        self._Button, self._Key = mouse.Button, keyboard.Key
        probe = np.asarray(self._mss.grab(self._mon))
        # pixels per input unit (2.0 on Retina, 1.0 elsewhere once DPI aware)
        self._scale = probe.shape[1] / float(self._mon["width"])
        self._w, self._h = probe.shape[1], probe.shape[0]
        self._lock_checked = 0.0

    def size(self) -> tuple[int, int]:
        return self._w, self._h

    def capture(self, region: Rect | None = None) -> Frame:
        now = time.monotonic()
        if sys.platform == "win32" and now - self._lock_checked > 2.0:
            self._lock_checked = now
            if windows_session_locked():
                raise SightlineError(
                    "the Windows session is LOCKED (or a secure desktop like UAC is shown): "
                    "the screen shows the lock screen, not your apps. Keep the session "
                    "unlocked while the foreground 'local' backend runs.")
        s = self._scale
        if region is None:
            box, ox, oy = self._mon, 0, 0
        else:
            r = region.intersect(Rect(0, 0, self._w, self._h)) or Rect(0, 0, 1, 1)
            box = {"left": self._mon["left"] + int(r.x / s), "top": self._mon["top"] + int(r.y / s),
                   "width": max(int(np.ceil(r.w / s)), 1), "height": max(int(np.ceil(r.h / s)), 1)}
            ox, oy = int(box["left"] - self._mon["left"]) * s, int(box["top"] - self._mon["top"]) * s
        shot = np.asarray(self._mss.grab(box))[:, :, :3]
        return Frame(np.ascontiguousarray(shot), int(ox), int(oy), time.time())

    def _to_input(self, x: int, y: int) -> tuple[int, int]:
        return (int(round(self._mon["left"] + x / self._scale)),
                int(round(self._mon["top"] + y / self._scale)))

    def mouse_move(self, x: int, y: int) -> None:
        self._mouse.position = self._to_input(x, y)

    def _button(self, b: str):
        return {"left": self._Button.left, "right": self._Button.right,
                "middle": self._Button.middle}[b]

    def mouse_down(self, button: str = "left") -> None:
        self._mouse.press(self._button(button))

    def mouse_up(self, button: str = "left") -> None:
        self._mouse.release(self._button(button))

    def scroll(self, x: int, y: int, clicks: int, horizontal: bool = False) -> None:
        self.mouse_move(x, y)
        for _ in range(abs(int(clicks))):
            step = 1 if clicks > 0 else -1
            # pynput: positive dy scrolls UP; our convention: positive = down/right
            self._mouse.scroll(step if horizontal else 0, 0 if horizontal else -step)
            time.sleep(0.03)

    def _pkey(self, key: str):
        K = self._Key
        if len(key) == 1:
            return key
        table = {"ctrl": K.ctrl, "shift": K.shift, "alt": K.alt, "cmd": K.cmd, "win": K.cmd,
                 "enter": K.enter, "tab": K.tab, "esc": K.esc, "backspace": K.backspace,
                 "delete": K.delete, "home": K.home, "end": K.end, "pageup": K.page_up,
                 "pagedown": K.page_down, "up": K.up, "down": K.down, "left": K.left,
                 "right": K.right, "space": K.space, "capslock": K.caps_lock,
                 "insert": getattr(K, "insert", K.home), "menu": getattr(K, "menu", K.alt),
                 "printscreen": getattr(K, "print_screen", K.home)}
        table.update({f"f{i}": getattr(K, f"f{i}") for i in range(1, 13)})
        return table[key]

    def key_down(self, key: str) -> None:
        self._kb.press(self._pkey(key))

    def key_up(self, key: str) -> None:
        self._kb.release(self._pkey(key))

    def type_text(self, text: str, interval: float = 0.0) -> None:
        if not interval:
            self._kb.type(text)
            return
        for ch in text:
            self._kb.type(ch)
            time.sleep(interval)

    # ---- clipboard of this machine ----
    def get_clipboard(self) -> str:
        if sys.platform == "win32":
            return _win_clipboard_get()
        if sys.platform == "darwin":
            return subprocess.run(["pbpaste"], capture_output=True, text=True).stdout
        for cmd in (["xclip", "-selection", "clipboard", "-o"], ["xsel", "-b", "-o"],
                    ["wl-paste", "-n"]):
            try:
                return subprocess.run(cmd, capture_output=True, text=True, timeout=5).stdout
            except FileNotFoundError:
                continue
        raise Unsupported("Install xclip, xsel or wl-clipboard to read the clipboard")

    def set_clipboard(self, text: str) -> None:
        if sys.platform == "win32":
            return _win_clipboard_set(text)
        if sys.platform == "darwin":
            subprocess.run(["pbcopy"], input=text, text=True, check=True)
            return
        for cmd in (["xclip", "-selection", "clipboard"], ["xsel", "-b", "-i"], ["wl-copy"]):
            try:
                subprocess.run(cmd, input=text, text=True, check=True, timeout=5)
                return
            except FileNotFoundError:
                continue
        raise Unsupported("Install xclip, xsel or wl-clipboard to set the clipboard")

    def close(self) -> None:
        self._mss.close()


def _win32():                                                 # pragma: no cover - Windows
    """Private user32/kernel32 handles with FULL prototypes. Without argtypes ctypes passes
    handles as 32-bit ints: on 64-bit Windows that fails ('int too long to convert') or
    truncates a handle - intermittently, depending on the handle's value."""
    import ctypes
    from ctypes import wintypes
    u = ctypes.WinDLL("user32", use_last_error=True)
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    H, B, U = wintypes.HANDLE, wintypes.BOOL, wintypes.UINT
    for f, res, args in [
        (u.OpenClipboard, B, [wintypes.HWND]), (u.CloseClipboard, B, []),
        (u.EmptyClipboard, B, []), (u.GetClipboardData, H, [U]),
        (u.SetClipboardData, H, [U, H]),
        (k.GlobalAlloc, H, [U, ctypes.c_size_t]), (k.GlobalFree, H, [H]),
        (k.GlobalLock, ctypes.c_void_p, [H]), (k.GlobalUnlock, B, [H]),
        (u.OpenInputDesktop, H, [wintypes.DWORD, B, wintypes.DWORD]),
        (u.CloseDesktop, B, [H]),
        (u.GetUserObjectInformationW, B, [H, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
                                          ctypes.POINTER(wintypes.DWORD)]),
    ]:
        f.restype, f.argtypes = res, args
    return u, k


def _open_clipboard(u) -> None:                                 # pragma: no cover - Windows
    for _ in range(40):                    # another process may hold the clipboard briefly
        if u.OpenClipboard(None):
            return
        time.sleep(0.05)
    raise SightlineError("Clipboard is locked by another application")


def _win_clipboard_get() -> str:                               # pragma: no cover - Windows
    import ctypes
    u, k = _win32()
    _open_clipboard(u)
    try:
        h = u.GetClipboardData(13)         # CF_UNICODETEXT
        if not h:
            return ""
        p = k.GlobalLock(h)
        if not p:
            return ""
        try:
            return ctypes.wstring_at(p)
        finally:
            k.GlobalUnlock(h)
    finally:
        u.CloseClipboard()


def _win_clipboard_set(text: str) -> None:                     # pragma: no cover - Windows
    import ctypes
    u, k = _win32()
    data = (text + "\0").encode("utf-16-le")
    _open_clipboard(u)
    try:
        u.EmptyClipboard()
        h = k.GlobalAlloc(0x0002, len(data))                    # GMEM_MOVEABLE
        if not h:
            raise SightlineError(f"GlobalAlloc failed ({ctypes.get_last_error()})")
        p = k.GlobalLock(h)
        ctypes.memmove(p, data, len(data))
        k.GlobalUnlock(h)
        if not u.SetClipboardData(13, h):                       # the system now owns h
            k.GlobalFree(h)
            raise SightlineError(f"SetClipboardData failed ({ctypes.get_last_error()})")
    finally:
        u.CloseClipboard()
