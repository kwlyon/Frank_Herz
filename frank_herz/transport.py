"""Physical serial and protocol-compatible simulated transports."""

from __future__ import annotations

import math
import random
import threading
import time
from typing import Callable

import serial

from . import config

LineCallback = Callable[[bytes], None]
ErrorCallback = Callable[[str], None]


class SerialTransport:
    """Read newline-framed serial records on a background thread."""

    def __init__(self, on_line: LineCallback, on_error: ErrorCallback) -> None:
        self._on_line = on_line
        self._on_error = on_error
        self._serial: serial.Serial | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._buffer = bytearray()
        self._error_reported = False

    def open(self, port: str, baud: int = config.BAUD_RATE) -> None:
        self.close()
        try:
            connection = serial.Serial(port=port, baudrate=baud, timeout=0.1)
        except (OSError, serial.SerialException) as exc:
            raise ConnectionError(f"Could not open {port}: {exc}") from exc
        with self._lock:
            self._serial = connection
        self._error_reported = False
        self._buffer.clear()
        self._stop.clear()
        self._thread = threading.Thread(target=self._read_loop, daemon=True)
        self._thread.start()

    def is_open(self) -> bool:
        with self._lock:
            return bool(self._serial and self._serial.is_open)

    def write(self, data: bytes) -> None:
        try:
            with self._lock:
                if not self._serial or not self._serial.is_open:
                    raise ConnectionError("The serial port is closed.")
                self._serial.write(data)
        except (OSError, serial.SerialException) as exc:
            self._fail(f"Serial write failed: {exc}")
            raise ConnectionError(str(exc)) from exc

    def close(self) -> None:
        self._stop.set()
        with self._lock:
            connection = self._serial
            self._serial = None
        if connection is not None:
            try:
                connection.close()
            except (OSError, serial.SerialException):
                pass
        thread = self._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=0.5)
        self._thread = None
        self._buffer.clear()

    def _read_loop(self) -> None:
        while not self._stop.is_set():
            try:
                with self._lock:
                    connection = self._serial
                if not connection or not connection.is_open:
                    break
                chunk = connection.read(config.SERIAL_READ_CHUNK)
                if not chunk:
                    continue
                self._buffer.extend(chunk)
                while True:
                    newline = self._buffer.find(b"\n")
                    if newline < 0:
                        break
                    line = bytes(self._buffer[:newline]).rstrip(b"\r")
                    del self._buffer[: newline + 1]
                    self._on_line(line)
            except (OSError, serial.SerialException) as exc:
                if not self._stop.is_set():
                    self._fail(f"Serial communication interrupted: {exc}")
                break
            except Exception as exc:  # keep an unexpected driver error out of Tk
                if not self._stop.is_set():
                    self._fail(f"Unexpected serial error: {exc}")
                break

    def _fail(self, message: str) -> None:
        if self._error_reported:
            return
        self._error_reported = True
        self._stop.set()
        with self._lock:
            connection = self._serial
            self._serial = None
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass
        self._on_error(message)


