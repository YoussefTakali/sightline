"""Developer tools.

  sightline shot  out.png              [--profile P]       capture the target screen
  sightline find  "text:Valider"       [--profile P | --image shot.png] [--out found.png]
  sightline ocr                        [--profile P | --image shot.png] [--region x,y,w,h]
  sightline crop  shot.png x y w h assets/toolbar/save.png   cut an asset from a capture
  sightline vnc-check host[:port]      [--password-env VAR]  diagnose a VNC target
  sightline browser-login URL --user-data-dir DIR [--channel msedge]   one-time SSO/MFA login
  sightline desktop open|show|close NAME [--portal URL]   visit a hidden desktop, open apps there
  sightline init [dir] --title "My App"              create a project (profiles, flow, test)
  sightline inspect [--profile P | --image shot.png] live view, locator tester, asset cropper
  sightline run [paths] --profile P [-k expr] [--junit out.xml]   run tests + YAML flows
  sightline windows                                  list windows (Windows)
  sightline doctor --window "App" [--click x,y]      what can run headless here, for this app

`find` is the main debugging loop: capture once, then try locators against the same
picture until it finds what you want, see the boxes, then put the locator in the test.
"""
from __future__ import annotations

import argparse
import os
import sys
import time

import cv2

from .errors import SightlineError


def _session(args):
    from .session import Session
    if getattr(args, "image", None):
        from .backends.replay import ReplayBackend
        return Session(ReplayBackend([args.image]), assets=args.assets or [], trace="off",
                       timeout=0, settle=0, ocr_lang=args.lang or "eng")
    over = {"trace": "off"}
    if args.assets:
        over["assets"] = args.assets
    if args.lang:
        over["ocr_lang"] = args.lang
    return Session.from_profile(args.profile, args.config, **over)


def _rect(s: str | None):
    from .geometry import Rect
    return Rect(*[int(v) for v in s.split(",")]) if s else None


def cmd_shot(args):
    with _session(args) as s:
        t = time.perf_counter()
        f = s.capture()
        f.save(args.out)
        print(f"{args.out}: {f.width}x{f.height} in {1000 * (time.perf_counter() - t):.0f} ms")


def cmd_find(args):
    from .spec import parse_locator
    from .trace import annotate
    with _session(args) as s:
        loc = parse_locator(args.locator)
        frame = s.capture()
        from .locators import FindContext
        ctx = FindContext(frame, s, _rect(args.region))
        t = time.perf_counter()
        ms = sorted(loc.find(ctx), key=lambda m: -m.score)
        dt = 1000 * (time.perf_counter() - t)
        print(f"{loc.describe()}: {len(ms)} match(es) in {dt:.0f} ms")
        for i, m in enumerate(ms, 1):
            print(f"  {i}. {m}")
        for d in ctx.diag:
            print(f"  - {d}")
        if len(ms) > 1:
            try:
                s.pick(ms, loc)
            except SightlineError as e:
                print("  ! a click on it would FAIL (ambiguous). " + str(e).splitlines()[-1])
        out = args.out or "sightline-find.png"
        cv2.imwrite(out, annotate(frame, ms[0].rect if ms else None, [m.rect for m in ms[1:]],
                                  None, loc.describe(), ctx.near[0] if ctx.near and not ms else None))
        print(f"annotated: {out}")
        return 0 if ms else 1


def cmd_ocr(args):
    with _session(args) as s:
        t = time.perf_counter()
        phrases = s.read_words(_rect(args.region))
        print(f"{len(phrases)} phrase(s) in {1000 * (time.perf_counter() - t):.0f} ms")
        for text, r in phrases:
            print(f"  {str(r):>24}  {text}")


def cmd_crop(args):
    img = cv2.imread(args.image, cv2.IMREAD_UNCHANGED)
    if img is None:
        raise SightlineError(f"cannot read {args.image}")
    x, y, w, h = args.x, args.y, args.w, args.h
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    piece = img[y:y + h, x:x + w]
    from .vision.match import Template
    Template.from_array(piece[:, :, :3] if piece.ndim == 3 else piece, args.out)  # validates
    cv2.imwrite(args.out, piece)
    print(f"{args.out}: {w}x{h}")


