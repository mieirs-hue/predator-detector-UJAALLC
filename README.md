# predator-detector-UJAALLC

A robust multi-node RF telemetry and zone presence detection system using ESP32-S3 lighthouses and a Jetson Orin Nano spatial engine.

## Architecture

*   **Edge Layer:** ESP32-S3 Lighthouses responsible for packet sniffing and RF telemetry collection.
*   **Spatial Engine:** Jetson Orin Nano backend running the Python-based hysteresis engine and telemetry aggregation.
*   **Digital Twin:** WebGL-based visualization of RF zones and presence confidence mapping.

## Jetson Pre-Flash Workflow

Build-only validation (safe, no board writes):

```bash
~/.local/bin/pio run -d lighthouses
```

Guarded flash helper (saved for later):

```bash
scripts/flash_fss_nodes.sh all
scripts/flash_fss_nodes.sh --yes all
```

The first command previews mapped actions only. Uploads are performed only when
`--yes` is provided.

## Jetson 16x2 I2C LCD diagnostic HUD

The Jetson can drive a standard PCF8574-backed HD44780 LCD directly over I2C.
`lcd_test.py` displays the company splash; `lcd_hud.py` shows the splash for
two seconds and then cycles through crawler state/battery/heading, LIPS target
and distance, STM32 e-stop and wheel RPM, and the Jetson's selected IPv4
address. Live data comes read-only from the existing dashboard WebSocket and
stale telemetry is shown as offline. Neither script sends crawler commands.

Verify the bus/address first. On the tested Jetson, read-mode I2C probing found
`0x27` on bus 7. Install the dashboard requirements and run the HUD on the
Jetson that has the display attached:

```bash
i2cdetect -r -y 7 0x27 0x27
python3 -m pip install -r requirements-dashboard.txt
python3 lcd_hud.py --bus 7 --address 0x27
```

For a dashboard hosted elsewhere, set
`--dashboard-url ws://<dashboard-host>:8000/ws/dashboard`. The IP page is the
Jetson route address, not the crawler's: crawler telemetry currently contains
no crawler IP field. The `lighthouses` PlatformIO project builds ESP32 sensor
nodes; the LCD remains connected to Jetson GPIO and does not require flashing
the USB-C-attached MCU boards.
