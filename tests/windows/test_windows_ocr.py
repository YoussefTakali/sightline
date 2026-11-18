"""Windows' built-in OCR engine on real Windows (CI: windows-latest)."""
import sys

import numpy as np
import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="real Windows only")


def _screen():
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGB", (640, 220), (236, 236, 236))
    d = ImageDraw.Draw(img)
    f = ImageFont.truetype("C:/Windows/Fonts/arial.ttf", 15)
    d.text((20, 20), "Valider la commande", font=f, fill=(20, 20, 20))
    d.rectangle([20, 60, 180, 92], fill=(30, 100, 200))
    d.text((40, 67), "Enregistrer", font=f, fill=(255, 255, 255))            # white on blue
    d.text((20, 120), "Commande CMD-0042 - Martin", font=f, fill=(20, 20, 20))
    d.text((20, 160), "Statut : Reçu le 12/03/2026", font=f, fill=(60, 60, 60))
    return np.asarray(img)[:, :, ::-1].copy()


def test_windows_ocr_reads_ui_text():
    from sightline.vision.ocr import fold
    from sightline.vision.winocr import WindowsOcr
    ocr = WindowsOcr()
    try:
        words = [fold(w.text) for w in ocr.words(_screen())]
        print("language:", ocr.language, "words:", words)
        for expected in ("valider", "commande", "enregistrer", "cmd-0042", "martin", "12/03/2026"):
            assert expected in words, f"{expected!r} not read; got {words}"
        text, _ = ocr.line(_screen()[115:145, 10:400])
        assert "CMD-0042" in text
    finally:
        ocr.close()


def test_text_locator_with_windows_ocr():
    from sightline import Session, Text
    from sightline.backends.replay import ReplayBackend
    s = Session(ReplayBackend([_screen()]), ocr="windows", trace="off", timeout=0, settle=0)
    try:
        m = s.wait(Text("Enregistrer"))
        assert 20 <= m.center.x <= 180 and 60 <= m.center.y <= 92
        assert s.wait(Text("CMD-0042", exact=True))
        assert s.exists(Text("Supprimer", exact=True)) is None
    finally:
        s.close()
