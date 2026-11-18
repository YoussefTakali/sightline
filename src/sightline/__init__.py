"""sightline - visual GUI automation: image + OCR locators, headed or headless."""
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
