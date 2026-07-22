#!/bin/bash
# Start the virtual display stack for browser-based logins, then the app
# server (which itself spawns and supervises the latchkey gateway).
set -euo pipefail

# 8:5, matching #vnc-screen's aspect-ratio.
SCREEN_GEOMETRY="${LATCHKEY_SCREEN_GEOMETRY:-1600x1000x24}"
DISPLAY_NUM="${DISPLAY#:}"

Xvfb ":${DISPLAY_NUM}" -screen 0 "${SCREEN_GEOMETRY}" -nolisten tcp &

# Wait for the X socket so the clients below don't race it.
for _ in $(seq 1 50); do
    [ -e "/tmp/.X11-unix/X${DISPLAY_NUM}" ] && break
    sleep 0.1
done

# Kiosk-style window manager: fullscreens every window, so the login browser
# fills the screen (and thus the whole VNC viewer) exactly.
matchbox-window-manager -use_titlebar no &

# VNC on localhost only; the app bridges it to an owner-gated websocket.
x11vnc -display ":${DISPLAY_NUM}" -localhost -rfbport 5900 -forever -shared -nopw -quiet &

exec uv run --frozen --no-dev hypercorn server.web.app:app --bind 0.0.0.0:8080
