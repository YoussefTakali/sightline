"""Windows 'app' backend: name the app, sightline finds the best way to automate it.

    s = Session(AppBackend("Gestion"))           # or: backend = "app", app = "Gestion"

1. FIND it: a window with that name already open -> that window (never launched twice);
   else `launch=` if given; else a Start menu / desktop shortcut with that name.
2. RUN it the most headless way: on its own invisible desktop. If a launcher hands the app
   to the user's desktop instead (single-instance apps, Citrix...), sightline follows it.
3. CHECK it obeys: if the app ignores every window message on the hidden desktop, sightline
   relaunches it invisibly on the user's desktop and drives it with polite real input.
4. REMEMBER the route that worked, per app: next runs go straight to it.

The user never chooses a backend, a desktop or an input mode.
"""
from __future__ import annotations

import json
import os
import threading
import time

from ..errors import SightlineError
from .base import Backend
from .window_win import AppElsewhere, NeedsRealInput, WindowBackend, _exe_name, _modes_file, list_windows

SHORTCUT_EXTS = (".lnk", ".url", ".appref-ms")


def find_shortcut(name: str) -> str | None:
    """Start menu / desktop shortcut whose name contains `name` (exact name first)."""
    roots = [os.path.join(os.environ.get(v, ""), *sub) for v, sub in (
        ("APPDATA", ("Microsoft", "Windows", "Start Menu", "Programs")),
        ("PROGRAMDATA", ("Microsoft", "Windows", "Start Menu", "Programs")),
        ("USERPROFILE", ("Desktop",)), ("PUBLIC", ("Desktop",)))]
    want = name.casefold()
    exact, partial = [], []
    for root in roots:
        if not os.path.isdir(root):
            continue
        for d, _, files in os.walk(root):
            for f in files:
                stem, ext = os.path.splitext(f)
                if ext.lower() not in SHORTCUT_EXTS:
                    continue
                if stem.casefold() == want:
                    exact.append(os.path.join(d, f))
                elif want in stem.casefold():
                    partial.append(os.path.join(d, f))
    found = exact or sorted(partial, key=len)
    return found[0] if found else None


def _routes_file() -> str:
    return os.path.join(os.path.dirname(_modes_file()), "routes.json")