def cmd_vnc_check(args):
    from .backends.vnc import VNCBackend
    host, _, port = args.target.partition(":")
    pw = os.environ.get(args.password_env) if args.password_env else None
    t = time.perf_counter()
    b = VNCBackend(host, int(port or 5900), pw)
    t1 = time.perf_counter()
    w, h = b.size()
    caps = []
    for _ in range(5):
        a = time.perf_counter()
        b.capture()
        caps.append(1000 * (time.perf_counter() - a))
    print(f"connected to '{b.desktop_name}' {w}x{h} in {1000 * (t1 - t):.0f} ms; "
          f"capture {min(caps):.0f}-{max(caps):.0f} ms")
    if args.out:
        b.capture().save(args.out)
        print(f"screenshot: {args.out}")
    b.close()


def cmd_browser_login(args):
    """Visible browser on a persistent profile: log in (SSO/MFA) yourself once; headless
    runs with the same --user-data-dir reuse the session cookies."""
    from playwright.sync_api import sync_playwright
    opts = {"headless": False}
    if args.channel:
        opts["channel"] = args.channel
    if args.executable:
        opts["executable_path"] = args.executable
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(os.path.expanduser(args.user_data_dir),
                                                   no_viewport=True, **opts)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(args.url)
        print("Log in in the browser window (MFA included). Close the window when done.")
        ctx.wait_for_event("close", timeout=0)
    print(f"profile saved in {args.user_data_dir}")


def cmd_desktop(args):
    from . import desktops
    if args.action == "list":
        for name in desktops.list_desktops():
            mine = " (yours)" if name == "Default" else ""
            print(f"{name}{mine}")
            if name != "Default" and args.name != "-":
                try:
                    for w in desktops.windows_on(name):
                        print(f"    {w['exe']:<18} {w['title']}")
                except SightlineError:
                    print("    (no access)")
        return
    if args.action == "open":
        desktops.open_desktop(args.name, args.portal or "", switch=not args.no_switch,
                              max_minutes=args.max_minutes)
    elif args.action == "show":
        desktops.show(args.name, args.max_minutes)
    else:
        print(f"closed: {desktops.close_desktop(args.name)}")


def cmd_inspect(args):
    import webbrowser

    from .inspector import Inspector
    s = _session(args)
    assets = args.save_to or (str(s.assets.roots[0]) if s.assets.roots else "assets")
    insp = Inspector(s, assets)
    server, url = insp.serve(args.port)
    print(f"inspector: {url}\n(assets saved to {assets}; Ctrl+C to stop)")
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        s.close()


def cmd_windows(args):
    if sys.platform != "win32":
        raise SightlineError("`windows` lists Windows windows; on Linux use the 'display' backend")
    from .backends.window_win import _exe_name, list_windows
    for w in list_windows():
        if w["title"]:
            print(f"pid {w['pid']:>6}  {_exe_name(w['pid'])[:18]:<18}  {str(w['rect']):>22}  "
                  f"{'(min) ' if w['minimized'] else ''}{w['class'][:24]:<24}  {w['title']}")


def cmd_doctor(args):
    """What can run headless here, for this app - measured, not assumed."""
    print(f"platform: {sys.platform}")
    if sys.platform.startswith("linux"):
        import shutil
        for prog, pkg in (("Xvfb", "xvfb / xorg-x11-server-Xvfb"), ("x11vnc", "x11vnc"),
                          ("openbox", "openbox (optional)"), ("tesseract", "tesseract (OCR)")):
            print(f"  {prog:<10} {'found' if shutil.which(prog) else 'MISSING -> install ' + pkg}")
        print("Linux runs local apps fully headless on a private display:\n"
              '  [profiles.app]\n  backend = "display"\n  [profiles.app.connection]\n'
              '  command = ["/path/to/app"]')
        return
    if sys.platform != "win32":
        print("macOS: background window automation is not implemented yet; use the 'local' "
              "backend (foreground) or a remote/web client.")
        return
    from .desktops import child_sessions_enabled
    cs = child_sessions_enabled()
    print("child sessions (second session of your account, real input, never touches yours): "
          + {True: "ENABLED - ask for the 'session' route", False: "not enabled (needs admin once)",
             None: "unknown"}[cs])
    if not args.window:
        raise SightlineError("doctor --window \"part of the window title\"")
    from .backends.window_win import WindowBackend
    from .frame import region_changed
    b = WindowBackend(title=args.window, timeout=10)
    wins = b._app_windows()
    print(f"window: {wins[0]['title']!r} pid {b.pid}, {len(wins)} visible window(s) of the app")
    f = b.capture()
    f.save(args.out or "doctor_capture.png")
    if not f.pixels.any():
        verdict = "NO - the app draws nothing for background capture"
    elif b.fallback_capture:
        verdict = "ONLY WHILE UNCOVERED (GPU-drawn app): keep it visible, not under other windows"
    else:
        verdict = "OK - works behind other windows and off-screen (not minimized)"
    print(f"background capture: {verdict}\n  picture: {args.out or 'doctor_capture.png'} - check "
          "that it shows the app correctly")
    if args.click:
        x, y = (int(v) for v in args.click.split(","))
        before = b.capture()
        b.click(before.origin_x + x, before.origin_y + y)
        time.sleep(1.0)
        after = b.capture()
        changed = (before.pixels.shape != after.pixels.shape or
                   region_changed(before, after, before.rect))
        print(f"posted click at ({x},{y}): "
              + ("the app REACTED - background input works" if changed else
                 "no visible reaction - choose a spot that should change something, or this "
                 "app ignores posted input (then use the 'local' backend for it)"))
    print('\nprofile:\n  [profiles.app]\n  backend = "window"\n  [profiles.app.connection]\n'
          f'  title = "{args.window}"')


