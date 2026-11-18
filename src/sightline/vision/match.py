"""Template matching that behaves well on real screens.

- ALL occurrences, not "whatever SikuliX returns first": local maxima + non-maximum
  suppression, so duplicates can be counted, ordered and disambiguated.
- Scale: searched at the expected scale first (screen DPI / asset DPI); only if that
  fails, a bounded range around it (logged, so you capture a proper variant).
- Transparent PNG regions are masked out (crop a button WITHOUT its background).
- Optional colour check: TM_CCOEFF_NORMED is blind to brightness/contrast, so a greyed
  (disabled) button or an unchecked box can score like the real one. color=True adds a
  per-pixel colour similarity gate.
- Flat (single-colour) crops are rejected at load time: they match everywhere.
- The best candidate BELOW threshold is always reported, which turns "image not found"
  into "best was 0.86 at (412,300), threshold 0.90".
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np

from ..errors import BadAsset
from ..geometry import Rect

_SCALE_RE = re.compile(r"@(\d+(?:\.\d+)?)x$")


@dataclass
class Template:
    path: str
    bgr: np.ndarray
    gray: np.ndarray
    mask: np.ndarray | None = None
    scale: float = 1.0          # DPI scale of the screen the crop was taken on
    _cache: dict = field(default_factory=dict, repr=False)

    @classmethod
    def load(cls, path: str | Path) -> Template:
        p = Path(path)
        raw = cv2.imread(str(p), cv2.IMREAD_UNCHANGED)
        if raw is None:
            raise BadAsset(f"Cannot read image: {p}")
        mask = None
        if raw.ndim == 2:
            bgr = cv2.cvtColor(raw, cv2.COLOR_GRAY2BGR)
        elif raw.shape[2] == 4:
            alpha = raw[:, :, 3]
            bgr = np.ascontiguousarray(raw[:, :, :3])
            if (alpha < 250).any():
                mask = np.where(alpha >= 128, 255, 0).astype(np.uint8)
                if mask.sum() == 0:
                    raise BadAsset(f"Image is fully transparent: {p}")
        else:
            bgr = raw
        m = _SCALE_RE.search(p.stem)
        return cls.from_array(bgr, str(p), mask, float(m.group(1)) if m else 1.0)

    @classmethod
    def from_array(cls, bgr: np.ndarray, name: str = "<array>", mask: np.ndarray | None = None,
                   scale: float = 1.0) -> Template:
        if bgr.ndim == 2:
            bgr = cv2.cvtColor(bgr, cv2.COLOR_GRAY2BGR)
        h, w = bgr.shape[:2]
        if h < 3 or w < 3:
            raise BadAsset(f"Image too small to match reliably ({w}x{h}): {name}")
        gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
        vals = gray[mask > 0] if mask is not None else gray
        if float(vals.std()) < 3.0:
            raise BadAsset(f"Image is (almost) a single colour, it would match anywhere: {name}. "
                           "Crop something with an edge, text or icon on it.")
        return cls(name, bgr, gray, mask, scale)

    @property
    def size(self) -> tuple[int, int]:
        return self.bgr.shape[1], self.bgr.shape[0]

    def scaled(self, s: float) -> tuple[np.ndarray, np.ndarray, np.ndarray | None]:
        key = round(s, 4)
        if key not in self._cache:
            if abs(s - 1.0) < 1e-3:
                self._cache[key] = (self.gray, self.bgr, self.mask)
            else:
                w, h = self.size
                nw, nh = max(int(round(w * s)), 3), max(int(round(h * s)), 3)
                interp = cv2.INTER_AREA if s < 1 else cv2.INTER_CUBIC
                g = cv2.resize(self.gray, (nw, nh), interpolation=interp)
                c = cv2.resize(self.bgr, (nw, nh), interpolation=interp)
                m = None if self.mask is None else cv2.resize(self.mask, (nw, nh),
                                                              interpolation=cv2.INTER_NEAREST)
                self._cache[key] = (g, c, m)
        return self._cache[key]


@dataclass
class Candidate:
    rect: Rect                  # in the coordinates of the searched image
    score: float
    scale: float = 1.0
    color: float | None = None

    def __str__(self) -> str:
        c = f" colour={self.color:.3f}" if self.color is not None else ""
        s = f" scale={self.scale:.2f}" if abs(self.scale - 1) > 1e-3 else ""
        return f"{self.score:.3f} at {self.rect}{s}{c}"


def nms(cands: list[Candidate], iou: float = 0.3) -> list[Candidate]:
    """Keep the best of overlapping candidates (also across scales)."""
    out: list[Candidate] = []
    for c in sorted(cands, key=lambda c: -c.score):
        if all(c.rect.iou(o.rect) < iou and not _center_inside(c.rect, o.rect) for o in out):
            out.append(c)
    return out


def _center_inside(a: Rect, b: Rect) -> bool:
    return b.contains(a.center) or a.contains(b.center)


def color_similarity(hay_bgr: np.ndarray, rect: Rect, tpl_bgr: np.ndarray,
                     mask: np.ndarray | None) -> float:
    patch = hay_bgr[rect.y:rect.bottom, rect.x:rect.right]
    if patch.shape[:2] != tpl_bgr.shape[:2]:
        tpl_bgr = cv2.resize(tpl_bgr, (patch.shape[1], patch.shape[0]))
        if mask is not None:
            mask = cv2.resize(mask, (patch.shape[1], patch.shape[0]),
                              interpolation=cv2.INTER_NEAREST)
    diff = cv2.absdiff(patch, tpl_bgr).astype(np.float32).mean(axis=2)
    if mask is not None:
        diff = diff[mask > 0]
    return float(1.0 - diff.mean() / 255.0) if diff.size else 0.0


def _peaks(res: np.ndarray, threshold: float, tw: int, th: int, limit: int) -> list[tuple[float, int, int]]:
    k = max(3, (min(tw, th) // 2) | 1)
    dil = cv2.dilate(res, np.ones((k, k), np.uint8))
    ys, xs = np.where((res >= threshold) & (res >= dil - 1e-6))
    if xs.size == 0:
        return []
    scores = res[ys, xs]
    order = np.argsort(-scores)[: limit * 4]
    return [(float(scores[i]), int(xs[i]), int(ys[i])) for i in order]


def match_template(hay_bgr: np.ndarray, hay_gray: np.ndarray, tpl: Template, threshold: float,
                   scales: list[float], color_threshold: float | None = None,
                   limit: int = 64) -> tuple[list[Candidate], Candidate | None]:
    """Returns (matches >= threshold after NMS, best candidate overall for diagnostics)."""
    found: list[Candidate] = []
    best: Candidate | None = None
    H, W = hay_gray.shape[:2]
    for s in scales:
        g, c, m = tpl.scaled(s)
        th, tw = g.shape[:2]
        if th > H or tw > W:
            continue
        if m is not None:
            res = cv2.matchTemplate(hay_gray, g, cv2.TM_CCOEFF_NORMED, mask=m)
        else:
            res = cv2.matchTemplate(hay_gray, g, cv2.TM_CCOEFF_NORMED)
        res = np.nan_to_num(res, nan=0.0, posinf=0.0, neginf=0.0)
        np.clip(res, -1.0, 1.0, out=res)
        _, mx, _, (bx, by) = cv2.minMaxLoc(res)
        if best is None or mx > best.score:
            best = Candidate(Rect(bx, by, tw, th), float(mx), s)
        for score, x, y in _peaks(res, threshold, tw, th, limit):
            cand = Candidate(Rect(x, y, tw, th), score, s)
            if color_threshold is not None:
                cand.color = color_similarity(hay_bgr, cand.rect, c, m)
                if cand.color < color_threshold:
                    if best is not None and best.rect == cand.rect:
                        best.color = cand.color
                    continue
            found.append(cand)
    return nms(found)[:limit], best


def scale_ladder(expected: float, lo: float = 0.75, hi: float = 1.35, step: float = 0.05) -> list[float]:
    """Fallback scales ordered by closeness to the expected one."""
    vals = {round(float(expected * f), 3) for f in np.arange(lo, hi + 1e-9, step)}
    vals.discard(round(float(expected), 3))
    return sorted(vals, key=lambda v: abs(v - expected))


def search_scales(hay_bgr: np.ndarray, hay_gray: np.ndarray, tpl: Template, threshold: float,
                  expected: float, color_threshold: float | None = None
                  ) -> tuple[list[Candidate], Candidate | None]:
    """Coarse ladder, then a 0.01-step pass around the two best coarse scales.
    Small crops are very sensitive to rounding: on a 1.25x screen a 31 px tall button
    can score 0.77 at 1.25 (39 px) and 0.97 at 1.24 (38 px)."""
    found: list[Candidate] = []
    per_scale: list[tuple[float, float]] = []
    best: Candidate | None = None
    for s in scale_ladder(expected):
        c, b = match_template(hay_bgr, hay_gray, tpl, threshold, [s], color_threshold)
        found += c
        if b is not None:
            per_scale.append((b.score, s))
            if best is None or b.score > best.score:
                best = b
    tried = set(scale_ladder(expected)) | {round(float(expected), 3)}
    for _, s0 in sorted(per_scale, reverse=True)[:2]:
        fine = [round(s0 + d, 3) for d in np.arange(-0.04, 0.0401, 0.01)]
        fine = [s for s in fine if s > 0.2 and s not in tried]
        tried.update(fine)
        c, b = match_template(hay_bgr, hay_gray, tpl, threshold, fine, color_threshold)
        found += c
        if b is not None and (best is None or b.score > best.score):
            best = b
    return nms(found), best
