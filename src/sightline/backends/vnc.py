"""VNC (RFB 3.3/3.7/3.8) backend, pure Python.

Why our own client instead of a library: we need (1) a framebuffer that is kept up to
date in the background so a capture costs a memcpy, not a round-trip, (2) the remote
clipboard (Ctrl+C on the target -> ServerCutText), and (3) no Twisted / Java bridge.

Works with any standard server: x11vnc / TigerVNC (Linux, Xvfb containers),
TightVNC / UltraVNC (Windows, free, no account), macOS Screen Sharing (VNC password).

Encodings: Zlib (6), Raw (0), CopyRect (1), DesktopSize (-223). Zlib keeps a 1080p
full refresh around 0.3-1 MB instead of 8 MB, which matters over a real network.

Clipboard: classic RFB cut-text is Latin-1. French accents (é è à ç ô) are in Latin-1,
the euro sign and Œ are not (they arrive as '?').
"""
from __future__ import annotations

import logging
import socket
import struct
import threading
import time
import zlib

import numpy as np

from ..errors import SightlineError
from ..frame import Frame
from ..geometry import Rect
from .base import Backend

log = logging.getLogger(__name__)

ENC_RAW, ENC_COPYRECT, ENC_ZLIB = 0, 1, 6
ENC_DESKTOP_SIZE, ENC_LAST_RECT, ENC_CURSOR = -223, -224, -239

KEYSYMS = {
    "enter": 0xFF0D, "tab": 0xFF09, "esc": 0xFF1B, "backspace": 0xFF08, "delete": 0xFFFF,
    "insert": 0xFF63, "home": 0xFF50, "end": 0xFF57, "pageup": 0xFF55, "pagedown": 0xFF56,
    "left": 0xFF51, "up": 0xFF52, "right": 0xFF53, "down": 0xFF54, "space": 0x0020,
    "shift": 0xFFE1, "ctrl": 0xFFE3, "alt": 0xFFE9, "cmd": 0xFFE7, "win": 0xFFEB,
    "capslock": 0xFFE5, "menu": 0xFF67, "printscreen": 0xFF61,
    **{f"f{i}": 0xFFBE + i - 1 for i in range(1, 13)},
}
BUTTON_BITS = {"left": 1, "middle": 2, "right": 4}


def char_keysym(ch: str) -> int:
    if ch == "\n":
        return KEYSYMS["enter"]
    if ch == "\t":
        return KEYSYMS["tab"]
    cp = ord(ch)
    if 0x20 <= cp <= 0x7E or 0xA0 <= cp <= 0xFF:
        return cp                       # Latin-1 keysyms are the code point
    return 0x01000000 + cp              # X11 Unicode keysym


def _vnc_des_key(password: str) -> bytes:
    """VNC auth quirk: DES key = password (8 bytes, NUL padded), each byte bit-reversed."""
    raw = password.encode("latin-1", "replace")[:8].ljust(8, b"\0")
    return bytes(int(f"{b:08b}"[::-1], 2) for b in raw)


