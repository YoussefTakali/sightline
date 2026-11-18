import cv2
import numpy as np
import pytest
from conftest import crop, make_screen

from sightline import Ambiguous, BadAsset, Image, NotFound, Rect, parse_locator
from sightline.backends.base import parse_combo
from sightline.locators import At, Fallback, Nth, Within
from sightline.vision.match import Template, match_template


# ------------------------------------------------------------------ geometry / keys
def test_rect_ops():
    a, b = Rect(0, 0, 10, 10), Rect(5, 5, 10, 10)
    assert a.intersect(b) == Rect(5, 5, 5, 5)
    assert a.union(b) == Rect(0, 0, 15, 15)
    assert abs(a.iou(b) - 25 / 175) < 1e-9
    assert Rect(0, 0, 10, 10).vertical_overlap(Rect(50, 5, 10, 10)) == 0.5


def test_key_combos():
    assert parse_combo("ctrl+shift+s") == ["ctrl", "shift", "s"]
    assert parse_combo("mod+c", "macos") == ["cmd", "c"]
    assert parse_combo("mod+c", "windows") == ["ctrl", "c"]
    assert parse_combo("ctrl++") == ["ctrl", "+"]
    assert parse_combo("Control+Return") == ["ctrl", "enter"]
    with pytest.raises(ValueError):
        parse_combo("ctrl+bogus")


# ------------------------------------------------------------------ matching
def test_finds_exact_crop(screen):
    bgr, lay = screen
    t = Template.from_array(crop(bgr, lay["valider"]))
    ms, best = match_template(bgr, cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY), t, 0.9, [1.0])
    assert len(ms) == 1 and ms[0].rect == Rect(*lay["valider"])
    assert best.score > 0.99


def test_all_occurrences_not_just_first(screen):
    bgr, lay = screen
    t = Template.from_array(crop(bgr, lay["icon0"]))
    ms, _ = match_template(bgr, cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY), t, 0.9, [1.0])
    xs = sorted(m.rect.x for m in ms)
    # 3 blue icons + the green one has the same SHAPE: grey matching cannot tell colours
    assert xs[:3] == [20, 60, 100]


def test_color_gate_separates_same_shape_different_colour(screen):
    bgr, lay = screen
    t = Template.from_array(crop(bgr, lay["icon0"]))
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    ms, _ = match_template(bgr, gray, t, 0.9, [1.0], color_threshold=0.92)
    assert sorted(m.rect.x for m in ms) == [20, 60, 100]  # green one rejected


def test_flat_crop_rejected():
    with pytest.raises(BadAsset):
        Template.from_array(np.full((20, 40, 3), 200, np.uint8))


def test_transparent_mask(tmp_path, screen):
    bgr, lay = screen
    # icon crop with a big transparent margin of a DIFFERENT colour than the screen
    icon = crop(bgr, lay["valider"], pad=6)
    rgba = cv2.cvtColor(icon, cv2.COLOR_BGR2BGRA)
    rgba[:6, :, :3] = (0, 255, 255)
    rgba[:6, :, 3] = 0
    rgba[-6:, :, :3] = (255, 0, 255)
    rgba[-6:, :, 3] = 0
    p = tmp_path / "btn.png"
    cv2.imwrite(str(p), rgba)
    t = Template.load(p)
    assert t.mask is not None
    ms, _ = match_template(bgr, cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY), t, 0.9, [1.0])
    assert ms and abs(ms[0].rect.x - (lay["valider"][0] - 6)) <= 1


# ------------------------------------------------------------------ session + locators
def test_strict_ambiguity_then_nth(session_for, screen):
    bgr, lay = screen
    s = session_for(bgr)
    icon = Image(Template.from_array(crop(bgr, lay["icon0"])), color=True)
    with pytest.raises(Ambiguous) as e:
        s.wait(icon)
    assert "3 matches" in str(e.value)
    m = s.wait(icon.nth(1))
    assert m.rect.x == 60
    assert s.wait(icon.nth(-1)).rect.x == 100
    assert s.count(icon) == 3


