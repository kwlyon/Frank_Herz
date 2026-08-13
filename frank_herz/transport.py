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

    def open(self, port: str = config.SIMULATOR_PORT, baud: int = config.BAUD_RATE) -> None:
        del port, baud
        self.close()
        self._stop.clear()
        self._running.clear()
        self._opened = True
        self._started_at = time.perf_counter()
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
                self._averages = max(1, min(1000, int(command.split(",", 1)[1])))
                self._on_line(f"#avg,{self._averages}".encode("ascii"))
            except ValueError:
                self._on_line(b"ERR,BAD_AVG")
        elif command.startswith("delay,"):
            try:
                self._interval_ms = max(10, min(10_000, int(command.split(",", 1)[1])))
                self._on_line(f"#delay,{self._interval_ms}".encode("ascii"))
            except ValueError:
                self._on_line(b"ERR,BAD_DELAY")
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
        # A triangular sweep stays within the ADS1115 input range. With a
        # configured divider scale, the displayed voltage expands accordingly.
        span_v = config.ADS1115_FULL_SCALE_VOLTS * 0.85
        phase = (elapsed_s % 12.0) / 12.0
        triangle = phase * 2.0 if phase <= 0.5 else (1.0 - phase) * 2.0
        drive_adc_v = max(0.0, span_v * triangle)
        drive_actual_v = (
            drive_adc_v * config.DRIVE_VOLTAGE_SCALE
            + config.DRIVE_VOLTAGE_OFFSET_V
        )
        displayed_span = max(0.2, span_v * abs(config.DRIVE_VOLTAGE_SCALE))
        spacing = min(4.9, displayed_span / 7.0)
        oscillation = 0.5 + 0.5 * math.cos(2.0 * math.pi * drive_actual_v / spacing)
        current_pa = 40.0 + 165.0 * drive_adc_v / span_v + 85.0 * oscillation
        noise = self._rng.gauss(0.0, 2.0 / math.sqrt(self._averages))
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
        return (
            f"DATA,{elapsed_ms},{drive_raw:.3f},{current_raw:.3f},"
            f"{drive_adc_v:.6f},{current_adc_v:.6f}"
        ).encode("ascii")
