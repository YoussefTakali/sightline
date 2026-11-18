"""The Citrix-like lab (tools/citrix_lab.sh, Apache Guacamole): web portal login -> the
published app opens INSIDE the browser (canvas + websocket, like Citrix Workspace for
HTML5) -> the business app is driven through it, headless, by pixels only.

Started here with podman/docker, or point SIGHTLINE_LAB_URL at a running portal."""
import os
import shutil
import subprocess
import sys

import pytest
from conftest import needs_tesseract
from test_e2e_browser import CHROME

from sightline import Session, Text, handle_dialogs, read_table_via_clipboard

pytestmark = [pytest.mark.e2e, needs_tesseract]
ROOT = os.path.join(os.path.dirname(__file__), "..")
PORT = int(os.environ.get("LAB_PORT", "18080"))


@pytest.fixture(scope="module")
def portal():
    url = os.environ.get("SIGHTLINE_LAB_URL")
    if url:
        yield url
        return
    if not (shutil.which("podman") or shutil.which("docker")):
        pytest.skip("no podman/docker and no SIGHTLINE_LAB_URL")
    if CHROME is None:
        pytest.skip("no chromium/chrome")
    up = subprocess.run([os.path.join(ROOT, "tools", "citrix_lab.sh"), "up"], capture_output=True,
                        text=True, timeout=600, env=dict(os.environ, LAB_PORT=str(PORT)))
    assert up.returncode == 0, up.stdout + up.stderr
    try:
        yield f"http://127.0.0.1:{PORT}/guacamole/"
    finally:
        subprocess.run([os.path.join(ROOT, "tools", "citrix_lab.sh"), "down"], capture_output=True)


@pytest.fixture(scope="module")
def s(portal, tmp_path_factory):
    pytest.importorskip("playwright")
    from sightline.backends.browser import BrowserBackend
    b = BrowserBackend(portal, executable_path=CHROME, viewport=(1280, 800), target_os="linux",
                       clipboard="navigator")
    sess = Session(b, timeout=15, park_mouse=(1275, 795),
                   trace_dir=str(tmp_path_factory.mktemp("trace-lab")))
    yield sess
    sess.close()
    print("trace report:", sess.report)


def field(label):
    return Text(label).area("right", 200, gap=6)


def test_1_portal_login_opens_the_published_app(s):
    s.click(Text("Password").offset(0, -46))                 # the username field above it
    s.type("jean")
    s.click(Text("Password"))
    s.type("secret", secret=True)
    s.click(Text("Login"), expect=Text("Connexion"), expect_timeout=60)
    assert "#/client/" in s.backend.page.url                  # we are inside the session


def test_2_log_into_the_app_inside_the_session(s):
    s.type("Jean", into=field("Utilisateur :"))
    s.type("faux", into=field("Mot de passe :"))
    s.click(Text("Valider"), expect=Text("Mot de passe incorrect"), expect_timeout=10)
    s.type("secret", into=field("Mot de passe :"), clear=True)
    s.click(Text("Valider"), expect=Text("Bienvenue Jean"), expect_timeout=15)


def test_3_scroll_the_grid_and_open_a_row(s):
    row = Text("CMD-0042", exact=True)
    s.scroll_until_visible(row, at=Text("Numéro").area("below", 300, gap=4))
    s.double_click(row, expect=Text("Commande CMD-0042 - Martin"), expect_timeout=10)
    s.click(Text("Fermer", exact=True), vanish=True)


def test_4_whole_grid_through_the_remote_clipboard(s):
    table = read_table_via_clipboard(s, Text("CMD-0042", exact=True).nth(0),
                                     columns=["Numéro", "Client", "Montant", "Statut"],
                                     must_contain=["CMD-0042"])
    assert len(table) == 61
    rows = {r[0]: r for r in table[1:]}
    assert rows["CMD-0041"] == ["CMD-0041", "Lefèvre", "667,41", "Livré"]


def test_5_ocr_grid_through_a_compressed_remote_picture(s):
    """No clipboard: OCR the grid through the gateway's (lossy) picture encoding."""
    from sightline.components import read_grid
    from sightline.vision.ocr import fold
    cols = ["Numéro", "Client", "Montant", "Statut"]
    s.scroll_until_visible(Text("CMD-0001", exact=True), at=Text("Numéro").area("below", 300),
                           direction="up")
    exact = read_table_via_clipboard(s, Text("CMD-0001", exact=True).nth(0), columns=cols)[1:]
    got = read_grid(s, cols, key_column="Numéro")
    by_key = {fold(r[0]): r for r in got.rows}
    wrong = [(a, b) for e in exact for a, b in zip(e, by_key.get(fold(e[0]), [""] * 4))
             if fold(a) != fold(b)]
    accuracy = 1 - len(wrong) / (len(exact) * len(cols))
    print(f"OCR through the gateway: {len(got.rows)} rows, {got.pages} pages, "
          f"accuracy {accuracy:.2%}, wrong: {wrong[:5]}")
    assert len(got.rows) == 60 and accuracy >= 0.98


def test_6_menu_dropdown_and_dialogs(s):
    from sightline.actions import menu, select
    menu(s, "Fichier > Exporter > CSV")
    s.wait(Text("Export CSV lancé"), timeout=10)
    s.click(Text("OK", exact=True), vanish=True)
    select(s, Text("Pays :").area("right", 150, gap=4), "Suisse")
    s.wait(Text("Pays choisi : Suisse"), timeout=10)
    for expected in (["Exact('OK')"], ["Exact('Oui')", "Exact('OK')"]):
        s.click(Text("Exporter", exact=True))
        got = handle_dialogs(s, required=[Text("OK", exact=True)],
                             optional=[Text("Oui", exact=True)], timeout=15, quiet=1.5)
        assert got == expected


def test_7_yaml_flow_with_sightline_run(portal, s, tmp_path):
    """A non-programmer's flow: portal login + app, through the CLI runner.
    The session above is closed first: a remote desktop serves ONE client at a time, two
    browsers on the same user's session keep taking it from each other (= Citrix roaming)."""
    s.close()
    env = dict(os.environ, SIGHTLINE_LAB_URL=portal, SIGHTLINE_BROWSER=CHROME or "",
               DEMO_PASSWORD="secret", LAB_PASSWORD="secret")
    r = subprocess.run([sys.executable, "-m", "sightline.cli", "run", "examples/flows-citrix",
                        "--config", "examples/sightline.toml", "--profile", "citrix-lab",
                        "--", "--sl-results", str(tmp_path)],
                       cwd=ROOT, env=env, capture_output=True, text=True, timeout=600)
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-2000:]
    assert "2 passed" in r.stdout
