"""Visit a sightline desktop yourself (Windows): open apps on it by hand, then come back.

    sightline desktop open phileas --portal https://citrix.company.com
    sightline desktop show phileas          # go and look again
    sightline desktop close phileas         # close everything on it

Then scripts attach:  HiddenDesktopBackend(desktop="phileas", title="PHILEAS")
"""
from __future__ import annotations

import ctypes
import os
import sys
import time
from ctypes import wintypes

from .errors import SightlineError

GENERIC_ALL, SWITCH, READ = 0x10000000, 0x0100, 0x0001
PANEL_TITLE = "sightline desktop - "


def _u():
    u = ctypes.WinDLL("user32", use_last_error=True)
    H = wintypes.HANDLE
    for f, res, args in [
        (u.CreateDesktopW, H, [wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_void_p, wintypes.DWORD,
                               wintypes.DWORD, ctypes.c_void_p]),
        (u.OpenDesktopW, H, [wintypes.LPCWSTR, wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]),
        (u.OpenInputDesktop, H, [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]),
        (u.SwitchDesktop, wintypes.BOOL, [H]), (u.CloseDesktop, wintypes.BOOL, [H]),
        (u.GetUserObjectInformationW, wintypes.BOOL, [H, ctypes.c_int, ctypes.c_void_p,
                                                      wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]),
    ]:
        f.restype, f.argtypes = res, args
    return u


def input_desktop() -> str:
    """Name of the desktop on screen ('Default' = the user's own)."""
    u = _u()
    h = u.OpenInputDesktop(0, False, READ)
    if not h:
        return "?"                         # the secure desktop (Ctrl+Alt+Del, UAC) is on screen
    try:
        buf = ctypes.create_unicode_buffer(256)
        need = wintypes.DWORD()
        u.GetUserObjectInformationW(h, 2, buf, ctypes.sizeof(buf), ctypes.byref(need))  # UOI_NAME
        return buf.value
    finally:
        u.CloseDesktop(h)


def _panel_open(name: str) -> bool:
    from .backends.window_win import list_windows
    result = []

    def look():
        result.extend(w for w in list_windows() if w["title"] == PANEL_TITLE + name)
    _on_desktop(name, look)
    return bool(result)


def _on_desktop(name: str, fn):
    """Run fn on a thread attached to desktop `name`."""
    import threading
    u = _u()
    h = u.OpenDesktopW(name, 0, False, GENERIC_ALL)
    if not h:
        raise SightlineError(f"no desktop named {name!r} (sightline desktop open {name})")
    err = []

    def run():
        try:
            if not u.SetThreadDesktop(h):
                raise SightlineError("could not attach to the desktop")
            fn()
        except Exception as e:                     # re-raised below
            err.append(e)
    u.SetThreadDesktop.argtypes = [wintypes.HANDLE]
    t = threading.Thread(target=run)
    t.start()
    t.join()
    u.CloseDesktop(h)
    if err:
        raise err[0]


def open_desktop(name: str, portal: str = "", switch: bool = True, max_minutes: float = 15) -> None:
    """Create desktop `name` (or reuse it), put the control panel on it, and show it."""
    if sys.platform != "win32":
        raise SightlineError("desktops are a Windows feature (Linux: the 'display' backend)")
    from .backends.hidden_win import DesktopProcess
    u = _u()
    h = u.OpenDesktopW(name, 0, False, GENERIC_ALL) or \
        u.CreateDesktopW(name, None, None, 0, GENERIC_ALL, None)
    if not h:
        raise SightlineError(f"could not create desktop {name!r}: "
                             f"{ctypes.FormatError(ctypes.get_last_error())}")
    try:
        if not _panel_open(name):
            pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
            panel = os.path.join(os.path.dirname(__file__), "backends", "_desktop_panel.py")
            DesktopProcess([pyw if os.path.isfile(pyw) else sys.executable, panel, name]
                           + ([portal] if portal else []), None, f"WinSta0\\{name}")
            deadline = time.monotonic() + 15
            while not _panel_open(name):
                if time.monotonic() > deadline:
                    raise SightlineError("the desktop's control panel did not start (is tkinter "
                                         "installed with this Python?) - not switching to it")
                time.sleep(0.3)
        if switch:
            show(name, max_minutes)
    finally:
        u.CloseDesktop(h)                 # the panel (and the apps) keep the desktop alive


def show(name: str, max_minutes: float = 15) -> None:
    """Switch the screen to desktop `name` until the user clicks 'Back to my desktop'; after
    max_minutes the screen is brought back anyway (safety net)."""
    u = _u()
    h = u.OpenDesktopW(name, 0, False, SWITCH)
    if not h or not u.SwitchDesktop(h):
        raise SightlineError(f"could not show desktop {name!r}")
    u.CloseDesktop(h)
    print(f"You are on desktop '{name}'. Click 'Back to my desktop' in the panel to return "
          f"(automatic after {max_minutes:g} min).")
    deadline = time.monotonic() + max_minutes * 60
    time.sleep(1)
    while input_desktop() in (name, "?") and time.monotonic() < deadline:
        time.sleep(0.5)
    if input_desktop() == name:
        back = u.OpenDesktopW("Default", 0, False, SWITCH)
        u.SwitchDesktop(back)
        u.CloseDesktop(back)
        print("Brought you back (time limit).")
    print(f"Back on your desktop. Apps on '{name}' keep running; attach with "
          f'HiddenDesktopBackend(desktop="{name}", title="...")')


def close_desktop(name: str) -> list[int]:
    """Close every app on desktop `name` (the desktop disappears with them)."""
    from .backends.hidden_win import _terminate
    from .backends.window_win import list_windows
    pids: set[int] = set()
    _on_desktop(name, lambda: pids.update(w["pid"] for w in list_windows()))
    for pid in pids - {os.getpid()}:
        _terminate(pid)
    return sorted(pids)


def list_desktops() -> list[str]:
    """Desktops of this window station: 'Default' (yours), sightline's, Sysinternals
    Desktops' ('Sysinternals Desktop 1'...) - any of them can be attached to."""
    u = _u()
    u.GetProcessWindowStation.restype = wintypes.HANDLE
    proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.LPWSTR, wintypes.LPARAM)
    u.EnumDesktopsW.argtypes = [wintypes.HANDLE, proc, wintypes.LPARAM]
    names: list[str] = []
    cb = proc(lambda n, _: names.append(n) or True)
    u.EnumDesktopsW(u.GetProcessWindowStation(), cb, 0)
    return names


def windows_on(name: str) -> list[dict]:
    from .backends.window_win import _exe_name, list_windows
    out: list[dict] = []
    _on_desktop(name, lambda: out.extend(w for w in list_windows() if w["title"]))
    for w in out:
        w["exe"] = _exe_name(w["pid"])
    return out


def child_sessions_enabled() -> bool | None:
    """Windows child sessions (a second session of YOUR account in a window - what UiPath and
    Power Automate 'Picture-in-Picture' use): enabled on this PC? None if unknown."""
    try:
        w = ctypes.WinDLL("wtsapi32")
        flag = wintypes.BOOL()
        if w.WTSIsChildSessionsEnabled(ctypes.byref(flag)):
            return bool(flag.value)
    except (OSError, AttributeError):
        pass
    return None
