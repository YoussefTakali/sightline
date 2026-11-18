"""The runner: YAML flows + Python tests through the sightline pytest plugin, offline
(replay profile on a synthetic screen)."""
import os
import textwrap

import cv2
from conftest import crop, make_screen


def _project(pytester):
    bgr, lay = make_screen()
    cv2.imwrite(str(pytester.path / "screen.png"), bgr)
    (pytester.path / "assets").mkdir()
    cv2.imwrite(str(pytester.path / "assets" / "valider.png"), crop(bgr, lay["valider"]))
    cv2.imwrite(str(pytester.path / "assets" / "green.png"), crop(bgr, lay["icon_green"]))
    pytester.makefile(".toml", sightline=textwrap.dedent("""
        default_profile = "replay"
        [defaults]
        assets = ["assets"]
        timeout = 0.5
        poll = 0.01
        settle = 0.0
        recovery = [{press = "esc"}]
        [profiles.replay]
        backend = "replay"
        [profiles.replay.connection]
        frames = ["screen.png"]
    """))
    return lay


def test_flows_and_python_tests(pytester):
    lay = _project(pytester)
    pytester.makefile(".flow.yaml", orders=textwrap.dedent("""
        vars: {button: "image:valider"}
        tests:
          clicks the button:
            - wait: ${button}
            - click: ${button}
            - if_visible: image:green
              then:
                - set: {seen: yes}
            - log: "green icon seen: ${seen}"
          fails on a missing image:
            - wait: "at:0,0,60,60 >> image:valider"
              timeout: 0
    """))
    pytester.makepyfile(test_py=textwrap.dedent(f"""
        from sightline import Image
        def test_a_failure(app):
            app.click(Image("valider"))
            assert False, "boom"
        def test_recovery_ran_before_next_test(app):
            keys = [e for e in app.backend.events if e[0] == "key_down"]
            assert ("key_down", "esc") in keys
            x, y, w, h = {lay["valider"]}
            assert (x + w // 2, y + h // 2, "left") in app.backend.clicks()
    """))
    r = pytester.runpytest("-p", "no:cacheprovider")
    r.assert_outcomes(passed=2, failed=2)
    out = r.stdout.str()
    assert "step 1 [wait: at:0,0,60,60 >> image:valider]" in out
    assert "sightline report:" in out
    page = [l for l in out.splitlines() if "sightline report:" in l][0].split("report:")[1].strip(" -")
    with open(os.path.join(pytester.path, page), encoding="utf-8") as f:
        html = f.read()
    assert "2/4 passed" in html and "failed" in html and "<img" in html


def test_init_scaffold(tmp_path, capsys):
    from sightline.cli import main
    assert main(["init", str(tmp_path / "proj"), "--title", "Gestion"]) == 0
    for f in ("sightline.toml", "flows/smoke.flow.yaml", "tests/test_smoke.py", ".gitignore"):
        assert (tmp_path / "proj" / f).exists()
    from sightline.config import load_profile
    name, cfg, _ = load_profile("background", str(tmp_path / "proj" / "sightline.toml"))
    assert cfg["connection"]["title"] == "Gestion" and cfg["recovery"] == [{"press": "esc"},
                                                                           {"press": "esc"}]
    from sightline.flows import load_flow
    assert load_flow(tmp_path / "proj" / "flows" / "smoke.flow.yaml").tests
