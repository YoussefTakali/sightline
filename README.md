# sightline

Visual GUI automation for applications that expose **nothing but pixels** - legacy
desktop apps, Java/Delphi/WinDev grids, Citrix/RDP sessions, kiosks. No element ids, no
accessibility tree: images, OCR and geometry, made reliable.

- **Headless local**: apps installed on the user's own computer are automated in the
  background while the user keeps working (Windows: window messages, no mouse, no focus
  stealing; Linux: a private invisible display). No admin rights.
- **Its own framework**: CLI, project scaffold, Python tests *and* YAML flows, recovery
  after failures, per-step evidence and an HTML results page; a live inspector to build
  locators and image assets.
- **Built for the hard cases**: duplicates, slow screens, DPI/zoom, disabled vs enabled,
  dialogs in any order, grids read exactly (clipboard) or by OCR.

## Quick start

sightline is installed **from this repository** (it is not published on PyPI - and an
unrelated package called `sightline` exists there: `pip install sightline` would install
that one, and then `from sightline import Session` fails).

```bash
git clone https://github.com/YoussefTakali/sightline.git
cd sightline
python -m venv .venv
.venv/bin/activate                      # Windows (cmd): .venv\Scripts\activate
pip install -e ".[local,run,ocr-pip]"   # foreground backend, runner, pip-only OCR (no admin)
python -c "import sightline; print(sightline.__file__)"   # must point INTO this folder

sightline init myproject --title "Gestion des commandes"
cd myproject
sightline inspect --profile background  # live view: try locators, crop image assets
sightline run --profile background      # run tests/ and flows/, report in sightline-results/
```

Extras: `local` (foreground, mss+pynput) · `ocr` (Tesseract) · `ocr-pip` (RapidOCR, pip
only) · `run` (runner + YAML flows) · `browser` (web clients, Playwright) · `robot`
(Robot Framework keywords) · `oculix` (OculiX/SikuliX jar backend). VNC needs nothing extra.

**`cannot import name 'Session' from 'sightline'`** means the unrelated PyPI package is
installed: `pip uninstall -y sightline`, then `pip install -e ".[local,run,ocr-pip]"` again
from this folder, with the virtual environment activated.

## How it fits together

```
 sightline run  ─ Python tests (app fixture) + YAML flows ─ recovery ─ results page
        │
 Session        fresh capture → exactly one match → stable on 2 frames → act → verify → trace
        │
 Locators       Image · Text (OCR) · At  │ fallback · within · right_of/below · area · nth · offset
 Components     menu("Fichier > Exporter") · select · fill_form · read_table (clipboard) · read_grid (OCR)
        │
 Vision         all occurrences + NMS · DPI/zoom scale search · masks · colour gate · ink-OCR
        │
 Backends       window (Windows, background) · display (Linux, private screen) · local (foreground)
                vnc (any machine) · browser (Citrix/Horizon/AVD web clients) · oculix · replay (offline)
```

## Writing automation

**Python** (`tests/test_orders.py`; the `app` fixture is a Session of the chosen profile):

```python
from sightline import Text, Image
from sightline.actions import menu, fill_form
from sightline.components import read_grid

def test_export(app):
    fill_form(app, {"Utilisateur :": "Jean", "Mot de passe :": secret})
    app.click(Text("Valider"), expect=Text("Bienvenue"))      # waits for the slow screen
    menu(app, "Fichier > Exporter > CSV")
    app.click(Image("buttons/ok") | Text("OK", exact=True), vanish=True, retries=1)
    orders = read_grid(app, ["Numéro", "Client", "Montant", "Statut"]).rows
```

**YAML flows** (`flows/orders.flow.yaml`, no Python needed):

```yaml
vars: {user: Jean}
recover: [{press: esc}]
tests:
  log in:
    - if_visible: text:Connexion
      then:
        - type: ${user}
          into: "text:Utilisateur : @right:200"
        - type: ${env:APP_PASSWORD}
          into: "text:Mot de passe : @right:200"
          secret: true
        - click: text:Valider
          expect: text:Bienvenue
  export:
    - menu: Fichier > Exporter
    - handle_dialogs: {required: [exact:OK], optional: [exact:Oui]}
    - read_table: exact:CMD-0001[1]
      columns: [Numéro, Client, Montant, Statut]
      csv: results/orders.csv
```

Steps: `wait wait_gone click double_click right_click hover drag type press scroll
scroll_until_visible assert_visible assert_not_visible assert_text read_text read_table
handle_dialogs menu select wait_stable screenshot if_visible repeat set log run sleep`.

## Locators

| Python | String (flows / Robot / CLI) | Meaning |
|---|---|---|
| `Image("toolbar/save")` | `image:toolbar/save` | asset crop(s), every variant for this OS/theme/DPI |
| `Image("x", color=True)` | `image:x;color=true` | also compare colours (checked/unchecked, enabled/disabled) |
| `Text("Valider")` | `text:Valider` | OCR, fuzzy per word, accent/case-insensitive |
| `Text("OK", exact=True)` | `exact:OK` | OCR, exact |
| `Text(re.compile(r"\d+ lignes"))` | `regex:\d+ lignes` | OCR, regex |
| `a \| b` | `A \|\| B` | fallback |
| `t.within(c)` | `C >> T` | inside the (unique) container |
| `t.nth(0)` / `t.nth(-1)` | `T[1]` / `T[-1]` | n-th in reading order |
| `Text("Email :").area("right", 250)` | `text:Email : @right:250` | the region next to a label (a field) |
| `cb.right_of(label)`, `.below()`, `.near()` | | nearest candidate on that side, aligned |

