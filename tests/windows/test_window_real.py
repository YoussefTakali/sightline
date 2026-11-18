"""The Windows 'window' backend on REAL Windows (runs in CI on windows-latest).

What Wine cannot validate: PrintWindow capture of a window COVERED by another one, on a
real compositor. Plus the input path again (posted typing, Ctrl+A, menu bar)."""
import ctypes
import subprocess
import sys
import time

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="real Windows only")


def _find_text_control(hwnd):
    from ctypes import wintypes
    u = ctypes.windll.user32
    u.FindWindowExW.restype = wintypes.HWND
    u.FindWindowExW.argtypes = [wintypes.HWND, wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR]
    for cls in ("Edit", "RichEditD2DPT"):
        stack = [hwnd]
        while stack:
            h = stack.pop()
            found = u.FindWindowExW(h, None, cls, None)
            if found:
                return found
            child = u.FindWindowExW(h, None, None, None)
            while child:
                stack.append(child)
                child = u.FindWindowExW(h, child, None, None)
    return None


def _text(hwnd):
    from ctypes import wintypes
    u = ctypes.windll.user32
    u.SendMessageW.restype = ctypes.c_ssize_t
    u.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, ctypes.c_void_p]
    n = u.SendMessageW(hwnd, 0x0E, 0, None)
    buf = ctypes.create_unicode_buffer(n + 1)
    u.SendMessageW(hwnd, 0x0D, n + 1, buf)
    return buf.value


@pytest.fixture
def notepads():
    procs = []
    yield procs
    for p in procs:
        subprocess.run(["taskkill", "/F", "/PID", str(p.pid)], capture_output=True)


def test_background_window_on_real_windows(notepads, tmp_path):
    from sightline import At, Rect, Session
    from sightline.backends.window_win import WindowBackend
    b = WindowBackend(launch="notepad.exe", title="Notepad", timeout=20)
    notepads.append(b.process)
    s = Session(b, timeout=5, trace_dir=str(tmp_path))
    edit = _find_text_control(b.hwnd)
    assert edit, "no text control in Notepad"
    from ctypes import wintypes
    r = wintypes.RECT()
    ctypes.windll.user32.GetWindowRect(edit, ctypes.byref(r))
    area = Rect.from_corners(r.left, r.top, r.right, r.bottom)

    s.type("Bonjour é ç €", into=At(area))
    time.sleep(0.5)
    assert _text(edit).replace("\r", "") == "Bonjour é ç €"
    s.press("ctrl+a")
    s.type("remplacé")
    time.sleep(0.5)
    assert _text(edit).replace("\r", "") == "remplacé"

    uncovered = s.capture()
    assert not b.fallback_capture, "PrintWindow gave nothing: compositor capture failed"

    # cover it completely with ANOTHER Notepad, then capture ours again
    cover = subprocess.Popen(["notepad.exe"])
    notepads.append(cover)
    time.sleep(2)
    from sightline.backends.window_win import list_windows
    mine = [w for w in list_windows() if w["pid"] == b.pid][0]["rect"]
    covers = [w for w in list_windows() if w["pid"] == cover.pid]
    assert covers, "cover window did not open"
    ctypes.windll.user32.SetWindowPos(covers[0]["hwnd"], 0, mine.x, mine.y, mine.w + 40, mine.h + 40, 0x0040)
    time.sleep(1)
    covered = s.capture()
    uncovered.save(str(tmp_path / "uncovered.png"))
    covered.save(str(tmp_path / "covered.png"))
    a = uncovered.crop(area).gray.astype(int)
    c = covered.crop(area).gray.astype(int)
    assert a.shape == c.shape
    same = float((abs(a - c) <= 8).mean())
    assert same > 0.98, f"capture behind another window differs ({same:.1%} identical)"


