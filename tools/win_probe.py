"""Background-automation probe for Windows. Standard library only, no admin rights.

Question it answers, per application: can we SEE a window while it is behind other windows
(or parked off-screen), and CLICK / TYPE into it WITHOUT the real mouse and keyboard?

    python win_probe.py list                          windows: handle, title, class, size
    python win_probe.py capture "Bloc-notes" a.bmp    picture of the window's content
    python win_probe.py click   "Bloc-notes" 120 80   click at x,y of that picture
    python win_probe.py click   "Bloc-notes" 120 80 --double | --right
    python win_probe.py type    "Bloc-notes" "Bonjour é"
    python win_probe.py key     "Bloc-notes" enter    (enter, tab, esc, up, down, f5, ...)
    python win_probe.py move    "Bloc-notes" -3000 0  park it off-screen (move back: 100 100)

Do the test with the window BEHIND another window (or moved off-screen), keep using your
mouse meanwhile, then capture again and look at the picture. Never minimize the window:
a minimized window does not draw anything.
"""
import ctypes
import struct
import sys
import time
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    user32.SetProcessDPIAware()

H = wintypes.HANDLE
user32.GetDC.restype = H
user32.GetDC.argtypes = [wintypes.HWND]
user32.ReleaseDC.argtypes = [wintypes.HWND, H]
user32.PrintWindow.argtypes = [wintypes.HWND, H, wintypes.UINT]
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.RealChildWindowFromPoint.restype = wintypes.HWND
user32.RealChildWindowFromPoint.argtypes = [wintypes.HWND, wintypes.POINT]
user32.MapWindowPoints.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_void_p, wintypes.UINT]
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.c_void_p]
user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, wintypes.UINT]
gdi32.CreateCompatibleDC.restype = H
gdi32.CreateCompatibleDC.argtypes = [H]
gdi32.CreateCompatibleBitmap.restype = H
gdi32.CreateCompatibleBitmap.argtypes = [H, ctypes.c_int, ctypes.c_int]
gdi32.SelectObject.restype = H
gdi32.SelectObject.argtypes = [H, H]
gdi32.DeleteObject.argtypes = [H]
gdi32.DeleteDC.argtypes = [H]
gdi32.GetDIBits.argtypes = [H, H, wintypes.UINT, wintypes.UINT, ctypes.c_void_p, ctypes.c_void_p,
                            wintypes.UINT]

WM_MOUSEMOVE, WM_LBUTTONDOWN, WM_LBUTTONUP, WM_LBUTTONDBLCLK = 0x200, 0x201, 0x202, 0x203
WM_RBUTTONDOWN, WM_RBUTTONUP, WM_KEYDOWN, WM_KEYUP, WM_CHAR = 0x204, 0x205, 0x100, 0x101, 0x102
PW_CLIENTONLY, PW_RENDERFULLCONTENT = 1, 2
VK = {"enter": 0x0D, "tab": 0x09, "esc": 0x1B, "backspace": 0x08, "delete": 0x2E, "home": 0x24,
      "end": 0x23, "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27, "pageup": 0x21,
      "pagedown": 0x22, "space": 0x20, **{f"f{i}": 0x6F + i for i in range(1, 13)}}


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD),
                ("biCompression", wintypes.DWORD), ("biSizeImage", wintypes.DWORD),
                ("biXPelsPerMeter", wintypes.LONG), ("biYPelsPerMeter", wintypes.LONG),
                ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]


class GUITHREADINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("flags", wintypes.DWORD),
                ("hwndActive", wintypes.HWND), ("hwndFocus", wintypes.HWND),
                ("hwndCapture", wintypes.HWND), ("hwndMenuOwner", wintypes.HWND),
                ("hwndMoveSize", wintypes.HWND), ("hwndCaret", wintypes.HWND),
                ("rcCaret", wintypes.RECT)]


def windows():
    out = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(h, _):
        if user32.IsWindowVisible(h):
            n = user32.GetWindowTextLengthW(h)
            if n:
                t = ctypes.create_unicode_buffer(n + 1)
                user32.GetWindowTextW(h, t, n + 1)
                c = ctypes.create_unicode_buffer(256)
                user32.GetClassNameW(h, c, 256)
                r = wintypes.RECT()
                user32.GetClientRect(h, ctypes.byref(r))
                out.append((h, t.value, c.value, r.right, r.bottom, bool(user32.IsIconic(h))))
        return True
    user32.EnumWindows(cb, 0)
    return out


def find(title):
    hits = [w for w in windows() if title.lower() in w[1].lower()]
    if not hits:
        sys.exit(f"no visible window with '{title}' in its title (see: list)")
    if len(hits) > 1:
        print("several windows match, using the first:", [w[1] for w in hits])
    if hits[0][5]:
        sys.exit("that window is MINIMIZED: restore it (it can stay behind others / off-screen)")
    return hits[0][0]


