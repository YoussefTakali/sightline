#!/bin/sh
# Validate the Windows 'window' backend under Wine (Linux host + podman/docker).
# Downloads the Windows embeddable Python + Windows wheels into .wine-env/ (once).
set -e
cd "$(dirname "$0")/.."
ENGINE=$(command -v podman || command -v docker)
E=.wine-env
if [ ! -x "$E/py/python.exe" ]; then
  mkdir -p "$E/dl" "$E/py/site"
  curl -sSfL -o "$E/dl/py.zip" https://www.python.org/ftp/python/3.11.9/python-3.11.9-embed-amd64.zip
  python3 -m pip download -q --only-binary=:all: --platform win_amd64 --python-version 3.11 \
      --implementation cp -d "$E/dl" "numpy==1.26.4" "opencv-python-headless<4.11"
  python3 - "$E" <<'PY'
import glob, sys, zipfile
e = sys.argv[1]
zipfile.ZipFile(f"{e}/dl/py.zip").extractall(f"{e}/py")
for w in glob.glob(f"{e}/dl/*.whl"):
    zipfile.ZipFile(w).extractall(f"{e}/py/site")
open(f"{e}/py/python311._pth", "w").write("python311.zip\r\n.\r\nsite\r\nZ:\\work\\src\r\nimport site\r\n")
PY
fi
cat > "$E/Dockerfile" <<'D'
FROM docker.io/library/debian:bookworm-slim
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      wine wine64 xvfb openbox fonts-liberation && rm -rf /var/lib/apt/lists/* \
    && useradd -m dev && mkdir -p /tmp/.X11-unix && chmod 1777 /tmp/.X11-unix
USER dev
ENV WINEDEBUG=-all WINEARCH=win64 WINEDLLOVERRIDES=mscoree,mshtml=
D
$ENGINE build -q -t sightline-wine "$E" >/dev/null
if [ "$(basename "$ENGINE")" = podman ]; then USEROPT="--userns=keep-id"; else USEROPT="--user $(id -u):$(id -g) -e HOME=/tmp"; fi
$ENGINE run --rm $USEROPT -v "$PWD":/work:Z -w /work sightline-wine sh -c '
  Xvfb :1 -screen 0 1280x800x24 -nolisten tcp >/dev/null 2>&1 & sleep 1; export DISPLAY=:1
  openbox >/dev/null 2>&1 &
  timeout 120 wineboot -u >/dev/null 2>&1
  wine notepad >/dev/null 2>&1 & sleep 5
  wine /work/.wine-env/py/python.exe /work/tests/windows/wine_smoke.py 2>&1 | grep -v fixme'
