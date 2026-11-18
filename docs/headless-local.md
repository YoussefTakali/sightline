# Headless automation of LOCALLY installed apps

The need: the app is installed on this computer (nowhere else, no container, no remote
server), it must be automated with images/OCR like SikuliX, and the user must keep using
the computer meanwhile - no stolen mouse, no window jumping to the front.

SikuliX cannot do that: it uses THE screen, THE mouse and THE keyboard. sightline replaces
those three with per-app equivalents, the best way each OS allows, with no admin rights.

| OS | Backend | How | Headless level |
|---|---|---|---|
| **Linux** | `display` | sightline starts its own invisible X display (TigerVNC Xvnc, as your user) and launches the app on it | **complete**: every app (GTK, Qt, Java, Electron, Wine...), any number in parallel |
| **Windows** | `hidden` | starts the app on **its own invisible desktop** (created as your user) and drives it there with window messages | **complete for apps that accept window messages**: nothing on your screen, never waits for your mouse, copies in parallel |
| **Windows** | `window` | captures the app's windows from the compositor (`PrintWindow`) and **posts** clicks/keys into the right control (window messages) | **per app**: works for most desktop apps; check each with `sightline doctor` |
| macOS | - | not implemented yet (ScreenCaptureKit + per-process events) | use `local` (foreground) |

## Windows: the `hidden` backend (headless, like a headless browser)

```python
s = Session(HiddenDesktopBackend(launch=r"C:\Apps\gestion.exe"))
```
```toml
[profiles.gestion]
backend = "hidden"
[profiles.gestion.connection]
launch = 'C:\Apps\gestion.exe'
```

Windows lets any user create extra desktops in their session. sightline creates one per
session, starts the app **on it**, captures its windows there and sends it window messages.
The user's screen, taskbar, mouse and keyboard are never involved, so nothing waits for
the user to be idle. The app is closed with the session (`keep_app=True` leaves it running;
`desktop="name"` + `title=` attaches to it later). Try any app:
`python examples\hidden_try.py "C:\Apps\gestion.exe" --click-text "Clients"`.

**Apps without an .exe**: `launch` takes anything you could double-click - a shortcut
(`.lnk`), a `.bat`/`.cmd` (run without a console), a `.ps1`, a Java `.jar`, a Python
script, a ClickOnce `.appref-ms`, a document - or a command (`["javaw", "-jar", "app.jar"]`).
Launchers that start the real app as another process and exit are fine: everything they
start lands on the same hidden desktop, any window there is the app's (no title needed),
and all of it is closed with the session. With the `window` backend, give the app's
`title=` in that case. `sightline windows` shows each window's program, to find what
runs behind a shortcut.

Limits: real hardware input only reaches the desktop on screen, so an app that accepts ONLY
real input (like a Citrix seamless window) cannot be driven on a hidden desktop - see
[citrix-headless.md](citrix-headless.md) for Citrix's own headless routes. Apps that need
Explorer (tray icons, shell notifications) don't have one there.

Validated on real Windows (CI): the app is absent from the user's desktop, captured,
clicked, typed into (accents, Ctrl+A), the user's foreground window and cursor unchanged,
two copies in parallel, the app closed with the session.

## Windows: the `window` backend

```python
s = Session(WindowBackend(title="Gestion des commandes"))          # already running
s = Session(WindowBackend(launch=r"C:\Apps\gestion.exe", title="Gestion"))
```

What it does:

- **Sees** every visible window of the app's process - main window, dialogs, popup menus,
  combo-box lists - composited into one frame (screen coordinates), even when they are
  **behind other windows or moved off-screen**.
- **Acts** by posting window messages to the control under the point: clicks, double
  clicks (the real Windows sequence), drags (the pressed control keeps receiving the
  moves), wheel, menu bar / scrollbar / title bar clicks (non-client messages), typing
  (any Unicode: é ç €), keys, and **modifier combos** (Ctrl+C, Ctrl+A...) by giving the
  app's thread the right key state for the duration of the key.
- **Clipboard**: the app is local, so it is the user's clipboard: the session saves its
  text and puts it back after each copy.

Minimized apps: a minimized window draws **nothing** (with Citrix seamless, the server even
stops sending it), so no tool can see it. With `restore_minimized = true` in the profile,
sightline restores it **behind your windows without taking the focus** when it needs it,
and **minimizes it again** when the run ends: your taskbar looks the same before and after,
and you keep working in front during the run.

Limits (measured per app by the doctor):

| Situation | Effect |
|---|---|
| App draws with the GPU (some OpenGL/DirectX/video views) | capture only correct while the window is uncovered (`fallback_capture`) |
| App reads the physical keyboard/mouse state (games, some kiosk software) | posted input ignored → use the `local` backend for that app |
| Some Chromium/Electron widgets | partial posted input → test with the doctor |
| A dialog opened by ANOTHER process (e.g. Excel launched by an export) | not part of the app's frame: open a second session on that window |

```bat
sightline windows                                   :: titles of open windows
sightline doctor --window "Gestion"                 :: background capture: OK / uncovered only / NO
sightline doctor --window "Gestion" --click 120,80  :: posted click: did the app react?
```

### How it was validated

- `tools/wine_smoke.sh` runs the backend with **Windows Python under Wine** against
  Notepad: window discovery, hit-testing to the child control, posted typing with accents,
  Ctrl+A through the thread key state, Enter, a menu-bar (non-client) click opening the
  popup menu, the popup composited into the frame, Esc - all pass.
- Not validated by Wine: Windows' compositor capture behind other windows (Wine has no
  compositor). That part is what `sightline doctor` checks on a real machine.

## Linux: the `display` backend

```python
s = Session(PrivateDisplayBackend(["/opt/app/bin/app"], size=(1280, 800)))
```

- Needs TigerVNC's `Xvnc` (package `tigervnc-standalone-server`, Fedora
  `tigervnc-server-minimal`), or as a fallback `Xvfb` + `x11vnc`; `sightline doctor` says
  what's missing. Prefer Xvnc: x11vnc drops a clipboard copy identical to the previous one
  and relays the clipboard only ~10 s after connecting.
- Works on X11 and Wayland desktops (the app is started as an X11 client of the private
  display: GTK, Qt, Java, Wine, Electron all support it).
- The window manager is given time to really adopt windows before the app starts (a
  window shown while the WM is still starting can stay invisible forever - measured).
- If the app dies, the next step fails at once with its exit code and last output lines.
- Each session gets its own display: parallel runs don't interfere.
- Validated: `tests/test_display_linux.py` (both servers, login, full grid via clipboard,
  identical copies, two displays in parallel, crash reporting) in `docker/dev`.
