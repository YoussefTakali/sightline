from __future__ import annotations


class SightlineError(AssertionError):
    """Base error. Subclasses AssertionError so Robot Framework / pytest report it as a
    test failure (not an error in the framework). `artifact` is a debug screenshot."""

    def __init__(self, message: str, artifact: str | None = None):
        if artifact:
            message = f"{message}\nDebug screenshot: {artifact}"
        super().__init__(message)
        self.artifact = artifact


class NotFound(SightlineError):
    pass


class Ambiguous(SightlineError):
    """Several distinct matches where exactly one was required. Disambiguate with
    .nth(i), .within(...), an anchor (.right_of(...)) or a tighter crop."""

    def __init__(self, message: str, artifact: str | None = None, matches: list | None = None):
        super().__init__(message, artifact)
        self.matches = matches or []


class StillVisible(SightlineError):
    pass


class Unstable(SightlineError):
    """The element was found but kept moving / changing (animation never settled)."""


class Unsupported(SightlineError):
    """The backend cannot do this (e.g. clipboard over a backend that has none).
    Raised loudly instead of silently using the LOCAL machine's clipboard/files."""


class BadAsset(SightlineError):
    pass
