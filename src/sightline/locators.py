"""Locators: lazy, composable queries for something on screen.

A locator is a DESCRIPTION, never coordinates. It is re-run on a fresh capture every
time it is used, so a window that moved, a list that scrolled or a dialog that
re-opened never leaves a script holding stale positions.

    Image("toolbar/save")                       crop(s) from the asset store
    Text("Valider")                             OCR, fuzzy + accent-insensitive
    Text("Valider", exact=True), Text(re.compile(r"^Total\\b"))
    Image("ok") | Text("OK")                    fallback chain (first that finds wins)
    Text("OK").within(Image("dialog/title"), margin=300)
    Image("checkbox").right_of(Text("Actif"))   nearest one on the same row
    Text("Email :").area("right", 250)          the field next to a label (no crop needed)
    Image("row_icon").nth(2)                    3rd in reading order (0-based, -1 = last)
    Image("btn").offset(dx=40)                  click 40 px right of the match
"""
from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .frame import Frame
from .geometry import Rect
from .vision.match import Candidate, Template, match_template, nms, search_scales
from .vision.ocr import fold, group_phrases, search_text, similarity

if TYPE_CHECKING:
    from .session import Session


@dataclass
class Match:
    rect: Rect                       # screen pixels
    score: float
    by: str                          # which strategy found it
    text: str | None = None

    @property
    def center(self):
        return self.rect.center

    def __str__(self) -> str:
        t = f" '{self.text}'" if self.text else ""
        return f"{self.by}{t} {self.score:.3f} at {self.rect}"


@dataclass
class FindContext:
    frame: Frame
    session: Session
    region: Rect | None = None                 # search restriction (screen coords)
    diag: list[str] = field(default_factory=list)
    near: list[Rect] = field(default_factory=list)   # best below-threshold spots (debug)

    @property
    def area(self) -> Rect:
        r = self.frame.rect
        return (self.region.intersect(r) if self.region else r) or Rect(r.x, r.y, 0, 0)

    def narrowed(self, r: Rect) -> FindContext:
        inside = self.area.intersect(r)
        return FindContext(self.frame, self.session, inside or Rect(r.x, r.y, 0, 0), self.diag,
                           self.near)


def reading_order(ms: list[Match]) -> list[Match]:
    """Top-to-bottom rows (same row = vertical overlap >= 50%), left-to-right inside."""
    rows: list[list[Match]] = []
    for m in sorted(ms, key=lambda m: m.rect.center.y):
        for row in rows:
            if row[0].rect.vertical_overlap(m.rect) >= 0.5:
                row.append(m)
                break
        else:
            rows.append([m])
    return [m for row in rows for m in sorted(row, key=lambda m: m.rect.x)]


class Locator:
    def find(self, ctx: FindContext) -> list[Match]:
        raise NotImplementedError

    def describe(self) -> str:
        return type(self).__name__

    def __repr__(self) -> str:
        return self.describe()

    # -------- combinators
    def __or__(self, other: Locator) -> Fallback:
        return Fallback(self, other)

    def nth(self, index: int, order: str = "reading") -> Nth:
        return Nth(self, index, order)

    def within(self, container: Locator | Rect, margin: int = 0) -> Within:
        return Within(self, container, margin)

    def right_of(self, anchor: Locator, max_distance: int | None = None, aligned: bool = True):
        return Relative(self, anchor, "right", max_distance, aligned)

    def left_of(self, anchor: Locator, max_distance: int | None = None, aligned: bool = True):
        return Relative(self, anchor, "left", max_distance, aligned)

    def below(self, anchor: Locator, max_distance: int | None = None, aligned: bool = True):
        return Relative(self, anchor, "below", max_distance, aligned)

    def above(self, anchor: Locator, max_distance: int | None = None, aligned: bool = True):
        return Relative(self, anchor, "above", max_distance, aligned)

    def near(self, anchor: Locator, max_distance: int = 200):
        return Relative(self, anchor, "near", max_distance, False)

    def area(self, side: str, size: int, gap: int = 2, pad: int = 2) -> Area:
        return Area(self, side, size, gap, pad)

    def offset(self, dx: int = 0, dy: int = 0) -> Offset:
        return Offset(self, dx, dy)

    def where(self, predicate: Callable[[Match], bool], label: str = "filter") -> Where:
        return Where(self, predicate, label)


