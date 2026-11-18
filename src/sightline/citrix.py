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


# ====================================================================== web portal -> .ica
_DETECT_BUTTONS = ("Already installed", "Déjà installé", "Use full version", "Utiliser la version complète")
_OPEN_BUTTONS = ("Open", "Ouvrir", "Launch", "Lancer", "Démarrer")
_APP_TABS = ("Apps", "APPS", "Applications", "APPLICATIONS", "All Apps", "Toutes les applications")


def fetch_ica(portal: str, app: str, user_data_dir: str, out_dir: str | None = None,
              channel: str | None = None, executable: str | None = None, headless: bool = True,
              timeout: float = 90.0, browser_args: list[str] | None = None,
              screenshot: str | None = "citrix_portal.png") -> str:
    """Get a fresh .ica for `app` from a Citrix web portal (StoreFront / Workspace), with a
    headless browser of sightline's own: the file is CAUGHT, never opened - unlike a company
    browser that opens .ica files automatically (policies target Edge/Chrome, not this one).

    Login: once, in a visible window, with the same profile:
        sightline browser-login https://citrix.company.com --user-data-dir <dir>
    Windows single sign-on (Kerberos/NTLM): browser_args=["--auth-server-allowlist=*.company.com"].
    On failure the page is saved to `screenshot` to see where it stopped."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise SightlineError('fetch_ica needs Playwright: pip install -e ".[browser]" then '
                             "python -m playwright install chromium") from None
    opts: dict = {"headless": headless, "accept_downloads": True, "args": list(browser_args or [])}
    if channel:
        opts["channel"] = channel
    if executable:
        opts["executable_path"] = executable
    out_dir = out_dir or tempfile.mkdtemp(prefix="sightline-ica-")
    with sync_playwright() as p:
        try:
            ctx = p.chromium.launch_persistent_context(os.path.expanduser(user_data_dir), **opts)
        except Exception as e:
            raise SightlineError(f"could not start the headless browser ({str(e).splitlines()[0]}) - "
                                 "run: python -m playwright install chromium") from None
        downloads: list = []
        ctx.on("page", lambda pg: pg.on("download", lambda d: downloads.append(d)))
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.on("download", lambda d: downloads.append(d))
        try:
            page.goto(portal, wait_until="domcontentloaded")
            _open_app(page, app, downloads, timeout)
            dst = os.path.join(out_dir, "launch.ica")
            downloads[0].save_as(dst)
            return dst
        except BaseException:
            if screenshot:
                try:
                    page.screenshot(path=screenshot)
                except Exception:
                    pass
            raise
        finally:
            ctx.close()


def _visible(locator):
    try:
        n = locator.count()
    except Exception:
        return None
    for i in range(min(n, 20)):
        el = locator.nth(i)
        try:
            if el.is_visible():
                return el
        except Exception:
            continue
    return None


def _click_text(page, texts, exact=True) -> bool:
    for t in texts:
        for frame in page.frames:
            el = _visible(frame.get_by_role("button", name=t, exact=exact)) or \
                 _visible(frame.get_by_role("link", name=t, exact=exact))
            if el:
                el.click()
                return True
    return False


def _open_app(page, app: str, downloads: list, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    clicked_at, searched, tabs_tried = 0.0, False, False
    while not downloads:
        if time.monotonic() > deadline:
            what = "clicked, but no .ica came" if clicked_at else f"application {app!r} not found"
            raise SightlineError(f"Citrix portal: {what} within {timeout:g}s (see the screenshot)")
        if _visible(page.locator("input[type=password]")):
            raise SightlineError("the Citrix portal asks for a login: log in once with "
                                 "`sightline browser-login <portal> --user-data-dir <same dir>`")
        if _click_text(page, _DETECT_BUTTONS):             # client detection screen
            page.wait_for_timeout(800)
            continue
        if clicked_at and _click_text(page, _OPEN_BUTTONS):  # an app details pane
            page.wait_for_timeout(1500)
            continue
        if not clicked_at or time.monotonic() - clicked_at > 15:
            tile = None
            for frame in page.frames:
                tile = _visible(frame.get_by_text(app, exact=True)) or \
                       _visible(frame.get_by_text(app, exact=False))
                if tile:
                    break
            if tile:
                tile.click()
                clicked_at = time.monotonic()
                page.wait_for_timeout(1500)
                continue
            if not tabs_tried and _click_text(page, _APP_TABS, exact=False):
                tabs_tried = True
                page.wait_for_timeout(1500)
                continue
            box = _visible(page.locator("input[type=search], input[placeholder*='earch'], "
                                        "input[placeholder*='echerch'], input[aria-label*='earch']"))
            if box and not searched:
                box.fill(app)
                box.press("Enter")
                searched = True
                page.wait_for_timeout(1500)
                continue
        page.wait_for_timeout(500)


# ====================================================================== take over a session
CITRIX_VIEWERS = ("wfica32.exe", "wfica.exe", "cdviewer.exe")


def disconnect_local(window_title: str, wait: float = 3.0) -> list[int]:
    """Disconnect the Citrix session that shows `window_title` on the user's desktop, by
    closing the local Citrix VIEWER process only. The app keeps running on the Citrix server;
    the next launch of it reconnects to the same session (same screens, same data) - e.g. on
    a hidden desktop. Refuses to touch anything that is not a Citrix viewer.
    Returns the closed process ids ([] if no such window is open)."""
    if sys.platform != "win32":
        raise SightlineError("disconnect_local is Windows-only")
    import ctypes
    from ctypes import wintypes

    from .backends.window_win import _exe_name, list_windows
    needle = window_title.lower()
    pids = {w["pid"] for w in list_windows() if needle in w["title"].lower()}
    if not pids:
        return []
    others = {p: _exe_name(p) for p in pids if _exe_name(p) not in CITRIX_VIEWERS}
    if others:
        raise SightlineError(f"windows titled {window_title!r} belong to non-Citrix programs "
                             f"{sorted(set(others.values()))}: not touching them")
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.OpenProcess.restype = wintypes.HANDLE
    k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    for pid in pids:
        h = k.OpenProcess(0x0001, False, pid)                 # PROCESS_TERMINATE
        if h:
            k.TerminateProcess(h, 0)
            k.CloseHandle(h)
    time.sleep(wait)                                          # let the server see the disconnect
    return sorted(pids)
