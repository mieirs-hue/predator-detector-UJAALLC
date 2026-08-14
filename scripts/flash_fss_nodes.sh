#!/usr/bin/env bash
set -euo pipefail

# Guarded flashing helper for four mapped lighthouse nodes.
# It only flashes when --yes is provided.

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FW_DIR="$REPO_ROOT/lighthouses"
PIO_BIN="${PIO_BIN:-$HOME/.local/bin/pio}"

if [[ ! -x "$PIO_BIN" ]]; then
  if command -v pio >/dev/null 2>&1; then
    PIO_BIN="$(command -v pio)"
  else
    echo "ERROR: PlatformIO executable not found. Install pio or set PIO_BIN." >&2
    exit 1
  fi
fi

declare -A NODE_PORT_CANDIDATES=(
  [FSS-N01]="/dev/serial/by-id/usb-Arduino_NanoESP32_28848546D968-if01 /dev/serial/by-id/usb-Espressif_USB_JTAG_serial_debug_unit_28:84:85:46:D9:68-if00 /dev/ttyACM0"
  [FSS-N02]="/dev/serial/by-id/usb-Arduino_NanoESP32_28848546D8CC-if01 /dev/serial/by-id/usb-Espressif_USB_JTAG_serial_debug_unit_28:84:85:46:D8:CC-if00 /dev/ttyACM1"
  [FSS-N03]="/dev/serial/by-id/usb-Arduino_NanoESP32_E072A1CED858-if01 /dev/serial/by-id/usb-Espressif_USB_JTAG_serial_debug_unit_E0:72:A1:CE:D8:58-if00 /dev/ttyACM2"
  [FSS-N04]="/dev/serial/by-id/usb-Arduino_NanoESP32_E072A1F0A348-if01 /dev/serial/by-id/usb-Espressif_USB_JTAG_serial_debug_unit_E0:72:A1:F0:A3:48-if00 /dev/ttyACM3"
)

declare -A NODE_ENV=(
  [FSS-N01]="FSS-N01"
  [FSS-N02]="FSS-N02"
  [FSS-N03]="FSS-N03"
  [FSS-N04]="FSS-N04"
)

usage() {
  cat <<'EOF'
Usage:
  scripts/flash_fss_nodes.sh [--yes] [all|FSS-N01|FSS-N02|FSS-N03|FSS-N04]

Examples:
  scripts/flash_fss_nodes.sh all               # preview only (no flash)
  scripts/flash_fss_nodes.sh --yes all         # flash all four nodes
  scripts/flash_fss_nodes.sh --yes FSS-N03     # flash one node only

Notes:
  - Without --yes, the script only prints intended actions.
  - Uses stable /dev/serial/by-id paths to avoid tty reordering issues.
EOF
}

resolve_node_port() {
  local node="$1"
  local candidate
  for candidate in ${NODE_PORT_CANDIDATES[$node]}; do
    if [[ -e "$candidate" ]]; then
      echo "$candidate"
      return 0
    fi
  done
  return 1
}

CONFIRM=0
TARGET="all"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --yes)
      CONFIRM=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    all|FSS-N01|FSS-N02|FSS-N03|FSS-N04)
      TARGET="$1"
      shift
      ;;
    *)
      echo "ERROR: Unknown argument: $1" >&2
      usage
      exit 1
      ;;
  esac
done

if [[ ! -d "$FW_DIR" ]]; then
  echo "ERROR: Firmware directory not found at $FW_DIR" >&2
  exit 1
fi

NODES=(FSS-N01 FSS-N02 FSS-N03 FSS-N04)
if [[ "$TARGET" != "all" ]]; then
  NODES=("$TARGET")
fi

echo "Repository: $REPO_ROOT"
echo "Firmware dir: $FW_DIR"
echo "PIO binary: $PIO_BIN"
echo "Target nodes: ${NODES[*]}"

for node in "${NODES[@]}"; do
  port="$(resolve_node_port "$node" || true)"
  if [[ -z "$port" ]]; then
    echo "ERROR: Missing port for $node. Tried: ${NODE_PORT_CANDIDATES[$node]}" >&2
    exit 1
  fi
  echo "Mapped $node -> $port"
done

if [[ "$CONFIRM" -ne 1 ]]; then
  echo
  echo "Preview mode only. No firmware was flashed."
  echo "Re-run with --yes to execute uploads."
  exit 0
fi

echo
for node in "${NODES[@]}"; do
  port="$(resolve_node_port "$node")"
  env_name="${NODE_ENV[$node]}"
  echo "Flashing $node using env $env_name on $port"
  "$PIO_BIN" run -d "$FW_DIR" -e "$env_name" -t upload --upload-port "$port"
done

echo "All requested flash operations completed."
