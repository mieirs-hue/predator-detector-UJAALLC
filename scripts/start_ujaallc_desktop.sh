#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="/home/unclejesse/predator-detector-UJAALLC"
VENV_ACTIVATE="$PROJECT_DIR/.venv/bin/activate"
LOG_DIR="$PROJECT_DIR/.runtime-logs"
HUB_LOG="$LOG_DIR/hub.log"
DASH_LOG="$LOG_DIR/dashboard.log"
STARTUP_LOG="$LOG_DIR/startup.log"

mkdir -p "$LOG_DIR"

echo "[$(date '+%F %T')] Startup requested" >> "$STARTUP_LOG"

if [[ ! -f "$VENV_ACTIVATE" ]]; then
  notify-send "UJAALLC Startup" "Missing virtualenv at $VENV_ACTIVATE" 2>/dev/null || true
  echo "Virtualenv not found: $VENV_ACTIVATE"
  exit 1
fi

count_esp32() {
  lsusb | grep -ic "Arduino SA NanoESP32" || true
}

serial_map() {
  if [[ -d /dev/serial/by-id ]]; then
    ls -l /dev/serial/by-id 2>/dev/null || true
  else
    echo "/dev/serial/by-id not available"
  fi
  ls -l /dev/ttyACM* 2>/dev/null || echo "No /dev/ttyACM devices found"
}

ESP32_COUNT="$(count_esp32)"
{
  echo "----- $(date '+%F %T') preflight -----"
  echo "Detected NanoESP32 USB devices: $ESP32_COUNT"
  serial_map
  echo "ModemManager: $(systemctl is-active ModemManager 2>/dev/null || echo unknown)"
} >> "$STARTUP_LOG"

if [[ "$ESP32_COUNT" -lt 4 ]]; then
  notify-send "UJAALLC Startup" "Only $ESP32_COUNT of 4 NanoESP32 boards detected. Starting anyway." 2>/dev/null || true
fi

# Clear old processes from previous runs.
pkill -f "python3 jetson_engine/ujaallc_hub.py" 2>/dev/null || true
pkill -f "uvicorn jetson_engine.dashboard_server:app" 2>/dev/null || true
sleep 1

launch_in_terminal() {
  local title="$1"
  local command="$2"

  if command -v gnome-terminal >/dev/null 2>&1; then
    gnome-terminal --title="$title" -- bash -lc "$command"
    return 0
  fi

  if command -v x-terminal-emulator >/dev/null 2>&1; then
    x-terminal-emulator -T "$title" -e bash -lc "$command"
    return 0
  fi

  return 1
}

HUB_CMD="cd '$PROJECT_DIR' && source '$VENV_ACTIVATE' && python3 jetson_engine/ujaallc_hub.py 2>&1 | tee -a '$HUB_LOG'"
DASH_CMD="cd '$PROJECT_DIR' && source '$VENV_ACTIVATE' && python -m uvicorn jetson_engine.dashboard_server:app --host 127.0.0.1 --port 8000 2>&1 | tee -a '$DASH_LOG'"

if ! launch_in_terminal "UJAALLC Hub" "$HUB_CMD"; then
  nohup bash -lc "$HUB_CMD" >/dev/null 2>&1 &
fi

sleep 2

if ! launch_in_terminal "UJAALLC Dashboard Server" "$DASH_CMD"; then
  nohup bash -lc "$DASH_CMD" >/dev/null 2>&1 &
fi

sleep 2

CHROMIUM_BIN="$(command -v chromium || command -v chromium-browser || true)"
if [[ -n "$CHROMIUM_BIN" ]]; then
  "$CHROMIUM_BIN" \
    --new-window "http://127.0.0.1:8000" \
    --ignore-gpu-blocklist \
    --enable-gpu-rasterization \
    --use-gl=egl >/dev/null 2>&1 &
else
  notify-send "UJAALLC Startup" "Chromium not found in PATH" 2>/dev/null || true
fi

echo "[$(date '+%F %T')] Startup launch complete" >> "$STARTUP_LOG"
