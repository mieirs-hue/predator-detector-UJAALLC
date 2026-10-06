"""Show live crawler diagnostics from the Jetson dashboard on a 16x2 I2C LCD."""

import argparse
import asyncio
import fcntl
import ipaddress
import json
import socket
import sys
import time
from typing import Any
from urllib.parse import urlsplit

from lcd_test import I2C_SLAVE, LCD1602

PAGE_SECONDS = 3.0
TELEMETRY_STALE_SECONDS = 5.0
BOOT_SPLASH_SECONDS = 2.0


def local_ip_for_dashboard(dashboard_url: str) -> str:
    """Return the IPv4 address selected for the route to the dashboard."""
    parsed = urlsplit(dashboard_url)
    if parsed.scheme not in ("ws", "wss") or not parsed.hostname:
        raise ValueError("dashboard URL must use ws:// or wss:// and include a host")
    try:
        host_ip = ipaddress.ip_address(parsed.hostname) if parsed.hostname != "localhost" else None
        if parsed.hostname == "localhost" or (host_ip is not None and host_ip.is_loopback):
            # UDP connect selects the default route without sending a packet.
            destination = ("192.0.2.1", 9)
        else:
            destination = (
                parsed.hostname,
                parsed.port or (443 if parsed.scheme == "wss" else 80),
            )
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(destination)
            return str(ipaddress.IPv4Address(probe.getsockname()[0]))
    except OSError:
        return "UNAVAILABLE"


def fit_line(value: str) -> str:
    """Keep displayed text printable and within the LCD's 16-column width."""
    return "".join(char if 32 <= ord(char) <= 126 else "?" for char in value)[:16]


def format_hud_pages(
    payload: dict[str, Any] | None,
    *,
    connected: bool,
    local_ip: str,
) -> list[tuple[str, str]]:
    """Build read-only status pages from the dashboard's crawler telemetry shape."""
    ip_page = ("JETSON IP:", fit_line(local_ip))
    if not connected:
        return [("CRAWLER OFFLINE", "CHECK DASHBOARD"), ip_page]
    if payload is None:
        return [("CRAWLER ONLINE", "WAITING FOR DATA"), ip_page]

    telemetry = payload.get("telemetry")
    lips = payload.get("lips_perception")
    stm32 = payload.get("stm32_status")
    if not all(isinstance(part, dict) for part in (telemetry, lips, stm32)):
        return [("BAD TELEMETRY", "CHECK DASHBOARD"), ip_page]

    state = fit_line(str(telemetry.get("state", "UNKNOWN")).upper())
    battery = telemetry.get("battery_pct")
    heading = telemetry.get("heading_deg")
    state_detail = f"BAT {battery:.0f}% HDG {heading:.0f}" if (
        isinstance(battery, (int, float)) and isinstance(heading, (int, float))
    ) else "TELEMETRY ACTIVE"

    target_detected = lips.get("target_detected") is True
    distance = lips.get("target_distance_cm")
    distance_line = f"DIST {distance:.1f} cm" if isinstance(distance, (int, float)) else "DIST UNKNOWN"
    left_rpm = stm32.get("left_rpm")
    right_rpm = stm32.get("right_rpm")
    rpm_line = (
        f"L {left_rpm:.0f} R {right_rpm:.0f} RPM"
        if isinstance(left_rpm, (int, float)) and isinstance(right_rpm, (int, float))
        else "RPM UNKNOWN"
    )
    estop = stm32.get("e_stop")

    return [
        (state, fit_line(state_detail)),
        ("LIPS: TARGET" if target_detected else "LIPS: SEARCH", fit_line(distance_line)),
        ("E-STOP: ACTIVE" if estop is True else "E-STOP: CLEAR", fit_line(rpm_line)),
        ip_page,
    ]


def show_page(lcd: LCD1602, page: tuple[str, str]) -> None:
    lcd.line(fit_line(page[0]), 1)
    lcd.line(fit_line(page[1]), 2)


async def run_hud(
    lcd: LCD1602,
    websocket_client: Any,
    dashboard_url: str,
    crawler_id: str,
    local_ip: str,
) -> None:
    lcd.initialize()
    show_page(lcd, ("UNCLE JESSE'S", "ALWAYS AVAILABLE"))
    await asyncio.sleep(BOOT_SPLASH_SECONDS)

    while True:
        connected = False
        latest_payload: dict[str, Any] | None = None
        last_telemetry_at = 0.0
        last_page_at = 0.0
        current_page = -1
        try:
            async with websocket_client.connect(dashboard_url, open_timeout=5) as websocket:
                while True:
                    try:
                        raw_message = await asyncio.wait_for(websocket.recv(), timeout=1.0)
                    except TimeoutError:
                        raw_message = None

                    if raw_message is not None:
                        try:
                            message = json.loads(raw_message)
                        except json.JSONDecodeError:
                            message = None
                        if isinstance(message, dict) and message.get("crawler_id") == crawler_id:
                            if message.get("type") == "crawler_connection":
                                connected = message.get("connected") is True
                                if not connected:
                                    latest_payload = None
                            elif message.get("type") == "crawler_telemetry":
                                data = message.get("data")
                                if isinstance(data, dict) and data.get("crawler_id") == crawler_id:
                                    latest_payload = data
                                    connected = True
                                    last_telemetry_at = time.monotonic()

                    now = time.monotonic()
                    fresh = last_telemetry_at and now - last_telemetry_at <= TELEMETRY_STALE_SECONDS
                    pages = format_hud_pages(
                        latest_payload if fresh else None,
                        connected=bool(connected and fresh),
                        local_ip=local_ip,
                    )
                    page_index = int(now // PAGE_SECONDS) % len(pages)
                    if page_index != current_page or now - last_page_at >= PAGE_SECONDS:
                        show_page(lcd, pages[page_index])
                        current_page = page_index
                        last_page_at = now
        except (OSError, websocket_client.exceptions.ConnectionClosed) as error:
            print(f"Dashboard connection lost: {error}", file=sys.stderr)
            show_page(lcd, ("CRAWLER OFFLINE", "CHECK DASHBOARD"))
            await asyncio.sleep(2.0)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bus", type=int, required=True, help="Verified I2C bus number")
    parser.add_argument("--address", type=lambda value: int(value, 0), required=True,
                        help="Verified backpack address, e.g. 0x27")
    parser.add_argument("--dashboard-url", default="ws://127.0.0.1:8000/ws/dashboard")
    parser.add_argument("--crawler-id", default="CRAWLER-01")
    args = parser.parse_args()
    if args.bus < 0:
        parser.error("--bus must be nonnegative")
    if not 0x08 <= args.address <= 0x77:
        parser.error("--address must be a non-reserved 7-bit I2C address")

    try:
        import websockets
    except ImportError as error:
        print(f"Missing dashboard dependency: {error}. Install requirements-dashboard.txt.",
              file=sys.stderr)
        return 1

    try:
        local_ip = local_ip_for_dashboard(args.dashboard_url)
        with open(f"/dev/i2c-{args.bus}", "wb", buffering=0) as bus:
            fcntl.ioctl(bus.fileno(), I2C_SLAVE, args.address)
            asyncio.run(run_hud(
                LCD1602(bus), websockets, args.dashboard_url, args.crawler_id, local_ip
            ))
    except OSError as error:
        print(f"LCD hardware error on bus {args.bus}, address {args.address:#04x}: {error}",
              file=sys.stderr)
        return 1
    except ValueError as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
