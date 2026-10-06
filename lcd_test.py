"""Display two lines on a standard PCF8574-backed HD44780 LCD."""

import argparse
import fcntl
import sys
import time
from typing import BinaryIO

I2C_SLAVE = 0x0703
LCD_BACKLIGHT = 0x08
ENABLE = 0x04


class LCD1602:
    def __init__(self, bus: BinaryIO) -> None:
        self.bus = bus

    def write(self, bits: int) -> None:
        if self.bus.write(bytes([bits])) != 1:
            raise OSError("Incomplete I2C write")

    def nibble(self, bits: int) -> None:
        self.write(bits)
        time.sleep(0.0005)
        self.write(bits | ENABLE)
        time.sleep(0.0005)
        self.write(bits & ~ENABLE)
        time.sleep(0.0005)

    def byte(self, bits: int, mode: int = 0) -> None:
        self.nibble(mode | (bits & 0xF0) | LCD_BACKLIGHT)
        self.nibble(mode | ((bits << 4) & 0xF0) | LCD_BACKLIGHT)

    def initialize(self) -> None:
        time.sleep(0.05)
        # The power-on handshake uses single nibbles before entering 4-bit mode.
        for delay in (0.005, 0.005, 0.001):
            self.nibble(0x30 | LCD_BACKLIGHT)
            time.sleep(delay)
        self.nibble(0x20 | LCD_BACKLIGHT)
        for command in (0x28, 0x0C, 0x06, 0x01):
            self.byte(command)
        time.sleep(0.002)

    def line(self, message: str, row: int) -> None:
        if row not in (1, 2):
            raise ValueError("LCD row must be 1 or 2")
        if len(message) > 16 or any(not 32 <= ord(char) <= 126 for char in message):
            raise ValueError("LCD text must contain at most 16 printable ASCII characters")
        self.byte(0x80 if row == 1 else 0xC0)
        for character in message.ljust(16):
            self.byte(ord(character), 1)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bus", type=int, required=True, help="Verified I2C bus number")
    parser.add_argument("--address", type=lambda value: int(value, 0), required=True,
                        help="Verified backpack address, e.g. 0x27")
    args = parser.parse_args()
    if args.bus < 0:
        parser.error("--bus must be nonnegative")
    if not 0x08 <= args.address <= 0x77:
        parser.error("--address must be a non-reserved 7-bit I2C address")

    try:
        with open(f"/dev/i2c-{args.bus}", "wb", buffering=0) as bus:
            fcntl.ioctl(bus.fileno(), I2C_SLAVE, args.address)
            lcd = LCD1602(bus)
            lcd.initialize()
            lcd.line("UNCLE JESSE'S", 1)
            lcd.line("ALWAYS AVAILABLE", 2)
    except OSError as error:
        print(f"Hardware error on bus {args.bus}, address {args.address:#04x}: {error}",
              file=sys.stderr)
        return 1
    print(f"Test text sent to LCD on bus {args.bus}, address {args.address:#04x}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
