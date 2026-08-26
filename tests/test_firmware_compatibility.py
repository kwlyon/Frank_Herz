from __future__ import annotations

from pathlib import Path
import unittest


SKETCH = (
    Path(__file__).resolve().parents[1]
    / "arduino"
    / "Frank_Herz_DAQ"
    / "Frank_Herz_DAQ.ino"
)


class FirmwareArchitectureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = SKETCH.read_text(encoding="utf-8")

    def test_new_hardware_protocol_has_no_legacy_gain_or_stream_paths(self) -> None:
        self.assertIn(
            '#protocol,modern-lab-dual-channel,4',
            self.source,
        )
        obsolete_markers = (
            "SoftwareSerial",
            "LEGACY_MODE",
            "LEGACY_1X_CHANNEL",
            "LEGACY_10X_CHANNEL",
            '"1x"',
            '"10x"',
            '"mode,paired"',
            '"mode,legacy"',
            "scaleFactor",
            "SCALE_ADDR",
        )
        for marker in obsolete_markers:
            with self.subTest(marker=marker):
                self.assertNotIn(marker, self.source)

    def test_two_bnc_channels_are_sampled_with_independent_pga_ranges(self) -> None:
        required_markers = (
            "const uint8_t ADS1115_I2C_ADDRESS = 0x49;",
            "ads.begin(ADS1115_I2C_ADDRESS)",
            "const uint8_t CHANNEL_A_INPUT = 0;",
            "const uint8_t CHANNEL_B_INPUT = 2;",
            "uint8_t activeRange[2]",
            "ads.setGain(gainForRange(sampledRangeA));",
            "ads.readADC_SingleEnded(CHANNEL_A_INPUT)",
            "ads.setGain(gainForRange(sampledRangeB));",
            "ads.readADC_SingleEnded(CHANNEL_B_INPUT)",
        )
        for marker in required_markers:
            with self.subTest(marker=marker):
                self.assertIn(marker, self.source)

    def test_all_ads1115_ranges_and_autorange_hysteresis_are_present(self) -> None:
        required_markers = (
            "GAIN_TWOTHIRDS",
            "GAIN_ONE",
            "GAIN_TWO",
            "GAIN_FOUR",
            "GAIN_EIGHT",
            "GAIN_SIXTEEN",
            "AUTORANGE_WIDEN_COUNTS",
            "AUTORANGE_SATURATION_COUNTS",
            "AUTORANGE_NARROW_FRACTION",
            "AUTORANGE_NARROW_RECORDS",
            "AUTORANGE_COOLDOWN_RECORDS",
            "updateChannelAutorange(0, maximumA)",
            "updateChannelAutorange(1, maximumB)",
        )
        for marker in required_markers:
            with self.subTest(marker=marker):
                self.assertIn(marker, self.source)

    def test_voltage_math_uses_sampled_range_then_channel_divider(self) -> None:
        required_markers = (
            "fullScaleVoltsForRange(rangeIndex) / 32768.0",
            "rawCount * voltsPerCountForRange(rangeIndex)",
            "dividerMultiplier(highSide, lowSide)",
            "externalVoltage(averagedA, sampledRangeA, RAhigh, RAlow)",
            "externalVoltage(averagedB, sampledRangeB, RBhigh, RBlow)",
        )
        for marker in required_markers:
            with self.subTest(marker=marker):
                self.assertIn(marker, self.source)

    def test_divider_eeprom_is_versioned_validated_and_write_aware(self) -> None:
        required_markers = (
            "const uint32_t CONFIG_MAGIC",
            "const uint16_t CONFIG_VERSION = 2;",
            "struct DividerConfig",
            "dividerConfigChecksum",
            "dividerConfigIsValid",
            "const bool unchanged =",
            "if (!unchanged)",
            "EEPROM.put(CONFIG_EEPROM_ADDRESS, config);",
            "restoreDefaultDividers(true);",
        )
        for marker in required_markers:
            with self.subTest(marker=marker):
                self.assertIn(marker, self.source)

    def test_serial_interface_reports_and_controls_range_and_dividers(self) -> None:
        required_markers = (
            'strcmp(command, "range?") == 0',
            'strcmp(command, "autorange?") == 0',
            'strcmp(name, "range") == 0',
            'strcmp(name, "autorange") == 0',
            'strcmp(command, "dividers?") == 0',
            'strcmp(command, "defaults") == 0',
            'strcmp(name, "RAhigh") == 0',
            'strcmp(name, "RAlow") == 0',
            'strcmp(name, "RBhigh") == 0',
            'strcmp(name, "RBlow") == 0',
            'Serial.print(F("#range,A="));',
            'Serial.print(F("#autorange,"));',
        )
        for marker in required_markers:
            with self.subTest(marker=marker):
                self.assertIn(marker, self.source)


if __name__ == "__main__":
    unittest.main()
