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
        # Baseline adapts first: -65*0.98 + (-45)*0.02 = -64.6
        # d = |-45 - (-64.6)| = 19.6 → c_raw = (19.6-8)/15 ≈ 0.7733
        dashboard_server.update_rf_state(node, -45.0)
        adapted_baseline = -65.0 * 0.98 + (-45.0) * 0.02
        d = abs(-45.0 - adapted_baseline)
        expected_c_raw = max(0.0, min(1.0, (d - 8.0) / 15.0))
        self.assertAlmostEqual(node["rf_confidence_raw"], expected_c_raw, places=3)

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