def test_click_lands_on_center(session_for, screen):
    bgr, lay = screen
    s = session_for(bgr)
    s.click(Image(Template.from_array(crop(bgr, lay["valider"]))))
    x, y, w, h = lay["valider"]
    assert s.backend.clicks() == [(x + w // 2, y + h // 2, "left")]


def test_not_found_reports_best_candidate(session_for, screen):
    bgr, lay = screen
    s = session_for(bgr)
    other, _ = make_screen()
    patch = crop(other, lay["valider"]).copy()
    cv2.putText(patch, "X", (30, 25), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 0, 255), 2)
    with pytest.raises(NotFound) as e:
        s.wait(
            Image(Template.from_array(patch), similarity=0.97, scale_search=False),
            timeout=0,
        )
    assert "best 0." in str(e.value) and "< similarity 0.97" in str(e.value)


def test_scale_fallback_finds_scaled_session(session_for):
    small, lay = make_screen()
    # a remote session displayed scaled (Citrix/RDP/VNC viewer zoom): pure bitmap scaling.
    # (A real 125% DPI screen RE-RENDERS fonts: that needs an @1.25x asset variant.)
    big = cv2.resize(small, None, fx=1.25, fy=1.25, interpolation=cv2.INTER_LINEAR)
    s = session_for(big)
    t = Template.from_array(crop(small, lay["valider"]))
    m = s.wait(Image(t))  # found through the scale ladder
    assert abs(m.rect.x - int(150 * 1.25)) <= 3
    with pytest.raises(NotFound):
        s.wait(Image(t, scale_search=False), timeout=0)
    s2 = session_for(big, screen_scale=1.25)  # declared DPI: found at first try
    assert s2.wait(Image(t, scale_search=False))


def test_relative_and_within(session_for, screen):
    bgr, lay = screen
    s = session_for(bgr)
    cb_on = Template.from_array(crop(bgr, lay["cb_0"]))
    anchor_green = Image(Template.from_array(crop(bgr, lay["icon_green"])), color=True)
    icon = Image(Template.from_array(crop(bgr, lay["icon0"])), color=True)
    # nearest blue icon LEFT of the green one is the 3rd
    assert s.wait(icon.left_of(anchor_green)).rect.x == 100
    # within a fixed region
    assert s.wait(icon.within(Rect(50, 30, 40, 40))).rect.x == 60
    # checkbox below another checkbox's row: the unchecked one is found by shape
    assert s.wait(Image(cb_on)).rect.y == lay["cb_0"][1]


def test_fallback_uses_second(session_for, screen):
    bgr, lay = screen
    s = session_for(bgr)
    missing = np.zeros((20, 20, 3), np.uint8)
    cv2.circle(missing, (10, 10), 6, (0, 0, 255), -1)
    loc = Image(Template.from_array(missing), scale_search=False) | Image(
        Template.from_array(crop(bgr, lay["valider"]))
    )
    assert s.wait(loc).rect.x == lay["valider"][0]


def test_which_state(session_for, screen):
    bgr, lay = screen
    s = session_for(bgr)
    on = Template.from_array(crop(bgr, lay["cb_0"]))
    off = Template.from_array(crop(bgr, lay["cb_1"]))
    region = Rect(130, 185, 40, 25)  # row of "Actif"
    name, m = s.which(
        {"checked": Image(on, color=True), "unchecked": Image(off, color=True)},
        region=region,
    )
    assert name == "checked"
    name, _ = s.which(
        {"checked": Image(on, color=True), "unchecked": Image(off, color=True)},
        region=Rect(130, 215, 40, 25),
    )
    assert name == "unchecked"


def test_spec_parser():
    loc = parse_locator("image:a/b;similarity=0.8 || text:OK")
    assert isinstance(loc, Fallback) and loc.options[0].similarity == 0.8
    loc = parse_locator("image:dialog >> exact:OK[2]")
    assert (
        isinstance(loc, Within)
        and isinstance(loc.target, Nth)
        and loc.target.index == 1
    )
    assert isinstance(parse_locator("at:1,2,3,4"), At)
    assert parse_locator("save_btn.png").names == ("save_btn.png",)
    assert parse_locator(r"regex:^Total\s+\d+").query.pattern == r"^Total\s+\d+"


def test_asset_variants(tmp_path, screen):
    bgr, lay = screen
    from sightline.assets import AssetStore

    (tmp_path / "windows" / "toolbar").mkdir(parents=True)
    (tmp_path / "toolbar").mkdir()
    cv2.imwrite(str(tmp_path / "toolbar" / "save.png"), crop(bgr, lay["icon0"]))
    cv2.imwrite(str(tmp_path / "toolbar" / "save~old.png"), crop(bgr, lay["valider"]))
    cv2.imwrite(
        str(tmp_path / "toolbar" / "save@2x.png"),
        cv2.resize(crop(bgr, lay["icon0"]), None, fx=2, fy=2),
    )
    cv2.imwrite(
        str(tmp_path / "windows" / "toolbar" / "save.png"), crop(bgr, lay["icon_green"])
    )
    cv2.imwrite(str(tmp_path / "toolbar" / "saveas.png"), crop(bgr, lay["icon0"]))
    st = AssetStore([tmp_path], "windows")
    from pathlib import Path
    names = [Path(t.path).relative_to(tmp_path).parts for t in st.resolve("toolbar/save")]
    assert names[0][0] == "windows"  # most specific first (path parts: any OS separator)
    assert len(names) == 4  # saveas.png is not a variant
    assert {t.scale for t in st.resolve("toolbar/save")} == {1.0, 2.0}
    with pytest.raises(BadAsset):
        st.resolve("toolbar/nothing")


def test_fuzzy_text_is_word_aware():
    from sightline.vision.ocr import Word, phrase_similarity, search_text

    # a different short word must not pass because the rest of the line is identical
    assert (
        phrase_similarity("commande cmd-0042 - martin", "commande cmd-0042 - lefevre")
        < 0.6
    )
    # OCR typos inside words still match
    assert phrase_similarity("valder", "valider") > 0.8
    assert phrase_similarity("mot de pase", "mot de passe") > 0.8
    # glued punctuation ("Email:" for "Email :") still matches
    assert phrase_similarity("email:", "email :") > 0.8
    words = [
        Word(t, Rect(10 + 60 * i, 5, 50, 12), 0.9)
        for i, t in enumerate(["Commande", "CMD-0042", "-", "Martin"])
    ]
    assert search_text(words, "Commande CMD-0042 - Lefèvre") == []
    assert search_text(words, "Commande CMD-0042 - Martin")


def test_grid_cell_cleanup_keeps_data_symbols():
    from sightline.components.grid import _clean_cell
    assert _clean_cell("——— 87,01") == "87,01"
    assert _clean_cell("124,02 _") == "124,02"
    assert _clean_cell("| CMD-0001 |") == "CMD-0001"
    assert _clean_cell("-15,00") == "-15,00"
    assert _clean_cell("A / B") == "A / B"


def test_grid_second_look_rereads_a_cell_the_column_pass_missed():
    """The column-wide OCR pass loses one row: that cell shows ink, so it is re-read."""
    from conftest import font, to_bgr
    from PIL import Image as PILImage
    from PIL import ImageDraw

    from sightline import Session
    from sightline.backends.replay import ReplayBackend
    from sightline.components.grid import GridGeometry, _read_page
    from sightline.vision.ocr import Word

    img = PILImage.new("RGB", (300, 120), (255, 255, 255))
    d = ImageDraw.Draw(img)
    for i, y in enumerate((10, 40, 70)):
        d.text((10, y), f"ROW-{i + 1}", font=font(14), fill=(0, 0, 0))
    frame_bgr = to_bgr(img)

    class LossyOcr:                             # column pass forgets the middle row
        def words(self, bgr):
            return [Word("ROW-1", Rect(7, 10, 60, 14), 0.9), Word("ROW-3", Rect(7, 70, 60, 14), 0.9)]

        def line(self, bgr, cell=True):
            return "ROW-2", 0.9

    s = Session(ReplayBackend([frame_bgr]), trace="off", ocr=LossyOcr())
    geo = GridGeometry(header=Rect(0, 0, 300, 5), columns=[(0, 200)], body=Rect(0, 5, 300, 110))
    rows = _read_page(s, s.capture(), geo, 0)
    assert [r[0] for r in rows] == ["ROW-1", "ROW-2", "ROW-3"]