def as_locator(x: Locator | str | Rect) -> Locator:
    if isinstance(x, Locator):
        return x
    if isinstance(x, Rect):
        return At(x)
    if isinstance(x, str):
        from .spec import parse_locator
        return parse_locator(x)
    raise TypeError(f"Not a locator: {x!r}")


# ======================================================================= leaves
class Image(Locator):
    def __init__(self, *names: str | Template, similarity: float | None = None,
                 color: bool | float = False, scale_search: bool | None = None):
        if not names:
            raise ValueError("Image() needs at least one asset name or path")
        self.names = names
        self.similarity = similarity
        self.color = color
        self.scale_search = scale_search

    def describe(self) -> str:
        return "Image(" + ", ".join(n if isinstance(n, str) else n.path for n in self.names) + ")"

    def templates(self, session: Session) -> list[Template]:
        out: list[Template] = []
        for n in self.names:
            out.extend([n] if isinstance(n, Template) else session.assets.resolve(n))
        return out

    def find(self, ctx: FindContext) -> list[Match]:
        s = ctx.session
        area = ctx.area
        if area.w == 0 or area.h == 0:
            return []
        sub = ctx.frame.crop(area)
        thr = self.similarity if self.similarity is not None else s.similarity
        cthr = None
        if self.color:
            cthr = float(self.color) if not isinstance(self.color, bool) else s.color_similarity
        tpls = self.templates(s)
        found: list[Candidate] = []
        best: tuple[Candidate, Template] | None = None
        for t in tpls:
            expected = s.screen_scale / t.scale
            scales = [expected] if abs(expected - 1) < 1e-3 else \
                [expected] + [round(expected + d, 3) for d in (-0.01, 0.01, -0.02, 0.02)]
            cands, b = match_template(sub.pixels, sub.gray, t, thr, scales, cthr)
            found += cands
            if b and (best is None or b.score > best[0].score):
                best = (b, t)
        allow_scale = s.scale_search if self.scale_search is None else self.scale_search
        if not found and allow_scale:
            for t in tpls:
                cands, b = search_scales(sub.pixels, sub.gray, t, thr, s.screen_scale / t.scale,
                                         cthr)
                if cands:
                    ctx.diag.append(f"{self.describe()}: found only at scale "
                                    f"{cands[0].scale:.2f} - capture a variant for this screen "
                                    f"(e.g. {t.path.rsplit('.', 1)[0]}@{s.screen_scale:g}x.png)")
                found += cands
                if b and (best is None or b.score > best[0].score):
                    best = (b, t)
        found = nms(found)
        if not found and best:
            b, t = best
            ctx.near.append(b.rect.offset(sub.origin_x, sub.origin_y))
            ctx.diag.append(f"{self.describe()}: best {b.score:.3f} at "
                            f"{b.rect.offset(sub.origin_x, sub.origin_y)}"
                            + (f" colour {b.color:.3f}" if b.color is not None else "")
                            + f" < similarity {thr} ({t.path})")
        return [Match(c.rect.offset(sub.origin_x, sub.origin_y), c.score, self.describe())
                for c in found]


