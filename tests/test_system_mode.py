import unittest

import main as system_core


class SystemModeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        system_core.system_state.update(
            {
                "mode_state": "STOP",
                "active_dataset": None,
                "frame_index": 0,
                "is_ready": True,
            }
        )
        system_core.active_injector = None

    async def test_seek_preserves_active_injector_and_updates_cursor(self) -> None:
        await system_core.handle_system_mode("REPLAY", {"dataset": "session_001_folsom"})
        before_cursor = system_core.active_injector.cursor if system_core.active_injector else None

        ack = await system_core.handle_system_mode("SEEK", {"frame_index": 37})

        self.assertTrue(ack["success"])
        self.assertEqual(ack["frame_index"], 37)
        self.assertIsNotNone(system_core.active_injector)
        self.assertEqual(system_core.active_injector.cursor, 37)
        self.assertNotEqual(before_cursor, system_core.active_injector.cursor)

    async def test_resume_returns_to_replay_state(self) -> None:
        await system_core.handle_system_mode("REPLAY", {"dataset": "session_001_folsom"})
        await system_core.handle_system_mode("PAUSE", {})

        ack = await system_core.handle_system_mode("RESUME", {})

        self.assertTrue(ack["success"])
        self.assertEqual(ack["mode_state"], "REPLAY")


if __name__ == "__main__":
    unittest.main()
