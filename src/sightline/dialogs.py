"""Dialogs that come in any order, some always, some only sometimes.

Real apps: "Export done" always shows; "File exists, replace?" only the 2nd time;
a licence nag on Mondays. Scripting them in a fixed order is flaky. handle_dialogs
watches for all of them and answers whatever shows up, until every required one was
answered and no optional one appeared for `quiet` seconds.
"""
from __future__ import annotations

import time
from collections.abc import Sequence
from typing import TYPE_CHECKING

from .errors import SightlineError, StillVisible
from .locators import as_locator

if TYPE_CHECKING:
    from .session import Session


def handle_dialogs(session: Session, required: Sequence = (), optional: Sequence = (),
                   timeout: float = 20.0, quiet: float = 3.0, close_timeout: float = 10.0) -> list[str]:
    """Each item is the locator of the BUTTON to press (e.g. Text('OK').within(...)).
    Returns the descriptions of the buttons pressed, in order."""
    req = [as_locator(x) for x in required]
    opt = [as_locator(x) for x in optional]
    pending = list(req)
    pressed: list[str] = []
    deadline = time.monotonic() + timeout
    quiet_since = time.monotonic()
    while True:
        hit = None
        frame = session.capture()
        for loc in pending + opt:
            if session.find_all(loc, frame=frame):
                hit = loc
                break
        if hit is not None:
            session.click(hit, vanish=True, retries=1, expect_timeout=close_timeout)
            pressed.append(hit.describe())
            if hit in pending:
                pending.remove(hit)
            quiet_since = time.monotonic()
            deadline = max(deadline, time.monotonic() + 5)
            continue
        if not pending and (not opt or time.monotonic() - quiet_since >= quiet):
            return pressed
        if pending and time.monotonic() > deadline:
            msg = f"dialog(s) never appeared within {timeout:g}s: {[p.describe() for p in pending]}"
            raise SightlineError(msg, session.tracer.failure("handle_dialogs", "", msg, frame))
        time.sleep(session.poll)


__all__ = ["handle_dialogs", "StillVisible"]