def capture(hwnd, path):
    r = wintypes.RECT()
    user32.GetClientRect(hwnd, ctypes.byref(r))
    w, h = r.right, r.bottom
    screen = user32.GetDC(None)
    mem = gdi32.CreateCompatibleDC(screen)
    bmp = gdi32.CreateCompatibleBitmap(screen, w, h)
    old = gdi32.SelectObject(mem, bmp)
    t = time.perf_counter()
    ok = user32.PrintWindow(hwnd, mem, PW_CLIENTONLY | PW_RENDERFULLCONTENT)
    ms = 1000 * (time.perf_counter() - t)
    gdi32.SelectObject(mem, old)
    bi = BITMAPINFOHEADER(40, w, h, 1, 32, 0, 0, 0, 0, 0, 0)        # bottom-up rows = BMP order
    buf = ctypes.create_string_buffer(w * h * 4)
    gdi32.GetDIBits(mem, bmp, 0, h, buf, ctypes.byref(bi), 0)
    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(mem)
    user32.ReleaseDC(None, screen)
    data = buf.raw
    with open(path, "wb") as f:
        f.write(b"BM" + struct.pack("<IHHI", 54 + len(data), 0, 0, 54))
        f.write(struct.pack("<IiiHHIIiiII", 40, w, h, 1, 32, 0, len(data), 0, 0, 0, 0))
        f.write(data)
    colours = len(set(data[i:i + 3] for i in range(0, len(data), 4 * 97)))
    print(f"{path}: {w}x{h}, PrintWindow={'ok' if ok else 'FAILED'}, {ms:.0f} ms, "
          f"~{colours} colours" + ("  <-- BLANK: this app does not draw for us" if colours < 3 else ""))


def target_at(hwnd, x, y):
    """Deepest child control under client point (x, y), and the point in ITS coordinates."""
    pt = wintypes.POINT(x, y)
    while True:
        child = user32.RealChildWindowFromPoint(hwnd, pt)
        if not child or child == hwnd:
            return hwnd, pt
        user32.MapWindowPoints(hwnd, child, ctypes.byref(pt), 1)
        hwnd = child


def lp(x, y):
    return ((y & 0xFFFF) << 16) | (x & 0xFFFF)


def click(hwnd, x, y, double=False, right=False):
    h, pt = target_at(hwnd, x, y)
    down, up, flag = (WM_RBUTTONDOWN, WM_RBUTTONUP, 2) if right else (WM_LBUTTONDOWN, WM_LBUTTONUP, 1)
    p = user32.PostMessageW
    p(h, WM_MOUSEMOVE, 0, lp(pt.x, pt.y))
    p(h, down, flag, lp(pt.x, pt.y))
    p(h, up, 0, lp(pt.x, pt.y))
    if double and not right:
        p(h, WM_LBUTTONDBLCLK, flag, lp(pt.x, pt.y))
        p(h, up, 0, lp(pt.x, pt.y))
    cls = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(h, cls, 256)
    print(f"posted {'double ' if double else ''}{'right' if right else 'left'} click to control "
          f"'{cls.value}' at ({pt.x},{pt.y})")


def focus_of(hwnd):
    tid = user32.GetWindowThreadProcessId(hwnd, None)
    gi = GUITHREADINFO(cbSize=ctypes.sizeof(GUITHREADINFO))
    if user32.GetGUIThreadInfo(tid, ctypes.byref(gi)) and gi.hwndFocus:
        return gi.hwndFocus
    return hwnd


def type_text(hwnd, text):
    h = focus_of(hwnd)
    for ch in text:
        user32.PostMessageW(h, WM_CHAR, ord(ch), 1)
        time.sleep(0.01)
    print(f"posted {len(text)} characters to the focused control of that window")


def key(hwnd, name):
    h, vk = focus_of(hwnd), VK[name.lower()]
    user32.PostMessageW(h, WM_KEYDOWN, vk, 1)
    if vk == 0x0D:
        user32.PostMessageW(h, WM_CHAR, 0x0D, 1)
    user32.PostMessageW(h, WM_KEYUP, vk, 0xC0000001)
    print(f"posted key {name}")


def main(a):
    if not a or a[0] == "list":
        for h, t, c, w, hh, mini in windows():
            print(f"{int(h):>10}  {w:>5}x{hh:<5} {'(min) ' if mini else ''}{c:<28} {t}")
        return
    cmd, hwnd = a[0], find(a[1])
    if cmd == "capture":
        capture(hwnd, a[2] if len(a) > 2 else "window.bmp")
    elif cmd == "click":
        click(hwnd, int(a[2]), int(a[3]), "--double" in a, "--right" in a)
    elif cmd == "type":
        type_text(hwnd, a[2])
    elif cmd == "key":
        key(hwnd, a[2])
    elif cmd == "move":
        user32.SetWindowPos(hwnd, None, int(a[2]), int(a[3]), 0, 0, 0x0001 | 0x0004 | 0x0010)
        print("moved (no activation); move it back with: move", f'"{a[1]}"', "100 100")
    else:
        print(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
