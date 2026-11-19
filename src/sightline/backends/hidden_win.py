"""Windows 'hidden' backend: the app runs on its OWN invisible desktop - like a headless browser.

Windows lets any user (no admin rights) create extra desktops in their session. sightline
creates one, starts the app ON it, and drives it there:

- nothing appears on the user's screen or taskbar, and the user's mouse/keyboard are never
  touched or waited for: they belong to the visible desktop, the app lives on another one;
- capture: the app's windows are drawn on the hidden desktop (PrintWindow);
- input: window messages (post, then send) - the only input that reaches a desktop that is
  not on screen. Apps that accept ONLY real hardware input cannot be driven there;
- several apps (or several copies of one app) can run in parallel, one desktop each;
- the clipboard is shared with the user's (same window station): the session saves and
  restores it, as with the 'window' backend.

All calls run on one worker thread attached to the hidden desktop (Windows binds window
enumeration and input routing to the calling thread's desktop); the 'window' backend's
capture and input code is reused unchanged.
"""
from __future__ import annotations

import concurrent.futures
import ctypes
import itertools
import os
import queue
import subprocess
import sys
import threading
from ctypes import wintypes

from ..errors import SightlineError
from .base import Backend
from .window_win import NeedsRealInput, WindowBackend, list_windows, shell_command

GENERIC_ALL = 0x10000000
_counter = itertools.count(1)


class STARTUPINFOW(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("lpReserved", wintypes.LPWSTR), ("lpDesktop", wintypes.LPWSTR),
                ("lpTitle", wintypes.LPWSTR), ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD),
                ("dwXSize", wintypes.DWORD), ("dwYSize", wintypes.DWORD),
                ("dwXCountChars", wintypes.DWORD), ("dwYCountChars", wintypes.DWORD),
                ("dwFillAttribute", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("wShowWindow", wintypes.WORD), ("cbReserved2", wintypes.WORD),
                ("lpReserved2", ctypes.c_void_p), ("hStdInput", wintypes.HANDLE),
                ("hStdOutput", wintypes.HANDLE), ("hStdError", wintypes.HANDLE)]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [("hProcess", wintypes.HANDLE), ("hThread", wintypes.HANDLE),
                ("dwProcessId", wintypes.DWORD), ("dwThreadId", wintypes.DWORD)]


def _api():
    u = ctypes.WinDLL("user32", use_last_error=True)
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    H = wintypes.HANDLE
    for f, res, args in [
        (u.CreateDesktopW, H, [wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_void_p, wintypes.DWORD,
                               wintypes.DWORD, ctypes.c_void_p]),
        (u.OpenDesktopW, H, [wintypes.LPCWSTR, wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]),
        (u.SetThreadDesktop, wintypes.BOOL, [H]),
        (u.CloseDesktop, wintypes.BOOL, [H]),
        (k.CreateProcessW, wintypes.BOOL, [wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.c_void_p,
                                           ctypes.c_void_p, wintypes.BOOL, wintypes.DWORD,
                                           ctypes.c_void_p, wintypes.LPCWSTR,
                                           ctypes.POINTER(STARTUPINFOW),
                                           ctypes.POINTER(PROCESS_INFORMATION)]),
        (k.WaitForSingleObject, wintypes.DWORD, [H, wintypes.DWORD]),
        (k.GetExitCodeProcess, wintypes.BOOL, [H, ctypes.POINTER(wintypes.DWORD)]),
        (k.TerminateProcess, wintypes.BOOL, [H, wintypes.UINT]),
        (k.CloseHandle, wintypes.BOOL, [H]),
    ]:
        f.restype, f.argtypes = res, args
    return u, k


class DesktopProcess:
    """A process started on a given desktop (subprocess.Popen cannot choose the desktop)."""

    def __init__(self, command, cwd, desktop: str, flags: int = 0):
        _, self._k = _api()
        cmd = command if isinstance(command, str) else subprocess.list2cmdline(command)
        si = STARTUPINFOW(cb=ctypes.sizeof(STARTUPINFOW), lpDesktop=desktop)
        pi = PROCESS_INFORMATION()
        buf = ctypes.create_unicode_buffer(cmd)
        if not self._k.CreateProcessW(None, buf, None, None, False, flags, None, cwd,
                                      ctypes.byref(si), ctypes.byref(pi)):
            raise SightlineError(f"could not start {cmd!r} on the hidden desktop: "
                                 f"{ctypes.FormatError(ctypes.get_last_error())}")
        self._k.CloseHandle(pi.hThread)
        self._h, self.pid, self.args = pi.hProcess, pi.dwProcessId, command
        self.returncode = None

    def poll(self):
        if self.returncode is None and self._k.WaitForSingleObject(self._h, 0) == 0:
            code = wintypes.DWORD()
            self._k.GetExitCodeProcess(self._h, ctypes.byref(code))
            self.returncode = code.value
        return self.returncode

    def wait(self, timeout: float | None = None):
        ms = 0xFFFFFFFF if timeout is None else int(timeout * 1000)
        if self._k.WaitForSingleObject(self._h, ms) != 0:
            raise subprocess.TimeoutExpired(self.args, timeout)
        return self.poll()

    def terminate(self) -> None:
        if self.poll() is None:
            self._k.TerminateProcess(self._h, 1)

    kill = terminate


