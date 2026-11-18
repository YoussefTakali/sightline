"""The VNC client against an in-process fake RFB 3.8 server."""
import os
import socket
import struct
import threading
import time
import zlib

import numpy as np
import pytest

from sightline import Session, SightlineError, read_table_via_clipboard
from sightline.backends._des import des_ecb_encrypt
from sightline.backends.vnc import VNCBackend, _vnc_des_key


class FakeVNCServer:
    def __init__(self, img: np.ndarray, password: str | None = "s3cret", zlib_ok=True):
        self.img = img.copy()
        self.password, self.zlib_ok = password, zlib_ok
        self.events: list[tuple] = []
        self.client_cut: list[str] = []
        self.on_copy = None                                  # text "copied" on ctrl+c
        self._srv = socket.socket()
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(1)
        self.port = self._srv.getsockname()[1]
        self._lock = threading.Lock()
        self._pending = False
        self._ctrl = False
        self._encodings: list[int] = []
        self._z = zlib.compressobj()
        threading.Thread(target=self._serve, daemon=True).start()

    def _recv(self, n):
        b = b""
        while len(b) < n:
            c = self.c.recv(n - len(b))
            if not c:
                raise ConnectionError
            b += c
        return b

    def _send(self, data):
        with self._lock:
            self.c.sendall(data)

    def _rect(self, x, y, w, h):
        px = np.zeros((h, w, 4), np.uint8)
        px[:, :, :3] = self.img[y:y + h, x:x + w]
        raw = px.tobytes()
        if self.zlib_ok and 6 in self._encodings:
            comp = self._z.compress(raw) + self._z.flush(zlib.Z_SYNC_FLUSH)
            return struct.pack(">HHHHiI", x, y, w, h, 6, len(comp)) + comp
        return struct.pack(">HHHHi", x, y, w, h, 0) + raw

    def _update(self, rects):
        self._send(struct.pack(">BxH", 0, len(rects)) + b"".join(self._rect(*r) for r in rects))

    def set_image(self, img):
        self.img = img.copy()
        if self._pending:
            self._pending = False
            h, w = img.shape[:2]
            self._update([(0, 0, w, h)])

    def _serve(self):
        self.c, _ = self._srv.accept()
        try:
            self._send(b"RFB 003.008\n")
            assert self._recv(12) == b"RFB 003.008\n"
            if self.password is None:
                self._send(b"\x01\x01")
                assert self._recv(1) == b"\x01"
                self._send(struct.pack(">I", 0))
            else:
                self._send(b"\x01\x02")
                assert self._recv(1) == b"\x02"
                chal = os.urandom(16)
                self._send(chal)
                resp = self._recv(16)
                ok = resp == des_ecb_encrypt(_vnc_des_key(self.password), chal)
                if not ok:
                    msg = b"bad password"
                    self._send(struct.pack(">II", 1, len(msg)) + msg)
                    return
                self._send(struct.pack(">I", 0))
            self._recv(1)                                       # ClientInit
            h, w = self.img.shape[:2]
            name = b"fake"
            self._send(struct.pack(">HH", w, h) + bytes(16) + struct.pack(">I", len(name)) + name)
            while True:
                t = self._recv(1)[0]
                if t == 0:
                    self._recv(19)
                elif t == 2:
                    (n,) = struct.unpack(">xH", self._recv(3))
                    self._encodings = list(struct.unpack(f">{n}i", self._recv(4 * n)))
                elif t == 3:
                    inc, x, y, rw, rh = struct.unpack(">BHHHH", self._recv(9))
                    if inc:
                        self._pending = True                    # defer until something changes
                    else:
                        self._update([(x, y, rw, rh)])
                elif t == 4:
                    down, key = struct.unpack(">BxxI", self._recv(7))
                    self.events.append(("key", down, key))
                    if key == 0xFFE3:
                        self._ctrl = bool(down)
                    if key == ord("c") and down and self._ctrl and self.on_copy:
                        txt = self.on_copy.encode("latin-1")
                        self._send(struct.pack(">Bxxxi", 3, len(txt)) + txt)
                elif t == 5:
                    mask, x, y = struct.unpack(">BHH", self._recv(5))
                    self.events.append(("ptr", mask, x, y))
                elif t == 6:
                    (n,) = struct.unpack(">xxxi", self._recv(7))
                    self.client_cut.append(self._recv(n).decode("latin-1"))
        except (ConnectionError, OSError):
            pass