class Text(Locator):
    def __init__(self, query: str | re.Pattern, exact: bool = False,
                 similarity: float | None = None, accents: bool = False, case: bool = False):
        self.query, self.exact = query, exact
        self.similarity, self.accents, self.case = similarity, accents, case

    def describe(self) -> str:
        q = self.query.pattern if isinstance(self.query, re.Pattern) else self.query
        kind = "Regex" if isinstance(self.query, re.Pattern) else ("Exact" if self.exact else "Text")
        return f"{kind}({q!r})"

    def find(self, ctx: FindContext) -> list[Match]:
        s = ctx.session
        area = ctx.area
        if area.w == 0 or area.h == 0:
            return []
        words = s.ocr_words(ctx.frame, area)
        thr = self.similarity if self.similarity is not None else s.text_similarity
        hits = search_text(words, self.query, self.exact, thr, self.accents, self.case)
        if not hits and not isinstance(self.query, re.Pattern):
            q = fold(self.query, self.accents, self.case)
            near = sorted(((similarity(fold(p.text, self.accents, self.case), q), p)
                           for p in group_phrases(words)), key=lambda t: -t[0])[:3]
            if near:
                ctx.diag.append(f"{self.describe()}: closest OCR text " + ", ".join(
                    f"'{p.text}' ({sc:.2f}) at {p.rect.offset(area.x, area.y)}" for sc, p in near))
            else:
                ctx.diag.append(f"{self.describe()}: OCR read no text in {area}")
        return [Match(h.rect.offset(area.x, area.y), h.score, self.describe(), h.text)
                for h in hits]


class At(Locator):
    """A fixed screen rectangle (last resort, or a region to search in)."""

    def __init__(self, rect: Rect):
        self.rect = rect

    def describe(self) -> str:
        return f"At{self.rect}"

    def find(self, ctx: FindContext) -> list[Match]:
        return [Match(self.rect, 1.0, self.describe())]


# ======================================================================= combinators
class Fallback(Locator):
    def __init__(self, *options: Locator):
        flat: list[Locator] = []
        for o in options:
            flat.extend(o.options if isinstance(o, Fallback) else [o])
        self.options = flat

    def describe(self) -> str:
        return " | ".join(o.describe() for o in self.options)

    def find(self, ctx: FindContext) -> list[Match]:
        for o in self.options:
            ms = o.find(ctx)
            if ms:
                return ms
        return []


class Nth(Locator):
    def __init__(self, target: Locator, index: int, order: str = "reading"):
        self.target, self.index, self.order = target, index, order

    def describe(self) -> str:
        return f"{self.target.describe()}.nth({self.index})"

    def find(self, ctx: FindContext) -> list[Match]:
        ms = self.target.find(ctx)
        ordered = {"reading": reading_order, "x": lambda m: sorted(m, key=lambda a: a.rect.x),
                   "y": lambda m: sorted(m, key=lambda a: a.rect.y),
                   "score": lambda m: sorted(m, key=lambda a: -a.score)}[self.order](ms)
        try:
            return [ordered[self.index]]
        except IndexError:
            if ms:
                ctx.diag.append(f"{self.describe()}: only {len(ms)} match(es)")
            return []


def _one(anchor: Locator, ctx: FindContext, role: str) -> Match | None:
    """Anchors must be unambiguous, otherwise 'right of X' means nothing."""
    ms = anchor.find(ctx)
    if not ms:
        ctx.diag.append(f"{role} {anchor.describe()} not found")
        return None
    return ctx.session.pick(ms, anchor, role=role)


class Within(Locator):
    def __init__(self, target: Locator, container: Locator | Rect, margin: int = 0):
        self.target, self.margin = target, margin
        self.container = At(container) if isinstance(container, Rect) else container

    def describe(self) -> str:
        return f"{self.target.describe()}.within({self.container.describe()})"

    def find(self, ctx: FindContext) -> list[Match]:
        c = _one(self.container, ctx, "container")
        if c is None:
            return []
        return self.target.find(ctx.narrowed(c.rect.expand(self.margin)))


