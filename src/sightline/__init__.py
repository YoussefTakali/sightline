"""sightline - visual GUI automation: image + OCR locators, headed or headless."""
import sys as _sys

for _mod, _pkg in (("numpy", "numpy"), ("cv2", "opencv-python-headless")):
    try:
        __import__(_mod)
    except ImportError as _e:                   # say WHAT is missing and HOW to fix it
        raise ImportError(
            f"sightline needs '{_pkg}', which is not installed for this Python "
            f"({_sys.executable}, {_sys.version.split()[0]}): {_e}. Fix: activate the virtual "
            f"environment, then run `pip install -e .` in the sightline folder (see "
            f"`python tools/diagnose.py`).") from _e

from .backends import Backend, create_backend
from .dialogs import handle_dialogs
from .errors import Ambiguous, BadAsset, NotFound, SightlineError, StillVisible, Unstable, Unsupported
from .frame import Frame
from .geometry import Point, Rect
from .locators import At, Image, Locator, Match, Text
from .session import Element, Session
from .spec import parse_locator
from .tables import parse_tsv, read_table_via_clipboard

__version__ = "0.1.0"

__all__ = [
    "Ambiguous", "At", "Backend", "BadAsset", "Element", "Frame", "Image", "Locator", "Match",
    "NotFound", "Point", "Rect", "Session", "SightlineError", "StillVisible", "Text", "Unstable",
    "Unsupported", "create_backend", "handle_dialogs", "parse_locator", "parse_tsv",
    "read_table_via_clipboard",
]