class SimulatorTransport:
    """Generate realistic-looking paired records using the production protocol."""

    def __init__(self, on_line: LineCallback, on_error: ErrorCallback) -> None:
        self._on_line = on_line
        self._on_error = on_error
        self._stop = threading.Event()
        self._running = threading.Event()
        self._thread: threading.Thread | None = None
        self._opened = False
        self._started_at = 0.0
        self._rng = random.Random(814_1935)
        self._averages = config.DEFAULT_AVERAGES
        self._interval_ms = config.DEFAULT_SAMPLE_INTERVAL_MS
        self._mode = "legacy"
        self._legacy_channel = 0

    def open(self, port: str = config.SIMULATOR_PORT, baud: int = config.BAUD_RATE) -> None:
        del port, baud
        self.close()
        self._stop.clear()
        self._running.clear()
        self._opened = True
        self._started_at = time.perf_counter()
        self._mode = "legacy"
        self._legacy_channel = 0
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        self._on_line(config.HANDSHAKE_BANNER.encode("ascii"))
        self._on_line(config.PROTOCOL_CAPABILITY.encode("ascii"))

    def is_open(self) -> bool:
        return self._opened

    def write(self, data: bytes) -> None:
        if not self._opened:
            raise ConnectionError("The simulator is disconnected.")
        command = data.decode("ascii", errors="ignore").strip().lower()
        if command == "run":
            self._running.set()
            self._on_line(b"#run")
        elif command == "stop":
            self._running.clear()
            self._on_line(b"#stop")
        elif command.startswith("avg,"):
            try:
                requested = int(command.split(",", 1)[1])
                self._averages = max(1, min(65_535, requested))
                self._on_line(f"#avg{self._averages}".encode("ascii"))
            except ValueError:
                self._on_line(b"ERR,BAD_AVG")
        elif command.startswith("delay,"):
            try:
                self._interval_ms = max(1, int(command.split(",", 1)[1]))
                self._on_line(f"#delay{self._interval_ms}".encode("ascii"))
            except ValueError:
                self._on_line(b"ERR,BAD_DELAY")
        elif command == "1x":
            self._mode = "legacy"
            self._legacy_channel = 0
            self._on_line(b"#1x")
        elif command == "10x":
            self._mode = "legacy"
            self._legacy_channel = 1
            self._on_line(b"#10x")
        elif command == "mode,legacy":
            self._mode = "legacy"
            self._on_line(b"#mode,legacy")
        elif command == "mode,paired":
            self._mode = "paired"
            self._on_line(config.PAIRED_MODE_ACK.encode("ascii"))
        elif command == "idn?":
            self._on_line(config.HANDSHAKE_BANNER.encode("ascii"))
            self._on_line(config.PROTOCOL_CAPABILITY.encode("ascii"))
        else:
            self._on_line(b"ERR,UNKNOWN_COMMAND")

    def close(self) -> None:
        self._stop.set()
        self._running.clear()
        self._opened = False
        thread = self._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=0.5)
        self._thread = None

    def simulate_disconnect(self) -> None:
        if not self._opened:
            return
        self.close()
        self._on_error("Simulator connection interrupted.")

    def _loop(self) -> None:
        while not self._stop.is_set():
            if not self._running.wait(timeout=0.05):
                continue
            elapsed = time.perf_counter() - self._started_at
            line = self._make_sample(elapsed)
            self._on_line(line)
            self._stop.wait(self._interval_ms / 1000.0)

    def _make_sample(self, elapsed_s: float) -> bytes:
        # Sweep the simulated 0--30 V monitor through the same ADC range and
        # production protocol used by the hardware.
        span_v = config.SIMULATOR_DRIVE_ADC_MAX_VOLTS
        phase = (
            elapsed_s % config.SIMULATOR_SWEEP_PERIOD_SECONDS
        ) / config.SIMULATOR_SWEEP_PERIOD_SECONDS
        triangle = phase * 2.0 if phase <= 0.5 else (1.0 - phase) * 2.0
        drive_adc_v = max(0.0, span_v * triangle)
        drive_actual_v = (
            drive_adc_v * config.DRIVE_VOLTAGE_SCALE
            + config.DRIVE_VOLTAGE_OFFSET_V
        )
        current_pa = self._ideal_current_pa(drive_actual_v)

        # This is the noise remaining after the requested ADC averages. At the
        # default of ten averages it is about 1.9 pA RMS: visible on the trace,
        # but small compared with the Franck-Hertz peak structure.
        noise = self._rng.gauss(0.0, 6.0 / math.sqrt(self._averages))
        current_pa += noise
        current_adc_v = (
            current_pa
            * config.PICOAMMETER_MV_PER_PA
            / 1000.0
            / config.PICOAMMETER_POLARITY
            + config.PICOAMMETER_ZERO_V
        )
        current_adc_v = min(config.ADS1115_FULL_SCALE_VOLTS * 0.95, max(0.0, current_adc_v))
        drive_raw = drive_adc_v / config.ADS1115_VOLTS_PER_COUNT
        current_raw = current_adc_v / config.ADS1115_VOLTS_PER_COUNT
        elapsed_ms = int(elapsed_s * 1000.0)
        if self._mode == "legacy":
            legacy_adc_v = drive_adc_v
            if self._legacy_channel == 1:
                legacy_adc_v = min(
                    config.ADS1115_FULL_SCALE_VOLTS, drive_adc_v * 10.0
                )
            selected_mv = legacy_adc_v * 1000.0
            return f"{elapsed_ms},{selected_mv:.6f}".encode("ascii")
        return (
            f"DATA,{elapsed_ms},{drive_raw:.3f},{current_raw:.3f},"
            f"{drive_adc_v:.6f},{current_adc_v:.6f}"
        ).encode("ascii")

    @staticmethod
    def _baseline_current_pa(drive_actual_v: float) -> float:
        """Monotonic tube current expected without inelastic-loss peaks."""

        voltage = max(0.0, drive_actual_v)
        # A V^(3/2) dependence is the Child-Langmuir space-charge trend. The
        # coefficients put the simulated picoammeter signal in a useful range.
        return 18.0 + 0.95 * voltage**1.5

    @classmethod
    def _ideal_current_pa(cls, drive_actual_v: float) -> float:
        """Return a smooth mercury Franck-Hertz characteristic before noise."""

        voltage = max(0.0, drive_actual_v)
        current_pa = cls._baseline_current_pa(voltage)
        peak_voltage = config.SIMULATOR_FIRST_PEAK_VOLTS

        # Broad peaks reproduce the oscilloscope-like envelope more closely
        # than a pure cosine. Their 4.9 V spacing represents mercury's first
        # excitation energy; their growth follows the increasing electron flux.
        while peak_voltage <= 30.0:
            width_v = 0.90 if voltage < peak_voltage else 1.20
            separation_v = (voltage - peak_voltage) / width_v
            peak_amplitude_pa = 38.0 + 1.65 * peak_voltage
            current_pa += peak_amplitude_pa * math.exp(
                -0.5 * separation_v * separation_v
            )
            peak_voltage += config.MERCURY_EXCITATION_VOLTS

        return current_pa
