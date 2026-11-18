"""OculiX / SikuliX jar as a backend, through py4j.

Only screen capture and input go through Java; matching and OCR stay in sightline
(OpenCV / Tesseract in-process), which avoids one py4j round-trip per search and per
OCR call - the main source of slowness in big grid reads.

Capture uses BMP (no compression) instead of PNG: encoding a 1080p PNG in Java and
pushing it through the py4j socket is the slowest part of a capture.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import cv2
import numpy as np

from ..errors import SightlineError, Unsupported
from ..frame import Frame
from ..geometry import Rect
from .base import Backend


class OculiXBackend(Backend):
    name = "oculix"

    def __init__(self, jar_path: str | None = None, vnc_host: str | None = None,
                 vnc_port: int = 5900, vnc_password: str = "", vnc_width: int | None = None,
                 vnc_height: int | None = None, target_os: str = "windows"):
        super().__init__(target_os)
        try:
            from py4j.java_gateway import GatewayParameters, JavaGateway, launch_gateway
        except ImportError as exc:
            raise SightlineError("OculiX backend needs: pip install py4j") from exc
        jar = jar_path or os.getenv("OCULIX_JAR")
        if not jar or not Path(jar).exists():
            raise SightlineError(f"OculiX jar not found: {jar!r} (set jar_path or OCULIX_JAR)")
        port = launch_gateway(classpath=str(jar), die_on_exit=True)
        self._gw = JavaGateway(gateway_parameters=GatewayParameters(port=port, auto_convert=True))
        S = self._S = self._gw.jvm.org.sikuli.script
        S.support.RunTime.get()
        self._remote = bool(vnc_host)
        if vnc_host:
            if not (vnc_width and vnc_height):
                raise SightlineError("OculiX VNCScreen needs vnc_width and vnc_height")
            self._screen = self._gw.jvm.org.sikuli.vnc.VNCScreen.start(
                vnc_host, int(vnc_port), vnc_password or "", int(vnc_width), int(vnc_height))
        else:
            self._screen = S.Screen()
        self.capabilities = frozenset({"mouse", "keyboard", "scroll"} |
                                      (set() if self._remote else {"clipboard"}))
        self._sx, self._sy = int(self._screen.getX()), int(self._screen.getY())
        self._lw, self._lh = int(self._screen.getW()), int(self._screen.getH())
        probe = self._grab(None)
        self._scale = probe.shape[1] / float(self._lw)   # capture px per logical px (DPI)
        self._w, self._h = probe.shape[1], probe.shape[0]

    def _grab(self, logical: tuple[int, int, int, int] | None) -> np.ndarray:
        x, y, w, h = logical or (self._sx, self._sy, self._lw, self._lh)
        bimg = self._screen.capture(self._screen.newRegion(x, y, w, h)).getImage()
        jvm = self._gw.jvm
        baos = jvm.java.io.ByteArrayOutputStream()
        rgb = jvm.java.awt.image.BufferedImage(bimg.getWidth(), bimg.getHeight(), 1)  # RGB
        rgb.getGraphics().drawImage(bimg, 0, 0, None)     # BMP writer refuses ARGB images
        jvm.javax.imageio.ImageIO.write(rgb, "bmp", baos)
        data = baos.toByteArray()
        img = cv2.imdecode(np.frombuffer(bytes(data), np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            raise SightlineError("OculiX capture could not be decoded")
        return img

    def size(self) -> tuple[int, int]:
        return self._w, self._h

    def capture(self, region: Rect | None = None) -> Frame:
        if region is None:
            return Frame(self._grab(None), 0, 0, time.time())
        s = self._scale
        r = region.intersect(Rect(0, 0, self._w, self._h)) or Rect(0, 0, 1, 1)
        lx, ly = int(r.x / s), int(r.y / s)
        lw, lh = max(int(np.ceil(r.w / s)), 1), max(int(np.ceil(r.h / s)), 1)
        px = self._grab((self._sx + lx, self._sy + ly, lw, lh))
        return Frame(px, int(lx * s), int(ly * s), time.time())

    def _loc(self, x: int, y: int):
        return self._S.Location(self._sx + int(round(x / self._scale)),
                                self._sy + int(round(y / self._scale)))

    def mouse_move(self, x: int, y: int) -> None:
        self._screen.mouseMove(self._loc(x, y))

    def _btn(self, b: str) -> int:
        B = self._S.Button
        return {"left": B.LEFT, "right": B.RIGHT, "middle": B.MIDDLE}[b]

    def mouse_down(self, button: str = "left") -> None:
        self._screen.mouseDown(self._btn(button))

    def mouse_up(self, button: str = "left") -> None:
        self._screen.mouseUp(self._btn(button))

    def scroll(self, x: int, y: int, clicks: int, horizontal: bool = False) -> None:
        if horizontal:
            # SikuliX has no horizontal wheel: Shift+wheel is the de-facto convention
            self.key_down("shift")
            try:
                self.scroll(x, y, clicks)
            finally:
                self.key_up("shift")
            return
        B = self._S.Button
        self._screen.wheel(self._loc(x, y), B.WHEEL_DOWN if clicks > 0 else B.WHEEL_UP,
                           abs(int(clicks)))

    def _skey(self, key: str) -> str:
        if len(key) == 1:
            return key
        K = self._S.Key
        table = {"ctrl": K.CTRL, "shift": K.SHIFT, "alt": K.ALT, "cmd": K.CMD, "win": K.WIN,
                 "enter": K.ENTER, "tab": K.TAB, "esc": K.ESC, "backspace": K.BACKSPACE,
                 "delete": K.DELETE, "insert": K.INSERT, "home": K.HOME, "end": K.END,
                 "pageup": K.PAGE_UP, "pagedown": K.PAGE_DOWN, "up": K.UP, "down": K.DOWN,
                 "left": K.LEFT, "right": K.RIGHT, "space": " ", "capslock": K.CAPS_LOCK,
                 "printscreen": K.PRINTSCREEN}
        table.update({f"f{i}": getattr(K, f"F{i}") for i in range(1, 13)})
        return table[key]

    def key_down(self, key: str) -> None:
        self._screen.keyDown(self._skey(key))

    def key_up(self, key: str) -> None:
        self._screen.keyUp(self._skey(key))

    def type_text(self, text: str, interval: float = 0.0) -> None:
        if interval:
            for ch in text:
                self._screen.type(ch)
                time.sleep(interval)
        else:
            self._screen.type(text)

    def get_clipboard(self) -> str:
        if self._remote:
            raise Unsupported("OculiX VNCScreen: the clipboard read would be THIS machine's, "
                              "not the target's. Use the native 'vnc' backend instead.")
        return str(self._S.App.getClipboard() or "")

    def set_clipboard(self, text: str) -> None:
        if self._remote:
            raise Unsupported("OculiX VNCScreen cannot set the target's clipboard; "
                              "use the native 'vnc' backend")
        self._S.App.setClipboard(text)

    def close(self) -> None:
        try:
            self._gw.shutdown()
        except Exception:
            pass
