"""fetch_ica against a fake Citrix StoreFront: detection screen, app list, details pane,
.ica download - the file must be CAUGHT, not opened."""
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from test_e2e_browser import CHROME

pytest.importorskip("playwright")
pytestmark = pytest.mark.skipif(CHROME is None, reason="no chromium/chrome/edge")

ICA = "[ApplicationServers]\r\nPHILEAS=\r\n[PHILEAS]\r\nAddress=;40;STA;TICKET\r\nTWIMode=On\r\n"
PORTAL = """<html><body><div id=root>
<p>Welcome to Citrix Workspace</p><button id=detect>Detect Citrix Workspace app</button>
<button onclick="apps()">Already installed</button></div>
<script>
function apps() {
  document.getElementById('root').innerHTML =
    '<nav><a href="#" onclick="list()">Apps</a> <a href="#">Favorites</a></nav><p>No favorites yet</p>';
}
function list() {
  document.getElementById('root').innerHTML =
    '<div class=tile onclick="details()">GrDF - PHILEAS</div><div class=tile>Outlook</div>';
}
function details() {
  document.getElementById('root').innerHTML += '<div><h2>GrDF - PHILEAS</h2><button onclick="go()">Open</button></div>';
}
function go() { const f = document.createElement('iframe'); f.style.display='none';
  f.src = '/Citrix/StoreWeb/Resources/LaunchIca/phileas.ica'; document.body.appendChild(f); }
</script></body></html>"""
LOGIN = "<html><body><form>User <input name=u> Password <input type=password name=p></form></body></html>"


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.endswith(".ica"):
            body, ctype = ICA.encode(), "application/x-ica"
            extra = [("Content-Disposition", 'attachment; filename="launch.ica"')]
        else:
            body, ctype, extra = (LOGIN if "login" in self.path else PORTAL).encode(), "text/html", []
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        for k, v in extra:
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@pytest.fixture
def portal():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def test_fetch_ica_through_detection_tabs_and_details(portal, tmp_path):
    from sightline.citrix import fetch_ica, prepare_ica, read_ica
    ica = fetch_ica(portal + "/Citrix/StoreWeb/", "PHILEAS", str(tmp_path / "profile"),
                    executable=CHROME, out_dir=str(tmp_path), timeout=30,
                    screenshot=str(tmp_path / "fail.png"))
    assert read_ica(ica) == ICA.replace("\r", "").splitlines()
    assert "TWIMode=Off" in read_ica(prepare_ica(ica, out_dir=str(tmp_path)))


def test_fetch_ica_reports_login_needed(portal, tmp_path):
    from sightline.citrix import fetch_ica
    from sightline.errors import SightlineError
    with pytest.raises(SightlineError, match="browser-login"):
        fetch_ica(portal + "/login", "PHILEAS", str(tmp_path / "profile"), executable=CHROME,
                  timeout=15, screenshot=None)
