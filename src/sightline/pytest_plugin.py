"""sightline's test runner, on top of pytest (used through `sightline run`).

Python tests get an `app` fixture (a Session from the selected profile):

    def test_login(app):
        app.type("Jean", into=Text("Utilisateur :").area("right", 200))
        app.click(Text("Valider"), expect=Text("Bienvenue"))

YAML flows (`*.flow.yaml`) are collected as tests too. One session per test FILE by
default (GUI state flows from one test to the next, like a user would); after a failing
test the profile's recovery steps run so the next one starts clean.

Results: sightline-results/<run>/index.html (every test, status, time, failure
screenshot, link to its step-by-step trace), plus JUnit XML with --junitxml.
"""
from __future__ import annotations

import html
import os
import time
from pathlib import Path

import pytest

from .errors import SightlineError

_SESSIONS = pytest.StashKey[dict]()
_RESULTS = pytest.StashKey[list]()
_OUTDIR = pytest.StashKey[Path]()


def pytest_addoption(parser):
    g = parser.getgroup("sightline")
    g.addoption("--profile", dest="sl_profile", default=None,
                help="sightline profile (sightline.toml); default: SIGHTLINE_PROFILE or default_profile")
    g.addoption("--sl-config", dest="sl_config", default=None, help="path of sightline.toml")
    g.addoption("--sl-trace", dest="sl_trace", default=None,
                help="off | failures | actions (default: the profile's)")
    g.addoption("--sl-results", dest="sl_results", default="sightline-results",
                help="folder for the run report")
    parser.addini("sightline_scope", "session scope: function | module | session", default="module")


def pytest_configure(config):
    config.addinivalue_line("markers", "profile(name): run this test with another profile")
    config.stash[_SESSIONS] = {}
    config.stash[_RESULTS] = []
    out = Path(config.getoption("sl_results")) / time.strftime("run-%Y%m%d-%H%M%S")
    config.stash[_OUTDIR] = out


def _scope_key(item, scope: str):
    if scope == "session":
        return "session"
    if scope == "function":
        return item.nodeid
    return str(item.path)


def _open(config, item) -> object:
    from .session import Session
    marker = item.get_closest_marker("profile")
    profile = marker.args[0] if marker else (getattr(item, "flow_profile", None)
                                              or config.getoption("sl_profile"))
    scope = config.getini("sightline_scope")
    key = (_scope_key(item, scope), profile)
    sessions = config.stash[_SESSIONS]
    if key not in sessions:
        over = {"trace_dir": str(config.stash[_OUTDIR] / "traces")}
        if config.getoption("sl_trace"):
            over["trace"] = config.getoption("sl_trace")
        sessions[key] = Session.from_profile(profile, config.getoption("sl_config"), **over)
    return sessions[key]


def _close_finished(config, item, nextitem) -> None:
    """Close every session the NEXT test will not reuse (end of its file/test/run)."""
    scope = config.getini("sightline_scope")
    nxt = _scope_key(nextitem, scope) if nextitem is not None else None
    sessions = config.stash[_SESSIONS]
    for key in [k for k in sessions if k[0] != nxt]:
        s = sessions.pop(key)
        s.close()
        for r in config.stash[_RESULTS]:
            if r["key"] == key and not r.get("trace"):
                r["trace"] = s.report


@pytest.fixture
def app(request):
    """The Session of the current profile (shared per test file by default)."""
    return _open(request.config, request.node)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    rep = outcome.get_result()
    if not (isinstance(item, FlowItem) or "app" in getattr(item, "fixturenames", ())):
        return                                         # not a sightline test: stay out of it
    if rep.when != "call" and not (rep.when == "setup" and rep.failed):
        return
    sessions = item.config.stash[_SESSIONS]
    scope = item.config.getini("sightline_scope")
    key = next((k for k in sessions if k[0] == _scope_key(item, scope)), None)
    artifact = None
    if rep.failed and call.excinfo is not None:
        exc = call.excinfo.value
        artifact = getattr(exc, "artifact", None)
        while artifact is None and exc.__cause__ is not None:
            exc = exc.__cause__
            artifact = getattr(exc, "artifact", None)
        if artifact:
            rep.sections.append(("sightline", f"failure screenshot: {artifact}"))
        if key is not None:
            problems = sessions[key].recover()
            if problems:
                rep.sections.append(("sightline recovery", "; ".join(problems)))
    message = ""
    if rep.failed and call.excinfo is not None:
        message = (str(call.excinfo.value).splitlines() or [""])[0][:300]
    item.config.stash[_RESULTS].append({
        "nodeid": item.nodeid, "outcome": rep.outcome, "duration": rep.duration,
        "artifact": artifact, "key": key, "message": message})