class Relative(Locator):
    def __init__(self, target: Locator, anchor: Locator, direction: str,
                 max_distance: int | None, aligned: bool):
        self.target, self.anchor, self.direction = target, anchor, direction
        self.max_distance, self.aligned = max_distance, aligned

    def describe(self) -> str:
        return f"{self.target.describe()}.{self.direction}({self.anchor.describe()})"

    def find(self, ctx: FindContext) -> list[Match]:
        a = _one(self.anchor, ctx, "anchor")
        if a is None:
            return []
        ar, d = a.rect, self.direction
        scored = []
        for m in self.target.find(ctx):
            r = m.rect
            if r.iou(ar) > 0.5:
                continue                                   # the anchor itself
            if d == "right":
                ok, dist, al = r.center.x > ar.right - 2, r.x - ar.right, r.vertical_overlap(ar)
            elif d == "left":
                ok, dist, al = r.center.x < ar.x + 2, ar.x - r.right, r.vertical_overlap(ar)
            elif d == "below":
                ok, dist, al = r.center.y > ar.bottom - 2, r.y - ar.bottom, r.horizontal_overlap(ar)
            elif d == "above":
                ok, dist, al = r.center.y < ar.y + 2, ar.y - r.bottom, r.horizontal_overlap(ar)
            else:                                          # near
                dx = max(ar.x - r.right, r.x - ar.right, 0)
                dy = max(ar.y - r.bottom, r.y - ar.bottom, 0)
                ok, dist, al = True, int((dx * dx + dy * dy) ** 0.5), 1.0
            if not ok or (self.aligned and al < 0.4):
                continue
            if self.max_distance is not None and dist > self.max_distance:
                continue
            scored.append((max(dist, 0), m))
        scored.sort(key=lambda t: (t[0], -t[1].score))
        if not scored:
            ctx.diag.append(f"nothing {d} of {a}")
            return []
        return [scored[0][1]]                              # the nearest one


class Area(Locator):
    """A synthetic region next to an anchor: the input field right of its label, the
    cell under a column title... `size` is the width (left/right) or height (above/
    below); the other dimension follows the anchor (+pad)."""

    def __init__(self, anchor: Locator, side: str, size: int, gap: int = 2, pad: int = 2):
        if side not in ("right", "left", "above", "below"):
            raise ValueError("side must be right/left/above/below")
        self.anchor, self.side, self.size, self.gap, self.pad = anchor, side, size, gap, pad

    def describe(self) -> str:
        return f"{self.anchor.describe()}.area({self.side},{self.size})"

    def find(self, ctx: FindContext) -> list[Match]:
        a = _one(self.anchor, ctx, "anchor")
        if a is None:
            return []
        r, g, p, s = a.rect, self.gap, self.pad, self.size
        rect = {"right": Rect(r.right + g, r.y - p, s, r.h + 2 * p),
                "left": Rect(r.x - g - s, r.y - p, s, r.h + 2 * p),
                "below": Rect(r.x - p, r.bottom + g, r.w + 2 * p, s),
                "above": Rect(r.x - p, r.y - g - s, r.w + 2 * p, s)}[self.side]
        rect = rect.intersect(ctx.frame.rect)
        return [Match(rect, a.score, self.describe())] if rect else []


class Offset(Locator):
    def __init__(self, target: Locator, dx: int, dy: int):
        self.target, self.dx, self.dy = target, dx, dy

    def describe(self) -> str:
        return f"{self.target.describe()}.offset({self.dx},{self.dy})"

    def find(self, ctx: FindContext) -> list[Match]:
        return [Match(m.rect.offset(self.dx, self.dy), m.score, self.describe(), m.text)
                for m in self.target.find(ctx)]


class Where(Locator):
    def __init__(self, target: Locator, predicate: Callable[[Match], bool], label: str):
        self.target, self.predicate, self.label = target, predicate, label

    def describe(self) -> str:
        return f"{self.target.describe()}.where({self.label})"

    def find(self, ctx: FindContext) -> list[Match]:
        return [m for m in self.target.find(ctx) if self.predicate(m)]
