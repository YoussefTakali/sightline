#!/bin/sh
# Citrix-like lab (Apache Guacamole): a web portal with a login, a "published app" that
# opens inside the browser (canvas + websocket, like Citrix Workspace for HTML5), a gateway
# (guacd) and an RDP desktop (xrdp) running the demo business app.
#
#   tools/citrix_lab.sh up      portal on http://127.0.0.1:${LAB_PORT:-18080}/guacamole/  (jean / secret)
#   tools/citrix_lab.sh down
set -e
cd "$(dirname "$0")/.."
ENGINE=$(command -v podman || command -v docker)
NET=sightline-lab
PORT=${LAB_PORT:-18080}
case "$1" in
  up)
    $ENGINE build -q -t sightline-lab-rdp -f docker/citrix-lab/rdp-desktop/Dockerfile docker >/dev/null
    $ENGINE network exists $NET 2>/dev/null || $ENGINE network create $NET >/dev/null 2>&1 || true
    $ENGINE rm -f lab-desktop lab-guacd lab-portal >/dev/null 2>&1 || true
    $ENGINE run -d --rm --name lab-desktop --network $NET sightline-lab-rdp >/dev/null
    $ENGINE run -d --rm --name lab-guacd --network $NET docker.io/guacamole/guacd:1.5.5 >/dev/null
    $ENGINE run -d --rm --name lab-portal --network $NET -p "127.0.0.1:$PORT:8080" \
        -e GUACD_HOSTNAME=lab-guacd -e GUACAMOLE_HOME=/lab-home \
        -v "$PWD/docker/citrix-lab/guacamole-home:/lab-home:ro,Z" \
        docker.io/guacamole/guacamole:1.5.5 >/dev/null
    printf "waiting for the portal"
    i=0
    until curl -sf -o /dev/null "http://127.0.0.1:$PORT/guacamole/"; do
      i=$((i + 1)); [ $i -gt 120 ] && { echo " - timeout"; $ENGINE logs lab-portal | tail -20; exit 1; }
      printf "."; sleep 1
    done
    printf "\nportal: http://127.0.0.1:%s/guacamole/  (user jean, password secret)\n" "$PORT"
    ;;
  down)
    $ENGINE rm -f lab-desktop lab-guacd lab-portal >/dev/null 2>&1 || true
    $ENGINE network rm $NET >/dev/null 2>&1 || true
    ;;
  *) echo "usage: $0 up|down"; exit 2 ;;
esac
