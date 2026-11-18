"""End to end against the headless Linux target (docker/linux-target) over VNC.

Runs when podman or docker is available (the container is started/stopped here), or
against an already running target: SIGHTLINE_E2E_VNC=host:port[:password].
"""
import os
import shutil
import socket
import subprocess
import time

import pytest
from conftest import needs_tesseract

from sightline import Rect, Session, Text, handle_dialogs, read_table_via_clipboard
from sightline.backends.vnc import VNCBackend
from sightline.locators import Image
from sightline.vision.match import Template

pytestmark = [pytest.mark.e2e, needs_tesseract]
ENGINE = shutil.which("podman") or shutil.which("docker")
HERE = os.path.dirname(__file__)


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def target():
    env = os.environ.get("SIGHTLINE_E2E_VNC")
    if env:
        host, port, *pw = env.split(":")
        yield host, int(port), (pw[0] if pw else None)
        return
    if not ENGINE:
        pytest.skip("no podman/docker and no SIGHTLINE_E2E_VNC")
    ctx = os.path.join(HERE, "..", "docker", "linux-target")
    subprocess.run([ENGINE, "build", "-q", "-t", "sightline-target", ctx], check=True,
                   capture_output=True)
    port, name = _free_port(), f"sightline-e2e-{os.getpid()}"
    subprocess.run([ENGINE, "run", "-d", "--rm", "-p", f"127.0.0.1:{port}:5900", "-e",
                    "VNC_PASSWORD=e2e-pw", "--name", name, "sightline-target"], check=True,
                   capture_output=True)
    try:
        deadline = time.time() + 30
        while True:
            try:
                VNCBackend("127.0.0.1", port, "e2e-pw").close()
                break
            except Exception:
                if time.time() > deadline:
                    raise
                time.sleep(0.5)
        yield "127.0.0.1", port, "e2e-pw"
    finally:
        subprocess.run([ENGINE, "rm", "-f", name], capture_output=True)


@pytest.fixture(scope="module")
def s(target, tmp_path_factory):
    host, port, pw = target
    sess = Session(VNCBackend(host, port, pw, target_os="linux"), timeout=10,
                   trace="actions", trace_dir=str(tmp_path_factory.mktemp("trace")),
                   park_mouse=(1275, 795))           # empty desktop corner: no hover effects
    sess.wait(Text("Connexion"), timeout=30)                       # app started
    yield sess
    sess.close()
    print("trace report:", sess.report)


def field(label):
    return Text(label).area("right", 200, gap=6)


def test_1_login_with_retry_on_wrong_password(s):
    s.type("Jean", into=field("Utilisateur :"))
    s.type("faux", into=field("Mot de passe :"))
    s.click(Text("Valider"), expect=Text("Mot de passe incorrect"), expect_timeout=5)
    s.type("secret", into=field("Mot de passe :"), clear=True)
    # the main screen appears 600 ms later: expect= waits for it, no sleep anywhere
    s.click(Text("Valider"), expect=Text("Bienvenue Jean"), expect_timeout=10)


def test_2_checkbox_state_by_its_own_look(s):
    label = s.wait(Text("Actif", exact=True))
    box = Rect(label.rect.x - 26, label.rect.y - 4, 24, label.rect.h + 8)  # indicator, left of label
    before = s.capture()
    on = Template.from_array(before.crop(box).pixels, "on")
    s.click(box.center)
    s.wait_for_change(before, box, timeout=5)
    after = s.wait_until_stable(box.expand(4), quiet=0.3)     # redraw finished
    off = Template.from_array(after.crop(box).pixels, "off")
    states = {"checked": Image(on, color=True, similarity=0.95),
              "unchecked": Image(off, color=True, similarity=0.95)}
    assert s.which(states, region=box.expand(4))[0] == "unchecked"
    s.click(box.center)
    s.wait_until_stable(box.expand(4), quiet=0.3)
    assert s.which(states, region=box.expand(4))[0] == "checked"


def test_3_scroll_grid_until_row_visible_then_open_it(s):
    grid = Text("Numéro").area("below", 350, gap=4)
    row = Text("CMD-0042", exact=True)
    m = s.scroll_until_visible(row, at=grid)
    assert m.text == "CMD-0042"
    s.double_click(row, expect=Text("Commande CMD-0042 - Martin"), expect_timeout=5)
    s.click(Text("Fermer", exact=True), vanish=True)


