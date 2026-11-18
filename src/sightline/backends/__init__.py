"""Backend factory. A profile names a backend and its options; scripts never do."""
from __future__ import annotations

from typing import Any

from .base import Backend, normalize_key, parse_combo


def create_backend(kind: str, **opts: Any) -> Backend:
    kind = kind.lower()
    if kind == "vnc":
        from .vnc import VNCBackend
        return VNCBackend(**opts)
    if kind == "local":
        from .local import LocalBackend
        return LocalBackend(**opts)
    if kind == "oculix":
        from .oculix import OculiXBackend
        return OculiXBackend(**opts)
    if kind == "display":
        from .display import PrivateDisplayBackend
        return PrivateDisplayBackend(**opts)
    if kind == "window":
        from .window_win import WindowBackend
        return WindowBackend(**opts)
    if kind == "browser":
        from .browser import BrowserBackend
        return BrowserBackend(**opts)
    if kind == "replay":
        from .replay import ReplayBackend
        return ReplayBackend(**opts)
    raise ValueError(f"Unknown backend '{kind}' (vnc | local | oculix | replay)")


__all__ = ["Backend", "create_backend", "normalize_key", "parse_combo"]