def test_minimized_app_is_restored_behind_without_taking_focus(notepads, tmp_path):
    """restore_minimized=True: a minimized app is restored BEHIND the user's windows; the
    user's foreground window keeps the focus, and the app can be captured again."""
    from sightline import At, Rect, Session
    from sightline.backends.window_win import WindowBackend, list_windows
    u = ctypes.windll.user32
    b = WindowBackend(launch="notepad.exe", title="Notepad", timeout=20, restore_minimized=True)
    notepads.append(b.process)
    s = Session(b, timeout=5, trace_dir=str(tmp_path))
    edit = _find_text_control(b.hwnd)
    from ctypes import wintypes
    r = wintypes.RECT()
    u.GetWindowRect(edit, ctypes.byref(r))
    s.type("visible after restore", into=At(Rect.from_corners(r.left, r.top, r.right, r.bottom)))
    time.sleep(0.5)

    other = subprocess.Popen(["notepad.exe"])               # the "user's" window, in front
    notepads.append(other)
    time.sleep(2)
    mine = [w for w in list_windows() if w["pid"] == other.pid][0]["hwnd"]
    u.SetForegroundWindow(mine)
    u.ShowWindow(b.hwnd, 6)                                  # SW_MINIMIZE the automated app
    time.sleep(1)
    fg_before = u.GetForegroundWindow()

    frame = s.capture()                                      # restores it behind, then captures
    assert not [w for w in list_windows() if w["hwnd"] == b.hwnd][0]["minimized"]
    assert u.GetForegroundWindow() == fg_before, "the user's foreground window lost the focus"
    assert frame.width > 100 and frame.pixels.std() > 5
    frame.save(str(tmp_path / "restored_behind.png"))


def test_restored_app_is_minimized_again_when_the_session_closes(notepads, tmp_path):
    from sightline import Session
    from sightline.backends.window_win import WindowBackend, list_windows
    u = ctypes.windll.user32
    b = WindowBackend(launch="notepad.exe", title="Notepad", timeout=20, restore_minimized=True)
    notepads.append(b.process)
    other = subprocess.Popen(["notepad.exe"])
    notepads.append(other)
    time.sleep(2)
    u.SetForegroundWindow([w for w in list_windows() if w["pid"] == other.pid][0]["hwnd"])
    u.ShowWindow(b.hwnd, 6)                                  # the user minimized the app
    time.sleep(1)
    fg = u.GetForegroundWindow()
    s = Session(b, timeout=5, trace="off")
    s.capture()                                              # restored behind for the run
    assert not [w for w in list_windows() if w["hwnd"] == b.hwnd][0]["minimized"]
    s.close()                                                # ...and minimized again
    time.sleep(1)
    assert [w for w in list_windows() if w["hwnd"] == b.hwnd][0]["minimized"]
    assert u.GetForegroundWindow() == fg


def _maximized_notepad(notepads, restore=False):
    from sightline.backends.window_win import WindowBackend
    b = WindowBackend(launch="notepad.exe", title="Notepad", timeout=20, restore_minimized=restore)
    notepads.append(b.process)
    ctypes.windll.user32.ShowWindow(b.hwnd, 3)               # SW_MAXIMIZE: full screen
    time.sleep(1)
    assert ctypes.windll.user32.IsZoomed(b.hwnd)
    return b


def test_full_screen_app_behind_another_window(notepads, tmp_path):
    from ctypes import wintypes

    from sightline import At, Rect, Session
    from sightline.backends.window_win import list_windows
    u = ctypes.windll.user32
    b = _maximized_notepad(notepads)
    s = Session(b, timeout=5, trace_dir=str(tmp_path))
    edit = _find_text_control(b.hwnd)
    r = wintypes.RECT()
    u.GetWindowRect(edit, ctypes.byref(r))
    area = Rect.from_corners(r.left, r.top, r.right, r.bottom)
    cover = subprocess.Popen(["notepad.exe"])                # another window on top of it
    notepads.append(cover)
    time.sleep(2)
    u.SetForegroundWindow([w for w in list_windows() if w["pid"] == cover.pid][0]["hwnd"])
    time.sleep(0.5)
    s.type("plein écran", into=At(area))
    s.press("ctrl+a")
    s.type("remplacé en plein écran")
    time.sleep(0.5)
    assert _text(edit).replace("\r", "") == "remplacé en plein écran"
    frame = s.capture()
    sw, sh = u.GetSystemMetrics(0), u.GetSystemMetrics(1)
    assert frame.width >= 0.9 * sw and frame.height >= 0.8 * sh, (frame.width, frame.height, sw, sh)
    assert not b.fallback_capture
    frame.save(str(tmp_path / "full_screen_covered.png"))


