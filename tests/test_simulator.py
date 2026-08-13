from __future__ import annotations

import queue
import time
import unittest

from frank_herz import config
from frank_herz.core import AcquisitionController
from frank_herz.transport import SimulatorTransport


class SimulatorTests(unittest.TestCase):
    def test_production_protocol_pause_resume_and_disconnect(self) -> None:
        lines: queue.Queue[bytes] = queue.Queue()
        errors: queue.Queue[str] = queue.Queue()
        simulator = SimulatorTransport(lines.put, errors.put)
        controller = AcquisitionController(simulator.write)
        simulator.open()
        self.addCleanup(simulator.close)

        banner = lines.get(timeout=1.0)
        self.assertEqual(banner.decode("ascii"), config.HANDSHAKE_BANNER)
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


if __name__ == "__main__":
    unittest.main()
