# Headless Linux target. Replace demo_app.py / CMD with your application.
#   podman build -t sightline-target docker/linux-target
#   podman run -d --rm -p 127.0.0.1:5900:5900 -e VNC_PASSWORD=... --name target sightline-target
FROM docker.io/library/debian:bookworm-slim
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        tigervnc-standalone-server tigervnc-tools openbox python3 python3-tk \
        fonts-liberation fonts-dejavu-core novnc websockify \
    && rm -rf /var/lib/apt/lists/* && fc-cache -f
COPY entrypoint.sh /entrypoint.sh
COPY demo_app.py wait_wm.py /opt/
RUN chmod +x /entrypoint.sh && useradd -m app && mkdir -p /tmp/.X11-unix && chmod 1777 /tmp/.X11-unix
USER app
ENV DISPLAY=:99 RESOLUTION=1280x800 VNC_PORT=5900
EXPOSE 5900 6080
ENTRYPOINT ["/entrypoint.sh"]
CMD ["python3", "/opt/demo_app.py"]
