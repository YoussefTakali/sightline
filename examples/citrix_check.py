r"""Can sightline drive YOUR Citrix app in the background? Step by step, on your work PC.

Nothing is clicked or typed unless you ask for it with an option.

  python examples\citrix_check.py --window "part of the app's title"
  python examples\citrix_check.py --window "..." --click-text "Rechercher"
  python examples\citrix_check.py --window "..." --type-into "Nom :" --text "test"
  python examples\citrix_check.py --window "..." --copy-from "a value shown in a grid"

  --ocr windows     Windows' built-in OCR: nothing to install, any Python version
  --ocr rapidocr    OCR with pip only (no admin): pip install rapidocr-onnxruntime
  --ocr tesseract   if Tesseract is installed (--lang fra+eng needs fra.traineddata)

Only use --click-text / --type-into on something harmless (a search field, a tab).
"""
import argparse
import sys
import time

import numpy as np

from sightline import Session, Text
from sightline.backends.window_win import WindowBackend, list_windows
from sightline.frame import region_changed


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--window", required=True, help="part of the Citrix app's window title")
    ap.add_argument("--ocr", default="auto", help="auto | windows | rapidocr | tesseract")
    ap.add_argument("--lang", default="eng", help="Tesseract languages, e.g. fra+eng")
    ap.add_argument("--click-text", help="click this text (something harmless)")
    ap.add_argument("--type-into", help="label of a field, e.g. 'Nom :' (field right of it)")
    ap.add_argument("--text", default="test", help="what to type with --type-into")
    ap.add_argument("--copy-from", help="text of a grid cell to click, then Ctrl+A / Ctrl+C")
    ap.add_argument("--no-wait", action="store_true", help="skip the 5 s 'cover the window' pause")
    ap.add_argument("--click-mode", default="post", choices=["post", "send", "real"],
                    help="post/send: window messages, never interrupts; real: waits until you are "
                         "idle, brings the app forward a fraction of a second, gives your window back")
    ap.add_argument("--restore", action="store_true",
                    help="if the window is minimized, restore it behind your windows without asking")
    a = ap.parse_args()
    if sys.platform != "win32":
        print("Run this on the Windows PC where the Citrix app is open.")
        return 2
    results = []

    def verdict(step, ok, detail):
        results.append((step, ok, detail))
        print(f"   -> {'OK ' if ok is True else ('?? ' if ok is None else 'NO ')} {detail}")

    print("1. Finding the window")
    try:
        b = WindowBackend(title=a.window, timeout=10, input_mode=a.click_mode)
    except Exception as e:
        print(f"   -> NO  {str(e).splitlines()[0]}\n   Run `sightline windows` to see the exact titles.")
        return 1
    mine = [w for w in list_windows() if w["pid"] == b.pid]
    for w in mine:
        state = "  (MINIMIZED)" if w["minimized"] else ""
        print(f"   {w['class'][:30]:<30} {str(w['rect']):>22}  {w['title']}{state}")
    verdict("window", True, f"{len(mine)} window(s) of that process (pid {b.pid})")
    if any(w["minimized"] for w in mine if w["hwnd"] == b.hwnd):
        print("   The window is MINIMIZED: a minimized window draws nothing, so nothing can see it.")
        answer = "y" if a.restore else input("   Restore it BEHIND your other windows, without taking "
                                             "the focus (minimized again at the end)? [Y/n] ").strip().lower()
        if answer in ("", "y", "yes", "o", "oui"):
            b.restore_behind()
            b.restore_minimized = True
            print("   -> restored behind your windows (your current window kept the focus); "
                  "it will be minimized again at the end")
        else:
            print("   Restore it yourself (click it in the taskbar), then run this again.")
            return 1
    s = Session(b, timeout=10, ocr=a.ocr, ocr_lang=a.lang, trace_dir="citrix-check-traces")

    print("2. Seeing it in the background")
    if not a.no_wait:
        print("   Cover the Citrix window with another window now (do NOT minimize it)... 5 s")
        time.sleep(5)
    try:
        f = s.capture()
    except Exception as e:
        verdict("capture", False, str(e).splitlines()[0])
        return _summary(results, s)
    f.save("citrix_1_capture.png")
    colours = len(np.unique(f.pixels.reshape(-1, 3)[:: max(1, f.pixels.size // 30000)], axis=0))
    if colours < 4:
        verdict("capture", False, "blank picture: Citrix draws nothing for background capture "
                "(-> foreground 'local' mode, or the light version in a browser)")
    elif b.fallback_capture:
        verdict("capture", None, "works only while the window is UNCOVERED (GPU-drawn) - "
                "open citrix_1_capture.png and check")
    else:
        verdict("capture", True, "background capture works - open citrix_1_capture.png: it "
                "must show the app even though it was covered")

    print("3. Reading text (OCR)")
    try:
        phrases = s.read_words()
        for t, r in phrases[:15]:
            print(f"   {str(r):>22}  {t}")
        verdict("ocr", bool(phrases), f"{len(phrases)} phrase(s) read" if phrases else
                "nothing read (try --ocr windows, or --lang fra+eng)")
    except Exception as e:
        verdict("ocr", False, f"OCR not available: {str(e).splitlines()[0]}")

    if a.click_text:
        print(f"4. Clicking '{a.click_text}' without the mouse")
        before = s.capture()
        try:
            s.click(Text(a.click_text))
            time.sleep(1.5)
            after = s.capture()
            after.save("citrix_2_after_click.png")
            changed = before.pixels.shape != after.pixels.shape or region_changed(before, after, before.rect)
            hint = {"post": " -> try again with --click-mode send, then --click-mode real",
                    "send": " -> try again with --click-mode real", "real": ""}[a.click_mode]
            verdict("click", True if changed else None,
                    f"the app reacted (click mode '{a.click_mode}')" if changed else
                    f"no visible change with click mode '{a.click_mode}' - check "
                    f"citrix_2_after_click.png{hint}")
        except Exception as e:
            verdict("click", False, str(e).splitlines()[0])

    if a.type_into:
        print(f"5. Typing '{a.text}' into the field right of '{a.type_into}'")
        field = Text(a.type_into).area("right", 220, gap=6)
        try:
            s.type(a.text, into=field, clear=True)
            time.sleep(1.0)
            s.capture().save("citrix_3_after_typing.png")
            read = s.read_text(field)
            ok = a.text.lower() in read.lower()
            verdict("type", True if ok else None, f"field now reads {read!r}"
                    + ("" if ok else " - check citrix_3_after_typing.png"))
        except Exception as e:
            verdict("type", False, str(e).splitlines()[0])

    if a.copy_from:
        print(f"6. Copying the grid that contains '{a.copy_from}' (Ctrl+A, Ctrl+C)")
        try:
            s.click(Text(a.copy_from, exact=True))
            s.press("ctrl+a")
            text = s.copy("ctrl+c", timeout=10)
            lines = text.replace("\r\n", "\n").split("\n")
            print("   " + "\n   ".join(lines[:5]))
            verdict("copy", True, f"{len(lines)} line(s) through the clipboard "
                    "(your own clipboard was restored)")
        except Exception as e:
            verdict("copy", False, str(e).splitlines()[0] + " (Citrix clipboard policy?)")

    return _summary(results, s)


def _summary(results, s) -> int:
    s.close()
    print("\nSummary")
    for step, ok, detail in results:
        print(f"  {'OK ' if ok is True else ('?? ' if ok is None else 'NO ')} {step:<8} {detail}")
    if s.report:
        print(f"\nStep-by-step pictures: {s.report}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
