import re

import pytest
from conftest import needs_tesseract

from sightline import NotFound, Point, Rect, Text

pytestmark = needs_tesseract


def test_text_on_any_background(session_for, screen):
    bgr, lay = screen
    s = session_for(bgr, timeout=0)
    v = s.wait(Text("Valider"))                     # white on blue
    x, y, w, h = lay["valider"]
    assert Rect(x, y, w, h).contains(v.center)
    a = s.wait(Text("Annuler"))                     # dark on grey
    assert a.rect.x < v.rect.x
    t = s.wait(Text("Gestion des commandes"))       # white on dark title bar
    assert t.rect.y < 28


def test_multiword_phrase_and_sub_phrase(session_for, screen):
    bgr, _ = screen
    s = session_for(bgr, timeout=0)
    m = s.wait(Text("Mot de passe"))
    assert 130 <= m.rect.y <= 150
    # a word inside a longer label is located on just that word
    c = s.wait(Text("commandes"))
    full = s.wait(Text("Gestion des commandes"))
    assert c.rect.w < full.rect.w and c.rect.right == pytest.approx(full.rect.right, abs=3)


def test_accent_insensitive_and_regex(session_for, screen):
    bgr, _ = screen
    s = session_for(bgr, timeout=0)
    assert s.wait(Text("Statut : Recu"))           # accent-insensitive by default
    d = s.wait(Text(re.compile(r"\d{2}/\d{2}/\d{4}")))
    assert d.text == "12/03/2026"


def test_label_anchored_field_and_read(session_for, screen):
    bgr, lay = screen
    s = session_for(bgr, timeout=0)
    field = Text("Email :").area("right", 300, gap=4)
    s.click(field)
    (cx, cy, _), = s.backend.clicks()
    assert Rect(*lay["email_field"]).contains(Point(cx, cy))


def test_not_found_lists_closest_text(session_for, screen):
    bgr, _ = screen
    s = session_for(bgr, timeout=0)
    with pytest.raises(NotFound) as e:
        s.wait(Text("Supprimer", exact=True))
    assert "closest OCR text" in str(e.value)
