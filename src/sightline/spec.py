"""Locator strings, for Robot Framework keywords and the CLI.

    image:toolbar/save                 asset name (or a path ending in .png)
    toolbar/save.png                   same (bare image path)
    text:Valider                       OCR, fuzzy, accent/case-insensitive
    exact:Valider                      OCR, exact (after whitespace/case folding)
    regex:^Total\\s+\\d+                 OCR, regular expression
    at:100,200,50,20                   fixed rectangle x,y,w,h

    options after ';'                  image:save;similarity=0.8;color=true
    A || B                             fallback: B only if A finds nothing
    C >> T                             T searched inside C (C must be unique)
    ...[2]                             2nd match in reading order (1-based, [-1] = last)
    text:Email : @right:250            the 250 px wide area right of the label (a field)
"""
from __future__ import annotations

import re

from .geometry import Rect
from .locators import At, Fallback, Image, Locator, Text

_NTH = re.compile(r"^(?P<body>.*?)\s*\[(?P<n>-?\d+)\]\s*$")
_AREA = re.compile(r"^(?P<body>.*?)\s+@(?P<side>right|left|above|below):(?P<size>\d+)\s*$")


def _bool(v: str) -> bool:
    return v.strip().lower() in ("1", "true", "yes", "on")


def _leaf(spec: str) -> Locator:
    s = spec.strip()
    a = _AREA.match(s)
    if a:
        return _leaf(a.group("body")).area(a.group("side"), int(a.group("size")), gap=4)
    m = _NTH.match(s)
    if m and not s.startswith(("regex:", "re:")):
        n = int(m.group("n"))
        return _leaf(m.group("body")).nth(n - 1 if n > 0 else n)
    kind, sep, rest = s.partition(":")
    if not sep or kind.lower() not in ("image", "img", "text", "exact", "regex", "re", "at"):
        if s.lower().endswith((".png", ".jpg", ".bmp")):
            kind, rest = "image", s
        else:
            raise ValueError(f"Locator '{spec}': use image:, text:, exact:, regex: or at:")
    kind = kind.lower()
    body, *opts = rest.split(";") if kind not in ("regex", "re") else [rest]
    kw = {}
    for o in opts:
        k, _, v = o.partition("=")
        kw[k.strip().lower()] = v.strip()
    if kind in ("image", "img"):
        color: bool | float = False
        if "color" in kw:
            try:
                color = float(kw["color"]) if "." in kw["color"] else _bool(kw["color"])
            except ValueError:
                raise ValueError(f"Locator '{spec}': color=true|false|0.93") from None
        return Image(*[b.strip() for b in body.split("|")], color=color,
                     similarity=float(kw["similarity"]) if "similarity" in kw else None)
    if kind == "text":
        return Text(body.strip(), similarity=float(kw["similarity"]) if "similarity" in kw else None,
                    accents=_bool(kw.get("accents", "false")), case=_bool(kw.get("case", "false")))
    if kind == "exact":
        return Text(body.strip(), exact=True, accents=_bool(kw.get("accents", "false")),
                    case=_bool(kw.get("case", "false")))
    if kind in ("regex", "re"):
        return Text(re.compile(rest.strip()))
    x, y, w, h = (int(v) for v in body.split(","))
    return At(Rect(x, y, w, h))


def parse_locator(spec: str) -> Locator:
    alternatives = [a for a in re.split(r"\s+\|\|\s+", spec.strip()) if a]
    locs = []
    for alt in alternatives:
        parts = [p for p in re.split(r"\s+>>\s+", alt) if p]
        loc = _leaf(parts[-1])
        for container in reversed(parts[:-1]):
            loc = loc.within(_leaf(container))
        locs.append(loc)
    return locs[0] if len(locs) == 1 else Fallback(*locs)