INIT_TOML = '''# sightline project. Profiles = WHERE the app runs; tests never know.
default_profile = "local"

[defaults]
assets = ["assets"]
timeout = 10
ocr_lang = "eng"                 # e.g. "fra+eng"
trace = "actions"                # off | failures | actions
# steps run after a failed test, so the next one starts clean
recovery = [{press = "esc"}, {press = "esc"}]

[profiles.local]                 # foreground, this machine
backend = "local"

[profiles.background]            # Windows: the app runs off your screen, you keep working
backend = "app"                  # sightline finds the best route for this app, and remembers it
[profiles.background.connection]
app = "{title}"                  # its window title or Start menu name
# launch = 'C:\\Apps\\app.exe'     # only if no shortcut has that name (.exe .bat .jar .lnk .ica...)

[profiles.private-display]       # Linux: the app on an invisible display
backend = "display"
[profiles.private-display.connection]
command = ["/path/to/app"]
'''

INIT_FLOW = '''# A flow = automation without Python.  Run: sightline run
name: {title} smoke test
steps:
  - wait: "text:{title}"
    timeout: 30
  - screenshot: started
  # - click: text:OK
  #   expect: text:Done
  # - type: hello
  #   into: "text:Name : @right:200"
'''

INIT_TEST = '''"""Python tests: the `app` fixture is a sightline Session of the selected profile."""
from sightline import Image, Text


def test_app_is_up(app):
    app.wait(Text("{title}"), timeout=30)
'''


def cmd_init(args):
    root = os.path.abspath(args.dir)
    files = {
        "sightline.toml": INIT_TOML.replace("{title}", args.title),
        "flows/smoke.flow.yaml": INIT_FLOW.replace("{title}", args.title),
        "tests/test_smoke.py": INIT_TEST.replace("{title}", args.title),
        "assets/README.md": "Image crops: assets/<name>.png, per-OS variants in assets/windows/... "
                            "(see sightline.assets). Create them with `sightline inspect`.\n",
        ".gitignore": "sightline-results/\n__pycache__/\n",
    }
    for rel, content in files.items():
        p = os.path.join(root, rel)
        if os.path.exists(p):
            print(f"  exists, kept: {rel}")
            continue
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(content)
        print(f"  created: {rel}")
    print(f"\nNext: cd {args.dir} && sightline run --profile local")


def cmd_run(args, extra):
    try:
        import pytest
    except ImportError:
        raise SightlineError("sightline run needs: pip install pytest pyyaml") from None
    from importlib.metadata import entry_points
    paths = args.paths or [p for p in ("tests", "flows") if os.path.isdir(p)] or ["."]
    pargs = list(paths)
    if not any(e.name == "sightline" for e in entry_points(group="pytest11")):
        pargs += ["-p", "sightline.pytest_plugin"]
    if args.profile:
        pargs += ["--profile", args.profile]
    if args.config:
        pargs += ["--sl-config", args.config]
    if args.trace:
        pargs += ["--sl-trace", args.trace]
    if args.k:
        pargs += ["-k", args.k]
    if args.exitfirst:
        pargs.append("-x")
    if args.junit:
        pargs += ["--junitxml", args.junit]
    pargs += ["-o", f"sightline_scope={args.scope}", "-p", "no:cacheprovider", "-q",
              "-W", "ignore::pytest.PytestAssertRewriteWarning"] + extra
    return pytest.main(pargs)


