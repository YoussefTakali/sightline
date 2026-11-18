# Headless on Windows (and macOS)

## The one thing to understand

Linux can render a GUI with no screen at all (Xvfb). **Windows cannot**: an application
draws only inside an *interactive desktop session* that is **logged in, unlocked and
rendering**. "Headless" on Windows therefore means: *a session nobody looks at*, on a
VM or a spare machine, that you reach over VNC.

| Situation | Does the app render? |
|---|---|
| User logged in at the console, unlocked | yes |
| Screen locked / screensaver / display asleep | **no** (secure desktop: captures are black/lock screen) |
| RDP session, window open | yes |
| RDP session **minimized** | **no**, unless the registry fix below |
| RDP session **disconnected** | **no** (the session is locked), unless moved to the console with `tscon` |
| Nobody logged in (service / scheduled task "run whether logged on or not") | **no**: session 0 has no visible desktop |

## Recommended setup: a Windows VM + a free VNC server

1. **VM** (Hyper-V, VirtualBox, Proxmox, VMware, a cloud VM). Its virtual display is your
   "headless screen" with a fixed resolution, e.g. 1920x1080.
2. **VNC server**: free, no account needed:
   - **TightVNC** (GPL, free for commercial use) - recommended, install the *Server* as a
     Windows service (it then also shows the logon screen after a reboot).
   - **UltraVNC** (GPL) - also fine.
   - Avoid **RealVNC Server** for this: it needs a RealVNC account/licence
     (the free tier is non-commercial). The TigerVNC Windows server is unmaintained.
   - Set a *VNC password* (classic VNC auth, max 8 characters) - sightline supports it.
3. **Keep the session alive and unlocked**
   - Automatic logon of the test user: Sysinternals **Autologon** (stores the password
     encrypted in LSA, not in clear text in the registry).
   - No screensaver, no lock: *Settings > Personalization > Lock screen > Screen saver: None*,
     and "On resume, display logon screen" off.
   - Never sleep / never turn the display off:
     ```bat
     powercfg /change monitor-timeout-ac 0
     powercfg /change standby-timeout-ac 0
     ```
   - Windows Update: set active hours / no automatic restart during test windows.
4. **Make the pixels deterministic** (this is what makes image assets reusable):
   - fixed resolution and **display scaling** (100%, or capture assets at the scaling you
     use and name them `name@1.25x.png`);
   - same theme, same ClearType setting on every test machine (font smoothing changes text
     pixels);
   - *System > Advanced > Performance: Adjust for best performance* (no animations,
     no fading menus).
5. **Network security**: VNC is not encrypted. Allow port 5900 only from the CI runner
   (Windows firewall), or bind it to localhost and tunnel through SSH (Windows ships an
   OpenSSH server): `ssh -N -L 5900:127.0.0.1:5900 tester@vm`.

Then, from ANY machine (Linux CI included):

```bash
SIGHTLINE_VNC_PASSWORD=... sightline vnc-check 10.0.0.50:5900 --password-env SIGHTLINE_VNC_PASSWORD --out vm.png
SIGHTLINE_PROFILE=windows-vm robot tests/
```

## If you must use RDP

- **Disconnecting RDP locks the session.** Before disconnecting, send the session back to
  the console so it keeps rendering (run as admin *inside* the RDP session):
  ```bat
  for /f "skip=1 tokens=3" %s in ('query user %USERNAME%') do tscon.exe %s /dest:console
  ```
  The console resolution then applies (often 1024x768 on a VM without a display driver
  setting): set it explicitly.
- **Minimized RDP window stops rendering.** On the machine that runs the RDP *client*:
  ```
  HKCU\Software\Microsoft\Terminal Server Client   RemoteDesktop_SuppressWhenMinimized = 2 (DWORD)
  ```
  (also under `HKLM\SOFTWARE\Microsoft\Terminal Server Client` and the `Wow6432Node`
  equivalent for 64-bit clients).

## Citrix / RemoteApp (app runs on a server, shown on a client)

The application is just pixels inside a local window - perfect for visual automation.
Run the automation on the *client* machine (local backend), or make that client a VM
with a VNC server (headless). Clipboard redirection must be enabled in the Citrix/RDP
policy for `read_table_via_clipboard` to work through the remote session.

## Where does the automation code run?

| Mode | Code runs on | Backend | Notes |
|---|---|---|---|
| Headed, local | the Windows machine | `local` | simplest; the screen must not be used meanwhile |
| Headless, remote | CI runner (any OS) | `vnc` | runner and target separated; one VM per parallel worker |
| Headless, in-VM | inside the VM | `local` | VNC only to watch; files/Excel windows are local again |
| Your OculiX jar | the Windows machine | `oculix` | capture + input through the jar, matching/OCR in sightline |

With the `vnc` backend, *the clipboard is the target's* (RFB cut-text, Latin-1: é è à ç
fine, € and Œ arrive as `?`). Files written on the target (CSV exports) are **not** on the
runner: export to a shared folder, or run in-VM.

## macOS

Built-in VNC: *System Settings > General > Sharing > Screen Sharing* > (i) >
"VNC viewers may control screen with password". A user must be logged in; for a Mac
without a monitor use a dummy HDMI plug or a virtual display. Grant the runner
*Accessibility* and *Screen Recording* permissions if you use the local backend.
