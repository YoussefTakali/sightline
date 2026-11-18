"""The pip-only OCR engine on the same synthetic screen as the Tesseract tests."""
import pytest

from sightline import Point, Rect, Text

pytest.importorskip("rapidocr_onnxruntime")


@pytest.fixture
def rapid_session(session_for, screen):
    from sightline.vision.ocr import RapidOcr
    bgr, lay = screen
    return session_for(bgr, timeout=0, ocr=RapidOcr()), lay


def test_rapid_finds_buttons_labels_and_anchors(rapid_session):
    s, lay = rapid_session
    v = s.wait(Text("Valider"))
    assert Rect(*lay["valider"]).contains(v.center)                 # white on blue
    assert s.wait(Text("Mot de passe")).rect.y == pytest.approx(140, abs=8)
    s.click(Text("Email :").area("right", 300, gap=4))
    (x, y, _), = s.backend.clicks()
    assert Rect(*lay["email_field"]).contains(Point(x, y))


def test_rapid_reads_a_line(rapid_session):
    s, _ = rapid_session
    text = s.read_text(Rect(25, 325, 300, 26))
    assert "12/03/2026" in text
