"""Image assets with variants.

Scripts name an element ("toolbar/save"); the store finds every crop that may show it
on the CURRENT target, most specific first:

    <root>/<os>/<theme>/toolbar/save*.png
    <root>/<os>/toolbar/save*.png
    <root>/<theme>/toolbar/save*.png
    <root>/toolbar/save*.png

"save*.png" = save.png plus variants: save~disabled_tab.png, save@2x.png (crop taken
on a 200% screen), save~old_version.png... All variants are tried; any of them counts.
So a new look of a button = drop one more file, no script change.
"""
from __future__ import annotations

import re
from pathlib import Path

from .errors import BadAsset
from .vision.match import Template

_VARIANT = re.compile(r"^(?P<base>[^~@]+)(~[^@]*)?(@[\d.]+x)?$")


class AssetStore:
    def __init__(self, roots: list[str | Path] | None = None, target_os: str | None = None,
                 theme: str | None = None):
        self.roots = [Path(r).resolve() for r in (roots or [])]
        self.target_os, self.theme = target_os, theme
        self._cache: dict[str, list[Template]] = {}

    def _dirs(self, root: Path) -> list[Path]:
        out = []
        if self.target_os and self.theme:
            out.append(root / self.target_os / self.theme)
        if self.target_os:
            out.append(root / self.target_os)
        if self.theme:
            out.append(root / self.theme)
        out.append(root)
        return out

    def resolve(self, name: str | Path) -> list[Template]:
        key = str(name)
        if key in self._cache:
            return self._cache[key]
        p = Path(name)
        if p.suffix.lower() in (".png", ".jpg", ".jpeg", ".bmp") and (p.is_absolute() or p.exists()):
            if not p.exists():
                raise BadAsset(f"Image not found: {p}")
            found = [p]
        else:
            stem = p.with_suffix("") if p.suffix.lower() in (".png", ".jpg", ".bmp") else p
            found = []
            for root in self.roots:
                for d in self._dirs(root):
                    folder = (d / stem).parent
                    if not folder.is_dir():
                        continue
                    for f in sorted(folder.iterdir()):
                        if f.suffix.lower() not in (".png", ".jpg", ".jpeg", ".bmp"):
                            continue
                        m = _VARIANT.match(f.stem)
                        if m and m.group("base") == stem.name:
                            found.append(f)
                if found:
                    break                       # first root that has it wins
            if not found:
                tried = [str(d / stem) + "*.png" for r in self.roots for d in self._dirs(r)]
                raise BadAsset(f"No image for '{name}'. Looked for: {tried or '(no asset roots)'}")
        # most specific folder first, then file order
        self._cache[key] = [Template.load(f) for f in found]
        return self._cache[key]
