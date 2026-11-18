"""Try background automation on any Windows app - no OCR needed.

    python examples\\windows_try.py "Notepad"

During the 5-second countdown: put another window OVER the app (a browser...) and keep
using your mouse. Then open the three try_*.png pictures: they must show the app's content
even though it was covered, with the typed text appearing.
"""
import sys
import time

from sightline import Session
from sightline.backends.window_win import WindowBackend

title = sys.argv[1] if len(sys.argv) > 1 else "Notepad"
b = WindowBackend(title=title, timeout=10)
s = Session(b, trace_dir="try-traces", timeout=5)
main = [w for w in b._app_windows() if w["hwnd"] == b.hwnd][0]
print(f"found: {main['title']!r} at {main['rect']}")
print("Cover the app with another window now and keep using your mouse... 5 s")
time.sleep(5)

f = s.capture()
f.save("try_1_capture.png")
print(f"1. capture {f.width}x{f.height}"
      + ("  (fallback: only correct while uncovered)" if b.fallback_capture else "  (background capture)"))

r = main["rect"]
s.click((r.x + r.w // 2, r.y + r.h // 2))           # middle of the window
s.type("Hello from sightline: é ç € ")
time.sleep(0.5)
s.capture().save("try_2_after_typing.png")
print("2. typed text  -> try_2_after_typing.png")

s.press("ctrl+a")
s.type("Replaced through Ctrl+A ")
time.sleep(0.5)
s.capture().save("try_3_after_ctrl_a.png")
print("3. Ctrl+A + typing -> try_3_after_ctrl_a.png")

s.close()
print(f"trace report: {s.report}")
print("Did your mouse ever move by itself, or the app come to the front? It should not have.")
