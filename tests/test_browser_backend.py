import time

import pytest
from test_e2e_browser import CHROME

pytest.importorskip("playwright")
pytestmark = pytest.mark.skipif(CHROME is None, reason="no chromium/chrome/edge")


def test_follows_session_opened_in_new_tab():
    from sightline.backends.browser import BrowserBackend
    b = BrowserBackend(None, executable_path=CHROME, viewport=(320, 200), clipboard=None)
    try:
        b.page.set_content("<body style='background:#ffffff'>portal</body>")
        first = b.page
        # the portal launches the 'session' in a new tab, like Citrix StoreFront
        first.evaluate("""() => { const w = window.open('about:blank');
            w.document.write("<body style='margin:0;background:#123456'></body>"); }""")
        deadline = time.time() + 5
        while b.page is first and time.time() < deadline:
            first.wait_for_timeout(50)
        assert b.page is not first
        px = b.capture().pixels[100, 160]
        assert tuple(int(v) for v in px) == (0x56, 0x34, 0x12)          # BGR of #123456
        assert b.size() == (320, 200)
        assert "clipboard" not in b.capabilities
    finally:
        b.close()
