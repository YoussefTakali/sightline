#!/bin/sh
set -e
rm -f /var/run/xrdp/*.pid /var/run/xrdp-sesman.pid 2>/dev/null || true
mkdir -p /var/run/xrdp
xrdp-sesman
exec xrdp --nodaemon