class VNCBackend(Backend):
    name = "vnc"
    capabilities = frozenset({"mouse", "keyboard", "scroll", "clipboard"})

    def __init__(self, host: str = "127.0.0.1", port: int = 5900, password: str | None = None,
                 target_os: str = "linux", timeout: float = 10.0, shared: bool = True,
                 compress: bool = True, sync_timeout: float = 0.3, hide_cursor: bool = True):
        super().__init__(target_os)
        self.host, self.port = host, int(port)
        self.sync_timeout = float(sync_timeout)
        self._sock = socket.create_connection((host, self.port), timeout=timeout)
        self._sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._buf = bytearray()
        self._send_lock = threading.Lock()
        self._fb_lock = threading.Lock()
        self._cond = threading.Condition()
        self._serial = 0
        self._clip_text = ""
        self._clip_serial = 0
        self._closed = False
        self._error: Exception | None = None
        self._mask = 0
        self._pos = (0, 0)
        self._zlib = zlib.decompressobj()
        self._handshake(password, shared)
        # never block forever: a send to a stalled server fails after 10 s; the reader treats
        # an idle timeout as "nothing new yet" (see _recv)
        self._sock.settimeout(10.0)
        # ENC_CURSOR: "I draw the cursor myself" -> the server stops painting it into the
        # framebuffer, so captures are cursor-free (a cursor over a small icon or a
        # checkbox otherwise breaks matching). We simply never draw it.
        encodings = ([ENC_ZLIB] if compress else []) + [ENC_COPYRECT, ENC_RAW, ENC_DESKTOP_SIZE,
                                                       ENC_LAST_RECT] + \
            ([ENC_CURSOR] if hide_cursor else [])
        self._set_pixel_format()
        self._send(struct.pack(">BxH", 2, len(encodings)) +
                   b"".join(struct.pack(">i", e) for e in encodings))
        self._request(incremental=False)
        self._reader_started = True
        self._thread = threading.Thread(target=self._reader, name="vnc-reader", daemon=True)
        self._thread.start()
        self._wait_serial(0, timeout)

    # ------------------------------------------------------------------ protocol
    def _recv(self, n: int) -> bytes:
        while len(self._buf) < n:
            try:
                chunk = self._sock.recv(max(65536, n - len(self._buf)))
            except TimeoutError:
                if self._closed or not getattr(self, "_reader_started", False):
                    raise                                 # handshake: a real timeout
                continue                                  # idle screen: keep waiting
            if not chunk:
                raise ConnectionError("VNC server closed the connection")
            self._buf += chunk
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out

    def _send(self, data: bytes) -> None:
        with self._send_lock:
            self._sock.sendall(data)

    def _reason(self) -> str:
        (n,) = struct.unpack(">I", self._recv(4))
        return self._recv(n).decode("utf-8", "replace")

    def _handshake(self, password: str | None, shared: bool) -> None:
        banner = self._recv(12)
        if not banner.startswith(b"RFB "):
            raise SightlineError(f"{self.host}:{self.port} is not a VNC server ({banner!r})")
        major, minor = int(banner[4:7]), int(banner[8:11])
        minor = 8 if (major, minor) >= (3, 8) else (7 if (major, minor) >= (3, 7) else 3)
        self._send(b"RFB 003.%03d\n" % minor)
        if minor == 3:
            (sec,) = struct.unpack(">I", self._recv(4))
            if sec == 0:
                raise SightlineError(f"VNC refused: {self._reason()}")
        else:
            (n,) = struct.unpack(">B", self._recv(1))
            if n == 0:
                raise SightlineError(f"VNC refused: {self._reason()}")
            offered = list(self._recv(n))
            if 2 in offered and password is not None:
                sec = 2
            elif 1 in offered:
                sec = 1
            elif 2 in offered:
                raise SightlineError("The VNC server requires a password (vnc_password)")
            else:
                raise SightlineError(
                    f"No supported VNC security type offered {offered}. Use classic VNC "
                    "password auth (type 2) or none (1); tunnel it through SSH/VPN.")
            self._send(bytes([sec]))
        if sec == 2:
            from ._des import des_ecb_encrypt
            challenge = self._recv(16)
            self._send(des_ecb_encrypt(_vnc_des_key(password or ""), challenge))
        if sec == 2 or minor == 8:
            (result,) = struct.unpack(">I", self._recv(4))
            if result != 0:
                why = self._reason() if minor == 8 else "authentication failed"
                raise SightlineError(f"VNC authentication failed: {why}")
        self._send(bytes([1 if shared else 0]))
        w, h = struct.unpack(">HH", self._recv(4))
        self._recv(16)                                    # server pixel format (we override)
        self.desktop_name = self._reason()
        self._fb = np.zeros((h, w, 4), np.uint8)
        log.info("VNC connected to %s:%s '%s' %dx%d", self.host, self.port,
                 self.desktop_name, w, h)

    def _set_pixel_format(self) -> None:
        # 32 bpp, depth 24, little endian, true colour, BGRX in memory = OpenCV's order
        pf = struct.pack(">BBBBHHHBBBxxx", 32, 24, 0, 1, 255, 255, 255, 16, 8, 0)
        self._send(struct.pack(">Bxxx", 0) + pf)

    def _request(self, incremental: bool = True, rect: Rect | None = None) -> None:
        h, w = self._fb.shape[:2]
        r = rect or Rect(0, 0, w, h)
        self._send(struct.pack(">BBHHHH", 3, 1 if incremental else 0, r.x, r.y, r.w, r.h))

    def _reader(self) -> None:
        try:
            while not self._closed:
                (mtype,) = struct.unpack(">B", self._recv(1))
                if mtype == 0:
                    changed = self._read_update()
                    with self._cond:
                        self._serial += 1
                        self._cond.notify_all()
                    if not changed:
                        time.sleep(0.02)                  # server answers instantly: don't spin
                    self._request(incremental=True)
                elif mtype == 1:                          # colour map: ignored (true colour)
                    _, n = struct.unpack(">xHH", self._recv(5))
                    self._recv(6 * n)
                elif mtype == 2:                          # bell
                    pass
                elif mtype == 3:
                    self._recv(3)
                    (n,) = struct.unpack(">i", self._recv(4))
                    text = self._recv(abs(n)).decode("latin-1")
                    with self._cond:
                        self._clip_text = text.replace("\r\n", "\n")
                        self._clip_serial += 1
                        self._cond.notify_all()
                else:
                    raise ConnectionError(f"unknown VNC server message type {mtype}")
        except Exception as exc:
            if not self._closed:
                self._error = exc
                log.error("VNC reader stopped: %s", exc)
            with self._cond:
                self._cond.notify_all()

    def _read_update(self) -> int:
        (n,) = struct.unpack(">xH", self._recv(3))
        changed = 0
        for _ in range(n):
            x, y, w, h, enc = struct.unpack(">HHHHi", self._recv(12))
            if enc == ENC_LAST_RECT:
                break
            if enc == ENC_CURSOR:                         # cursor shape: pixels + bitmask
                self._recv(w * h * 4 + ((w + 7) // 8) * h)
                continue
            if enc == ENC_DESKTOP_SIZE:
                with self._fb_lock:
                    self._fb = np.zeros((h, w, 4), np.uint8)
                self._request(incremental=False)
                changed += 1
                continue
            if enc == ENC_RAW:
                data = self._recv(w * h * 4)
            elif enc == ENC_ZLIB:
                (ln,) = struct.unpack(">I", self._recv(4))
                data = self._zlib.decompress(self._recv(ln))
            elif enc == ENC_COPYRECT:
                sx, sy = struct.unpack(">HH", self._recv(4))
                with self._fb_lock:
                    self._fb[y:y + h, x:x + w] = self._fb[sy:sy + h, sx:sx + w].copy()
                changed += 1
                continue
            else:
                raise ConnectionError(f"server sent unrequested encoding {enc}")
            if w and h:
                px = np.frombuffer(data, np.uint8, count=w * h * 4).reshape(h, w, 4)
                with self._fb_lock:
                    self._fb[y:y + h, x:x + w] = px
                changed += 1
        return changed

    def _check(self) -> None:
        if self._error is not None:
            raise SightlineError(f"VNC connection lost: {self._error}")

    def _wait_serial(self, after: int, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        with self._cond:
            while self._serial <= after and self._error is None:
                left = deadline - time.monotonic()
                if left <= 0:
                    return False
                self._cond.wait(left)
        self._check()
        return True

    # ------------------------------------------------------------------ Backend API
    def size(self) -> tuple[int, int]:
        h, w = self._fb.shape[:2]
        return w, h

    def capture(self, region: Rect | None = None, sync: bool = True) -> Frame:
        """Copy of the framebuffer. sync=True makes it current: a NON-incremental request
        for 1 pixel forces an immediate reply, and servers put every pending change of the
        (always outstanding) incremental request into that same reply. So after one round
        trip the buffer holds everything the server knew when we asked - no fixed sleep,
        no stale frame right after a click."""
        self._check()
        if sync:
            # Bounded: some servers (x11vnc) occasionally fold this request into their own
            # schedule and answer late. Freshness is a best effort here - correctness comes
            # from the session's polling + two-frame stability check, never from one frame.
            s = self._serial
            self._request(incremental=False, rect=Rect(0, 0, 1, 1))
            if self._wait_serial(s, self.sync_timeout):
                # an update already in flight may have arrived first: give ours a moment
                self._wait_serial(s + 1, 0.03)
        with self._fb_lock:
            fb = self._fb
            if region is not None:
                r = region.intersect(Rect(0, 0, fb.shape[1], fb.shape[0])) or Rect(0, 0, 0, 0)
                px = fb[r.y:r.bottom, r.x:r.right, :3].copy()
                return Frame(px, r.x, r.y, time.time())
            return Frame(fb[:, :, :3].copy(), 0, 0, time.time())

    def _pointer(self) -> None:
        x, y = self._pos
        self._send(struct.pack(">BBHH", 5, self._mask, max(x, 0), max(y, 0)))

    def mouse_move(self, x: int, y: int) -> None:
        self._check()
        self._pos = (int(x), int(y))
        self._pointer()

    def mouse_down(self, button: str = "left") -> None:
        self._mask |= BUTTON_BITS[button]
        self._pointer()

    def mouse_up(self, button: str = "left") -> None:
        self._mask &= ~BUTTON_BITS[button]
        self._pointer()

    def scroll(self, x: int, y: int, clicks: int, horizontal: bool = False) -> None:
        self.mouse_move(x, y)
        bit = (64 if clicks > 0 else 32) if horizontal else (16 if clicks > 0 else 8)
        for _ in range(abs(int(clicks))):
            self._send(struct.pack(">BBHH", 5, self._mask | bit, x, y))
            self._send(struct.pack(">BBHH", 5, self._mask, x, y))
            time.sleep(0.03)

    def _key(self, keysym: int, down: bool) -> None:
        self._send(struct.pack(">BBxxI", 4, 1 if down else 0, keysym))

    def _keysym(self, key: str) -> int:
        return KEYSYMS[key] if len(key) > 1 else char_keysym(key)

    def key_down(self, key: str) -> None:
        self._check()
        self._key(self._keysym(key), True)

    def key_up(self, key: str) -> None:
        self._key(self._keysym(key), False)

    def type_text(self, text: str, interval: float = 0.0) -> None:
        self._check()
        for ch in text:
            ks = char_keysym(ch)
            self._key(ks, True)
            self._key(ks, False)
            time.sleep(interval or 0.005)

    def get_clipboard(self) -> str:
        self._check()
        return self._clip_text

    def set_clipboard(self, text: str) -> None:
        data = text.replace("\n", "\r\n" if self.target_os == "windows" else "\n")
        raw = data.encode("latin-1", "replace")
        self._send(struct.pack(">Bxxxi", 6, len(raw)) + raw)
        self._clip_text = text

    def clipboard_serial(self) -> int:
        return self._clip_serial

    def close(self) -> None:
        self._closed = True
        try:
            self._sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self._sock.close()