**Assets** live in `assets/`, with optional variants: `assets/windows/...` (per OS),
`save~disabled.png` (another look), `save@1.25x.png` (cropped on a 125 % screen). A new
look of a button is one more file, never a script change. Create them with
`sightline inspect` (drag a rectangle); flat crops that would match anywhere are refused.

## The reliability contract

| Situation | What sightline does |
|---|---|
| Same icon several times | strict: `Ambiguous` listing every candidate, drawn on a screenshot |
| Element appears late / slides in | polls, then requires the same position on 2 consecutive frames |
| Click swallowed while a dialog draws | `click(expect=..., vanish=True, retries=1)` |
| Screen still repainting | `wait_until_stable(region)` / `wait_for_change()` - no sleeps |
| Remote session zoomed / other DPI | declared scale ±2 %, then coarse+fine scale search (logged) |
| Disabled vs enabled, checked vs unchecked | colour gate + `which({state: locator})` |
| Mouse cursor / hover on the target | VNC cursor pseudo-encoding + `park_mouse` |
| OCR "almost" matches the wrong line | per-word fuzzy scoring ("… Martin" ≠ "… Lefèvre") |
| Menu item = button text elsewhere | `menu()`/`select()` search only in what just appeared |
| Item somewhere in a long list | `scroll_until_visible` stops when scrolling changes nothing |
| Grid values cut ("Lefè…") or OCR typos | `read_table` (clipboard, exact, cross-checked) or `read_grid` (flags truncation) |
| A test failed and left a dialog open | profile/flow `recovery` steps run before the next test |
| Ctrl+A isn't select-all (Tk, emacs bindings) | `clear=True` uses End, Shift+Home, Delete |
| Windows session locked (foreground mode) | fails at once with that reason, not "not found" |
| "It failed at 3 a.m. headless" | annotated frame per step, best near-miss in blue, results page + traces |

## Where the app runs: profiles

`sightline.toml` holds profiles; tests never know which one runs.

| Backend | Use | Docs |
|---|---|---|
| `window` | **local app on Windows, in the background** (check it: `sightline doctor --window "App"`) | [headless-local](docs/headless-local.md) |
| `display` | **local app on Linux, private invisible display** (TigerVNC Xvnc) | [headless-local](docs/headless-local.md) |
| `local` | foreground, this machine (Windows, macOS, Linux X11) | |
| `vnc` | any machine with a VNC server: containers, Windows VMs (TightVNC/UltraVNC), Macs | [windows-headless](docs/windows-headless.md) |
| `browser` | Citrix / Horizon / AVD **web clients** in a headless browser (try it on the lab: `tools/citrix_lab.sh up`) | [citrix-headless](docs/citrix-headless.md) |
| `oculix` | the OculiX/SikuliX jar drives the screen; vision stays here | |
| `replay` | recorded screenshots: test locators and readers offline | |

OCR: `ocr = "tesseract"` (needs the program; `ocr_lang = "fra+eng"`) or `ocr = "rapidocr"`
(`pip install sightline[ocr-pip]`, no admin). Default `auto` tries Tesseract, then RapidOCR.

## CLI

```bash
sightline init [dir] --title "My App"            # project: profiles, a flow, a test
sightline run [paths] --profile P [-k expr] [--junit out.xml] [--scope module]
sightline inspect [--profile P | --image shot.png]  # live view, locator tester, asset cropper
sightline doctor [--window "App" --click x,y]    # what can run headless here, for this app
sightline windows                                # Windows: window titles
sightline find "text:Valider" --image shot.png   # try a locator on a capture
sightline ocr --image shot.png                   # what OCR reads, with positions
sightline shot screen.png --profile P | crop shot.png x y w h out.png
sightline vnc-check host:5900 --password-env VNC_PW
sightline browser-login URL --user-data-dir DIR --channel msedge
```

Robot Framework users: `Library  sightline.rf.SightlineLibrary  profile=...` (see
`examples/robot/orders.robot`).

## Tests

| Suite | Covers | Runs |
|---|---|---|
| `tests/test_core.py`, `test_ocr.py`, `test_ocr_rapid.py` | matching, scales, masks, colour, locators, assets, OCR engines | offline |
| `tests/test_vnc_protocol.py` | RFB client vs an in-process fake server (auth, zlib, sync, input, clipboard, DES) | offline |
| `tests/test_runner.py`, `test_inspector.py` | runner, flows, recovery, results page, `init`; inspector API + page | offline |
| `tests/test_e2e_headless.py` | a real app over VNC: login, checkbox state, scrolling, dialogs, clipboard grid, **OCR grid vs clipboard**, a YAML flow via `sightline run` | podman/docker |
| `tests/test_e2e_browser.py` | the same app through a web client in headless Chromium | podman/docker |
| `tests/test_e2e_citrix_lab.py` | **Citrix-like lab** (`tools/citrix_lab.sh`): portal login → published app in the browser over RDP → app, grid by clipboard and by OCR, menus, dialogs, a YAML flow | podman/docker |
| `tests/test_display_linux.py` | private displays (Xvnc and Xvfb+x11vnc), parallel, crash reporting | `docker/dev` |
| `tools/wine_smoke.sh` | the Windows `window` backend under Wine | podman/docker |
| `tests/windows/test_window_real.py` | background capture **behind another window** on real Windows | CI `windows-latest` |

CI (`.github/workflows/ci.yml`): lint, unit tests on Linux/Windows/macOS × Python
3.10/3.12, real-Windows backend test, Linux end-to-end, Wine.

## Not done yet

- macOS background backend (ScreenCaptureKit + per-process events).
- More grid heuristics on top of `read_grid` (dragging separators to widen cut columns).
- RDP backend (headless RDP client); ZRLE/Tight VNC encodings for slow links.
