"""The inspector's HTTP API (and its page, in headless Chromium if available)."""
import json
import threading
import urllib.request

import pytest
from conftest import make_screen


@pytest.fixture
def inspector(session_for, tmp_path):
    from sightline.inspector import Inspector
    bgr, lay = make_screen()
    s = session_for(bgr)
    s.assets.roots = [tmp_path]
    insp = Inspector(s, str(tmp_path))
    server, url = insp.serve(0)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    yield insp, url.split("/?")[0], lay, tmp_path
    server.shutdown()
    server.server_close()


def call(base, insp, path, body=None, token=None):
    tok = token or insp.token
    req = urllib.request.Request(f"{base}{path}?token={tok}", method="POST" if body else "GET",
                                 data=json.dumps(body).encode() if body else None,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        data = r.read()
        return json.loads(data) if r.headers["Content-Type"] == "application/json" else data


def test_token_required(inspector):
    insp, base, _, _ = inspector
    with pytest.raises(urllib.error.HTTPError) as e:
        call(base, insp, "/info", token="wrong")
    assert e.value.code == 403


def test_crop_then_find_then_click(inspector):
    insp, base, lay, assets = inspector
    png = call(base, insp, "/shot.png")
    assert png[:4] == b"\x89PNG"
    x, y, w, h = lay["valider"]
    r = call(base, insp, "/crop", {"name": "buttons/valider", "rect": [x, y, w, h]})
    assert r["locator"] == "image:buttons/valider" and (assets / "buttons" / "valider.png").exists()
    f = call(base, insp, "/find", {"locator": "image:buttons/valider"})
    assert len(f["matches"]) == 1 and f["matches"][0]["rect"] == [x, y, w, h]
    flat = call(base, insp, "/crop", {"name": "flat", "rect": [600, 500, 40, 20]})
    assert "single colour" in flat["error"]
    bad = call(base, insp, "/crop", {"name": "../escape", "rect": [x, y, w, h]})
    assert "relative name" in bad["error"]
    c = call(base, insp, "/click", {"locator": "image:buttons/valider"})
    assert c["done"].startswith("clicked")
    assert insp.s.backend.clicks()[-1][:2] == (x + w // 2, y + h // 2)


def test_ambiguity_and_diagnostics_reported(inspector, tmp_path):
    import cv2
    from conftest import crop
    insp, base, lay, assets = inspector
    bgr, _ = make_screen()
    cv2.imwrite(str(assets / "icon.png"), crop(bgr, lay["icon0"]))
    f = call(base, insp, "/find", {"locator": "image:icon;color=true"})
    assert len(f["matches"]) == 3 and "AMBIGUOUS" in f["verdict"]
    f = call(base, insp, "/find", {"locator": "at:0,0,30,30 >> image:icon"})
    assert f["matches"] == [] and f["diag"]


def test_page_runs_without_js_errors(inspector):
    pytest.importorskip("playwright")
    from test_e2e_browser import CHROME
    if CHROME is None:
        pytest.skip("no chromium")
    from playwright.sync_api import sync_playwright
    insp, base, lay, _ = inspector
    errors = []
    with sync_playwright() as p:
        b = p.chromium.launch(executable_path=CHROME)
        page = b.new_page(viewport={"width": 1300, "height": 800})
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"{base}/?token={insp.token}")
        page.wait_for_function("document.getElementById('shot').naturalWidth > 0")
        page.fill("#loc", "image:nothing_here")
        page.press("#loc", "Enter")
        page.wait_for_function("document.getElementById('out').textContent.includes('nothing_here')")
        assert "No image for 'nothing_here'" in page.text_content("#out")
        b.close()
    assert not errors, errors
