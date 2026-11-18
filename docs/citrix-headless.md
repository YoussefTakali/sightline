# Citrix (and other remote apps) headless, without admin rights, without blocking your PC

## The idea

Your Citrix apps don't run on your PC: they run on a Citrix server, and a *client* only
shows their pixels and sends your clicks. The usual client (Citrix Workspace app) draws in
a window on YOUR desktop, so automating it takes over your screen.

But Citrix also has a **web client** (Citrix Workspace app for HTML5, the "light
version"). It draws the same session into a browser page. A browser can run **headless**:
no window at all. So:

```
your Windows session (you keep working normally)
 └─ headless Edge (no window)  ──HTTPS/HDX──►  Citrix server: your app
      ▲  screenshots / mouse+key events via DevTools, to THAT page only
 sightline (BrowserBackend)
```

- nothing appears on your screen, your mouse/keyboard/clipboard are never used;
- **no admin rights**: Python per-user + `pip install`, and the Edge already installed
  (`channel = "msedge"`, no browser download);
- your own identity, the official Citrix client, the normal login - no security bypass;
- everything else in sightline (image/OCR locators, grids via clipboard, dialogs, traces)
  works unchanged: the session is pixels, exactly as before.

This was validated end-to-end here with a remote app rendered by a web client in headless
Chromium (`tests/test_e2e_browser.py`: login, scrolling a grid, reading the full grid via
the REMOTE clipboard with accents, dialogs). The same pattern applies to VMware Horizon
HTML Access, Azure Virtual Desktop / Windows 365 web client, Guacamole.

## Try it without Citrix: the lab

`tools/citrix_lab.sh up` starts a Citrix-like environment with containers (Apache
Guacamole): a **web portal with a login**, a **published app** that opens inside the
browser (canvas + websocket, like Citrix Workspace for HTML5), a **gateway**, and an **RDP
desktop** running a demo business app. `tests/test_e2e_citrix_lab.py` drives it headless
by pixels only: portal login, the app's own login, scrolling a grid, opening a row, the
whole grid through the remote clipboard, the grid by OCR through the gateway's compressed
picture (60 rows, verified against the clipboard copy), a menu path, a drop-down, dialogs
in varying order, and a YAML flow through `sightline run` (`examples/flows-citrix`).

What the lab taught (and sightline now handles):

| Found in the lab | Handled by |
|---|---|
| Only the FIRST copy reached the browser: the gateway pushes the browser clipboard to the session on a window *focus* event; an app that copies again while it already owns the clipboard never notifies | the `navigator` clipboard adapter signals focus after writing its marker - every copy arrives (0.5 s), identical ones included |
| The web client draws the mouse pointer INTO the picture: after a wheel scroll it covered a grid row | the pointer is parked after every scroll (`park_mouse`) |
| Whole-screen OCR dropped the first letter next to a border / selection corner ("MD-0001") | second look: near misses that look truncated are re-read from a tight crop; exact stays exact |
| The same text in a status bar ("Commande CMD-0001 sélectionnée") stopped a scroll | `scroll_until_visible(at=...)` only searches inside the scrolled rows |
| Reconnecting a second browser to the same session: both kept taking it (= Citrix roaming) | one client per session - close the first (the tests do) |

## Step 1 - check that your company allows the HTML5 client (2 minutes)

1. Open your Citrix portal (the StoreFront / Workspace URL) in Edge.
2. If it asks to *Detect Citrix Workspace app*, choose **Use light version** / **Use web
   browser**. Otherwise: your account menu → *Account settings* → *Change Citrix Workspace
   app* → **Use light version**.
3. Launch the app. If it opens **in a browser tab**, you're good.

If that option does not exist, the Citrix admins disabled HTML5 (StoreFront setting
"Use Citrix Workspace app for HTML5"). Then ask them - it is a standard, supported setting.

## Step 2 - log in once, yourself

```bash
sightline browser-login https://citrix.company.com/Citrix/StoreWeb/ \
    --user-data-dir ~/.sightline/citrix --channel msedge
```

A normal Edge window opens on a dedicated profile: log in (SSO, MFA), choose *light
version*, close the window. Headless runs reuse that profile's cookies until the portal
session expires; then run `browser-login` again.

## Step 3 - profile

```toml
[profiles.citrix-web]
backend = "browser"
park_mouse = [5, 5]
[profiles.citrix-web.connection]
url = "https://citrix.company.com/Citrix/StoreWeb/"
channel = "msedge"
user_data_dir = "~/.sightline/citrix"
viewport = [1920, 1080]            # = the session resolution: keep it fixed
clipboard = "navigator"
target_os = "windows"
```

```python
with Session.from_profile("citrix-web") as s:
    s.click(Text("Gestion Commandes"))                 # the app tile on the portal page
    s.wait(Text("Connexion"), timeout=90)              # session opens in a new tab: followed
    ...
```

## What to know (honestly)

| Topic | Detail |
|---|---|
| **Session roaming** | Citrix moves a user's session to the client that launched last. If you have the SAME app (or another app on the same server) open in your installed Workspace, launching it headless can take that session away from your desktop. Use the robot when you don't need that app, or ask IT for a separate published app / session-sharing rule. |
| Portal session expiry | SSO cookies expire (hours/days). The run fails cleanly at the portal → `browser-login` again. Never store your password in scripts. |
| Keyboard layout | Key events go through the web client; with an AZERTY remote session check typed text once (`expect=` / OCR). |
| Clipboard | The HTML5 client exchanges the clipboard through the browser's clipboard API (permission granted by the backend). It must be allowed by the Citrix clipboard policy. To verify on your setup. |
| Image quality | Citrix compresses the picture (lossy). Prefer Text/OCR locators and slightly lower image similarity (0.85), or ask for the "Visual quality: Build to lossless" policy. |
| Policy | It is your account doing the work. For scheduled/unattended robots, companies usually want a service account: ask before running it at scale. |

## Options that were considered and rejected

| Option | Why not |
|---|---|
| Citrix Workspace for Linux in a container | needs a Linux runtime (Docker/WSL usually need admin to install) and the portal's device checks often refuse unmanaged clients. Works where WSL is already enabled: `.ica`/portal login in Xvfb + the `vnc` backend. |
| A second Windows session on your PC | client Windows allows one interactive session; more needs admin/hacks. |
| A hidden Windows desktop (`CreateDesktop`) | apps there get no real input (SendInput only reaches the visible desktop), and security tools (EDR) flag "hidden desktop + injected input" as a known malware technique (hVNC). |
| Writing our own Citrix (ICA/HDX) client | proprietary protocol, not feasible. |

## No admin rights - the whole chain

| Piece | How |
|---|---|
| Python | python.org installer → *Install for me only* (or the company's Python) |
| sightline + Playwright | clone the repo, then `pip install -e ".[browser,ocr-pip]"` in a virtual environment (no admin) |
| Browser | the installed Edge (`channel="msedge"`): no download |
| OCR | Tesseract's installer may need admin → use a portable copy (`tesseract_cmd=`), or the pip-only OCR engine planned next (RapidOCR/ONNX) |
