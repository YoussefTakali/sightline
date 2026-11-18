"""The remote app drawn in a HEADLESS browser (noVNC page here; Citrix Workspace for HTML5
in real life) and driven through it: no window on the desktop, the real mouse/keyboard/
clipboard untouched. SIGHTLINE_E2E_NOVNC=<vnc.html url> to use a running target."""
import os
import shutil
import subprocess
import time
import urllib.request

import pytest
from conftest import needs_tesseract
from test_e2e_headless import ENGINE, HERE, _free_port

from sightline import Session, Text, handle_dialogs, read_table_via_clipboard

pytestmark = [pytest.mark.e2e, needs_tesseract]
CHROME = next((p for p in (shutil.which(n) for n in
                           ("chromium-browser", "chromium", "google-chrome", "msedge")) if p), None)


@pytest.fixture(scope="module")
def novnc_url():
    env = os.environ.get("SIGHTLINE_E2E_NOVNC")
    if env:
        yield env
        return
    if not ENGINE:
        pytest.skip("no podman/docker and no SIGHTLINE_E2E_NOVNC")
    ctx = os.path.join(HERE, "..", "docker", "linux-target")
    subprocess.run([ENGINE, "build", "-q", "-t", "sightline-target", ctx], check=True,
                   capture_output=True)
    port, name = _free_port(), f"sightline-e2e-web-{os.getpid()}"
    subprocess.run([ENGINE, "run", "-d", "--rm", "-p", f"127.0.0.1:{port}:6080",
                    "-e", "VNC_PASSWORD=e2e-pw", "-e", "NOVNC_PORT=6080", "--name", name,
                    "sightline-target"], check=True, capture_output=True)
    try:
        deadline = time.time() + 30
        while True:
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/vnc.html", timeout=2)
                break
            except Exception:
                if time.time() > deadline:
                    raise
                time.sleep(0.5)
        yield (f"http://127.0.0.1:{port}/vnc.html?autoconnect=true&password=e2e-pw"
               "&resize=off&reconnect=true")
    finally:
        subprocess.run([ENGINE, "rm", "-f", name], capture_output=True)


@pytest.fixture(scope="module")
def s(novnc_url, tmp_path_factory):
    pytest.importorskip("playwright")
    from sightline.backends.browser import BrowserBackend
    b = BrowserBackend(novnc_url, headless=True, executable_path=CHROME, clipboard="novnc",
                       target_os="linux")
    sess = Session(b, timeout=10, park_mouse=(1275, 795),
                   trace_dir=str(tmp_path_factory.mktemp("trace-web")))
    sess.wait(Text("Connexion"), timeout=40)
    yield sess
    sess.close()
    print("trace report:", sess.report)


def field(label):
    return Text(label).area("right", 200, gap=6)


def test_1_login(s):
    s.type("Jean", into=field("Utilisateur :"))
    s.type("secret", into=field("Mot de passe :"))
    s.click(Text("Valider"), expect=Text("Bienvenue Jean"), expect_timeout=10)


def test_2_scroll_and_open(s):
    row = Text("CMD-0042", exact=True)
    s.scroll_until_visible(row, at=Text("Numéro").area("below", 300, gap=4))
    s.double_click(row, expect=Text("Commande CMD-0042 - Martin"), expect_timeout=5)
    s.click(Text("Fermer", exact=True), vanish=True)


def test_3_whole_grid_through_remote_clipboard(s):
    table = read_table_via_clipboard(s, Text("CMD-0042", exact=True).nth(0),
                                     columns=["Numéro", "Client", "Montant", "Statut"],
                                     must_contain=["CMD-0042"])
    assert len(table) == 61
    assert {r[0]: r for r in table[1:]}["CMD-0041"] == ["CMD-0041", "Lefèvre", "667,41", "Livré"]


def test_4_dialogs(s):
    for expected in (["Exact('OK')"], ["Exact('Oui')", "Exact('OK')"]):
        s.click(Text("Exporter", exact=True))
        got = handle_dialogs(s, required=[Text("OK", exact=True)],
                             optional=[Text("Oui", exact=True)], timeout=10, quiet=1.0)
        assert got == expected
