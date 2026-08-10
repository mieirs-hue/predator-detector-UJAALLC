import inspect
import unittest

from jetson_engine import ujaallc_hub


class HubConfigTests(unittest.TestCase):
    def test_telemetry_port_matches_firmware(self) -> None:
        # Firmware constant PORT_TELEMETRY in main.cpp must stay in sync
        self.assertEqual(ujaallc_hub.PORT_TELEMETRY, 5007)

    def test_visualizer_handler_accepts_single_websocket_argument(self) -> None:
        signature = inspect.signature(ujaallc_hub.visualizer_endpoint_handler)
        parameters = list(signature.parameters.values())

        self.assertEqual(len(parameters), 1)
        self.assertEqual(parameters[0].name, "websocket")


if __name__ == "__main__":
    unittest.main()
