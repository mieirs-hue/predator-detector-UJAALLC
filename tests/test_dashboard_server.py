import unittest

from jetson_engine import dashboard_server


class DashboardServerTests(unittest.TestCase):
    def setUp(self) -> None:
        dashboard_server.dashboard_state = dashboard_server.build_initial_state()

    def make_node(self, node_id: str = "FSS-N01") -> dict:
        return dashboard_server.get_node_state(node_id)

    def make_predator_packet(self, node_id: str = "FSS-N01") -> dict:
        return {
            "zone_data": {
                "lighthouse": node_id,
                "state": {"state": "HOLD"},
                "raw_telemetry": {
                    "distance_cm": 42,
                    "status": "PREDATOR_DETECTED",
                    "audio_ready": True,
                },
            }
        }

    def make_clear_packet(self, node_id: str = "FSS-N01") -> dict:
        return {
            "zone_data": {
                "lighthouse": node_id,
                "state": {"state": "CLEAR"},
                "raw_telemetry": {
                    "distance_cm": 120,
                    "status": "CLEAR",
                    "audio_ready": True,
                },
            }
        }

    def test_automatic_mode_arms_audio_on_predator_alert(self) -> None:
        dashboard_server.set_operation_mode("FULLY_AUTOMATIC")
        node = self.make_node()

        dashboard_server.update_motion_state(node, self.make_predator_packet())

        self.assertEqual(node["motion_label"], "PREDATOR_DETECTED")
        self.assertTrue(node["siren_on"])
        self.assertTrue(node["intercom_on"])
        self.assertTrue(node["auto_alert_engaged"])

        dashboard_server.update_motion_state(node, self.make_clear_packet())

        self.assertEqual(node["motion_label"], "CLEAR")
        self.assertFalse(node["siren_on"])
        self.assertFalse(node["intercom_on"])
        self.assertFalse(node["auto_alert_engaged"])

    def test_manual_siren_off_latch_blocks_rearm_until_clear(self) -> None:
        dashboard_server.set_operation_mode("FULLY_AUTOMATIC")
        node = self.make_node()

        dashboard_server.update_motion_state(node, self.make_predator_packet())
        self.assertTrue(node["siren_on"])

        dashboard_server.toggle_node_feature(node["node_id"], "siren", False)
        self.assertFalse(node["siren_on"])
        self.assertTrue(node["siren_manual_off_latch"])

        dashboard_server.update_motion_state(node, self.make_predator_packet())

        self.assertFalse(node["siren_on"])
        self.assertTrue(node["siren_manual_off_latch"])

    def test_node_mic_toggle_tracks_explicit_muted_state(self) -> None:
        node = self.make_node()

        updated = dashboard_server.toggle_node_feature(node["node_id"], "mic", True)
        self.assertIsNotNone(updated)
        self.assertTrue(updated["mic_enabled"])
        self.assertEqual(updated["mic_state"], "UNMUTED")

        updated = dashboard_server.toggle_node_feature(node["node_id"], "mic", False)
        self.assertFalse(updated["mic_enabled"])
        self.assertEqual(updated["mic_state"], "MUTED")

    def test_cycle_speaker_sequence_includes_all_four_nodes(self) -> None:
        source = dashboard_server.__file__
        with open(source, "r", encoding="utf-8") as handle:
            contents = handle.read()

        self.assertIn("const sequenceTargets = speakerNodes;", contents)
        for node_id in ["FSS-N01", "FSS-N02", "FSS-N03", "FSS-N04"]:
            self.assertIn(f"{{ id: '{node_id}', zone:", contents)


if __name__ == "__main__":
    unittest.main()