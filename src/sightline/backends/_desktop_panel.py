"""Control panel shown on a sightline desktop (stdlib only, runs ON that desktop).

    pythonw _desktop_panel.py NAME [PORTAL_URL]

Programs started from it inherit its desktop: a browser opened here downloads/opens .ica
files here, so Citrix starts on this desktop too.
"""
import ctypes
import os
import subprocess
import sys
import tkinter as tk
from ctypes import wintypes

name = sys.argv[1]
portal = sys.argv[2] if len(sys.argv) > 2 else ""
u = ctypes.WinDLL("user32", use_last_error=True)
u.OpenDesktopW.restype = wintypes.HANDLE
u.OpenDesktopW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
u.SwitchDesktop.argtypes = [wintypes.HANDLE]
NO_WINDOW, NEW_CONSOLE = 0x08000000, 0x00000010


def back():
    h = u.OpenDesktopW("Default", 0, False, 0x0100)          # DESKTOP_SWITCHDESKTOP
    if h:
        u.SwitchDesktop(h)


def browser():
    # a SEPARATE profile: otherwise Edge hands the window to the Edge on your own desktop
    prof = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "sightline",
                        f"browser-{name}")
    args = ["cmd.exe", "/d", "/c", "start", "", "msedge", f"--user-data-dir={prof}",
            "--no-first-run"]
    subprocess.Popen(args + ([portal] if portal else []), creationflags=NO_WINDOW)


def command_prompt():
    subprocess.Popen(["cmd.exe", "/k", f"title sightline desktop {name}"], creationflags=NEW_CONSOLE)


root = tk.Tk()
root.title(f"sightline desktop - {name}")
root.attributes("-topmost", True)
root.geometry("+10+10")
tk.Label(root, text=f"Desktop '{name}'\nApps opened here stay here, off your screen.",
         justify="left", padx=10, pady=6).pack(anchor="w")
for label, fn in (("Open browser" + (" (Citrix portal)" if portal else ""), browser),
                  ("Command prompt", command_prompt), ("Back to my desktop", back)):
    tk.Button(root, text=label, command=fn, width=34, pady=4).pack(padx=10, pady=3)
root.protocol("WM_DELETE_WINDOW", back)                     # closing = going back; panel stays
root.mainloop()