def test_full_screen_app_minimized_comes_back_full_size_and_reopens_maximized(notepads):
    from ctypes import wintypes

    from sightline import Session
    from sightline.backends.window_win import WINDOWPLACEMENT, list_windows
    u = ctypes.windll.user32
    b = _maximized_notepad(notepads, restore=True)
    other = subprocess.Popen(["notepad.exe"])
    notepads.append(other)
    time.sleep(2)
    u.SetForegroundWindow([w for w in list_windows() if w["pid"] == other.pid][0]["hwnd"])
    u.ShowWindow(b.hwnd, 6)                                  # minimized while full screen
    time.sleep(1)
    fg = u.GetForegroundWindow()
    s = Session(b, timeout=5, trace="off")
    frame = s.capture()                                      # restored behind, FULL size
    work = wintypes.RECT()
    u.SystemParametersInfoW(0x30, 0, ctypes.byref(work), 0)  # SPI_GETWORKAREA
    r = [w for w in list_windows() if w["hwnd"] == b.hwnd][0]["rect"]
    assert abs(r.w - (work.right - work.left)) <= 20 and abs(r.h - (work.bottom - work.top)) <= 20, r
    assert u.GetForegroundWindow() == fg, "the user's window lost the focus"
    assert frame.width > 100
    s.close()                                                # back to minimized...
    time.sleep(1)
    assert [w for w in list_windows() if w["hwnd"] == b.hwnd][0]["minimized"]
    wp = WINDOWPLACEMENT(length=ctypes.sizeof(WINDOWPLACEMENT))
    u.GetWindowPlacement(b.hwnd, ctypes.byref(wp))
    assert wp.flags & 0x2, "it would no longer reopen maximized"   # WPF_RESTORETOMAXIMIZED


@pytest.mark.parametrize("mode", ["send", "real"])
def test_input_modes_for_apps_that_ignore_posted_messages(notepads, mode):
    """'send' (synchronous messages) and 'real' (polite real input): the text lands in the app
    behind; with 'real', the user's window and cursor are given back afterwards."""
    from ctypes import wintypes

    from sightline import At, Rect, Session
    from sightline.backends.window_win import WindowBackend, list_windows
    u = ctypes.windll.user32
    b = WindowBackend(launch="notepad.exe", title="Notepad", timeout=20, input_mode=mode, idle_ms=0)
    notepads.append(b.process)
    edit = _find_text_control(b.hwnd)
    r = wintypes.RECT()
    u.GetWindowRect(edit, ctypes.byref(r))
    area = Rect.from_corners(r.left, r.top, r.right, r.bottom)
    user = subprocess.Popen(["notepad.exe"])                 # the user's window, in front
    notepads.append(user)
    time.sleep(2)
    user_hwnd = [w for w in list_windows() if w["pid"] == user.pid][0]["hwnd"]
    u.SetForegroundWindow(user_hwnd)
    u.SetCursorPos(30, 30)
    time.sleep(0.5)
    s = Session(b, timeout=5, trace="off")
    s.click(At(area))
    s.press("ctrl+a")
    s.type(f"mode {mode} é")
    time.sleep(0.5)
    assert _text(edit).replace("\r", "") == f"mode {mode} é"
    assert u.GetForegroundWindow() == user_hwnd, "the user's window was not given back"
    p = wintypes.POINT()
    u.GetCursorPos(ctypes.byref(p))
    assert (p.x, p.y) == (30, 30), "the user's cursor was not put back"
    s.close()


