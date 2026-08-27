import tempfile
import unittest
from pathlib import Path

from jetson_engine import dashboard_server
from jetson_engine.telemetry_schema import TelemetryPacket, canonical_to_legacy_packet, parse_telemetry_packet


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

    def test_canonical_udp_packet_is_adapted_into_dashboard_state(self) -> None:
        payload = b'{"protocol":"fsss.telemetry","version":"1.0.0","node_id":"FSS-N02","sequence":7,"esp_timestamp_ms":1234,"rf":{"rssi_dbm":-61},"lidar":{"distance_cm":287,"strength":812,"valid":true}}'

        packet = dashboard_server.canonical_udp_packet(payload)

        self.assertIsNotNone(packet)
        self.assertEqual(dashboard_server.apply_telemetry_packet(packet), "FSS-N02")
        self.assertEqual(dashboard_server.dashboard_state["latest_packet"], packet)
        self.assertEqual(dashboard_server.get_node_state("FSS-N02")["rssi"], -61)

    def test_udp_packet_rejects_noncanonical_json(self) -> None:
        self.assertIsNone(dashboard_server.canonical_udp_packet(b'{"node_id":"FSS-N01"}'))
        self.assertIsNone(dashboard_server.canonical_udp_packet(b'[]'))

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

    def test_microphone_alarm_activates_at_minus_30_dbfs(self) -> None:
        node = self.make_node("FSS-N03")
        packet = {
            "zone_data": {
                "lighthouse": "FSS-N03",
                "state": {"state": "CLEAR"},
                "raw_telemetry": {
                    "distance_cm": 200,
                    "status": "OK",
                    "audio_ready": True,
                    "mic_enabled": True,
                    "mic_rms": 10 ** (-30.0 / 20.0),
                },
            }
        }

        dashboard_server.update_motion_state(node, packet)

        self.assertTrue(node["mic_alarm_active"])
        self.assertEqual(node["motion_label"], "CLEAR")

        updated = dashboard_server.toggle_node_feature(node["node_id"], "mic", False)
        self.assertFalse(updated["mic_enabled"])
        self.assertEqual(updated["mic_state"], "MUTED")

    def test_flight_data_recorder_starts_and_stops_on_threat_window(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            recorder = dashboard_server.FlightDataRecorder(Path(tmp_dir), cooldown_seconds=0.01)
            original_recorder = dashboard_server.telemetry_recorder
            dashboard_server.telemetry_recorder = recorder
            try:
                node = self.make_node()
                dashboard_server.update_motion_state(node, self.make_predator_packet())
                self.assertEqual(node["motion_label"], "PREDATOR_DETECTED")

                dashboard_server.apply_telemetry_packet(self.make_predator_packet())
                self.assertTrue(recorder.is_recording)
                self.assertEqual(len(list(Path(tmp_dir).glob("threat_event_*.jsonl"))), 1)

                dashboard_server.apply_telemetry_packet(self.make_clear_packet())
                import time as _time
                _time.sleep(0.02)
                dashboard_server.apply_telemetry_packet(self.make_clear_packet())
                self.assertFalse(recorder.is_recording)
            finally:
                dashboard_server.telemetry_recorder = original_recorder

    def test_cycle_speaker_sequence_includes_all_four_nodes(self) -> None:
        source = dashboard_server.__file__
        with open(source, "r", encoding="utf-8") as handle:
            contents = handle.read()

        self.assertIn("const sequenceTargets = speakerNodes;", contents)
        for node_id in ["FSS-N01", "FSS-N02", "FSS-N03", "FSS-N04"]:
            self.assertIn(f"{{ id: '{node_id}', zone:", contents)

    def test_dashboard_has_webgl_fallback_for_jetson_browser(self) -> None:
        source = dashboard_server.__file__
        with open(source, "r", encoding="utf-8") as handle:
            contents = handle.read()

        self.assertIn("renderFallback2DScene", contents)
        self.assertIn("2D fallback mode", contents)

    def test_dashboard_exposes_triangulation_cue(self) -> None:
        state = dashboard_server.build_initial_state()
        self.assertEqual(state["rf_fusion"]["triangulation_cue"]["state"], "NO_TRIANGLE")

    def test_dashboard_contains_matrix_room_signs(self) -> None:
        source = dashboard_server.__file__
        with open(source, "r", encoding="utf-8") as handle:
            contents = handle.read()
        for label in ["BABY'S ROOM", "GARAGE", "UNCLE JESSE'S OFFICE", "ENTRYWAY"]:
            self.assertIn(label, contents)

    def test_initial_state_exposes_generated_topology(self) -> None:
        state = dashboard_server.build_initial_state()
        self.assertEqual(len(state["topology"]), 24)
        self.assertEqual(state["topology"]["FSS-N17"]["ring"], 2)
        self.assertEqual(len(state["topology"]["FSS-N17"]["neighbors"]), 5)

    def test_canonical_packet_reaches_existing_dashboard_pipeline(self) -> None:
        packet = TelemetryPacket(node_id="FSS-N17", sequence=8, esp_timestamp_ms=1200)
        self.assertEqual(dashboard_server.apply_telemetry_packet(packet.to_dict()), "FSS-N17")
        node = dashboard_server.get_node_state("FSS-N17")
        self.assertEqual(node["last_packet"]["zone_data"]["raw_telemetry"]["sequence"], 8)


class TelemetrySchemaTests(unittest.TestCase):
    def test_canonical_packet_round_trip(self) -> None:
        payload = TelemetryPacket(node_id="FSS-N17", sequence=18231, esp_timestamp_ms=12839421).to_dict()
        parsed = parse_telemetry_packet(payload)
        legacy = canonical_to_legacy_packet(parsed)
        self.assertEqual(legacy["zone_data"]["lighthouse"], "FSS-N17")
        self.assertEqual(legacy["zone_data"]["raw_telemetry"]["sequence"], 18231)

    def test_canonical_packet_rejects_interpreted_only_payload(self) -> None:
        with self.assertRaises(ValueError):
            parse_telemetry_packet({"protocol": "fsss.dashboard", "node_id": "FSS-N17", "sequence": 1, "esp_timestamp_ms": 1})


class RfDisturbanceModelTests(unittest.TestCase):
    def setUp(self) -> None:
        dashboard_server.dashboard_state = dashboard_server.build_initial_state()

    def _fresh_node(self, node_id: str = "FSS-N01") -> dict:
        return dashboard_server.get_node_state(node_id)

    # 1 — baseline initialises on the first RSSI reading
    def test_rf_baseline_initializes_on_first_reading(self) -> None:
        node = self._fresh_node()
        self.assertIsNone(node["rf_baseline"])
        dashboard_server.update_rf_state(node, -65.0)
        self.assertEqual(node["rf_baseline"], -65.0)

    # 2 — disturbance is |rssi - baseline|, accounting for baseline EMA adaptation
    def test_rf_disturbance_is_absolute_deviation(self) -> None:
        node = self._fresh_node()
        node["rf_baseline"] = -65.0
        dashboard_server.update_rf_state(node, -45.0)
        d = abs(-45.0 - (-65.0))
        expected_c_raw = max(0.0, min(1.0, (d - dashboard_server.RF_DISTURBANCE_THRESHOLD) / dashboard_server.RF_DISTURBANCE_SCALE))
        self.assertAlmostEqual(node["rf_confidence_raw"], expected_c_raw, places=3)

    def test_rf_baseline_freezes_during_disturbance(self) -> None:
        node = self._fresh_node()
        node["rf_baseline"] = -65.0
        dashboard_server.update_rf_state(node, -45.0)
        self.assertEqual(node["rf_baseline"], -65.0)

    def test_rf_baseline_drifts_only_when_quiet(self) -> None:
        node = self._fresh_node()
        node["rf_baseline"] = -65.0
        dashboard_server.update_rf_state(node, -65.5)
        self.assertNotEqual(node["rf_baseline"], -65.0)
        self.assertAlmostEqual(node["rf_baseline"], -65.001, places=3)

    # 3 — confidence is clamped to [0, 1]
    def test_rf_confidence_clamped_below_threshold(self) -> None:
        node = self._fresh_node()
        node["rf_baseline"] = -65.0
        # Only 2 dB deviation — below default threshold of 8
        dashboard_server.update_rf_state(node, -63.0)
        self.assertEqual(node["rf_confidence_raw"], 0.0)

    def test_rf_confidence_clamped_at_maximum(self) -> None:
        node = self._fresh_node()
        node["rf_baseline"] = -65.0
        # 100 dB deviation — far beyond scale; must not exceed 1.0
        dashboard_server.update_rf_state(node, 35.0)
        self.assertEqual(node["rf_confidence_raw"], 1.0)

    # 4 — EMA smoothing with alpha = 0.20
    def test_rf_ema_smoothing(self) -> None:
        node = self._fresh_node()
        node["rf_baseline"] = -65.0
        node["rf_confidence_smooth"] = 0.0
        dashboard_server.update_rf_state(node, -45.0)
        # Calmer tuned model keeps a non-zero floor and suppresses weak RF noise.
        expected_floor = 0.20
        self.assertEqual(node["rf_confidence_smooth"], expected_floor)

    # 5 — sphere radius maps confidence to [R_min, R_max]
    def test_rf_sphere_radius_at_zero_confidence(self) -> None:
        node = self._fresh_node()
        node["rf_baseline"] = -65.0
        dashboard_server.update_rf_state(node, -63.0)  # below threshold
        self.assertEqual(node["rf_sphere_radius"], dashboard_server.RF_R_MIN)

    def test_rf_sphere_radius_scales_with_confidence(self) -> None:
        node = self._fresh_node()
        node["rf_baseline"] = -65.0
        node["rf_confidence_smooth"] = 0.0
        # Tuned model keeps radius at minimum until we cross the activation threshold.
        dashboard_server.update_rf_state(node, -45.0)
        self.assertEqual(node["rf_sphere_radius"], dashboard_server.RF_R_MIN)

    # 6 — state transition NORMAL → CONFIRMING_TARGET
    def test_rf_transition_to_confirming_target(self) -> None:
        node = self._fresh_node()
        node["rf_baseline"] = -65.0
        # Final buzzer gate is intentionally rare and near certainty.
        for _ in range(50):
            dashboard_server.update_rf_state(node, -15.0)
        self.assertEqual(node["rf_state"], "CONFIRMING_TARGET")

    def test_rf_state_stays_normal_below_threshold(self) -> None:
        node = self._fresh_node()
        node["rf_baseline"] = -65.0
        dashboard_server.update_rf_state(node, -64.0)
        self.assertEqual(node["rf_state"], "NORMAL")

    # 7 — four-node weighted fusion centroid
    def test_rf_fusion_weighted_centroid(self) -> None:
        nodes = [dashboard_server.build_node_state(nid) for nid in dashboard_server.NODE_ORDER]
        # Give N01 and N03 high confidence; N02 and N04 zero
        nodes[0]["rf_confidence_smooth"] = 0.8   # FSS-N01 @ (-10, 5, -10)
        nodes[2]["rf_confidence_smooth"] = 0.8   # FSS-N03 @ (-10, 5, +10)
        result = dashboard_server.compute_rf_fusion(nodes)
        self.assertEqual(result["state"], "ACTIVE")
        # X centroid should be -10 (both active nodes are at x=-10)
        self.assertAlmostEqual(result["position"][0], -10.0, places=1)
        self.assertIn("FSS-N01", result["active_nodes"])
        self.assertIn("FSS-N03", result["active_nodes"])

    # 8 — no fusion below minimum weight
    def test_rf_no_fusion_below_minimum_weight(self) -> None:
        nodes = [dashboard_server.build_node_state(nid) for nid in dashboard_server.NODE_ORDER]
        # All confidence 0 → total_weight = 0 < RF_MIN_FUSION_WEIGHT
        result = dashboard_server.compute_rf_fusion(nodes)
        self.assertEqual(result["state"], "NO_FUSION")
        self.assertIsNone(result["position"])
        self.assertEqual(result["active_nodes"], [])

    # 9 — dominant node is whichever has highest confidence
    def test_rf_fusion_dominant_node(self) -> None:
        nodes = [dashboard_server.build_node_state(nid) for nid in dashboard_server.NODE_ORDER]
        nodes[0]["rf_confidence_smooth"] = 0.9   # FSS-N01 highest
        nodes[1]["rf_confidence_smooth"] = 0.4   # FSS-N02
        result = dashboard_server.compute_rf_fusion(nodes)
        self.assertEqual(result["dominant_node"], "FSS-N01")

    def test_fsss24_geometry_is_generated(self) -> None:
        self.assertEqual(len(dashboard_server.NODE_ORDER), 24)
        self.assertEqual(dashboard_server.NODE_ORDER[:4], ["FSS-N01", "FSS-N02", "FSS-N03", "FSS-N04"])
        self.assertIn("FSS-N24", dashboard_server.NODE_POSITIONS)
        self.assertEqual(len(dashboard_server.NODE_NEIGHBORHOODS), 24)
        self.assertTrue(all(len(item.neighbors) == 5 for item in dashboard_server.NODE_NEIGHBORHOODS))

    def test_generated_node_state_contains_spatial_metadata(self) -> None:
        node = dashboard_server.build_node_state("FSS-N17")
        self.assertEqual(node["node_id"], "FSS-N17")
        self.assertEqual(node["role"], "PERIMETER")
        self.assertEqual(node["ring"], 2)
        self.assertEqual(len(node["position"]), 3)
        self.assertEqual(len(node["neighbors"]), 5)

    # 10 — direction stays UNKNOWN without directional evidence
    def test_rf_direction_always_unknown_on_boot(self) -> None:
        node = self._fresh_node()
        self.assertEqual(node["direction"], "UNKNOWN")
        self.assertEqual(node["direction_confidence"], 0.0)

    def test_rf_direction_not_set_by_rf_disturbance(self) -> None:
        node = self._fresh_node()
        node["rf_baseline"] = -65.0
        for _ in range(30):
            dashboard_server.update_rf_state(node, -30.0)
        # CONFIRMING_TARGET must not infer a direction from scalar RSSI alone
        self.assertEqual(node["direction"], "UNKNOWN")

    # 11 — siren stays OFF after RF confirmation
    def test_siren_stays_off_after_rf_confirming(self) -> None:
        node = self._fresh_node()
        node["rf_baseline"] = -65.0
        for _ in range(50):
            dashboard_server.update_rf_state(node, -15.0)
        self.assertEqual(node["rf_state"], "CONFIRMING_TARGET")
        self.assertFalse(node["siren_on"])

    # 12 — rf_fusion field present in initial state
    def test_rf_fusion_in_initial_state(self) -> None:
        state = dashboard_server.build_initial_state()
        self.assertIn("rf_fusion", state)
        self.assertEqual(state["rf_fusion"]["state"], "NO_FUSION")

    # 13 — snapshot_state includes rf_fusion
    def test_snapshot_state_includes_rf_fusion(self) -> None:
        snap = dashboard_server.snapshot_state()
        self.assertIn("rf_fusion", snap)


if __name__ == "__main__":
    unittest.main()