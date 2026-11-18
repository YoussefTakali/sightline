# Linux dev/test environment: TigerVNC Xvnc (+ Xvfb/x11vnc fallback) + Tk + Tesseract + Python deps.
#   podman build -t sightline-dev docker/dev
#   podman run --rm -v "$PWD":/work:Z -w /work -e PYTHONPATH=/work/src sightline-dev \
#       python3 -m pytest -q tests/test_display_linux.py
FROM docker.io/library/debian:bookworm-slim
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        tigervnc-standalone-server xvfb x11vnc openbox python3 python3-tk python3-numpy python3-opencv python3-pil \
        python3-pycryptodome python3-pytest tesseract-ocr tesseract-ocr-fra fonts-liberation \
        fonts-dejavu-core python3-pip python3-yaml python3-xlib \
    && pip3 install --break-system-packages --no-deps pytesseract && fc-cache -f \
    && rm -rf /var/lib/apt/lists/* && useradd -m dev && mkdir -p /tmp/.X11-unix \
    && chmod 1777 /tmp/.X11-unix
USER dev
