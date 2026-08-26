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
        simulator._mode = "paired"

        # Halfway through the triangular period is the 30 V endpoint.
        raw = simulator._make_sample(config.SIMULATOR_SWEEP_PERIOD_SECONDS / 2.0)
        fields = raw.decode("ascii").split(",")
        drive_adc_v = float(fields[4])
        displayed_drive_v = (
            drive_adc_v * config.DRIVE_VOLTAGE_SCALE
            + config.DRIVE_VOLTAGE_OFFSET_V
        )
        self.assertAlmostEqual(displayed_drive_v, 30.0, places=6)

        # Locate the maxima of the noise-free curve at fine voltage spacing.
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
        simulator._mode = "paired"
        self.assertGreater(
            simulator._baseline_current_pa(30.0),
            simulator._baseline_current_pa(10.0),
        )

        simulator._averages = config.DEFAULT_AVERAGES
        ideal = simulator._ideal_current_pa(15.0)
        noisy_currents: list[float] = []
        for _ in range(200):
            # A quarter period is 15 V on the upward sweep.
            raw = simulator._make_sample(
                config.SIMULATOR_SWEEP_PERIOD_SECONDS / 4.0
            )
            current_adc_v = float(raw.decode("ascii").split(",")[5])
            noisy_currents.append(current_adc_v * 1000.0)
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

        banner = lines.get(timeout=1.0)
        self.assertEqual(banner.decode("ascii"), config.HANDSHAKE_BANNER)
        capability = lines.get(timeout=1.0)
        self.assertEqual(capability.decode("ascii"), config.PROTOCOL_CAPABILITY)
        simulator.write(config.PAIRED_MODE_COMMAND)
        self.assertEqual(
            lines.get(timeout=1.0).decode("ascii"), config.PAIRED_MODE_ACK
        )
        controller.mark_device_ready()
        simulator.write(b"delay,10\n")
        controller.start()
        deadline = time.monotonic() + 1.0
        while len(controller.dataset) < 3 and time.monotonic() < deadline:
            controller.ingest(lines.get(timeout=0.5))
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
            controller.ingest(lines.get(timeout=0.5))
        self.assertGreater(len(controller.dataset), paused_count)

        simulator.simulate_disconnect()
        self.assertIn("interrupted", errors.get(timeout=1.0).lower())
        controller.disconnect()
        self.assertFalse(controller.running)
        self.assertGreater(len(controller.dataset), 0)

    def test_legacy_serialplotter_mode_is_default_and_gain_commands_select_it(self) -> None:
        lines: queue.Queue[bytes] = queue.Queue()
        simulator = SimulatorTransport(lines.put, lambda error: None)
        simulator.open()
        self.addCleanup(simulator.close)
        lines.get(timeout=1.0)  # shared banner
        lines.get(timeout=1.0)  # capability (ignored by SerialPlotter)

        # This is the command sequence recorder4.py sends after its handshake.
        simulator.write(b"1x\n")
        simulator.write(b"avg,100\n")
        simulator.write(b"run\n")
        deadline = time.monotonic() + 1.0
        data_line = ""
        while time.monotonic() < deadline:
            candidate = lines.get(timeout=0.5).decode("ascii")
            if not candidate.startswith("#"):
                data_line = candidate
                break
        fields = data_line.split(",")
        self.assertEqual(len(fields), 2)
        float(fields[0])
        float(fields[1])

        simulator.write(b"mode,paired\n")
        self.assertEqual(lines.get(timeout=1.0), b"#mode,paired")
        simulator.write(b"10x\n")  # legacy command must always restore legacy mode
        self.assertEqual(lines.get(timeout=1.0), b"#10x")


if __name__ == "__main__":
    unittest.main()