def test_4_read_whole_grid_exactly_via_remote_clipboard(s):
    # "CMD-0042" is now ALSO in the status bar ("Commande CMD-0042 sélectionnée"): strict
    # mode refuses to guess, so say which one - the first in reading order is the grid cell
    # (the status bar is below the grid).
    cell = Text("CMD-0042", exact=True).nth(0)
    table = read_table_via_clipboard(s, cell,
                                     columns=["Numéro", "Client", "Montant", "Statut"],
                                     must_contain=["CMD-0042"])
    assert len(table) == 61                                       # header + 60, not just visible
    rows = {r[0]: r for r in table[1:]}
    assert rows["CMD-0042"] == ["CMD-0042", "Martin", "704,42", "Reçu"]
    assert rows["CMD-0041"] == ["CMD-0041", "Lefèvre", "667,41", "Livré"]   # accents intact


def test_5_dialogs_in_variable_order(s):
    s.click(Text("Exporter", exact=True))
    first = handle_dialogs(s, required=[Text("OK", exact=True)], optional=[Text("Oui", exact=True)],
                           timeout=10, quiet=1.0)
    assert first == ["Exact('OK')"]
    s.click(Text("Exporter", exact=True))                          # 2nd time: 'Remplacer ?' first
    second = handle_dialogs(s, required=[Text("OK", exact=True)], optional=[Text("Oui", exact=True)],
                            timeout=10, quiet=1.0)
    assert second == ["Exact('Oui')", "Exact('OK')"]


def test_6_trace_report_written(s):
    shot = s.capture()
    assert shot.width == 1280
    assert s.tracer.dir and any(p.suffix == ".jpg" for p in s.tracer.dir.iterdir())


def test_7_yaml_flow_through_sightline_run(target, tmp_path):
    """The same app driven by a YAML flow through the CLI runner (separate process)."""
    import sys
    host, port, pw = target
    root = os.path.join(HERE, "..")
    env = dict(os.environ, SIGHTLINE_VNC_PORT=str(port), SIGHTLINE_VNC_PASSWORD=pw or "",
               DEMO_PASSWORD="secret")
    r = subprocess.run([sys.executable, "-m", "sightline.cli", "run", "examples/flows",
                        "--config", "examples/sightline.toml", "--profile", "headless",
                        "--", "--sl-results", str(tmp_path)],
                       cwd=root, env=env, capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-2000:]
    assert "3 passed" in r.stdout
    with open(os.path.join(root, "examples", "flows", "results", "orders.csv"),
              encoding="utf-8-sig") as f:
        csv_text = f.read()
    assert "CMD-0041;Lefèvre;667,41;Livré" in csv_text


def test_8_ocr_grid_reader_matches_the_clipboard(s):
    """The OCR grid reader (for grids without copy-paste) against the exact clipboard copy:
    every row found across scrolled pages, every cell compared."""
    from sightline.components import read_grid
    from sightline.vision.ocr import fold
    cols = ["Numéro", "Client", "Montant", "Statut"]
    s.scroll_until_visible(Text("CMD-0001", exact=True), at=Text("Numéro").area("below", 300),
                           direction="up")
    exact = read_table_via_clipboard(s, Text("CMD-0001", exact=True).nth(0), columns=cols,
                                     must_contain=["CMD-0001"])[1:]
    got = read_grid(s, cols, key_column="Numéro")
    assert got.pages > 1                                         # it had to scroll
    by_key = {fold(r[0]): r for r in got.rows}
    missing = [r[0] for r in exact if fold(r[0]) not in by_key]
    assert not missing, f"rows not read: {missing}"
    cells = [(e, by_key[fold(e[0])]) for e in exact]
    wrong = [(a, b) for e, g in cells for a, b in zip(e, g) if fold(a) != fold(b)]
    accuracy = 1 - len(wrong) / (len(exact) * len(cols))
    print(f"OCR grid: {len(got.rows)} rows, {got.pages} pages, accuracy {accuracy:.3%}, wrong: {wrong[:5]}")
    assert len(got.rows) == 60 and accuracy >= 0.98


def test_9_menu_path_is_not_confused_with_the_button(s):
    """'Exporter' is a menu item AND a button: menu() must click the menu one."""
    from sightline.actions import menu
    menu(s, "Fichier > Exporter > CSV")
    s.wait(Text("Export CSV lancé"), timeout=5)
    s.click(Text("OK", exact=True), vanish=True)


def test_10_select_in_a_drop_down(s):
    from sightline.actions import select
    select(s, Text("Pays :").area("right", 150, gap=4), "France")
    s.wait(Text("Pays choisi : France"), timeout=5)
    select(s, Text("Pays :").area("right", 150, gap=4), "Tunisie")
    s.wait(Text("Pays choisi : Tunisie"), timeout=5)
