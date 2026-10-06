import unittest
from unittest.mock import patch

from lcd_hud import format_hud_pages, local_ip_for_dashboard


class LCDHUDTests(unittest.TestCase):
    def setUp(self) -> None:
        self.payload = {
            "crawler_id": "CRAWLER-01",
            "telemetry": {
                "state": "MANUAL",
                "battery_pct": 86.7,
                "heading_deg": 123.4,
            },
            "lips_perception": {
                "target_detected": True,
                "target_distance_cm": 42.5,
            },
            "stm32_status": {
                "left_rpm": 120,
                "right_rpm": 118,
                "e_stop": False,
            },
        }

    def test_online_pages_show_live_crawler_values(self) -> None:
        pages = format_hud_pages(self.payload, connected=True, local_ip="192.168.1.20")
        self.assertEqual(pages, [
            ("MANUAL", "BAT 87% HDG 123"),
            ("LIPS: TARGET", "DIST 42.5 cm"),
            ("E-STOP: CLEAR", "L 120 R 118 RPM"),
            ("JETSON IP:", "192.168.1.20"),
        ])
        self.assertTrue(all(len(line) <= 16 for page in pages for line in page))

    def test_offline_and_waiting_states_are_explicit(self) -> None:
        self.assertEqual(
            format_hud_pages(None, connected=False, local_ip="10.0.0.2")[0],
            ("CRAWLER OFFLINE", "CHECK DASHBOARD"),
        )
        self.assertEqual(
            format_hud_pages(None, connected=True, local_ip="10.0.0.2")[0],
            ("CRAWLER ONLINE", "WAITING FOR DATA"),
        )

    def test_malformed_payload_does_not_raise(self) -> None:
        self.assertEqual(
            format_hud_pages({"telemetry": None}, connected=True, local_ip="UNAVAILABLE")[0],
            ("BAD TELEMETRY", "CHECK DASHBOARD"),
        )

    def test_dashboard_url_must_be_websocket_url(self) -> None:
        with self.assertRaises(ValueError):
            local_ip_for_dashboard("http://127.0.0.1:8000/")

    def test_local_dashboard_uses_jetsons_default_route_ip(self) -> None:
        with patch("lcd_hud.socket.socket") as socket_factory:
            probe = socket_factory.return_value.__enter__.return_value
            probe.getsockname.return_value = ("10.0.0.12", 0)
            self.assertEqual(local_ip_for_dashboard("ws://127.0.0.1:8000/ws/dashboard"),
                             "10.0.0.12")
        probe.connect.assert_called_once_with(("192.0.2.1", 9))


if __name__ == "__main__":
    unittest.main()
