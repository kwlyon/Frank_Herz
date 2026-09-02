from __future__ import annotations

import math
import queue
import time
import unittest

from frank_herz import config
from frank_herz.core import AcquisitionController
from frank_herz.transport import SimulatorTransport


class SimulatorTests(unittest.TestCase):
    def test_mercury_model_sweeps_thirty_volts_with_4_9_volt_peak_spacing(self) -> None:
        simulator = SimulatorTransport(lambda line: None, lambda error: None)
        simulator._range_indices[0] = config.ADC_RANGE_VOLTS.index(4.096)

        raw = simulator._make_sample(config.SIMULATOR_SWEEP_PERIOD_SECONDS / 2.0)
        fields = raw.decode("ascii").split(",")
        self.assertAlmostEqual(float(fields[4]), 30.0, places=3)

        voltages = [index / 100.0 for index in range(3_000)]
        currents = [simulator._ideal_current_pa(voltage) for voltage in voltages]
        maxima = [
            voltages[index]
            for index in range(1, len(voltages) - 1)
            if currents[index - 1] < currents[index] > currents[index + 1]
        ]
        self.assertGreaterEqual(len(maxima), 5)
        for left, right in zip(maxima, maxima[1:]):
            self.assertAlmostEqual(right - left, 4.9, delta=0.15)

    def test_mercury_model_has_rising_baseline_and_small_random_noise(self) -> None:
        simulator = SimulatorTransport(lambda line: None, lambda error: None)
        self.assertGreater(
            simulator._baseline_current_pa(30.0),
            simulator._baseline_current_pa(10.0),
        )

        ideal = simulator._ideal_current_pa(15.0)
        noisy_currents: list[float] = []
        for _ in range(200):
            raw = simulator._make_sample(
                config.SIMULATOR_SWEEP_PERIOD_SECONDS / 4.0
            )
            current_input_v = float(raw.decode("ascii").split(",")[5])
            noisy_currents.append(
                current_input_v * 1000.0 / config.PICOAMMETER_MV_PER_PA
            )
        rms_noise = math.sqrt(
            sum((current - ideal) ** 2 for current in noisy_currents)
            / len(noisy_currents)
        )
        self.assertGreater(rms_noise, 1.0)
        self.assertLess(rms_noise, 3.0)

    def test_production_protocol_pause_resume_and_disconnect(self) -> None:
        lines: queue.Queue[bytes] = queue.Queue()
        errors: queue.Queue[str] = queue.Queue()
        simulator = SimulatorTransport(lines.put, errors.put)
        controller = AcquisitionController(simulator.write)
        simulator.open()
        self.addCleanup(simulator.close)

        self.assertEqual(lines.get(timeout=1.0).decode(), config.HANDSHAKE_BANNER)
        self.assertEqual(lines.get(timeout=1.0).decode(), config.PROTOCOL_CAPABILITY)
        self.assertEqual(lines.get(timeout=1.0), b"#autorange,1")
        self.assertTrue(lines.get(timeout=1.0).startswith(b"#range,"))
        self.assertTrue(lines.get(timeout=1.0).startswith(b"#dividers,"))

        controller.mark_device_ready()
        simulator.write(b"delay,10\n")
        controller.start()
        deadline = time.monotonic() + 1.0
        while len(controller.dataset) < 3 and time.monotonic() < deadline:
            line = lines.get(timeout=0.5)
            if line.startswith(b"DATA,"):
                controller.ingest(line)
        self.assertGreaterEqual(len(controller.dataset), 3)

        controller.stop()
        paused_count = len(controller.dataset)
        time.sleep(0.06)
        while not lines.empty():
            controller.ingest(lines.get_nowait())
        self.assertEqual(len(controller.dataset), paused_count)

        controller.start()
        deadline = time.monotonic() + 1.0
        while len(controller.dataset) == paused_count and time.monotonic() < deadline:
            line = lines.get(timeout=0.5)
            if line.startswith(b"DATA,"):
                controller.ingest(line)
        self.assertGreater(len(controller.dataset), paused_count)

        simulator.simulate_disconnect()
        self.assertIn("interrupted", errors.get(timeout=1.0).lower())
        controller.disconnect()
        self.assertFalse(controller.running)

    def test_manual_ranges_are_independent_and_require_autorange_off(self) -> None:
        lines: queue.Queue[bytes] = queue.Queue()
        simulator = SimulatorTransport(lines.put, lambda error: None)
        simulator.open()
        self.addCleanup(simulator.close)
        for _ in range(5):
            lines.get(timeout=1.0)

        simulator.write(b"range,A,1.024\n")
        self.assertTrue(lines.get(timeout=1.0).startswith(b"ERR,AUTORANGE_ENABLED"))
        simulator.write(b"autorange,0\n")
        self.assertEqual(lines.get(timeout=1.0), b"OK,autorange,0")
        self.assertEqual(lines.get(timeout=1.0), b"#autorange,0")
        lines.get(timeout=1.0)  # current range status

        simulator.write(b"range,A,1.024\n")
        self.assertEqual(lines.get(timeout=1.0), b"OK,range,a,1.024")
        self.assertEqual(lines.get(timeout=1.0), b"#range,A=1.024,B=2.048")
        simulator.write(b"range,B,0.512\n")
        self.assertEqual(lines.get(timeout=1.0), b"OK,range,b,0.512")
        self.assertEqual(lines.get(timeout=1.0), b"#range,A=1.024,B=0.512")
        self.assertEqual(simulator.active_ranges, (1.024, 0.512))

    def test_autorange_widens_before_clipping_and_channels_are_independent(self) -> None:
        simulator = SimulatorTransport(lambda line: None, lambda error: None)
        simulator._range_indices[:] = [3, 4]  # 1.024 V and 0.512 V
        changed = simulator._update_channel_autorange(0, 30_000.0)
        self.assertTrue(changed)
        self.assertEqual(simulator.active_ranges, (2.048, 0.512))

    def test_autorange_narrows_only_after_persistence_and_does_not_chatter(self) -> None:
        simulator = SimulatorTransport(lambda line: None, lambda error: None)
        simulator._range_indices[0] = config.ADC_RANGE_VOLTS.index(2.048)
        small_counts = 3_200.0
        for _ in range(config.AUTORANGE_NARROW_RECORDS - 1):
            self.assertFalse(simulator._update_channel_autorange(0, small_counts))
        self.assertTrue(simulator._update_channel_autorange(0, small_counts))
        self.assertEqual(simulator.active_ranges[0], 1.024)

        # A midrange signal neither approaches 90% nor qualifies for a still
        # narrower range, so repeated records cannot bounce the PGA setting.
        stable_counts = 16_000.0
        for _ in range(100):
            self.assertFalse(simulator._update_channel_autorange(0, stable_counts))
        self.assertEqual(simulator.active_ranges[0], 1.024)

    def test_reported_voltage_is_range_independent(self) -> None:
        simulator = SimulatorTransport(lambda line: None, lambda error: None)
        elapsed = config.SIMULATOR_SWEEP_PERIOD_SECONDS / 6.0  # 10 V
        measured: list[float] = []
        for range_v in (4.096, 2.048, 1.024):
            simulator._range_indices[0] = config.ADC_RANGE_VOLTS.index(range_v)
            fields = simulator._make_sample(elapsed).decode().split(",")
            measured.append(float(fields[4]))
        for voltage in measured:
            self.assertAlmostEqual(voltage, 10.0, delta=0.002)

    def test_removed_gain_and_mode_commands_are_rejected(self) -> None:
        lines: queue.Queue[bytes] = queue.Queue()
        simulator = SimulatorTransport(lines.put, lambda error: None)
        simulator.open()
        self.addCleanup(simulator.close)
        for _ in range(5):
            lines.get(timeout=1.0)
        for command in (b"1x\n", b"10x\n", b"mode,legacy\n", b"mode,paired\n"):
            simulator.write(command)
            self.assertEqual(lines.get(timeout=1.0), b"ERR,UNKNOWN_COMMAND")


if __name__ == "__main__":
    unittest.main()
