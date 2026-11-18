"""Linux: run a LOCALLY INSTALLED app on a private, invisible screen - fully headless.

    s = Session(PrivateDisplayBackend(["/opt/myapp/bin/myapp"]))

sightline starts its own X server as your normal user (no root, no container), launches
the app on it, and drives it through localhost-only VNC with a random password - the
already-tested VNC backend. Your own desktop, mouse, keyboard and clipboard are never
touched; several apps/tests can run in parallel, one display each.

Server, best first:
- TigerVNC `Xvnc` (package tigervnc-standalone-server / tigervnc-server-minimal): X server
  and VNC in one process; reliable clipboard.
- fallback `Xvfb` + `x11vnc`: works, but x11vnc drops a copy IDENTICAL to the previous one
  and only relays the clipboard ~10 s after connecting (both handled/flagged here).

Works under X11 AND Wayland desktops: the app is started as an X11 client of the private
display (GTK, Qt, Java, Wine, Electron... all speak X11).

`openbox` is used if present so dialogs get normal window placement.
"""
from __future__ import annotations

import os
import secrets
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path

from ..errors import SightlineError
from ._des import vnc_password_file
from .vnc import VNCBackend


def _free_display() -> int:
    for n in range(90, 400):
        if not Path(f"/tmp/.X11-unix/X{n}").exists() and not Path(f"/tmp/.X{n}-lock").exists():
            return n
    raise SightlineError("no free X display number between :90 and :399")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _need(prog: str, pkg: str) -> str:
    path = shutil.which(prog)
    if not path:
        raise SightlineError(f"'{prog}' not found: install the {pkg} package")
    return path


class PrivateDisplayBackend(VNCBackend):
    name = "display"

    def __init__(self, command: list[str] | str | None = None, size: tuple[int, int] | list[int] = (1280, 800),
                 window_manager: bool = True, env: dict | None = None, cwd: str | None = None,
                 start_timeout: float = 20.0, server: str = "auto", **vnc_opts):
        if not os.name == "posix" or os.uname().sysname != "Linux":
            raise SightlineError("private displays are Linux-only; on Windows use the 'window' backend")
        xvnc = shutil.which("Xvnc") if server in ("auto", "xvnc") else None
        if server == "xvnc" and not xvnc:
            raise SightlineError("'Xvnc' not found: install tigervnc-standalone-server "
                                 "(Fedora: tigervnc-server-minimal)")
        if not xvnc:
            xvfb = _need("Xvfb", "tigervnc-standalone-server (recommended) or xvfb")
            x11vnc = _need("x11vnc", "tigervnc-standalone-server (recommended) or x11vnc")
        self.server = "xvnc" if xvnc else "x11vnc"
        w, h = (int(v) for v in size)
        self.display = _free_display()
        self._procs: list[subprocess.Popen] = []
        self._tmp = tempfile.mkdtemp(prefix="sightline-display-")
        dpy = f":{self.display}"
        password = secrets.token_hex(4)                           # VNC passwords: 8 chars
        pwfile = os.path.join(self._tmp, "passwd")
        with open(pwfile, "wb") as f:
            f.write(vnc_password_file(password))
        os.chmod(pwfile, 0o600)
        port = _free_port()
        try:
            if xvnc:
                self._spawn([xvnc, dpy, "-geometry", f"{w}x{h}", "-depth", "24", "-dpi", "96",
                             "-rfbport", str(port), "-interface", "127.0.0.1", "-nolisten", "tcp",
                             "-SecurityTypes", "VncAuth", "-PasswordFile", pwfile,
                             "-AlwaysShared=1", "-SendCutText=1", "-AcceptCutText=1",
                             "-SendPrimary=0", "-SetPrimary=0", "-AcceptSetDesktopSize=0"])
                self._clip_ok = True                        # no clipboard warm-up with Xvnc
            else:
                self._spawn([xvfb, dpy, "-screen", "0", f"{w}x{h}x24", "-nolisten", "tcp",
                             "-dpi", "96"])
            # the socket FILE appears before the server accepts clients: an app started in
            # that gap fails to connect and exits silently - wait until it really accepts
            self._wait(lambda: _x_accepts(self.display), start_timeout,
                       f"{self.server} did not start")
            base_env = {k: v for k, v in os.environ.items()
                        if k not in ("WAYLAND_DISPLAY", "DISPLAY", "XAUTHORITY")}
            base_env.update({"DISPLAY": dpy, "GDK_BACKEND": "x11", "QT_QPA_PLATFORM": "xcb",
                             "SDL_VIDEODRIVER": "x11", "ELECTRON_OZONE_PLATFORM_HINT": "x11"})
            if window_manager:
                for wm in ("openbox", "fluxbox", "xfwm4", "metacity"):
                    if shutil.which(wm):
                        self._spawn([wm], env=base_env)
                        # an app mapping its window while the WM is still starting can stay
                        # invisible forever: wait until the WM really adopts windows
                        _wm_manages_windows(self.display)
                        break
            if not xvnc:
                self._spawn([x11vnc, "-display", dpy, "-rfbport", str(port), "-localhost",
                             "-rfbauth", pwfile, "-forever", "-shared", "-noprimary",
                             "-nosetprimary", "-add_keysyms", "-wait", "10", "-defer", "10",
                             "-nonap", "-o", os.path.join(self._tmp, "x11vnc.log")], env=base_env)
            self._wait(lambda: _port_open(port), start_timeout, "VNC server did not start")
            if command:
                cmd = command if isinstance(command, list) else ["sh", "-c", command]
                app_env = dict(base_env, **(env or {}))
                self._app_log = os.path.join(self._tmp, "app.log")
                self.app = self._spawn(cmd, env=app_env, cwd=cwd, log=self._app_log)
            vnc_opts.setdefault("target_os", "linux")
            self.port, self._password = port, password
            super().__init__("127.0.0.1", port, password, **vnc_opts)
        except Exception:
            self._stop_all()
            raise

    def _spawn(self, cmd, env=None, cwd=None, log=None) -> subprocess.Popen:
        out = open(log, "wb") if log else subprocess.DEVNULL     # noqa: SIM115 - handed to the child
        try:
            p = subprocess.Popen(cmd, env=env, cwd=cwd, stdin=subprocess.DEVNULL, stdout=out,
                                 stderr=subprocess.STDOUT if log else subprocess.DEVNULL,
                                 start_new_session=True)
        finally:
            if log:
                out.close()
        self._procs.append(p)
        return p

    def _check_app(self) -> None:
        """Fail fast with the app's own error instead of a 30 s 'not found'."""
        app = getattr(self, "app", None)
        if app is not None and app.poll() is not None:
            tail = ""
            try:
                with open(self._app_log, encoding="utf-8", errors="replace") as f:
                    tail = "".join(f.readlines()[-15:])
            except OSError:
                pass
            raise SightlineError(f"the application exited (code {app.returncode})"
                                 + (f":\n{tail}" if tail else ""))

    def capture(self, region=None, sync: bool = True):
        self._check_app()
        return super().capture(region, sync)

    @staticmethod
    def _wait(cond, timeout, msg) -> None:
        deadline = time.monotonic() + timeout
        while not cond():
            if time.monotonic() > deadline:
                raise SightlineError(msg)
            time.sleep(0.05)

    def _clipboard_ready(self, timeout: float = 25.0) -> None:
        """x11vnc starts watching the X clipboard ~10 s after a client connects ("created
        selwin" in its log); a copy before that is silently lost. Wait for it - only on
        first clipboard use, so start-up stays fast."""
        if getattr(self, "_clip_ok", False):
            return
        log = os.path.join(self._tmp, "x11vnc.log")

        def ready():
            try:
                with open(log, encoding="utf-8", errors="replace") as f:
                    return "created selwin" in f.read()
            except OSError:
                return False
        self._wait(ready, timeout, "x11vnc clipboard bridge did not become ready")
        self._clip_ok = True

    def get_clipboard(self) -> str:
        self._clipboard_ready()
        return super().get_clipboard()

    def set_clipboard(self, text: str) -> None:
        self._clipboard_ready()
        super().set_clipboard(text)

    def app_running(self) -> bool:
        return getattr(self, "app", None) is not None and self.app.poll() is None

    def _stop_all(self) -> None:
        for p in reversed(self._procs):
            if p.poll() is None:
                p.terminate()
        for p in reversed(self._procs):
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
        shutil.rmtree(self._tmp, ignore_errors=True)

    def close(self) -> None:
        try:
            super().close()
        finally:
            self._stop_all()


