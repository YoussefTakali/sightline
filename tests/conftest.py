"""Synthetic but realistic UI screens (real TTF font, anti-aliased) for offline tests."""
from __future__ import annotations

pytest_plugins = ["pytester"]

import glob
import shutil

import cv2
import numpy as np
import pytest
from PIL import Image as PILImage
from PIL import ImageDraw, ImageFont

from sightline import Session
from sightline.backends.replay import ReplayBackend

FONT_CANDIDATES = (glob.glob("/usr/share/fonts/**/LiberationSans-Regular.ttf", recursive=True)
                   + glob.glob("/usr/share/fonts/**/DejaVuSans.ttf", recursive=True)
                   + glob.glob("C:/Windows/Fonts/arial.ttf"))
HAVE_TESSERACT = shutil.which("tesseract") is not None


def font(size=14):
    if FONT_CANDIDATES:
        return ImageFont.truetype(FONT_CANDIDATES[0], size)
    return ImageFont.load_default()


def to_bgr(pil):
    return cv2.cvtColor(np.asarray(pil.convert("RGB")), cv2.COLOR_RGB2BGR)


def draw_icon(d: ImageDraw.ImageDraw, x, y, color=(30, 120, 220)):
    """A 'save' style icon: square, inner label, notch."""
    d.rectangle([x, y, x + 21, y + 21], fill=color, outline=(20, 20, 20))
    d.rectangle([x + 4, y + 2, x + 17, y + 8], fill=(240, 240, 240))
    d.rectangle([x + 5, y + 12, x + 16, y + 20], fill=(250, 250, 250))
    d.line([x + 8, y + 14, x + 13, y + 14], fill=(90, 90, 90))


def draw_checkbox(d, x, y, checked):
    d.rectangle([x, y, x + 13, y + 13], fill=(255, 255, 255), outline=(80, 80, 80))
    if checked:
        d.line([x + 3, y + 7, x + 6, y + 10, x + 11, y + 3], fill=(20, 20, 20), width=2)


def make_screen(w=900, h=600, scale=1.0):
    """A form-like app window. Returns (bgr, layout dict of known element rects)."""
    S = lambda v: int(round(v * scale))
    img = PILImage.new("RGB", (S(w), S(h)), (236, 236, 236))
    d = ImageDraw.Draw(img)
    f = font(S(14))
    lay = {}
    # title bar
    d.rectangle([0, 0, S(w), S(28)], fill=(45, 85, 140))
    d.text((S(10), S(6)), "Gestion des commandes", font=f, fill=(255, 255, 255))
    # toolbar with three identical icons + one green
    for i, x in enumerate((20, 60, 100)):
        draw_icon(d, S(x), S(40))
        lay[f"icon{i}"] = (S(x), S(40), S(22), S(22))
    draw_icon(d, S(140), S(40), color=(40, 170, 70))
    lay["icon_green"] = (S(140), S(40), S(22), S(22))
    # form
    d.text((S(30), S(100)), "Email :", font=f, fill=(20, 20, 20))
    d.rectangle([S(140), S(96), S(420), S(120)], fill=(255, 255, 255), outline=(150, 150, 150))
    lay["email_field"] = (S(140), S(96), S(281), S(25))
    d.text((S(30), S(140)), "Mot de passe :", font=f, fill=(20, 20, 20))
    d.rectangle([S(140), S(136), S(420), S(160)], fill=(255, 255, 255), outline=(150, 150, 150))
    # checkboxes with labels
    for i, (label, checked, y) in enumerate((("Actif", True, 190), ("Archivé", False, 220))):
        d.text((S(30), S(y)), label, font=f, fill=(20, 20, 20))
        draw_checkbox(d, S(140), S(y + 1), checked)
        lay[f"cb_{i}"] = (S(140), S(y + 1), S(14), S(14))
    # buttons: dark text on grey, white text on blue
    d.rectangle([S(30), S(270), S(130), S(300)], fill=(215, 215, 215), outline=(120, 120, 120))
    d.text((S(52), S(277)), "Annuler", font=f, fill=(20, 20, 20))
    d.rectangle([S(150), S(270), S(250), S(300)], fill=(30, 100, 200), outline=(20, 60, 140))
    d.text((S(175), S(277)), "Valider", font=f, fill=(255, 255, 255))
    lay["valider"] = (S(150), S(270), S(101), S(31))
    # a status line with an accented word
    d.text((S(30), S(330)), "Statut : Reçu le 12/03/2026", font=f, fill=(60, 60, 60))
    return to_bgr(img), lay


def crop(bgr, rect, pad=0):
    x, y, w, h = rect
    return bgr[y - pad:y + h + pad, x - pad:x + w + pad].copy()


@pytest.fixture
def screen():
    return make_screen()


@pytest.fixture
def session_for():
    made = []

    def _make(frames, **kw):
        kw.setdefault("trace", "off")
        kw.setdefault("timeout", 0.5)
        kw.setdefault("poll", 0.01)
        kw.setdefault("settle", 0.0)
        s = Session(ReplayBackend(frames if isinstance(frames, list) else [frames]), **kw)
        made.append(s)
        return s

    yield _make
    for s in made:
        s.close()


needs_tesseract = pytest.mark.skipif(not HAVE_TESSERACT, reason="tesseract not installed")