class _DesktopThread:
    """One thread attached to the hidden desktop; every backend call is run on it."""

    def __init__(self, hdesk):
        self._q: queue.Queue = queue.Queue()
        self._ready = threading.Event()
        self._error: str | None = None
        self._t = threading.Thread(target=self._run, args=(hdesk,), name="sightline-hidden",
                                   daemon=True)
        self._t.start()
        self._ready.wait()
        if self._error:
            raise SightlineError(self._error)

    def _run(self, hdesk) -> None:
        u, _ = _api()
        if not u.SetThreadDesktop(hdesk):               # a fresh thread owns no window: allowed
            self._error = f"could not attach to the hidden desktop: {ctypes.FormatError(ctypes.get_last_error())}"
            self._ready.set()
            return
        self._ready.set()
        while True:
            item = self._q.get()
            if item is None:
                return
            fn, args, kwargs, fut = item
            try:
                fut.set_result(fn(*args, **kwargs))
            except BaseException as e:                   # re-raised in the caller
                fut.set_exception(e)

    def call(self, fn, *args, **kwargs):
        if threading.current_thread() is self._t:
            return fn(*args, **kwargs)
        fut: concurrent.futures.Future = concurrent.futures.Future()
        self._q.put((fn, args, kwargs, fut))
        return fut.result()

    def stop(self) -> None:
        self._q.put(None)
        self._t.join(10)


class _OnHiddenDesktop(WindowBackend):
    """The 'window' backend, living on the hidden desktop: launches there, no real input."""
    _ladder = ("post", "send")
    _private_desktop = True                  # any window here is the app's (launchers welcome)

    def __init__(self, desktop: str, **kw):
        self._desktop = desktop
        super().__init__(**kw)

    def _no_reaction(self, what: str) -> None:
        # one silent click proves nothing (clicking an empty field changes nothing visible);
        # silent TYPING, or 3 silent clicks in a row, means the app ignores this desktop
        self._silent = getattr(self, "_silent", 0) + 1
        if self.requested_mode == "auto" and not self._confirmed and \
                (what.startswith("typing") or self._silent >= 3):
            raise NeedsRealInput(f"{self.app_key}: no reaction to {what} on a hidden desktop")
        super()._no_reaction(what)

    def _launch(self, launch, cwd):
        # children inherit the desktop: what a shortcut/launcher/script starts lands here too
        cmd, flags = shell_command(launch)
        return DesktopProcess(cmd, cwd, f"WinSta0\\{self._desktop}", flags)


def _terminate(pid: int) -> None:
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.OpenProcess.restype = wintypes.HANDLE
    k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    h = k.OpenProcess(0x0001, False, pid)                  # PROCESS_TERMINATE (own processes)
    if h:
        k.TerminateProcess(h, 1)
        k.CloseHandle(h)


def _delegate(name: str):
    def method(self, *args, **kwargs):
        return self._thread.call(getattr(self._inner, name), *args, **kwargs)
    method.__name__ = name
    return method


