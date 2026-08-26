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
            except Exception as exc:  # keep unexpected driver errors out of Tk
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
    """Generate dual-channel records using the production protocol."""

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
        self._autorange_enabled = True
        default_index = config.ADC_RANGE_VOLTS.index(config.DEFAULT_ADC_RANGE_VOLTS)
        self._range_indices = [default_index, default_index]
        self._narrow_persistence = [0, 0]
        self._range_cooldown = [0, 0]
        self._last_magnitudes = [0.0, 0.0]
        self._rahigh = 909_000
        self._ralow = 101_000
        self._rbhigh = 909_000
        self._rblow = 101_000

    @property
    def active_ranges(self) -> tuple[float, float]:
        return tuple(config.ADC_RANGE_VOLTS[index] for index in self._range_indices)

    @property
    def autorange_enabled(self) -> bool:
        return self._autorange_enabled

    def open(self, port: str = config.SIMULATOR_PORT, baud: int = config.BAUD_RATE) -> None:
        del port, baud
        self.close()
        self._stop.clear()
        self._running.clear()
        self._opened = True
        self._started_at = time.perf_counter()
        default_index = config.ADC_RANGE_VOLTS.index(config.DEFAULT_ADC_RANGE_VOLTS)
        self._range_indices[:] = [default_index, default_index]
        self._autorange_enabled = True
        self._reset_autorange_history()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        self._emit(config.HANDSHAKE_BANNER)
        self._emit(config.PROTOCOL_CAPABILITY)
        self._report_autorange()
        self._report_ranges()
        self._report_dividers()

    def is_open(self) -> bool:
        return self._opened

    def write(self, data: bytes) -> None:
        if not self._opened:
            raise ConnectionError("The simulator is disconnected.")
        original = data.decode("ascii", errors="ignore").strip()
        command = original.lower()
        if command == "run":
            self._running.set()
            self._emit("OK,run")
        elif command == "stop":
            self._running.clear()
            self._emit("OK,stop")
        elif command == "idn?":
            self._emit(config.HANDSHAKE_BANNER)
            self._emit(config.PROTOCOL_CAPABILITY)
        elif command == "autorange?":
            self._report_autorange()
            self._report_ranges()
        elif command.startswith("autorange,"):
            self._set_autorange(command)
        elif command == "range?":
            self._report_ranges()
        elif command.startswith("range,"):
            self._set_manual_range(command)
        elif command.startswith("avg,"):
            self._set_average(command)
        elif command.startswith("delay,"):
            self._set_delay(command)
        elif command == "dividers?":
            self._report_dividers()
        elif command == "defaults":
            self._rahigh, self._ralow = 909_000, 101_000
            self._rbhigh, self._rblow = 909_000, 101_000
            self._emit("OK,defaults")
            self._report_dividers()
        elif command.split(",", 1)[0] in {"rahigh", "ralow", "rbhigh", "rblow"}:
            self._set_divider(original)
        else:
            self._emit("ERR,UNKNOWN_COMMAND")

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

    def _emit(self, line: str) -> None:
        self._on_line(line.encode("ascii"))

    def _report_autorange(self) -> None:
        self._emit(f"#autorange,{int(self._autorange_enabled)}")

    def _report_ranges(self) -> None:
        channel_a, channel_b = self.active_ranges
        self._emit(f"#range,A={channel_a:.3f},B={channel_b:.3f}")

    def _report_dividers(self) -> None:
        multiplier_a = (self._rahigh + self._ralow) / self._ralow
        multiplier_b = (self._rbhigh + self._rblow) / self._rblow
        range_a, range_b = self.active_ranges
        self._emit(
            f"#dividers,RAhigh={self._rahigh},RAlow={self._ralow},"
            f"RBhigh={self._rbhigh},RBlow={self._rblow},"
            f"A_multiplier={multiplier_a:.6f},B_multiplier={multiplier_b:.6f},"
            f"A_full_scale_v={range_a * multiplier_a:.6f},"
            f"B_full_scale_v={range_b * multiplier_b:.6f}"
        )

    def _set_autorange(self, command: str) -> None:
        value = command.split(",", 1)[1]
        if value not in {"0", "1"}:
            self._emit("ERR,AUTORANGE_FORMAT,use autorange,0 or autorange,1")
            return
        self._autorange_enabled = value == "1"
        self._reset_autorange_history()
        self._emit(f"OK,autorange,{value}")
        self._report_autorange()
        self._report_ranges()

    def _set_manual_range(self, command: str) -> None:
        if self._autorange_enabled:
            self._emit(
                "ERR,AUTORANGE_ENABLED,disable autorange before setting a manual range"
            )
            return
        fields = command.split(",")
        if len(fields) != 3:
            self._emit("ERR,RANGE_FORMAT,use range,A|B|both,<range>")
            return
        channel, requested_text = fields[1:]
        try:
            requested = float(requested_text)
            index = next(
                index
                for index, supported in enumerate(config.ADC_RANGE_VOLTS)
                if math.isclose(requested, supported, abs_tol=0.000_5)
            )
        except (ValueError, StopIteration):
            self._emit("ERR,INVALID_RANGE")
            return
        if channel == "a":
            self._range_indices[0] = index
        elif channel == "b":
            self._range_indices[1] = index
        elif channel == "both":
            self._range_indices[:] = [index, index]
        else:
            self._emit("ERR,INVALID_CHANNEL,use A, B, or both")
            return
        self._reset_autorange_history()
        self._emit(f"OK,range,{channel},{requested:.3f}")
        self._report_ranges()

    def _set_average(self, command: str) -> None:
        try:
            requested = int(command.split(",", 1)[1])
            if not 1 <= requested <= 64:
                raise ValueError
        except ValueError:
            self._emit("ERR,INVALID_AVERAGE,valid range is 1..64")
            return
        self._averages = requested
        self._emit(f"OK,avg,{self._averages}")

    def _set_delay(self, command: str) -> None:
        try:
            requested = int(command.split(",", 1)[1])
            if not 0 <= requested <= 60_000:
                raise ValueError
        except ValueError:
            self._emit("ERR,INVALID_DELAY,valid range is 0..60000 ms")
            return
        self._interval_ms = requested
        self._emit(f"OK,delay,{self._interval_ms}")

    def _set_divider(self, command: str) -> None:
        fields = command.split(",")
        if len(fields) != 2:
            self._emit("ERR,INVALID_RESISTOR,expected an integer number of ohms")
            return
        name_lookup = {
            "rahigh": "_rahigh",
            "ralow": "_ralow",
            "rbhigh": "_rbhigh",
            "rblow": "_rblow",
        }
        key = fields[0].lower()
        try:
            value = int(fields[1])
            if not 1 <= value <= 100_000_000:
                raise ValueError
        except ValueError:
            self._emit("ERR,INVALID_RESISTOR")
            return
        values = {
            "_rahigh": self._rahigh,
            "_ralow": self._ralow,
            "_rbhigh": self._rbhigh,
            "_rblow": self._rblow,
        }
        values[name_lookup[key]] = value
        multiplier_a = (values["_rahigh"] + values["_ralow"]) / values["_ralow"]
        multiplier_b = (values["_rbhigh"] + values["_rblow"]) / values["_rblow"]
        if max(multiplier_a, multiplier_b) > config.MAX_DIVIDER_MULTIPLIER:
            self._emit("ERR,INVALID_RESISTOR")
            return
        setattr(self, name_lookup[key], value)
        canonical = {
            "rahigh": "RAhigh",
            "ralow": "RAlow",
            "rbhigh": "RBhigh",
            "rblow": "RBlow",
        }[key]
        self._emit(f"OK,{canonical},{value}")
        self._report_dividers()

    def _loop(self) -> None:
        while not self._stop.is_set():
            if not self._running.wait(timeout=0.05):
                continue
            elapsed = time.perf_counter() - self._started_at
            self._on_line(self._make_sample(elapsed))
            if self._update_autoranging():
                self._report_ranges()
            self._stop.wait(self._interval_ms / 1000.0)

    def _make_sample(self, elapsed_s: float) -> bytes:
        """Create one record with the ranges active at the start of conversion."""

        phase = (
            elapsed_s % config.SIMULATOR_SWEEP_PERIOD_SECONDS
        ) / config.SIMULATOR_SWEEP_PERIOD_SECONDS
        triangle = phase * 2.0 if phase <= 0.5 else (1.0 - phase) * 2.0
        drive_input_v = max(0.0, config.SIMULATOR_DRIVE_MAX_VOLTS * triangle)
        drive_multiplier = (self._rahigh + self._ralow) / self._ralow
        current_multiplier = (self._rbhigh + self._rblow) / self._rblow
        drive_adc_v = drive_input_v / drive_multiplier
        drive_actual_v = (
            drive_input_v * config.DRIVE_VOLTAGE_SCALE
            + config.DRIVE_VOLTAGE_OFFSET_V
        )
        current_pa = self._ideal_current_pa(drive_actual_v)

        # At ten averages this leaves about 1.9 pA RMS of visible experimental noise.
        current_pa += self._rng.gauss(0.0, 6.0 / math.sqrt(self._averages))
        current_input_v = (
            current_pa
            * config.PICOAMMETER_MV_PER_PA
            / 1000.0
            / config.PICOAMMETER_POLARITY
            + config.PICOAMMETER_ZERO_V
        )
        current_input_v = max(0.0, current_input_v)
        current_adc_v = current_input_v / current_multiplier

        range_a, range_b = self.active_ranges
        drive_raw = self._adc_counts(drive_adc_v, range_a)
        current_raw = self._adc_counts(current_adc_v, range_b)
        self._last_magnitudes[:] = [abs(drive_raw), abs(current_raw)]

        measured_drive_v = (
            drive_raw
            * range_a
            / 32768.0
            * drive_multiplier
        )
        measured_current_v = (
            current_raw
            * range_b
            / 32768.0
            * current_multiplier
        )
        elapsed_ms = int(elapsed_s * 1000.0)
        return (
            f"DATA,{elapsed_ms},{drive_raw:.3f},{current_raw:.3f},"
            f"{measured_drive_v:.6f},{measured_current_v:.6f}"
        ).encode("ascii")

    @staticmethod
    def _adc_counts(adc_voltage: float, range_v: float) -> float:
        return min(32767.0, max(-32768.0, adc_voltage / range_v * 32768.0))

    def _reset_autorange_history(self) -> None:
        self._narrow_persistence[:] = [0, 0]
        self._range_cooldown[:] = [0, 0]

    def _update_autoranging(self) -> bool:
        if not self._autorange_enabled:
            return False
        changed = False
        for channel in range(2):
            changed = self._update_channel_autorange(
                channel, self._last_magnitudes[channel]
            ) or changed
        return changed

    def _update_channel_autorange(self, channel: int, magnitude: float) -> bool:
        current = self._range_indices[channel]
        requested = current
        full_counts = 32768.0
        if (
            magnitude >= config.AUTORANGE_SATURATION_FRACTION * full_counts
            and current != 0
        ):
            requested = 0
        elif (
            magnitude >= config.AUTORANGE_WIDEN_FRACTION * full_counts
            and current > 0
        ):
            requested = current - 1

        if requested != current:
            self._range_indices[channel] = requested
            self._narrow_persistence[channel] = 0
            self._range_cooldown[channel] = config.AUTORANGE_COOLDOWN_RECORDS
            return True

        if self._range_cooldown[channel] > 0:
            self._range_cooldown[channel] -= 1
            self._narrow_persistence[channel] = 0
            return False

        if current + 1 >= len(config.ADC_RANGE_VOLTS):
            self._narrow_persistence[channel] = 0
            return False

        measured_v = magnitude * config.ADC_RANGE_VOLTS[current] / full_counts
        narrower_limit = (
            config.ADC_RANGE_VOLTS[current + 1]
            * config.AUTORANGE_NARROW_FRACTION
        )
        if measured_v <= narrower_limit:
            self._narrow_persistence[channel] += 1
            if self._narrow_persistence[channel] >= config.AUTORANGE_NARROW_RECORDS:
                self._range_indices[channel] = current + 1
                self._narrow_persistence[channel] = 0
                self._range_cooldown[channel] = config.AUTORANGE_COOLDOWN_RECORDS
                return True
        else:
            self._narrow_persistence[channel] = 0
        return False

    @staticmethod
    def _baseline_current_pa(drive_actual_v: float) -> float:
        """Monotonic tube current expected without inelastic-loss peaks."""

        voltage = max(0.0, drive_actual_v)
        return 18.0 + 0.95 * voltage**1.5

    @classmethod
    def _ideal_current_pa(cls, drive_actual_v: float) -> float:
        """Return a smooth mercury Franck-Hertz characteristic before noise."""

        voltage = max(0.0, drive_actual_v)
        current_pa = cls._baseline_current_pa(voltage)
        peak_voltage = config.SIMULATOR_FIRST_PEAK_VOLTS
        while peak_voltage <= 30.0:
            width_v = 0.90 if voltage < peak_voltage else 1.20
            separation_v = (voltage - peak_voltage) / width_v
            peak_amplitude_pa = 38.0 + 1.65 * peak_voltage
            current_pa += peak_amplitude_pa * math.exp(
                -0.5 * separation_v * separation_v
            )
            peak_voltage += config.MERCURY_EXCITATION_VOLTS
        return current_pa