def _load_routes() -> dict:
    try:
        with open(_routes_file(), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_route(app: str, route: str) -> None:
    routes = _load_routes()
    routes[app.casefold()] = route
    os.makedirs(os.path.dirname(_routes_file()), exist_ok=True)
    with open(_routes_file(), "w", encoding="utf-8") as f:
        json.dump(routes, f, indent=2, sort_keys=True)


def _user_desktop_windows() -> list[dict]:
    """Windows of the user's desktop, whatever desktop the calling thread is on."""
    out: list[dict] = []
    t = threading.Thread(target=lambda: out.extend(list_windows()))  # new threads: user's desktop
    t.start()
    t.join()
    return out


class AppBackend(Backend):
    name = "app"
    capabilities = WindowBackend.capabilities
    shares_user_clipboard = True

    def __init__(self, app: str, launch: str | list[str] | None = None, cwd: str | None = None,
                 timeout: float = 60.0, route: str = "auto", idle_ms: int = 800,
                 keep_app: bool = False, target_os: str = "windows"):
        """app: the app's name (window title / shortcut name). launch: how to start it, if no
        shortcut has that name (an .exe, a .bat, a .jar, an .ica...). route: 'auto' (learn),
        'hidden' or 'window' to force one. keep_app: leave an app we started running."""
        super().__init__(target_os)
        self.app, self.cwd, self.timeout, self.idle_ms = app, cwd, timeout, idle_ms
        self.keep_app = keep_app
        self._inner = None
        self._learned = route == "auto" and _load_routes().get(app.casefold())
        self.route = route if route != "auto" else (self._learned or "hidden")

        self.launch = launch or find_shortcut(app)
        mine = [w for w in _user_desktop_windows()
                if app.casefold() in w["title"].casefold() and w["rect"].w > 1]
        if mine and not launch:
            # already open: drive THAT window where it is (never start a second copy)
            self.route = "window"
            self._inner = WindowBackend(title=app, timeout=timeout, restore_minimized="invisible",
                                        idle_ms=idle_ms)
            return
        if not self.launch:
            raise SightlineError(f"no window named {app!r} is open and no shortcut named {app!r} "
                                 "was found in the Start menu or on the desktop: give launch=")
        if self.route == "hidden":
            self._start_hidden()
        else:
            self._start_window()

    # ---------------------------------------------------------------- the two routes
    def _start_hidden(self) -> None:
        from .hidden_win import HiddenDesktopBackend
        before = {w["hwnd"] for w in _user_desktop_windows()}
        self._exe_class = None

        def appeared_on_user_desktop() -> bool:
            return any(w["hwnd"] not in before and self.app.casefold() in w["title"].casefold()
                       for w in _user_desktop_windows())
        try:
            self._inner = HiddenDesktopBackend(launch=self.launch, cwd=self.cwd, timeout=self.timeout,
                                               idle_ms=self.idle_ms, abort_if=appeared_on_user_desktop)
        except AppElsewhere:
            # a launcher handed the app to the user's desktop: follow it there
            self._inner = WindowBackend(title=self.app, timeout=self.timeout,
                                        restore_minimized="invisible", idle_ms=self.idle_ms)
            self._hide_now()
            self._remember("window")
            self.route = "window"
            return
        self.route = "hidden"
        self._exe_class = (_exe_name(self._inner.pid),)

    def _start_window(self) -> None:
        before = {w["hwnd"] for w in _user_desktop_windows()}
        exe_class = getattr(self, "_exe_class", None)
        app = self.app.casefold()

        def ours(w) -> bool:                     # a NEW window of the app (not the user's own)
            if w["hwnd"] in before or w["class"] == "ConsoleWindowClass":
                return False
            if exe_class:
                return _exe_name(w["pid"]) == exe_class[0]
            return app in w["title"].casefold()
        self._inner = WindowBackend(launch=self.launch, cwd=self.cwd, timeout=self.timeout,
                                    match=ours, hide_launched=True, idle_ms=self.idle_ms,
                                    restore_minimized="invisible", close_launched=not self.keep_app)
        self.route = "window"

    def _hide_now(self) -> None:
        b = self._inner
        if b._make_invisible():
            b.api.u.SetWindowPos(b.hwnd, 1, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)

    def _remember(self, route: str) -> None:
        if not self._learned:
            _save_route(self.app, route)
            self._learned = route

    def _to_window_route(self) -> None:
        """The app ignores window messages on the hidden desktop: start it again on the
        user's desktop (invisible) where real input reaches it."""
        hidden = self._inner
        main = [w for w in hidden.run(hidden._inner._app_windows) if w["hwnd"] == hidden.hwnd]
        hidden.close()                           # closes the hidden copy
        self._start_window()
        if main:
            # same place, same size: what was located on the hidden copy is valid here
            r = main[0]["rect"]
            b = self._inner
            b.api.u.SetWindowPos(b.hwnd, 1, r.x, r.y, r.w, r.h, 0x0010)   # BOTTOM, NOACTIVATE
        self._remember("window")
        time.sleep(2.0)                          # let it draw

    def _call(self, name: str, *args, **kwargs):
        try:
            result = getattr(self._inner, name)(*args, **kwargs)
        except NeedsRealInput:
            if self.route != "hidden":
                raise
            self._to_window_route()
            return getattr(self._inner, name)(*args, **kwargs)
        if self.route == "hidden" and name in ("click", "type_text") and \
                getattr(self._inner, "_confirmed", False):
            self._remember("hidden")             # it obeyed on the hidden desktop
        return result

    def __getattr__(self, name):
        if name.startswith("__") or name == "_inner":
            raise AttributeError(name)
        return getattr(self._inner, name)

    def size(self):
        return self._call("size")

    def capture(self, region=None):
        return self._call("capture", region)

    def mouse_move(self, x, y):
        return self._call("mouse_move", x, y)

    def mouse_down(self, button="left"):
        return self._call("mouse_down", button)

    def mouse_up(self, button="left"):
        return self._call("mouse_up", button)

    def scroll(self, x, y, clicks, horizontal=False):
        return self._call("scroll", x, y, clicks, horizontal)

    def click(self, x, y, button="left", count=1, interval=0.08, hold=0.03):
        return self._call("click", x, y, button, count, interval, hold)

    def drag(self, *args, **kwargs):
        return self._call("drag", *args, **kwargs)

    def key_down(self, key):
        return self._call("key_down", key)

    def key_up(self, key):
        return self._call("key_up", key)

    def hotkey(self, combo):
        return self._call("hotkey", combo)

    def type_text(self, text, interval=0.0):
        return self._call("type_text", text, interval)

    def get_clipboard(self):
        return self._call("get_clipboard")

    def set_clipboard(self, text):
        return self._call("set_clipboard", text)

    def clipboard_serial(self):
        return self._call("clipboard_serial")

    def close(self):
        if self._inner is not None:
            self._inner.close()
