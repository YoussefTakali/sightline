r"""Why does `import sightline` fail? Standard library only - runs even when sightline can't.

    python tools\diagnose.py          (Windows, inside the activated virtual environment)
    python tools/diagnose.py          (Linux / macOS)

Prints what Python finds, what is missing, and the exact commands that fix it.
"""
import importlib.util
import os
import subprocess
import sys
import traceback
from importlib import metadata

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))     # the repo folder
SRC = os.path.join(HERE, "src", "sightline")
fixes: list[str] = []


def line(label, value):
    print(f"  {label:<26} {value}")


print("Python")
line("executable", sys.executable)
line("version", sys.version.split()[0])
in_venv = sys.prefix != getattr(sys, "base_prefix", sys.prefix)
line("virtual environment", "yes" if in_venv else "NO")
if not in_venv:
    fixes.append(r"Activate the environment first:  .venv\Scripts\activate   (Linux/macOS: source .venv/bin/activate)")

print("\nWhich 'sightline' Python finds")
spec = importlib.util.find_spec("sightline")
origin = spec.origin if spec else None
line("found at", origin or "NOT FOUND")
try:
    dist = metadata.distribution("sightline")
    line("installed distribution", f"sightline {dist.version}")
    summary = (dist.metadata.get("Summary") or "").strip()
    line("its summary", summary or "-")
    if "VLM" in summary or "procedural" in summary:
        fixes.append("The UNRELATED PyPI package 'sightline' is installed: pip uninstall -y sightline")
except metadata.PackageNotFoundError:
    line("installed distribution", "none")
if origin and os.path.normcase(os.path.dirname(origin)) != os.path.normcase(SRC):
    fixes.append(f"Python imports a sightline that is NOT this repo ({origin}). "
                 "Remove it: pip uninstall -y sightline (repeat until 'not installed'), "
                 f"delete that folder if it remains, then reinstall from {HERE}")
if spec is None:
    fixes.append(f"Install it from the repo: cd {HERE}  then  pip install -e \".[local,run]\"")
site_dirs = [p for p in sys.path if p.endswith("site-packages")]
for d in site_dirs:
    left = os.path.join(d, "sightline")
    if os.path.isdir(left):
        line("folder in site-packages", left)
        fixes.append(f"A leftover 'sightline' folder shadows the repo: delete {left}")

print("\nDependencies")
for mod, pkg, needed in (("numpy", "numpy", True), ("cv2", "opencv-python-headless", True),
                         ("mss", "mss", False), ("pynput", "pynput", False),
                         ("pytest", "pytest", False), ("yaml", "pyyaml", False),
                         ("pytesseract", "pytesseract", False), ("playwright", "playwright", False)):
    try:
        m = __import__(mod)
        line(pkg, f"ok {getattr(m, '__version__', '')}")
    except Exception as e:
        line(pkg, f"MISSING ({type(e).__name__}: {str(e).splitlines()[0][:80]})")
        if needed:
            fixes.append(f"pip install {pkg}")
        elif pkg in ("mss", "pynput"):
            fixes.append(f"pip install {pkg}   (needed by the foreground 'local' backend)")
        elif pkg == "pyyaml":
            print("  " + " " * 26 + "(only for YAML flows; Python tests work without it)")

print("\nImporting sightline")
try:
    import sightline
    from sightline import Session, Text  # noqa: F401
    line("result", f"OK - {sightline.__file__}")
except Exception:
    print("  FAILED:")
    print("    " + "".join(traceback.format_exc()).replace("\n", "\n    "))

if sys.platform == "win32":
    print("\nWindows OCR (built in)")
    ps = subprocess.run(["powershell", "-NoProfile", "-Command",
                         "[Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType=WindowsRuntime] "
                         "| Out-Null; [Windows.Media.Ocr.OcrEngine]::AvailableRecognizerLanguages "
                         "| ForEach-Object { $_.LanguageTag }"], capture_output=True, text=True)
    line("OCR languages", ", ".join(ps.stdout.split()) or f"none / error: {ps.stderr.strip()[:120]}")

print("\nWhat to do")
if fixes:
    for i, f in enumerate(dict.fromkeys(fixes), 1):
        print(f"  {i}. {f}")
    print(f"  then:  cd {HERE}  &&  pip install -e \".[local,run]\"  &&  python tools/diagnose.py")
else:
    print("  Nothing - sightline imports fine with this Python.")
