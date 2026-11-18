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