class HiddenDesktopBackend(Backend):
    name = "hidden"
    capabilities = WindowBackend.capabilities
    shares_user_clipboard = True

    def __init__(self, launch: str | list[str] | None = None, title: str | None = None,
                 desktop: str | None = None, cwd: str | None = None, timeout: float = 30.0,
                 regex: bool = False, input_mode: str = "auto", keep_app: bool = False,
                 citrix: dict | None = None,
                 target_os: str = "windows", **window_opts):
        """launch: the app to start on the hidden desktop (title: its window, if the process
        that owns the window is not the one launched - e.g. a launcher).
        desktop: name of the hidden desktop; reuse a name to attach to an app left running
        by an earlier session (keep_app=True), without launching it again.
        keep_app=False: the app is closed with the session (nobody could see it otherwise).
        citrix={"portal": url, "app": name, "user_data_dir": dir}: a Citrix app from a web
        portal - its .ica is fetched headless (never auto-opened) and opened here.
        Add "take_over": True (or a window title) to MOVE an already open session here: the
        Citrix viewer on the user's desktop is closed (disconnect) and the launch reconnects."""
        super().__init__(target_os)
        if sys.platform != "win32":
            raise SightlineError("the 'hidden' backend is Windows-only (Linux: 'display' backend)")
        if input_mode == "real":
            raise SightlineError("real input only reaches the desktop on screen: on a hidden "
                                 "desktop use input_mode 'auto', 'post' or 'send'")
        if citrix:
            # {portal, app, user_data_dir, [windowed, size, channel, browser_args]}: a fresh
            # .ica fetched by a headless browser, opened by the Citrix client right here
            from ..citrix import citrix_launch, fetch_ica
            c = dict(citrix)
            windowed, size = c.pop("windowed", True), tuple(c.pop("size", (1920, 1080)))
            take_over = c.pop("take_over", False)
            if take_over:
                # the app is already open on the user's screen: disconnect that viewer, the
                # launch below reconnects the SAME session here (screens and data kept)
                from ..citrix import disconnect_local
                disconnect_local(take_over if isinstance(take_over, str) else c["app"])
            launch = citrix_launch(fetch_ica(**c), windowed=windowed, size=size)
            timeout = max(timeout, 180.0)                     # a Citrix logon takes a while
        if not launch and not (desktop and title):
            raise SightlineError("hidden backend needs launch= (or desktop= + title= to attach)")
        self.keep_app = keep_app
        self.desktop_name = desktop or f"sightline-{os.getpid()}-{next(_counter)}"
        self._u, _ = _api()
        hdesk = self._u.OpenDesktopW(self.desktop_name, 0, False, GENERIC_ALL)
        if not hdesk:
            if not launch:
                raise SightlineError(f"no hidden desktop named {self.desktop_name!r} to attach to")
            hdesk = self._u.CreateDesktopW(self.desktop_name, None, None, 0, GENERIC_ALL, None)
        if not hdesk:
            raise SightlineError(f"could not create a hidden desktop: "
                                 f"{ctypes.FormatError(ctypes.get_last_error())}")
        self._hdesk = hdesk
        self._closed = False
        try:
            self._thread = _DesktopThread(hdesk)
            self._inner = self._thread.call(
                _OnHiddenDesktop, self.desktop_name, title=title, launch=launch, cwd=cwd,
                timeout=timeout, regex=regex, input_mode=input_mode, target_os=target_os,
                **window_opts)
        except BaseException:
            self._u.CloseDesktop(hdesk)
            raise

    def __getattr__(self, name):
        # hwnd, pid, process, input_mode, app_key, fallback_capture, forget_input_mode...
        if name.startswith("__") or name in ("_inner", "_thread"):
            raise AttributeError(name)
        value = getattr(self._inner, name)
        if callable(value):
            return lambda *a, **kw: self._thread.call(value, *a, **kw)
        return value

    def run(self, fn, *args, **kwargs):
        """Run fn on the hidden desktop's thread (for Win32 calls about the app's windows)."""
        return self._thread.call(fn, *args, **kwargs)

    size = _delegate("size")
    capture = _delegate("capture")
    mouse_move = _delegate("mouse_move")
    mouse_down = _delegate("mouse_down")
    mouse_up = _delegate("mouse_up")
    scroll = _delegate("scroll")
    click = _delegate("click")
    drag = _delegate("drag")
    key_down = _delegate("key_down")
    key_up = _delegate("key_up")
    hotkey = _delegate("hotkey")
    type_text = _delegate("type_text")
    get_clipboard = _delegate("get_clipboard")
    set_clipboard = _delegate("set_clipboard")
    clipboard_serial = _delegate("clipboard_serial")

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._thread.call(self._inner.close)
            if not self.keep_app:
                # every app on OUR desktop: the launched one and whatever it started
                pids = {w["pid"] for w in self._thread.call(list_windows)}
                proc = self._inner.process
                if proc is not None:
                    proc.terminate()
                for pid in pids - {os.getpid()}:
                    _terminate(pid)
                if proc is not None:
                    try:
                        proc.wait(10)
                    except subprocess.TimeoutExpired:
                        pass
        finally:
            self._thread.stop()
            self._u.CloseDesktop(self._hdesk)     # gone once no app uses it any more
