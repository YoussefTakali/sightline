r"""Run any Windows app HEADLESS: on its own invisible desktop, like a headless browser.

    python examples\hidden_try.py notepad.exe
    python examples\hidden_try.py "C:\Apps\gestion.exe" --title "Gestion" --click-text "Clients"
    python examples\hidden_try.py ica --click-text "Rechercher"      (Citrix from a web portal)

Citrix from a web portal, fully headless (works even when your browser auto-opens .ica):
    sightline browser-login https://citrix.company.com --user-data-dir %USERPROFILE%\citrix-profile
    python examples\hidden_try.py --portal https://citrix.company.com --app "PHILEAS" ^
        --profile %USERPROFILE%\citrix-profile --click-text "Rechercher"
Close the app first: Citrix would reuse the session already open on your screen.

Nothing appears on your screen; keep working. The pictures show what the app looks like
on its hidden desktop. The app is closed at the end (--keep to leave it running there).
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
import sightline
from sightline import Session, Text
from sightline.backends.hidden_win import HiddenDesktopBackend
from sightline.citrix import citrix_launch, wait_for_ica

ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
ap.add_argument("launch", nargs="?", help="the program/file to start on the hidden desktop, or 'ica' to "
                                "take the next .ica downloaded from a Citrix web page")
ap.add_argument("--title", help="its window title, if a launcher starts another process")
ap.add_argument("--click-text", help="a harmless text to click (found by OCR)")
ap.add_argument("--type", dest="type_text", help="text to type after the click")
ap.add_argument("--wait", type=float, default=5, help="seconds to let the app start (default 5)")
ap.add_argument("--keep", action="store_true", help="leave the app running on the hidden desktop")
ap.add_argument("--seamless", action="store_true",
                help="Citrix: keep seamless windows (default: one regular session window)")
ap.add_argument("--size", default="1920x1080", help="Citrix session size (default 1920x1080)")
ap.add_argument("--portal", help="Citrix web portal URL: fetch the .ica headless (never auto-opened)")
ap.add_argument("--app", help="Citrix: the application's name as shown in the portal")
ap.add_argument("--profile", help="Citrix: browser profile folder used with `sightline browser-login`")
ap.add_argument("--sso", help="Citrix: domain for Windows single sign-on, e.g. *.company.com")
ap.add_argument("--headed", action="store_true", help="Citrix: show the portal browser (to debug)")
ap.add_argument("--take-over", metavar="WINDOW_TITLE",
                help="Citrix: MOVE the session already open on your screen (window title, e.g. "
                     "PHILEAS) to the hidden desktop: disconnect it here, reconnect it there")
a = ap.parse_args()

launch, timeout = a.launch, 60
if a.portal:
    if not (a.app and a.profile):
        ap.error("--portal needs --app and --profile")
    from sightline.citrix import disconnect_local, fetch_ica
    if a.take_over:
        ok = input(f"   Move the Citrix session showing {a.take_over!r} to the hidden desktop? "
                   "It disappears from your screen and keeps running there. [y/N] ")
        if ok.strip().lower() not in ("y", "yes", "o", "oui"):
            sys.exit("cancelled")
        closed = disconnect_local(a.take_over)
        print(f"   disconnected the local Citrix viewer (pids {closed})" if closed else
              f"   no window {a.take_over!r} open: a new session will be started")
    print(f"0. Fetching a fresh .ica for {a.app!r} from {a.portal} (headless browser)")
    a.launch = fetch_ica(a.portal, a.app, a.profile, headless=not a.headed,
                         browser_args=[f"--auth-server-allowlist={a.sso}"] if a.sso else None)
elif not a.launch:
    ap.error("give the program/file to launch, 'ica', or --portal/--app/--profile")
launch = a.launch
if a.launch.lower() == "ica" or a.launch.lower().endswith(".ica"):
    if a.launch.lower() == "ica":
        print("0. Now click the application in the Citrix web page (waiting for the .ica in Downloads)")
        ica = wait_for_ica(since=time.time() - 5)
    else:
        ica = a.launch
    print(f"   .ica: {ica}")
    w, h = (int(v) for v in a.size.lower().split("x"))
    launch = citrix_launch(ica, windowed=not a.seamless, size=(w, h))
    timeout, a.wait = 180, max(a.wait, 30)                 # Citrix logon takes a while

print(f"sightline used: {os.path.dirname(sightline.__file__)}")
b = HiddenDesktopBackend(launch=launch, title=a.title, timeout=timeout, keep_app=a.keep)
print(f"1. started on hidden desktop {b.desktop_name!r} (pid {b.pid}) - nothing on your screen")
time.sleep(a.wait)
s = Session(b, trace_dir="hidden-traces", timeout=10)
s.capture().save("hidden_1_start.png")
print("2. captured -> hidden_1_start.png")
if a.click_text:
    s.click(Text(a.click_text))
    time.sleep(1)
    s.capture().save("hidden_2_after_click.png")
    print(f"3. clicked {a.click_text!r} with input mode '{b.input_mode}' -> hidden_2_after_click.png")
if a.type_text:
    s.type(a.type_text)
    time.sleep(0.5)
    s.capture().save("hidden_3_after_typing.png")
    print("4. typed -> hidden_3_after_typing.png")
s.close()
print(f"trace report: {s.report}")
print("Did anything appear on your screen or move your mouse? It should not have.")
