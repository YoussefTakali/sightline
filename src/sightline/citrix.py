"""Citrix apps launched from a web portal (.ica files), for the 'hidden' and 'window' backends.

A Citrix web portal (StoreFront) gives a .ica file: a one-time ticket (valid ~1 minute) that
the Citrix client (wfica32.exe) opens. Started on a hidden desktop, the whole Citrix session
lives there - nothing on the user's screen.

    from sightline.citrix import wait_for_ica, citrix_launch
    ica = wait_for_ica()                 # click the app in the browser: picks the fresh .ica
    s = Session(HiddenDesktopBackend(launch=citrix_launch(ica)))
"""
from __future__ import annotations

import os
import sys
import tempfile
import time

from .errors import SightlineError

# sections of a .ica file that are NOT an application/connection section
_GLOBAL_SECTIONS = {"applicationservers", "wfclient", "encoding", "compress", "encrypt",
                    "encryptionlevelsession", "virtualchannels"}


def downloads_folder() -> str:
    return os.path.join(os.path.expanduser("~"), "Downloads")


def wait_for_ica(folder: str | None = None, timeout: float = 180.0, since: float | None = None,
                 max_age: float = 60.0) -> str:
    """Path of a FRESH .ica in `folder` (default: Downloads): one downloaded after `since`
    (default: now - max_age), waiting up to `timeout` for it. Partial downloads
    (.crdownload/.part/.tmp) are ignored until complete."""
    folder = folder or downloads_folder()
    since = time.time() - max_age if since is None else since
    deadline = time.monotonic() + timeout
    while True:
        try:
            found = [os.path.join(folder, n) for n in os.listdir(folder) if n.lower().endswith(".ica")]
        except FileNotFoundError:
            found = []
        found = [p for p in found if os.path.getmtime(p) >= since and os.path.getsize(p) > 0]
        if found:
            newest = max(found, key=os.path.getmtime)
            time.sleep(0.3)                                 # let the browser finish writing
            return newest
        if time.monotonic() > deadline:
            raise SightlineError(f"no new .ica file appeared in {folder} within {timeout:g}s "
                                 "(click the application in the Citrix web page; if the browser "
                                 "opens .ica files automatically, turn that off for .ica)")
        time.sleep(0.5)


def read_ica(path: str) -> list[str]:
    for enc in ("utf-8-sig", "cp1252"):
        try:
            with open(path, encoding=enc) as f:
                return f.read().splitlines()
        except UnicodeDecodeError:
            continue
    raise SightlineError(f"{path}: not a readable .ica file")


def prepare_ica(path: str, windowed: bool = True, size: tuple[int, int] | None = (1920, 1080),
                out_dir: str | None = None) -> str:
    """A copy of the .ica, adjusted for automation:
    windowed=True: TWIMode=Off - the session is shown in ONE regular window (instead of
        seamless windows mixed with the user's desktop), at `size`.
    The original is not changed; the copy is private to the user (it holds a ticket)."""
    lines = read_ica(path)
    if not any(ln.strip().lower() == "[applicationservers]" for ln in lines) and \
            not any(ln.lower().startswith("address=") for ln in lines):
        raise SightlineError(f"{path} does not look like a Citrix .ica file")
    wanted: dict[str, str] = {}
    if windowed:
        wanted["TWIMode"] = "Off"
        if size:
            wanted.update(DesiredHRES=str(size[0]), DesiredVRES=str(size[1]), ScreenPercent="0")
    out: list[str] = []
    section, pending = None, {}

    def flush():
        if section is not None and section.lower() not in _GLOBAL_SECTIONS:
            out.extend(f"{k}={v}" for k, v in pending.items())

    for ln in lines:
        s = ln.strip()
        if s.startswith("[") and s.endswith("]"):
            flush()
            section, pending = s[1:-1], dict(wanted)
            out.append(ln)
            continue
        key = s.split("=", 1)[0].strip() if "=" in s else None
        if section is not None and section.lower() not in _GLOBAL_SECTIONS and key:
            match = next((k for k in pending if k.lower() == key.lower()), None)
            if match:
                out.append(f"{key}={pending.pop(match)}")
                continue
        out.append(ln)
    flush()
    fd, dst = tempfile.mkstemp(suffix=".ica", prefix="sightline-", dir=out_dir)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\r\n") as f:
        f.write("\n".join(out) + "\n")
    return dst


def find_wfica() -> str | None:
    """The Citrix client engine (wfica32.exe), machine-wide or per-user install."""
    if sys.platform == "win32":
        import winreg
        for root, key in ((winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Citrix\Install\ICA Client"),
                          (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Citrix\Install\ICA Client"),
                          (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Citrix\Install\ICA Client")):
            try:
                with winreg.OpenKey(root, key) as k:
                    folder = winreg.QueryValueEx(k, "InstallFolder")[0]
                exe = os.path.join(folder, "wfica32.exe")
                if os.path.isfile(exe):
                    return exe
            except OSError:
                continue
    for base in (os.environ.get("PROGRAMFILES(X86)"), os.environ.get("PROGRAMFILES"),
                 os.environ.get("LOCALAPPDATA")):
        if base:
            exe = os.path.join(base, "Citrix", "ICA Client", "wfica32.exe")
            if os.path.isfile(exe):
                return exe
    return None


def citrix_launch(ica: str, windowed: bool = True, size: tuple[int, int] | None = (1920, 1080),
                  remove_original: bool = True) -> list[str]:
    """The `launch` command for a backend: the Citrix client opening a prepared copy of the
    .ica. remove_original: the downloaded ticket is deleted (it must not be reused)."""
    exe = find_wfica()
    if not exe:
        raise SightlineError("Citrix client (wfica32.exe) not found - is Citrix Workspace installed?")
    prepared = prepare_ica(ica, windowed=windowed, size=size)
    if remove_original:
        try:
            os.remove(ica)
        except OSError:
            pass
    return [exe, prepared]