def main(argv=None):
    p = argparse.ArgumentParser(prog="sightline", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    def target(sp, image=True):
        sp.add_argument("--profile")
        sp.add_argument("--config")
        sp.add_argument("--assets", action="append")
        sp.add_argument("--lang", help="OCR language(s), e.g. fra+eng")
        if image:
            sp.add_argument("--image", help="use a saved screenshot instead of a live target")

    sp = sub.add_parser("shot")
    sp.add_argument("out")
    target(sp, image=False)
    sp.set_defaults(fn=cmd_shot)
    sp = sub.add_parser("find")
    sp.add_argument("locator")
    sp.add_argument("--out")
    sp.add_argument("--region")
    target(sp)
    sp.set_defaults(fn=cmd_find)
    sp = sub.add_parser("ocr")
    sp.add_argument("--region")
    target(sp)
    sp.set_defaults(fn=cmd_ocr)
    sp = sub.add_parser("crop")
    for a in ("image", "x", "y", "w", "h", "out"):
        sp.add_argument(a, type=int if a in ("x", "y", "w", "h") else str)
    sp.set_defaults(fn=cmd_crop)
    sp = sub.add_parser("vnc-check")
    sp.add_argument("target")
    sp.add_argument("--password-env")
    sp.add_argument("--out")
    sp.set_defaults(fn=cmd_vnc_check)
    sp = sub.add_parser("browser-login")
    sp.add_argument("url")
    sp.add_argument("--user-data-dir", required=True)
    sp.add_argument("--channel", help="msedge | chrome (an installed browser, no download)")
    sp.add_argument("--executable")
    sp.set_defaults(fn=cmd_browser_login)
    sp = sub.add_parser("desktop", help="visit a hidden desktop: open apps on it by hand")
    sp.add_argument("action", choices=["list", "open", "show", "close"])
    sp.add_argument("name", nargs="?", default="")
    sp.add_argument("--portal", help="URL the panel's browser opens (e.g. the Citrix portal)")
    sp.add_argument("--no-switch", action="store_true", help="create it without showing it")
    sp.add_argument("--max-minutes", type=float, default=15, help="safety: back after N minutes")
    sp.set_defaults(fn=cmd_desktop)
    sp = sub.add_parser("windows")
    sp.set_defaults(fn=cmd_windows)
    sp = sub.add_parser("doctor")
    sp.add_argument("--window", help="part of the app's window title (Windows)")
    sp.add_argument("--click", help="x,y in the captured picture: test posted input there")
    sp.add_argument("--out")
    sp.set_defaults(fn=cmd_doctor)
    sp = sub.add_parser("inspect", help="live view + locator tester + asset cropper")
    target(sp)
    sp.add_argument("--port", type=int, default=0)
    sp.add_argument("--save-to", help="assets folder (default: first asset root)")
    sp.add_argument("--no-browser", action="store_true")
    sp.set_defaults(fn=cmd_inspect)
    sp = sub.add_parser("init", help="create a sightline project")
    sp.add_argument("dir", nargs="?", default=".")
    sp.add_argument("--title", default="My App", help="window title / text of the app")
    sp.set_defaults(fn=cmd_init)
    sp = sub.add_parser("run", help="run Python tests and YAML flows")
    sp.add_argument("paths", nargs="*")
    sp.add_argument("--profile")
    sp.add_argument("--config")
    sp.add_argument("--trace", choices=["off", "failures", "actions"])
    sp.add_argument("-k", help="only tests matching this expression")
    sp.add_argument("-x", dest="exitfirst", action="store_true", help="stop at first failure")
    sp.add_argument("--junit", help="write JUnit XML (CI)")
    sp.add_argument("--scope", default="module", choices=["function", "module", "session"],
                    help="one app session per test, per file (default) or for the whole run")
    sp.set_defaults(fn=cmd_run)
    raw = list(sys.argv[1:] if argv is None else argv)
    extra: list[str] = []
    if "--" in raw:                                  # sightline run ... -- <pytest args>
        i = raw.index("--")
        raw, extra = raw[:i], raw[i + 1:]
    args = p.parse_args(raw)
    try:
        if args.fn is cmd_run:
            return args.fn(args, extra)
        return args.fn(args) or 0
    except SightlineError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
