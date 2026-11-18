#!/usr/bin/env python3
"""Wait until the window manager on $DISPLAY really adopts windows (stdlib only).

Started right before the app: a WM can advertise itself while still ignoring map
requests, and a window the app shows in that gap stays invisible forever. A probe
window is mapped; once the WM reparents it into a frame, it is ready.
(Same check as sightline.backends.display; standalone so any image can use it.)
Exit 0 when ready, 1 on timeout.
"""
import os
import socket
import sys
import time


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


if __name__ == "__main__":
    dpy = int(os.environ.get("DISPLAY", ":0").lstrip(":").split(".")[0])
    sys.exit(0 if _wm_manages_windows(dpy, float(sys.argv[1]) if len(sys.argv) > 1 else 10) else 1)
