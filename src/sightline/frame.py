"""A captured screen (or part of it) plus where it sits on the screen."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

import cv2
import numpy as np

from .geometry import Rect


@dataclass
class Frame:
    pixels: np.ndarray            # H x W x 3, BGR, uint8
    origin_x: int = 0             # screen position of pixels[0, 0]
    origin_y: int = 0
    timestamp: float = 0.0
    _gray: np.ndarray | None = field(default=None, repr=False)
    _digest: str | None = field(default=None, repr=False)

    @property
    def width(self) -> int:
        return int(self.pixels.shape[1])

    @property
    def height(self) -> int:
        return int(self.pixels.shape[0])

    @property
    def rect(self) -> Rect:
        return Rect(self.origin_x, self.origin_y, self.width, self.height)

    @property
    def gray(self) -> np.ndarray:
        if self._gray is None:
            self._gray = cv2.cvtColor(self.pixels, cv2.COLOR_BGR2GRAY)
        return self._gray

    @property
    def digest(self) -> str:
        """Content hash: used to cache OCR / matching results per identical frame."""
        if self._digest is None:
            self._digest = hashlib.blake2b(np.ascontiguousarray(self.pixels).tobytes(),
                                           digest_size=16).hexdigest()
        return self._digest

    def crop(self, r: Rect) -> Frame:
        """Sub-frame for a SCREEN rect (clipped to this frame)."""
        inside = r.intersect(self.rect)
        if inside is None:
            return Frame(np.zeros((0, 0, 3), np.uint8), r.x, r.y, self.timestamp)
        x0, y0 = inside.x - self.origin_x, inside.y - self.origin_y
        px = self.pixels[y0:y0 + inside.h, x0:x0 + inside.w]
        return Frame(px, inside.x, inside.y, self.timestamp)

    def to_local(self, r: Rect) -> Rect:
        return r.offset(-self.origin_x, -self.origin_y)

    def to_screen(self, r: Rect) -> Rect:
        return r.offset(self.origin_x, self.origin_y)

    def save(self, path: str) -> str:
        cv2.imwrite(str(path), self.pixels)
        return str(path)


def changed_bbox(a: Frame, b: Frame, pixel_threshold: int = 24, min_area: int = 30) -> Rect | None:
    """Screen rect covering what changed between two frames (what just APPEARED: a menu,
    a drop-down list, a dialog), or None. Frames may differ in size/origin (the window
    backend grows its frame when a popup opens): compared on their common screen area."""
    common = a.rect.union(b.rect)
    canvas = []
    for f in (a, b):
        img = np.zeros((common.h, common.w), np.uint8)
        x, y = f.origin_x - common.x, f.origin_y - common.y
        img[y:y + f.height, x:x + f.width] = f.gray
        canvas.append(img)
    mask = (cv2.absdiff(canvas[0], canvas[1]) > pixel_threshold).astype(np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((9, 9), np.uint8))
    n, _, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
    boxes = [stats[i] for i in range(1, n) if stats[i][4] >= min_area]
    if not boxes:
        return None
    x0 = min(s[0] for s in boxes)
    y0 = min(s[1] for s in boxes)
    x1 = max(s[0] + s[2] for s in boxes)
    y1 = max(s[1] + s[3] for s in boxes)
    return Rect.from_corners(x0 + common.x, y0 + common.y, x1 + common.x, y1 + common.y)


def region_changed(a: Frame, b: Frame, r: Rect, pixel_threshold: int = 24,
                   ratio: float = 0.002) -> bool:
    """True when the screen rect `r` differs between frames a and b by more than
    `ratio` of its pixels (each differing by more than `pixel_threshold` grey levels).
    Tolerant to anti-aliasing / compression noise, sensitive to real UI changes."""
    ca, cb = a.crop(r), b.crop(r)
    if ca.pixels.shape != cb.pixels.shape or ca.pixels.size == 0:
        return True
    diff = cv2.absdiff(ca.gray, cb.gray)
    changed = int((diff > pixel_threshold).sum())
    return changed > max(1, int(ratio * diff.size))
