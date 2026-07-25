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
