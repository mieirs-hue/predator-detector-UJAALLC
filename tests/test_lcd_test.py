import io
import unittest
from unittest.mock import patch

from lcd_test import LCD1602, main


class LCDTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sleep = patch("lcd_test.time.sleep")
        self.sleep.start()
        self.addCleanup(self.sleep.stop)
        self.bus = io.BytesIO()
        self.lcd = LCD1602(self.bus)

    def test_byte_pulses_high_then_low_nibbles(self) -> None:
        self.lcd.byte(0x41, 1)
        self.assertEqual(self.bus.getvalue(), bytes([0x49, 0x4D, 0x49, 0x19, 0x1D, 0x19]))

    def test_initialize_uses_power_on_handshake(self) -> None:
        self.lcd.initialize()
        self.assertEqual(self.bus.getvalue()[:12],
                         bytes([0x38, 0x3C, 0x38] * 3 + [0x28, 0x2C, 0x28]))
        self.assertEqual(len(self.bus.getvalue()), 36)

    def test_lines_are_padded_and_addressed(self) -> None:
        for row, address in ((1, 0x80), (2, 0xC0)):
            with self.subTest(row=row):
                self.bus.seek(0)
                self.bus.truncate()
                with patch.object(self.lcd, "byte") as send:
                    self.lcd.line("UNCLE JESSE'S", row)
                self.assertEqual(send.call_args_list[0].args, (address,))
                self.assertEqual([call.args for call in send.call_args_list[1:]],
                                 [(ord(char), 1) for char in "UNCLE JESSE'S".ljust(16)])

    def test_invalid_text_or_row_never_writes(self) -> None:
        for message, row in (("X" * 17, 1), ("\n", 1), ("OK", 3)):
            with self.subTest(message=message, row=row), self.assertRaises(ValueError):
                self.lcd.line(message, row)
        self.assertEqual(self.bus.getvalue(), b"")

    def test_failed_write_is_reported(self) -> None:
        with patch.object(self.bus, "write", return_value=0), self.assertRaises(OSError):
            self.lcd.byte(0x41)

    def test_missing_hardware_returns_failure(self) -> None:
        with patch("sys.argv", ["lcd_test.py", "--bus", "7", "--address", "0x27"]), \
                patch("builtins.open", side_effect=OSError("No device")), \
                patch("sys.stderr", new_callable=io.StringIO) as stderr:
            self.assertEqual(main(), 1)
        self.assertIn("Hardware error on bus 7, address 0x27", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