def pytest_runtest_teardown(item, nextitem):
    _close_finished(item.config, item, nextitem)


def pytest_sessionfinish(session, exitstatus):
    config = session.config
    for s in config.stash[_SESSIONS].values():
        s.close()
    config.stash[_SESSIONS].clear()
    results = config.stash[_RESULTS]
    if not results:
        return
    out = config.stash[_OUTDIR]
    out.mkdir(parents=True, exist_ok=True)
    page = out / "index.html"
    page.write_text(_summary_html(results, out), encoding="utf-8")
    config._sightline_report = str(page)


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    page = getattr(config, "_sightline_report", None)
    if page:
        terminalreporter.write_sep("-", f"sightline report: {page}")


def _summary_html(results, out: Path) -> str:
    def rel(p):
        return os.path.relpath(p, out).replace(os.sep, "/") if p else None
    rows = []
    for r in results:
        shot = rel(r.get("artifact"))
        trace = rel(r.get("trace"))
        rows.append(
            f'<tr class="{r["outcome"]}"><td>{html.escape(r["nodeid"])}</td>'
            f'<td>{r["outcome"]}</td><td>{r["duration"]:.1f}s</td>'
            f'<td>{html.escape(r["message"])}'
            + (f'<br><a href="{shot}"><img src="{shot}"></a>' if shot else "") + "</td>"
            + (f'<td><a href="{trace}">steps</a></td>' if trace else "<td></td>") + "</tr>")
    passed = sum(r["outcome"] == "passed" for r in results)
    return f"""<!doctype html><meta charset="utf-8"><title>sightline results</title>
<style>
:root{{color-scheme:light dark;--bg:#fff;--fg:#1d1d1f;--line:#ddd;--bad:#fdecea;--ok:#e8f5e9}}
@media (prefers-color-scheme:dark){{:root{{--bg:#16181c;--fg:#e6e6e6;--line:#333;--bad:#3a1a1a;--ok:#16301b}}}}
body{{font:14px system-ui,sans-serif;background:var(--bg);color:var(--fg);margin:16px}}
table{{border-collapse:collapse;width:100%}} td{{border-top:1px solid var(--line);padding:6px;vertical-align:top}}
tr.failed td{{background:var(--bad)}} tr.passed td:nth-child(2){{background:var(--ok)}}
img{{max-width:480px;width:100%;margin-top:6px;border:1px solid var(--line)}}
</style><h1>sightline results</h1><p>{passed}/{len(results)} passed</p>
<table><tr><th>test</th><th>result</th><th>time</th><th>details</th><th>trace</th></tr>
{"".join(rows)}</table>"""


# ---------------------------------------------------------------- YAML flows
def pytest_collect_file(parent, file_path):
    if file_path.name.endswith((".flow.yaml", ".flow.yml")):
        return FlowFile.from_parent(parent, path=file_path)
    return None


class FlowFile(pytest.File):
    def collect(self):
        from .flows import load_flow
        flow = load_flow(self.path)
        for name in flow.tests:
            yield FlowItem.from_parent(self, name=name, flow=flow)


class FlowItem(pytest.Item):
    def __init__(self, *, flow, **kw):
        super().__init__(**kw)
        self.flow = flow
        self.flow_profile = flow.profile

    def runtest(self):
        from .flows import run_steps
        s = _open(self.config, self)
        if self.flow.recover and getattr(s, "_sl_flow_recover", None) != self.flow.path:
            s._sl_flow_recover = self.flow.path
            flow, base = self.flow, str(self.path.parent)
            s.on_failure(lambda sess: run_steps(sess, flow.recover, dict(flow.vars), base))
        run_steps(s, self.flow.tests[self.name], dict(self.flow.vars), str(self.path.parent))

    def repr_failure(self, excinfo):
        if isinstance(excinfo.value, SightlineError):
            return str(excinfo.value)
        return super().repr_failure(excinfo)

    def reportinfo(self):
        return self.path, 0, f"flow: {self.name}"
