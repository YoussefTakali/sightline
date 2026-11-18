"""A backend made of screenshots: test locators, OCR and grid readers OFFLINE.

Record frames from the real app once (`sightline shot`), then unit-test the vision
logic against them in CI - no app, no VNC, no flakiness. Input calls are recorded in
`self.events` so tests can assert where a click would have landed.

`frames` can be paths or arrays; `advance_on` decides when the next frame is shown
("capture": every capture, "input": after each click/key, "manual": call next()).
"""
from __future__ import annotations

import time
from collections.abc import Sequence
from pathlib import Path

import cv2
import numpy as np

from ..errors import SightlineError
from ..frame import Frame
from ..geometry import Rect
from .base import Backend


class ReplayBackend(Backend):
    name = "replay"
    capabilities = frozenset({"mouse", "keyboard", "scroll", "clipboard"})

    def __init__(self, frames: Sequence[str | Path | np.ndarray], advance_on: str = "manual",
                 target_os: str = "windows"):
        super().__init__(target_os)
        self._frames = []
        for f in frames:
            img = f if isinstance(f, np.ndarray) else cv2.imread(str(f), cv2.IMREAD_COLOR)
            if img is None:
                raise SightlineError(f"cannot read frame {f}")
            self._frames.append(np.ascontiguousarray(img[:, :, :3]))
        if not self._frames:
            raise SightlineError("ReplayBackend needs at least one frame")
        self.index = 0
        self.advance_on = advance_on
        self.events: list[tuple] = []
        self.clipboard = ""

    def next(self) -> None:
        self.index = min(self.index + 1, len(self._frames) - 1)

    def _input(self, *ev) -> None:
        self.events.append(ev)
        if self.advance_on == "input" and ev[0] in ("up", "type", "key_up", "scroll"):
            self.next()

    def size(self) -> tuple[int, int]:
        h, w = self._frames[self.index].shape[:2]
        return w, h

    def capture(self, region: Rect | None = None) -> Frame:
        f = Frame(self._frames[self.index].copy(), 0, 0, time.time())
        if self.advance_on == "capture":
            self.next()
        return f.crop(region) if region else f

    def mouse_move(self, x, y):
        self._input("move", x, y)

    def mouse_down(self, button="left"):
        self._input("down", button)

    def mouse_up(self, button="left"):
        self._input("up", button)

    def scroll(self, x, y, clicks, horizontal=False):
        self._input("scroll", x, y, clicks, horizontal)

    def key_down(self, key):
        self._input("key_down", key)

    def key_up(self, key):
        self._input("key_up", key)

    def type_text(self, text, interval=0.0):
        self._input("type", text)

    def get_clipboard(self) -> str:
        return self.clipboard

    def set_clipboard(self, text: str) -> None:
        self.clipboard = text

    def clicks(self) -> list[tuple[int, int, str]]:
        """(x, y, button) of every completed click, for assertions."""
        out, pos = [], (None, None)
        for ev in self.events:
            if ev[0] == "move":
                pos = (ev[1], ev[2])
            elif ev[0] == "up":
                out.append((pos[0], pos[1], ev[1]))
        return out
