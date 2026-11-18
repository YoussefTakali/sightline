#!/bin/sh
# what an RDP login starts: a window manager, then the business app (after the WM is ready)
openbox &
python3 /opt/wait_wm.py 10 || sleep 2
exec python3 /opt/demo_app.py