@pytest.fixture
def img():
    rng = np.random.default_rng(1)
    return rng.integers(0, 255, (120, 160, 3), dtype=np.uint8)


@pytest.mark.parametrize("zlib_ok", [True, False])
def test_capture_auth_and_sync(img, zlib_ok):
    srv = FakeVNCServer(img, zlib_ok=zlib_ok)
    b = VNCBackend("127.0.0.1", srv.port, password="s3cret", compress=True)
    try:
        assert b.size() == (160, 120) and b.desktop_name == "fake"
        assert np.array_equal(b.capture().pixels, img)
        img2 = img.copy()
        img2[10:20, 10:20] = 0
        srv.set_image(img2)
        time.sleep(0.05)
        assert np.array_equal(b.capture().pixels, img2)
        assert np.array_equal(b.capture(region=None).crop(
            __import__("sightline").Rect(10, 10, 10, 10)).pixels, img2[10:20, 10:20])
    finally:
        b.close()


def test_wrong_password(img):
    srv = FakeVNCServer(img)
    with pytest.raises(SightlineError, match="bad password"):
        VNCBackend("127.0.0.1", srv.port, password="nope")


def test_input_events(img):
    srv = FakeVNCServer(img, password=None)
    b = VNCBackend("127.0.0.1", srv.port, password=None)
    try:
        b.click(10, 20)
        b.type_text("aé€")
        b.scroll(5, 5, 2)
        time.sleep(0.2)
        ptr = [e for e in srv.events if e[0] == "ptr"]
        assert ("ptr", 0, 10, 20) in ptr and ("ptr", 1, 10, 20) in ptr
        keys = [(d, k) for _, d, k in (e for e in srv.events if e[0] == "key")]
        assert keys == [(1, 0x61), (0, 0x61), (1, 0xE9), (0, 0xE9),
                        (1, 0x010020AC), (0, 0x010020AC)]
        assert sum(1 for e in ptr if e[1] == 16) == 2           # 2 wheel-down clicks
    finally:
        b.close()


def test_remote_clipboard_table(img):
    srv = FakeVNCServer(img, password=None)
    srv.on_copy = "Numéro\tClient\r\nA-001\tDupont\r\nA-002\tLefèvre\r\n"
    b = VNCBackend("127.0.0.1", srv.port, password=None, target_os="windows")
    s = Session(b, trace="off", timeout=1, settle=0.0)
    try:
        table = read_table_via_clipboard(s, (50, 50), columns=["Numéro", "Client"],
                                         must_contain=["Dupont"])
        assert table == [["Numéro", "Client"], ["A-001", "Dupont"], ["A-002", "Lefèvre"]]
        assert srv.client_cut and srv.client_cut[0].startswith("sightline-")   # sentinel sent
    finally:
        s.close()


def test_pure_python_des_matches_reference():
    DES = pytest.importorskip("Crypto.Cipher.DES")
    from sightline.backends import _des
    keys = [_vnc_des_key(p) for p in ("s3cret", "", "longerthan8", "é€x")]
    blocks = [os.urandom(16) for _ in range(4)] + [bytes(16), b"\xff" * 16]
    for k in keys:
        for blk in blocks:
            ref = DES.new(k, DES.MODE_ECB).encrypt(blk)
            sub = _des._subkeys(k)
            mine = b"".join(_des._encrypt_block(blk[i:i + 8], sub) for i in (0, 8))
            assert mine == ref
    # classic FIPS test vector
    sub = _des._subkeys(bytes.fromhex("133457799BBCDFF1"))
    assert _des._encrypt_block(bytes.fromhex("0123456789ABCDEF"), sub).hex() == "85e813540f0ab405"
