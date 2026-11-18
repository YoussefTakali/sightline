"""Linux 'headless local': a locally installed app on a private invisible display.
Needs Xvnc (TigerVNC) or Xvfb + x11vnc (run in docker/dev if your host lacks them)."""
import os
import shutil
import sys

import pytest
from conftest import needs_tesseract

from sightline import Session, Text, read_table_via_clipboard

APP = os.path.join(os.path.dirname(__file__), "..", "docker", "linux-target", "demo_app.py")
LINUX = sys.platform.startswith("linux")
SERVERS = [pytest.param("xvnc", marks=pytest.mark.skipif(not (LINUX and shutil.which("Xvnc")),
                                                          reason="needs Xvnc")),
           pytest.param("x11vnc", marks=pytest.mark.skipif(
               not (LINUX and shutil.which("Xvfb") and shutil.which("x11vnc")),
               reason="needs Xvfb + x11vnc"))]
pytestmark = needs_tesseract


def login(s):
    s.wait(Text("Connexion"), timeout=30)
    s.type("Jean", into=Text("Utilisateur :").area("right", 200, gap=6))
    s.type("secret", into=Text("Mot de passe :").area("right", 200, gap=6))
    s.click(Text("Valider"), expect=Text("Bienvenue Jean"), expect_timeout=10)


@pytest.mark.parametrize("server", SERVERS)
def test_local_app_on_private_display(server, tmp_path):
    from sightline.backends.display import PrivateDisplayBackend
    before = os.environ.get("DISPLAY")
    b = PrivateDisplayBackend([sys.executable, APP], size=(1280, 800), server=server)
    try:
        assert b.app_running() and b.server == server
        s = Session(b, timeout=10, trace_dir=str(tmp_path), park_mouse=(1275, 795))
        login(s)
        table = read_table_via_clipboard(s, Text("CMD-0001", exact=True).nth(0),
                                         columns=["Numéro", "Client", "Montant", "Statut"])
        assert len(table) == 61 and table[2][1] == "Martin"
    finally:
        b.close()
    assert os.environ.get("DISPLAY") == before            # the user's session untouched
    assert not os.path.exists(f"/tmp/.X11-unix/X{b.display}")   # display cleaned up


@pytest.mark.parametrize("server", SERVERS[:1])
def test_identical_copies_all_arrive(server):
    """x11vnc drops a copy identical to the previous one; Xvnc must not."""
    from sightline.backends.display import PrivateDisplayBackend
    b = PrivateDisplayBackend([sys.executable, APP], server=server)
    try:
        s = Session(b, timeout=10, trace="off", park_mouse=(1275, 795))
        login(s)
        s.click(Text("CMD-0003", exact=True).nth(0))
        copies = [s.copy("ctrl+c", 5) for _ in range(3)]
        assert copies[0] == copies[1] == copies[2] and "CMD-0003" in copies[0]
    finally:
        b.close()


@pytest.mark.parametrize("server", SERVERS[:1])
def test_two_private_displays_in_parallel(server):
    from sightline.backends.display import PrivateDisplayBackend
    a = PrivateDisplayBackend([sys.executable, APP], server=server)
    b = PrivateDisplayBackend([sys.executable, APP], server=server)
    try:
        assert a.display != b.display
        for backend in (a, b):
            Session(backend, timeout=30, trace="off").wait(Text("Connexion"))
    finally:
        a.close()
        b.close()


@pytest.mark.parametrize("server", SERVERS[:1])
def test_app_crash_is_reported_immediately(server):
    from sightline import SightlineError
    from sightline.backends.display import PrivateDisplayBackend
    b = PrivateDisplayBackend([sys.executable, "-c", "import sys; print('licence missing'); sys.exit(3)"],
                              server=server)
    try:
        import time
        time.sleep(1)
        with pytest.raises(SightlineError, match=r"exited \(code 3\)(.|\n)*licence missing"):
            Session(b, timeout=30, trace="off").wait(Text("Connexion"))
    finally:
        b.close()
