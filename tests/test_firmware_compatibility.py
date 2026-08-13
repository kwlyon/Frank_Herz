from __future__ import annotations

from pathlib import Path
import unittest


SKETCH = (
    Path(__file__).resolve().parents[1]
    / "arduino"
    / "Frank_Herz_DAQ"
    / "Frank_Herz_DAQ.ino"
)


class FirmwareCompatibilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = SKETCH.read_text(encoding="utf-8")

    def test_original_v4_hardware_and_transport_contract_is_retained(self) -> None:
        required_markers = (
            "const uint32_t HW_BAUD = 115200;",
            "const uint32_t SW_BAUD = 9600;",
            "const uint8_t SW_RX_PIN = A0;",
            "const uint8_t SW_TX_PIN = A1;",
            "SoftwareSerial swSer(SW_RX_PIN, SW_TX_PIN);",
            'const char DEVICE_NAME[] = "Modern Lab Data Acquisition Shield";',
            "const uint8_t ADS1115_ADDRESS = 0x49;",
            "ads.setGain(GAIN_TWO);",
            "analogReference(EXTERNAL);",
            "EEPROM.get(SCALE_ADDR, stored);",
            "EEPROM.put(SCALE_ADDR, stored);",
            "swSer.print(line);",
        )
        for marker in required_markers:
            with self.subTest(marker=marker):
                self.assertIn(marker, self.source)

    def test_legacy_mode_is_default_and_keeps_the_original_j1_gain_paths(self) -> None:
        required_markers = (
            "StreamMode streamMode = LEGACY_MODE;",
            "const uint8_t LEGACY_1X_CHANNEL = 0;",
            "const uint8_t LEGACY_10X_CHANNEL = 1;",
            "legacyChannel = LEGACY_1X_CHANNEL;",
            "legacyChannel = LEGACY_10X_CHANNEL;",
            'Serial.print(value, 6);',
        )
        for marker in required_markers:
            with self.subTest(marker=marker):
                self.assertIn(marker, self.source)

    def test_paired_mode_uses_the_two_independent_bnc_paths(self) -> None:
        self.assertIn("const uint8_t DRIVE_CHANNEL = 0;", self.source)
        self.assertIn("const uint8_t CURRENT_CHANNEL = 2;", self.source)
        self.assertIn('line.equalsIgnoreCase("mode,paired")', self.source)
        self.assertIn('Serial.println(F("#mode,paired"));', self.source)


if __name__ == "__main__":
    unittest.main()
