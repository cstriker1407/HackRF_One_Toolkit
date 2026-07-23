#!/bin/bash
# Start RF_Bridge + hackrf_web on this machine (the one with the HackRF).
# Web is LAN-open so you can browse from another host (e.g. Windows).
set -e
PROJ="$(cd "$(dirname "$0")" && pwd)"
VENV="$PROJ/.venv/bin"
mkdir -p "$PROJ/logs"

pkill -f "rf_bridge" 2>/dev/null || true
pkill -f "hackrf_web" 2>/dev/null || true
# also clear any orphaned RF child processes (a bridge restart doesn't kill these)
pkill -9 -f "gps-sdr-sim" 2>/dev/null || true
pkill -9 -f "hackrf_transfer" 2>/dev/null || true
pkill -9 -f "hackrf_sweep" 2>/dev/null || true
sleep 1

nohup "$VENV/python" -m rf_bridge --port 30001 \
      > "$PROJ/logs/rf_bridge.log" 2>&1 &
nohup "$VENV/python" -m hackrf_web --allow-lan --port 30000 \
      --bridge-url http://127.0.0.1:30001 \
      > "$PROJ/logs/hackrf_web.log" 2>&1 &
sleep 2

IP=$(hostname -I | awk '{print $1}')
echo "RF_Bridge  : http://127.0.0.1:30001  (log: logs/rf_bridge.log)"
echo "hackrf_web : http://${IP}:30000        (log: logs/hackrf_web.log)"
echo "在浏览器打开: http://${IP}:30000"
