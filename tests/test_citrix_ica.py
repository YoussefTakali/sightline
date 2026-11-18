import os
import time

import pytest

from sightline.citrix import prepare_ica, read_ica, wait_for_ica
from sightline.errors import SightlineError

ICA = """[Encoding]\r
InputEncoding=UTF8\r
[WFClient]\r
Version=2\r
[ApplicationServers]\r
PHILEAS=\r
[PHILEAS]\r
Address=;40;STA01;ABCDEF\r
InitialProgram=#PHILEAS\r
TWIMode=On\r
DesiredHRES=1024\r
LogonTicket=SECRET\r
"""


def test_prepare_ica_makes_one_regular_window(tmp_path):
    src = tmp_path / "launch.ica"
    src.write_bytes(ICA.encode())
    out = prepare_ica(str(src), size=(1600, 900), out_dir=str(tmp_path))
    lines = read_ica(out)
    app = lines[lines.index("[PHILEAS]"):]
    assert "TWIMode=Off" in app and "DesiredHRES=1600" in app and "DesiredVRES=900" in app
    assert "LogonTicket=SECRET" in app and "Address=;40;STA01;ABCDEF" in app
    assert sum(ln.startswith("DesiredHRES") for ln in lines) == 1
    wf = lines[lines.index("[WFClient]"):lines.index("[ApplicationServers]")]
    assert not any(ln.startswith("TWIMode") for ln in wf), "global sections are left alone"
    assert src.read_bytes() == ICA.encode(), "the original is not modified"


def test_prepare_ica_rejects_other_files(tmp_path):
    p = tmp_path / "x.ica"
    p.write_text("hello")
    with pytest.raises(SightlineError):
        prepare_ica(str(p))


def test_wait_for_ica_takes_only_fresh_complete_downloads(tmp_path):
    old = tmp_path / "launch.ica"
    old.write_text(ICA)
    os.utime(old, (time.time() - 600, time.time() - 600))
    (tmp_path / "launch (1).ica.crdownload").write_text(ICA)
    with pytest.raises(SightlineError):
        wait_for_ica(str(tmp_path), timeout=1)
    new = tmp_path / "launch (1).ica"
    new.write_text(ICA)
    assert wait_for_ica(str(tmp_path), timeout=2) == str(new)
