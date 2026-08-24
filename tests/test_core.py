from __future__ import annotations

import unittest

from frank_herz import config
from frank_herz.core import (
    AcquisitionController,
    Calibration,
    DataPoint,
    Dataset,
    ProtocolError,
    convert_sample,
    downsample_for_display,
    downsample_for_strip_recorder,
    nearest_xy_point,
    parse_data_line,
)


class ProtocolTests(unittest.TestCase):
    def test_paired_record_and_unit_conversion(self) -> None:
        raw = parse_data_line("DATA,125,1600,8000,0.100000,0.500000")
        point = convert_sample(
            raw,
            Calibration(
                drive_scale=40.0,
                drive_offset_v=0.5,
                picoammeter_mv_per_pa=2.0,
                picoammeter_zero_v=0.1,
                picoammeter_polarity=-1.0,
            ),
        )
        self.assertEqual(point.elapsed_ms, 125)
        self.assertAlmostEqual(point.drive_voltage_v, 4.5)
        self.assertAlmostEqual(point.tube_current_pa, -200.0)
        self.assertEqual(point.drive_adc_counts, 1600)
        self.assertEqual(point.current_adc_counts, 8000)

    def test_malformed_and_out_of_range_records_are_rejected(self) -> None:
        bad_records = (
            "",
            "DATA,1,2",
            "DATA,time,1,2,0.1,0.2",
            "DATA,1,40000,2,0.1,0.2",
            "DATA,1,1,2,3.0,0.2",
            "OTHER,1,1,2,0.1,0.2",
        )
        for record in bad_records:
            with self.subTest(record=record):
                with self.assertRaises(ProtocolError):
                    parse_data_line(record)


class AcquisitionStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sent: list[bytes] = []
        self.controller = AcquisitionController(self.sent.append)
        self.controller.mark_device_ready()

    def test_stop_retains_and_resume_appends(self) -> None:
        self.controller.start()
        self.controller.ingest("DATA,10,1600,3200,0.100000,0.200000")
        self.controller.stop()
        retained = self.controller.dataset.snapshot()
        self.assertEqual(len(retained), 1)
        self.controller.ingest("DATA,20,3200,6400,0.200000,0.400000")
        self.assertEqual(len(self.controller.dataset), 1, "paused data must be ignored")

        self.controller.start()
        self.controller.ingest("DATA,30,3200,6400,0.200000,0.400000")
        self.assertEqual(len(self.controller.dataset), 2)
        self.assertEqual(
            self.sent,
            [config.START_COMMAND, config.STOP_COMMAND, config.START_COMMAND],
        )

    def test_clear_requires_confirmation_and_erases_only_after_yes(self) -> None:
        self.controller.start()
        self.controller.ingest("DATA,10,1600,3200,0.100000,0.200000")
        confirmations: list[bool] = []

        def reject() -> bool:
            confirmations.append(True)
            return False

        self.assertFalse(self.controller.confirm_and_clear(reject))
        self.assertEqual(len(self.controller.dataset), 1)
        self.assertEqual(len(confirmations), 1)
        self.assertTrue(self.controller.confirm_and_clear(lambda: True))
        self.assertEqual(len(self.controller.dataset), 0)
        self.assertTrue(self.controller.running, "clearing must not change acquisition state")

    def test_malformed_data_cannot_crash_or_append(self) -> None:
        self.controller.start()
        self.assertIsNone(self.controller.ingest("garbage"))
        self.assertEqual(self.controller.malformed_lines, 1)
        self.assertEqual(len(self.controller.dataset), 0)

    def test_disconnection_stops_state_but_preserves_dataset(self) -> None:
        self.controller.start()
        self.controller.ingest("DATA,10,1600,3200,0.100000,0.200000")
        self.controller.disconnect()
        self.assertFalse(self.controller.device_ready)
        self.assertFalse(self.controller.running)
        self.assertEqual(len(self.controller.dataset), 1)

    def test_dataset_safety_limit_is_explicit(self) -> None:
        dataset = Dataset(max_points=1)
        raw = parse_data_line("DATA,1,1600,3200,0.100000,0.200000")
        dataset.append(convert_sample(raw, Calibration()))
        with self.assertRaises(OverflowError):
            dataset.append(convert_sample(raw, Calibration()))


class DisplayDownsamplingTests(unittest.TestCase):
    def test_xy_and_strip_views_select_the_same_points(self) -> None:
        points = tuple(
            DataPoint(
                elapsed_ms=index * 250,
                drive_voltage_v=float(index),
                tube_current_pa=float(index * 10),
                drive_adc_v=0.0,
                current_adc_v=0.0,
                drive_adc_counts=0.0,
                current_adc_counts=0.0,
            )
            for index in range(7)
        )

        drive_xy, current_xy = downsample_for_display(points, maximum=3)
        times, drive_strip, current_strip = downsample_for_strip_recorder(
            points, maximum=3
        )

        self.assertEqual(drive_xy, [0.0, 3.0, 6.0])
        self.assertEqual(current_xy, [0.0, 30.0, 60.0])
        self.assertEqual(times, [0.0, 0.75, 1.5])
        self.assertEqual(drive_strip, drive_xy)
        self.assertEqual(current_strip, current_xy)

    def test_measurement_cursor_selects_the_nearest_plotted_drive_voltage(self) -> None:
        drive = [0.0, 1.5, 3.0, 4.5]
        current = [10.0, 30.0, 20.0, 40.0]

        self.assertEqual(nearest_xy_point(drive, current, 2.8), (3.0, 20.0))
        self.assertEqual(nearest_xy_point(drive, current, -5.0), (0.0, 10.0))
        self.assertEqual(nearest_xy_point([], [], 1.0), None)

    def test_measurement_cursor_ties_retain_acquisition_order(self) -> None:
        self.assertEqual(
            nearest_xy_point([1.0, 3.0], [15.0, 35.0], 2.0),
            (1.0, 15.0),
        )
        with self.assertRaises(ValueError):
            nearest_xy_point([1.0], [], 1.0)


if __name__ == "__main__":
    unittest.main()
