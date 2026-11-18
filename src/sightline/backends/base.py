"""The Backend port: everything the framework needs from a screen + input device.

Implementations: LocalBackend (this machine), VNCBackend (any machine running a VNC
server: Xvfb container, Windows VM, Mac...), OculiXBackend (the OculiX/SikuliX jar).

A backend speaks capture pixels only (see geometry.py). Key names are the portable
names below; "mod" means Ctrl on Windows/Linux and Cmd on macOS *of the target* (with
VNC the target OS is not the OS this code runs on, so it comes from config).
"""
from __future__ import annotations

import time
from abc import ABC, abstractmethod

from ..errors import Unsupported
from ..frame import Frame
from ..geometry import Rect

KEY_ALIASES = {
    "control": "ctrl", "ctl": "ctrl", "return": "enter", "escape": "esc",
    "del": "delete", "pgup": "pageup", "pgdn": "pagedown", "page_up": "pageup",
    "page_down": "pagedown", "option": "alt", "command": "cmd", "meta": "cmd",
    "super": "win", "windows": "win", "bksp": "backspace", "ins": "insert",
    "arrowup": "up", "arrowdown": "down", "arrowleft": "left", "arrowright": "right",
}
NAMED_KEYS = {
    "ctrl", "shift", "alt", "cmd", "win", "enter", "tab", "esc", "backspace", "delete",
    "insert", "home", "end", "pageup", "pagedown", "up", "down", "left", "right", "space",
    "capslock", "menu", "printscreen",
} | {f"f{i}" for i in range(1, 13)}


def normalize_key(key: str, target_os: str = "windows") -> str:
    k = key.strip()
    if len(k) == 1:
        return k
    k = KEY_ALIASES.get(k.lower(), k.lower())
    if k == "mod":
        k = "cmd" if target_os == "macos" else "ctrl"
    if k not in NAMED_KEYS:
        raise ValueError(f"Unknown key name '{key}'. Known: {sorted(NAMED_KEYS)}")
    return k


def parse_combo(combo: str, target_os: str = "windows") -> list[str]:
    """'ctrl+shift+s' -> ['ctrl', 'shift', 's'];  '+' alone or 'ctrl++' -> plus key."""
    s = combo.strip()
    if s == "+":
        return ["+"]
    parts, buf = [], ""
    for i, ch in enumerate(s):
        if ch == "+" and buf:
            parts.append(buf)
            buf = ""
        elif ch == "+" and not buf and i == len(s) - 1:
            parts.append("+")
        else:
            buf += ch
    if buf:
        parts.append(buf)
    return [normalize_key(p, target_os) for p in parts]


class Backend(ABC):
    name = "abstract"
    #: subset of {"mouse", "keyboard", "scroll", "clipboard"}
    capabilities: frozenset[str] = frozenset({"mouse", "keyboard", "scroll"})

    def __init__(self, target_os: str = "windows"):
        self.target_os = target_os

    # ---- screen ----
    @abstractmethod
    def size(self) -> tuple[int, int]: ...

    @abstractmethod
    def capture(self, region: Rect | None = None) -> Frame: ...

    # ---- mouse (capture-pixel coordinates) ----
    @abstractmethod
    def mouse_move(self, x: int, y: int) -> None: ...

    @abstractmethod
    def mouse_down(self, button: str = "left") -> None: ...

    @abstractmethod
    def mouse_up(self, button: str = "left") -> None: ...

    @abstractmethod
    def scroll(self, x: int, y: int, clicks: int, horizontal: bool = False) -> None:
        """clicks > 0 scrolls down (or right), < 0 up (or left)."""

    def click(self, x: int, y: int, button: str = "left", count: int = 1,
              interval: float = 0.08, hold: float = 0.03) -> None:
        self.mouse_move(x, y)
        time.sleep(0.02)
        for i in range(count):
            self.mouse_down(button)
            time.sleep(hold)
            self.mouse_up(button)
            if i + 1 < count:
                time.sleep(interval)

    def drag(self, x0: int, y0: int, x1: int, y1: int, button: str = "left",
             steps: int = 12, hold: float = 0.15) -> None:
        """Press, move in small steps (many toolkits ignore a single jump), release."""
        self.mouse_move(x0, y0)
        time.sleep(0.05)
        self.mouse_down(button)
        time.sleep(hold)
        for i in range(1, steps + 1):
            self.mouse_move(round(x0 + (x1 - x0) * i / steps), round(y0 + (y1 - y0) * i / steps))
            time.sleep(0.015)
        time.sleep(hold)
        self.mouse_up(button)

    # ---- keyboard ----
    @abstractmethod
    def key_down(self, key: str) -> None: ...

    @abstractmethod
    def key_up(self, key: str) -> None: ...

    @abstractmethod
    def type_text(self, text: str, interval: float = 0.0) -> None: ...

    def hotkey(self, combo: str) -> None:
        keys = parse_combo(combo, self.target_os)
        for k in keys:
            self.key_down(k)
            time.sleep(0.02)
        for k in reversed(keys):
            self.key_up(k)
            time.sleep(0.02)

    # ---- clipboard (of the TARGET machine) ----
    def get_clipboard(self) -> str:
        raise Unsupported(f"{self.name} backend has no access to the target's clipboard")

    def set_clipboard(self, text: str) -> None:
        raise Unsupported(f"{self.name} backend has no access to the target's clipboard")

    def clipboard_serial(self) -> int:
        """Increments whenever the target clipboard is known to have changed (-1 = unknown,
        then callers compare contents instead)."""
        return -1

    def close(self) -> None:  # noqa: B027 - optional hook, most backends need nothing
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
