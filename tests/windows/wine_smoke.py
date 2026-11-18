"""Windows backend smoke test, run with WINDOWS Python under Wine (tools/wine_smoke.sh).
Wine validates the Win32 plumbing (window discovery, hit-testing, posted mouse/keys,
modifiers, non-client clicks, popup compositing, the session on top). It cannot validate
PrintWindow-behind-other-windows: that needs real Windows (`sightline doctor`)."""
import ctypes
import sys
import time
from ctypes import wintypes

from sightline import At, Image, Rect, Session
from sightline.backends.local import windows_session_locked
from sightline.backends.window_win import WindowBackend, list_windows
from sightline.vision.match import Template

u = ctypes.windll.user32
u.SendMessageW.restype = ctypes.c_ssize_t
u.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, ctypes.c_void_p]
u.FindWindowExW.restype = wintypes.HWND
u.FindWindowExW.argtypes = [wintypes.HWND, wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR]
failures = []


def check(name, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + name + (f"  ({detail})" if detail else ""))
    if not ok:
        failures.append(name)


def text_of(h):
    n = u.SendMessageW(h, 0x0E, 0, None)
    b = ctypes.create_unicode_buffer(n + 1)
    u.SendMessageW(h, 0x0D, n + 1, b)
    return b.value


b = WindowBackend(title="Notepad")
s = Session(b, timeout=5, settle=0.05, trace="off")
edit = u.FindWindowExW(b.hwnd, None, "Edit", None)
r = wintypes.RECT()
u.GetWindowRect(edit, ctypes.byref(r))
er = Rect.from_corners(r.left, r.top, r.right, r.bottom)
f = s.capture()
check("capture composite", f.width > 100 and f.pixels.any(), f"{f.width}x{f.height}")

hit = b._hit(er.center.x, er.center.y)
check("hit-test finds the child control", hit is not None and hit[0] == edit)

editor = At(er)
s.type("Bonjour é ç €", into=editor)
time.sleep(0.3)
check("posted typing incl. accents", text_of(edit) == "Bonjour é ç €", ascii(text_of(edit)))

s.press("ctrl+a")
s.type("remplacé")
time.sleep(0.3)
check("modifier combo (Ctrl+A) via thread key state", text_of(edit) == "remplacé", ascii(text_of(edit)))

s.type("\nligne 2")
time.sleep(0.3)
check("Enter key", text_of(edit) == "remplacé\r\nligne 2", ascii(text_of(edit)))

b.set_clipboard("user's own clipboard é€")
check("clipboard round trip (accents, euro)", b.get_clipboard() == "user's own clipboard é€",
      ascii(b.get_clipboard()))
s.press("ctrl+a")
copied = s.copy("ctrl+c", timeout=5)
check("Ctrl+C copy read back through the session", copied.replace("\r", "") == "remplacé\nligne 2",
      ascii(copied))
check("user's clipboard restored after the copy", b.get_clipboard() == "user's own clipboard é€",
      ascii(b.get_clipboard()))
check("session not reported as locked", windows_session_locked() is False)

menubar = Rect(f.origin_x + 4, er.y - 22, 40, 20)
s.click(Image(Template.from_array(f.crop(menubar).pixels, "file_menu")))
time.sleep(0.5)
popup = [w for w in list_windows() if w["pid"] == b.pid and w["class"] == "#32768"]
check("non-client click (menu bar) opens the menu", bool(popup))
f2 = s.capture()
check("popup window composited into the frame",
      bool(popup) and f2.rect.contains(popup[0]["rect"].center))
s.press("esc")
time.sleep(0.3)
check("Esc closes the menu",
      not [w for w in list_windows() if w["pid"] == b.pid and w["class"] == "#32768"])
s.close()
print("RESULT:", "OK" if not failures else f"{len(failures)} FAILED: {failures}")
sys.exit(1 if failures else 0)