def test_auto_learns_real_input_for_an_app_that_ignores_messages(notepads, tmp_path, monkeypatch):
    """Like a Citrix seamless window: the app reacts only to raw hardware input. 'auto' must
    try post, then send, then learn 'real' - with exactly ONE effective click - remember it,
    give the user's window back, and start with 'real' next time."""
    import os
    import sys

    from sightline import Session
    from sightline.backends.window_win import WindowBackend, list_windows
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))         # isolated memory of learned modes
    u = ctypes.windll.user32
    app = subprocess.Popen([sys.executable, os.path.join(os.path.dirname(__file__), "raw_input_app.py")])
    notepads.append(app)
    user = subprocess.Popen(["notepad.exe"])
    notepads.append(user)
    time.sleep(3)
    user_hwnd = [w for w in list_windows() if w["pid"] == user.pid][0]["hwnd"]
    u.SetForegroundWindow(user_hwnd)
    time.sleep(0.5)

    b = WindowBackend(title="RawOnly Test", timeout=20, idle_ms=0)
    assert b.input_mode == "post"
    s = Session(b, timeout=5, trace="off")
    f = s.capture()
    s.click((f.origin_x + f.width // 2, f.origin_y + f.height // 2))
    time.sleep(1)
    title = [w for w in list_windows() if w["pid"] == app.pid][0]["title"]
    assert b.input_mode == "real", b.input_mode
    assert title.endswith("clicks 1"), f"expected exactly one effective click: {title!r}"
    assert u.GetForegroundWindow() == user_hwnd, "the user's window was not given back"
    s.close()

    b2 = WindowBackend(title="RawOnly Test", timeout=20, idle_ms=0)        # a later run
    assert b2.input_mode == "real" and b2._confirmed, "the learned mode was not remembered"
    s2 = Session(b2, timeout=5, trace="off")
    f = s2.capture()
    s2.click((f.origin_x + f.width // 2, f.origin_y + f.height // 2))
    time.sleep(1)
    assert [w for w in list_windows() if w["pid"] == app.pid][0]["title"].endswith("clicks 2")
    s2.close()


def test_auto_keeps_background_messages_for_an_app_that_obeys_them(notepads, tmp_path, monkeypatch):
    from ctypes import wintypes

    from sightline import At, Rect, Session
    from sightline.backends.window_win import WindowBackend
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    b = WindowBackend(launch="notepad.exe", title="Notepad", timeout=20, idle_ms=0)
    notepads.append(b.process)
    edit = _find_text_control(b.hwnd)
    r = wintypes.RECT()
    ctypes.windll.user32.GetWindowRect(edit, ctypes.byref(r))
    s = Session(b, timeout=5, trace="off")
    s.type("auto mode", into=At(Rect.from_corners(r.left, r.top, r.right, r.bottom)))
    time.sleep(0.5)
    assert _text(edit).replace("\r", "") == "auto mode"
    assert b.input_mode == "post", "a normal app must stay in the never-interrupting mode"
    s.close()


def _alpha(hwnd):
    from ctypes import wintypes
    k, a, f = wintypes.DWORD(), wintypes.BYTE(), wintypes.DWORD()
    ok = ctypes.windll.user32.GetLayeredWindowAttributes(hwnd, ctypes.byref(k), ctypes.byref(a), ctypes.byref(f))
    return a.value if ok and f.value & 2 else 255


def test_minimized_app_works_invisibly(notepads, tmp_path, monkeypatch):
    """restore_minimized='invisible': the minimized app is never seen - opacity 0 while it
    works - yet it is captured and typed into; back to minimized + opaque at the end."""
    from ctypes import wintypes

    from sightline import At, Rect, Session
    from sightline.backends.window_win import WindowBackend, list_windows
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    u = ctypes.windll.user32
    b = WindowBackend(launch="notepad.exe", title="Notepad", timeout=20,
                      restore_minimized="invisible", idle_ms=0)
    notepads.append(b.process)
    edit = _find_text_control(b.hwnd)
    ex_before = ctypes.windll.user32.GetWindowLongW(b.hwnd, -20)
    user = subprocess.Popen(["notepad.exe"])
    notepads.append(user)
    time.sleep(2)
    user_hwnd = [w for w in list_windows() if w["pid"] == user.pid][0]["hwnd"]
    u.SetForegroundWindow(user_hwnd)
    u.ShowWindow(b.hwnd, 6)                                  # the user minimizes the app
    time.sleep(1)
    s = Session(b, timeout=5, trace="off")
    frame = s.capture()                                      # invisible but drawing
    assert not [w for w in list_windows() if w["hwnd"] == b.hwnd][0]["minimized"]
    assert _alpha(b.hwnd) == 0, "the app must be invisible while it works"
    assert frame.pixels.std() > 5, "an invisible window must still be captured"
    r = wintypes.RECT()
    u.GetWindowRect(edit, ctypes.byref(r))
    s.type("invisible é", into=At(Rect.from_corners(r.left, r.top, r.right, r.bottom)))
    time.sleep(0.5)
    assert _text(edit).replace("\r", "") == "invisible é"
    assert _alpha(b.hwnd) == 0 and u.GetForegroundWindow() == user_hwnd
    s.close()
    time.sleep(1)
    assert [w for w in list_windows() if w["hwnd"] == b.hwnd][0]["minimized"]
    assert ctypes.windll.user32.GetWindowLongW(b.hwnd, -20) == ex_before, "style not restored"


def test_citrix_like_app_clicked_with_real_input_while_invisible(notepads, tmp_path, monkeypatch):
    """The raw-input-only app (Citrix-like), minimized: made invisible, and 'auto' clicks it
    with REAL input through an invisible on-top window - one click, never shown, user's
    window and focus given back."""
    import os
    import sys

    from sightline import Session
    from sightline.backends.window_win import WindowBackend, list_windows
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    u = ctypes.windll.user32
    app = subprocess.Popen([sys.executable, os.path.join(os.path.dirname(__file__), "raw_input_app.py")])
    notepads.append(app)
    user = subprocess.Popen(["notepad.exe"])
    notepads.append(user)
    time.sleep(3)
    b = WindowBackend(title="RawOnly Test", timeout=20, restore_minimized="invisible", idle_ms=0)
    user_hwnd = [w for w in list_windows() if w["pid"] == user.pid][0]["hwnd"]
    u.SetForegroundWindow(user_hwnd)
    u.ShowWindow(b.hwnd, 6)
    time.sleep(1)
    s = Session(b, timeout=5, trace="off")
    f = s.capture()
    assert _alpha(b.hwnd) == 0
    s.click((f.origin_x + f.width // 2, f.origin_y + f.height // 2))
    time.sleep(1)
    title = [w for w in list_windows() if w["pid"] == app.pid][0]["title"]
    assert title.endswith("clicks 1"), title
    assert b.input_mode == "real"
    assert _alpha(b.hwnd) == 0, "the app must be invisible again right after the click"
    assert u.GetForegroundWindow() == user_hwnd
    s.close()
    time.sleep(1)
    assert [w for w in list_windows() if w["pid"] == app.pid][0]["minimized"]


def _user_state():
    from ctypes import wintypes
    p = wintypes.POINT()
    ctypes.windll.user32.GetCursorPos(ctypes.byref(p))
    return ctypes.windll.user32.GetForegroundWindow(), (p.x, p.y)


def _hidden_edit(b):
    from ctypes import wintypes

    from sightline import Rect
    edit = b.run(_find_text_control, b.hwnd)
    r = wintypes.RECT()
    b.run(ctypes.windll.user32.GetWindowRect, edit, ctypes.byref(r))
    return edit, Rect.from_corners(r.left, r.top, r.right, r.bottom)


def test_hidden_desktop_app_is_headless(notepads, tmp_path, monkeypatch):
    """The app runs on its own invisible desktop: not on the user's screen at all, captured,
    clicked and typed into (Ctrl+A too), the user's window and cursor never touched."""
    from sightline import At, Session
    from sightline.backends.hidden_win import HiddenDesktopBackend
    from sightline.backends.window_win import list_windows
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    user = subprocess.Popen(["notepad.exe"])
    notepads.append(user)
    time.sleep(2)
    user_hwnd = [w for w in list_windows() if w["pid"] == user.pid][0]["hwnd"]
    ctypes.windll.user32.SetForegroundWindow(user_hwnd)
    ctypes.windll.user32.SetCursorPos(30, 30)
    time.sleep(0.5)
    before = _user_state()

    b = HiddenDesktopBackend(launch="notepad.exe", timeout=20)
    notepads.append(b.process)
    assert b.pid not in [w["pid"] for w in list_windows()], "the app is visible on the user's desktop"
    s = Session(b, timeout=5, trace_dir=str(tmp_path))
    frame = s.capture()
    assert frame.pixels.std() > 5, "the hidden app must still be captured"
    edit, area = _hidden_edit(b)
    s.type("first", into=At(area))
    s.press("ctrl+a")
    s.type("hidden desktop é")
    time.sleep(0.5)
    assert b.run(_text, edit).replace("\r", "") == "hidden desktop é"
    assert _user_state() == before, "the user's foreground window or cursor changed"
    assert b.input_mode in ("post", "send")
    proc = b.process
    s.close()
    assert proc.wait(10) is not None, "the hidden app must be closed with the session"


def test_two_hidden_copies_in_parallel(notepads, tmp_path, monkeypatch):
    from sightline import At, Session
    from sightline.backends.hidden_win import HiddenDesktopBackend
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    bs = [HiddenDesktopBackend(launch="notepad.exe", timeout=20) for _ in range(2)]
    notepads.extend(b.process for b in bs)
    assert bs[0].desktop_name != bs[1].desktop_name
    for i, b in enumerate(bs):
        s = Session(b, timeout=5, trace="off")
        edit, area = _hidden_edit(b)
        s.type(f"copy {i}", into=At(area))
    time.sleep(0.5)
    assert [b.run(_text, _hidden_edit(b)[0]) for b in bs] == ["copy 0", "copy 1"]
    for b in bs:
        b.close()


def _shortcut(tmp_path, target="notepad.exe"):
    lnk = tmp_path / "My App.lnk"
    exe = subprocess.run(["where", target], capture_output=True, text=True).stdout.split()[0]
    subprocess.run(["powershell", "-NoProfile", "-Command",
                    f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{lnk}');"
                    f"$s.TargetPath='{exe}';$s.Save()"], check=True)
    return str(lnk)


@pytest.mark.parametrize("kind", ["shortcut", "launcher_bat"])
def test_hidden_app_without_exe(notepads, tmp_path, monkeypatch, kind):
    """No .exe to give: a shortcut, or a .bat launcher that starts the app as ANOTHER process
    and exits. No title needed: any window on the private desktop is the app's. Closing the
    session closes the app the launcher started."""
    from sightline import At, Session
    from sightline.backends.hidden_win import HiddenDesktopBackend
    from sightline.backends.window_win import list_windows
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    if kind == "shortcut":
        launch = _shortcut(tmp_path)
    else:
        bat = tmp_path / "start app.bat"
        bat.write_text('@echo off\r\nstart "" notepad.exe\r\n')
        launch = str(bat)
    before = _user_state()
    b = HiddenDesktopBackend(launch=launch, timeout=30)
    app_pid = b.pid
    notepads.append(type("P", (), {"pid": app_pid})())
    assert app_pid not in [w["pid"] for w in list_windows()]
    s = Session(b, timeout=5, trace="off")
    edit, area = _hidden_edit(b)
    s.type(f"started from {kind}", into=At(area))
    time.sleep(0.5)
    assert b.run(_text, edit) == f"started from {kind}"
    now = _user_state()
    thief = [w for w in list_windows() if w["hwnd"] == now[0]]
    assert now == before, f"user state changed, foreground is now {thief}"
    s.close()
    time.sleep(2)
    alive = subprocess.run(["tasklist", "/FI", f"PID eq {app_pid}"], capture_output=True, text=True).stdout
    assert str(app_pid) not in alive, "the app started by the launcher must be closed too"


def test_window_backend_launches_a_shortcut(notepads, tmp_path, monkeypatch):
    from sightline import Session
    from sightline.backends.window_win import WindowBackend
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    b = WindowBackend(launch=_shortcut(tmp_path), title="Notepad", timeout=30)
    notepads.append(type("P", (), {"pid": b.pid})())
    s = Session(b, timeout=5, trace="off")
    assert s.capture().pixels.std() > 5
    s.close()


def test_take_over_never_touches_non_citrix_apps(notepads):
    from sightline.backends.window_win import list_windows
    from sightline.citrix import disconnect_local
    from sightline.errors import SightlineError
    p = subprocess.Popen(["notepad.exe"])
    notepads.append(p)
    time.sleep(2)
    assert disconnect_local("no such window xyz", wait=0) == []
    with pytest.raises(SightlineError, match="non-Citrix"):
        disconnect_local("Notepad", wait=0)
    assert any(w["pid"] == p.pid for w in list_windows()), "notepad must still be open"


def test_visit_desktop_open_apps_by_hand_then_attach(tmp_path, monkeypatch):
    """`sightline desktop open`: a desktop with a control panel; an app opened from the panel
    lands on that desktop; a script attaches to it there; showing it always comes back."""
    from sightline import Session, Text
    from sightline.backends.hidden_win import HiddenDesktopBackend
    from sightline.backends.window_win import list_windows
    from sightline.desktops import PANEL_TITLE, _on_desktop, close_desktop, input_desktop, open_desktop, show
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    name = f"sltest{int(time.time()) % 100000}"
    open_desktop(name, switch=False)
    try:
        assert not [w for w in list_windows() if w["title"] == PANEL_TITLE + name], \
            "the panel must not be on the user's desktop"
        b = HiddenDesktopBackend(desktop=name, title=PANEL_TITLE + name, timeout=10)
        s = Session(b, timeout=10, trace_dir=str(tmp_path), ocr="windows")
        s.click(Text("Command prompt"))
        found = []
        deadline = time.time() + 15
        while not found and time.time() < deadline:
            time.sleep(0.5)
            _on_desktop(name, lambda: found.extend(
                w for w in list_windows() if f"sightline desktop {name}" in w["title"]
                and w["title"] != PANEL_TITLE + name))
        assert found, "the app opened from the panel must be on that desktop"
        assert not [w for w in list_windows() if f"sightline desktop {name}" in w["title"]]
        b.keep_app = True                      # the session must not close the panel
        s.close()

        show(name, max_minutes=0.05)           # safety net: back after 3 s
        assert input_desktop() == "Default"
    finally:
        close_desktop(name)
    time.sleep(1)
    from sightline.errors import SightlineError
    with pytest.raises(SightlineError):
        _on_desktop(name, lambda: None)        # the desktop is gone with its apps