def _x_request(sock, data: bytes) -> bytes:
    """Send one X11 request, return its 32-byte reply (+ extra data); skip events."""
    import struct
    sock.sendall(data)
    while True:
        head = _recv_exact(sock, 32)
        if head[0] == 1:                                   # reply
            extra = struct.unpack("<I", head[4:8])[0] * 4
            return head + (_recv_exact(sock, extra) if extra else b"")
        if head[0] == 0:                                   # error
            raise OSError(f"X error {head[1]}")


def _recv_exact(sock, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise OSError("X server closed the connection")
        buf += chunk
    return buf


def _x_connect(display: int):
    """Minimal X11 client connection: (socket, root window, resource-id base)."""
    import struct
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(1.0)
    s.connect(f"/tmp/.X11-unix/X{display}")
    s.sendall(struct.pack("<BxHHHHxx", 0x6C, 11, 0, 0, 0))
    head = _recv_exact(s, 8)
    body = _recv_exact(s, struct.unpack("<H", head[6:8])[0] * 4)
    if head[0] != 1:
        s.close()
        raise OSError("X connection refused")
    base, = struct.unpack("<I", body[4:8])
    vendor_len, = struct.unpack("<H", body[16:18])
    off = 32 + ((vendor_len + 3) // 4) * 4 + 8 * body[21]
    root, = struct.unpack("<I", body[off:off + 4])
    return s, root, base


def _wm_manages_windows(display: int, timeout: float = 5.0) -> bool:
    """True once the window manager PROCESSES map requests: a probe window is created and
    mapped, and must get reparented into a WM frame. (A WM can already advertise itself
    while still ignoring map requests - an app showing its window then stays invisible.)"""
    import struct
    try:
        s, root, base = _x_connect(display)
    except OSError:
        return False
    with s:
        wid = base | 1
        try:
            s.sendall(struct.pack("<BBHIIhhHHHHII", 1, 0, 8, wid, root, 0, 0, 1, 1, 0, 1, 0, 0))
            s.sendall(struct.pack("<BxHI", 8, 2, wid))                      # MapWindow
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                rep = _x_request(s, struct.pack("<BxHI", 15, 2, wid))       # QueryTree
                parent, = struct.unpack("<I", rep[12:16])
                if parent != root:
                    return True                                             # adopted by WM
                time.sleep(0.05)
            return False
        except OSError:
            return False


def _x_accepts(display: int) -> bool:
    path = f"/tmp/.X11-unix/X{display}"
    if not Path(path).exists():
        return False
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(0.2)
            s.connect(path)
        return True
    except OSError:
        return False


def _port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.2):
            return True
    except OSError:
        return False
