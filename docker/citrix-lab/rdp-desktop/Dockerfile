# The lab's "published desktop" over RDP (closer to Citrix ICA/HDX than VNC): xrdp + Xorg,
# a window manager and the demo business app; clipboard through RDP's own channel.
FROM docker.io/library/debian:bookworm-slim
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        xrdp xorgxrdp xserver-xorg-core openbox python3 python3-tk fonts-liberation \
        fonts-dejavu-core dbus-x11 \
    && rm -rf /var/lib/apt/lists/* && fc-cache -f \
    && useradd -m -s /bin/sh jean && echo "jean:labpass" | chpasswd
# build context = docker/ (one copy of the demo app, shared with linux-target):
#   podman build -f docker/citrix-lab/rdp-desktop/Dockerfile docker
COPY linux-target/demo_app.py linux-target/wait_wm.py /opt/
COPY citrix-lab/rdp-desktop/startwm.sh /etc/xrdp/startwm.sh
COPY citrix-lab/rdp-desktop/entrypoint.sh /entrypoint.sh
RUN chmod +x /etc/xrdp/startwm.sh /entrypoint.sh
EXPOSE 3389
ENTRYPOINT ["/entrypoint.sh"]
