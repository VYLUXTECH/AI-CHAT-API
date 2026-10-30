#!/usr/bin/env bash
# AI-CHAT-API — visible-Chrome session bootstrap (Xvfb + x11vnc + noVNC).
# Launches Chrome with a persistent profile, CDP on :9222, VNC UI on :6080.
set -euo pipefail
EXPOPT=(-noreset -nolisten tcp)
command -v Xvfb >/dev/null || apt-get install -y xvfb x11vnc novnc websockify chromium-browser >/dev/null
(pgrep -f 'Xvfb :99' >/dev/null) || (Xvfb :99 -screen 0 1440x900x24 -nolisten tcp &)
export DISPLAY=:99
sleep 1
mkdir -p ~/.aichatapi/profile
/opt/google/chrome/chrome --user-data-dir=$HOME/.aichatapi/profile \
  --remote-debugging-port=9222 --no-sandbox --disable-gpu --window-size=1440,860 \
  --window-position=0,0 --disable-blink-features=AutomationControlled \
  --no-first-run --no-default-browser-check https://chat.qwen.ai/ &
x11vnc -display :99 -forever -shared -rfbport 5900 -passwd "$VNC_PW" &
websockify --web /usr/share/novnc 6080 localhost:5900
