"""Rectangles and points in SCREEN PIXELS (the pixels of a captured frame).

Every coordinate inside sightline is a capture pixel. Backends are the only place
that converts to whatever their input API wants (logical points on macOS Retina,
scaled coordinates on a HiDPI Windows screen, ...). Mixing the two is the classic
source of "the click lands 20% off" bugs, so it is confined to one layer.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Point:
    x: int
    y: int

    def offset(self, dx: int = 0, dy: int = 0) -> Point:
        return Point(self.x + dx, self.y + dy)


@dataclass(frozen=True)
class Rect:
    x: int
    y: int
    w: int
    h: int

    @classmethod
    def from_corners(cls, x0: int, y0: int, x1: int, y1: int) -> Rect:
        return cls(int(x0), int(y0), int(max(x1 - x0, 0)), int(max(y1 - y0, 0)))

    @property
    def right(self) -> int:
        return self.x + self.w

    @property
    def bottom(self) -> int:
        return self.y + self.h

    @property
    def center(self) -> Point:
        return Point(self.x + self.w // 2, self.y + self.h // 2)

    @property
    def area(self) -> int:
        return max(self.w, 0) * max(self.h, 0)

    def offset(self, dx: int = 0, dy: int = 0) -> Rect:
        return Rect(self.x + dx, self.y + dy, self.w, self.h)

    def expand(self, px: int) -> Rect:
        return Rect(self.x - px, self.y - px, self.w + 2 * px, self.h + 2 * px)

    def intersect(self, other: Rect) -> Rect | None:
        x0, y0 = max(self.x, other.x), max(self.y, other.y)
        x1, y1 = min(self.right, other.right), min(self.bottom, other.bottom)
        if x1 <= x0 or y1 <= y0:
            return None
        return Rect(x0, y0, x1 - x0, y1 - y0)

    def union(self, other: Rect) -> Rect:
        return Rect.from_corners(min(self.x, other.x), min(self.y, other.y),
                                 max(self.right, other.right), max(self.bottom, other.bottom))

    def contains(self, p: Point) -> bool:
        return self.x <= p.x < self.right and self.y <= p.y < self.bottom

    def iou(self, other: Rect) -> float:
        inter = self.intersect(other)
        if inter is None:
            return 0.0
        return inter.area / float(self.area + other.area - inter.area)

    def vertical_overlap(self, other: Rect) -> float:
        """Fraction of the smaller height shared by both rects (1.0 = same row)."""
        top, bot = max(self.y, other.y), min(self.bottom, other.bottom)
        return max(bot - top, 0) / float(max(min(self.h, other.h), 1))

    def horizontal_overlap(self, other: Rect) -> float:
        left, right = max(self.x, other.x), min(self.right, other.right)
        return max(right - left, 0) / float(max(min(self.w, other.w), 1))

    def __str__(self) -> str:
        return f"({self.x},{self.y} {self.w}x{self.h})"
