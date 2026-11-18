"""Polite REAL input for Windows apps that ignore window messages (e.g. Citrix seamless).

A burst: wait until the user has been idle a moment, remember their foreground window and
cursor, bring the target forward, send real input (SendInput), then give the user their
window and cursor back. The user sees the target flash for a fraction of a second.
"""
from __future__ import annotations

import ctypes
import time
from contextlib import contextmanager
from ctypes import wintypes

ULONG_PTR = ctypes.c_size_t
INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP = 0x0002, 0x0004
MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP = 0x0008, 0x0010
MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP = 0x0020, 0x0040
MOUSEEVENTF_WHEEL, MOUSEEVENTF_HWHEEL = 0x0800, 0x1000
KEYEVENTF_KEYUP, KEYEVENTF_UNICODE, KEYEVENTF_EXTENDEDKEY = 0x0002, 0x0004, 0x0001
BUTTON_FLAGS = {"left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
                "right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
                "middle": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP)}


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


def _api():
    u = ctypes.WinDLL("user32", use_last_error=True)
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    HW = wintypes.HWND
    for f, res, args in [
        (u.SendInput, wintypes.UINT, [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]),
        (u.GetForegroundWindow, HW, []), (u.SetForegroundWindow, wintypes.BOOL, [HW]),
        (u.BringWindowToTop, wintypes.BOOL, [HW]),
        (u.GetWindowThreadProcessId, wintypes.DWORD, [HW, ctypes.c_void_p]),
        (u.AttachThreadInput, wintypes.BOOL, [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]),
        (u.GetCursorPos, wintypes.BOOL, [ctypes.POINTER(wintypes.POINT)]),
        (u.SetCursorPos, wintypes.BOOL, [ctypes.c_int, ctypes.c_int]),
        (u.GetLastInputInfo, wintypes.BOOL, [ctypes.POINTER(LASTINPUTINFO)]),
        (u.IsWindow, wintypes.BOOL, [HW]), (u.IsIconic, wintypes.BOOL, [HW]),
        (u.ShowWindow, wintypes.BOOL, [HW, ctypes.c_int]),
        (k.GetTickCount, wintypes.DWORD, []), (k.GetCurrentThreadId, wintypes.DWORD, []),
    ]:
        f.restype, f.argtypes = res, args
    return u, k


class RealInput:
    def __init__(self, idle_ms: int = 800, max_wait: float = 30.0):
        self.u, self.k = _api()
        self.idle_ms, self.max_wait = idle_ms, max_wait

    # ---------------------------------------------------------------- politeness
    def user_idle_ms(self) -> int:
        li = LASTINPUTINFO(cbSize=ctypes.sizeof(LASTINPUTINFO))
        self.u.GetLastInputInfo(ctypes.byref(li))
        return (self.k.GetTickCount() - li.dwTime) & 0xFFFFFFFF

    def wait_for_idle(self) -> None:
        """Wait until the user has not touched mouse/keyboard for idle_ms (bounded)."""
        deadline = time.monotonic() + self.max_wait
        while self.user_idle_ms() < self.idle_ms and time.monotonic() < deadline:
            time.sleep(0.1)

    def _activate(self, hwnd) -> bool:
        """Foreground a window from a background process: share input state with the
        current foreground thread for the call (no Alt-key trick: that can open the user's
        menu bar)."""
        u, k = self.u, self.k
        if u.IsIconic(hwnd):
            u.ShowWindow(hwnd, 9)                                # SW_RESTORE
        fg = u.GetForegroundWindow()
        mine = k.GetCurrentThreadId()
        theirs = u.GetWindowThreadProcessId(fg, None) if fg else 0
        attached = bool(theirs and theirs != mine and u.AttachThreadInput(mine, theirs, True))
        try:
            u.BringWindowToTop(hwnd)
            u.SetForegroundWindow(hwnd)
        finally:
            if attached:
                u.AttachThreadInput(mine, theirs, False)
        for _ in range(20):
            if u.GetForegroundWindow() == hwnd:
                return True
            time.sleep(0.01)
        return False

    @contextmanager
    def burst(self, hwnd, before=None, after=None):
        """with real.burst(hwnd): ... real input ...  - then the user gets everything back.
        before/after: hooks around it (the invisible mode puts the window on top at
        opacity 1/255 before, and back down, fully transparent, after)."""
        self.wait_for_idle()
        u = self.u
        prev_fg = u.GetForegroundWindow()
        pos = wintypes.POINT()
        u.GetCursorPos(ctypes.byref(pos))
        if before:
            before()
        try:
            if not self._activate(hwnd):
                raise RuntimeError("could not bring the application to the front for real input")
            yield
            time.sleep(0.05)                                      # let the input be consumed
        finally:
            u.SetCursorPos(pos.x, pos.y)
            if prev_fg and prev_fg != hwnd and u.IsWindow(prev_fg):
                self._activate(prev_fg)
            if after:
                after()

    # ---------------------------------------------------------------- primitives
    def _send(self, *inputs: INPUT) -> None:
        arr = (INPUT * len(inputs))(*inputs)
        if self.u.SendInput(len(inputs), arr, ctypes.sizeof(INPUT)) != len(inputs):
            raise RuntimeError(f"SendInput was blocked (error {ctypes.get_last_error()}): "
                               "is a higher-privilege window in front, or is the session locked?")

    def _mouse(self, flags: int, data: int = 0) -> INPUT:
        return INPUT(type=INPUT_MOUSE, mi=MOUSEINPUT(0, 0, data & 0xFFFFFFFF, flags, 0, 0))

    def _key(self, vk: int = 0, scan: int = 0, flags: int = 0) -> INPUT:
        return INPUT(type=INPUT_KEYBOARD, ki=KEYBDINPUT(vk, scan, flags, 0, 0))

    def move(self, x: int, y: int) -> None:
        self.u.SetCursorPos(int(x), int(y))

    def button(self, button: str, down: bool) -> None:
        self._send(self._mouse(BUTTON_FLAGS[button][0 if down else 1]))

    def click(self, x: int, y: int, button: str = "left", count: int = 1) -> None:
        self.move(x, y)
        time.sleep(0.02)
        for i in range(count):
            self._send(self._mouse(BUTTON_FLAGS[button][0]), self._mouse(BUTTON_FLAGS[button][1]))
            if i + 1 < count:
                time.sleep(0.06)

    def wheel(self, x: int, y: int, clicks: int, horizontal: bool = False) -> None:
        self.move(x, y)
        delta = (-120 if clicks > 0 else 120) * (-1 if horizontal else 1)
        for _ in range(abs(int(clicks))):
            self._send(self._mouse(MOUSEEVENTF_HWHEEL if horizontal else MOUSEEVENTF_WHEEL, delta))
            time.sleep(0.02)

    def vk(self, vk: int, down: bool, extended: bool = False) -> None:
        flags = (0 if down else KEYEVENTF_KEYUP) | (KEYEVENTF_EXTENDEDKEY if extended else 0)
        self._send(self._key(vk=vk, flags=flags))

    def text(self, text: str, interval: float = 0.0) -> None:
        b = text.encode("utf-16-le")
        for i in range(0, len(b), 2):
            unit = int.from_bytes(b[i:i + 2], "little")
            if unit in (0x0A, 0x0D):
                self.vk(0x0D, True)
                self.vk(0x0D, False)
                continue
            self._send(self._key(scan=unit, flags=KEYEVENTF_UNICODE),
                       self._key(scan=unit, flags=KEYEVENTF_UNICODE | KEYEVENTF_KEYUP))
            if interval:
                time.sleep(interval)
