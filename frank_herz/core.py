"""Protocol parsing, calibration, dataset storage, and acquisition state."""

from __future__ import annotations

from dataclasses import dataclass
import math
import threading
from typing import Callable, Iterable

from . import config


class ProtocolError(ValueError):
    """Raised when a serial line is not a valid Franck-Hertz data record."""


@dataclass(frozen=True, slots=True)
class Calibration:
    drive_scale: float = config.DRIVE_VOLTAGE_SCALE
    drive_offset_v: float = config.DRIVE_VOLTAGE_OFFSET_V
    picoammeter_mv_per_pa: float = config.PICOAMMETER_MV_PER_PA
    picoammeter_zero_v: float = config.PICOAMMETER_ZERO_V
    picoammeter_polarity: float = config.PICOAMMETER_POLARITY

    def __post_init__(self) -> None:
        values = (
            self.drive_scale,
            self.drive_offset_v,
            self.picoammeter_mv_per_pa,
            self.picoammeter_zero_v,
            self.picoammeter_polarity,
        )
        if not all(math.isfinite(value) for value in values):
            raise ValueError("Calibration values must be finite.")
        if self.drive_scale == 0:
            raise ValueError("Drive-voltage scale must be nonzero.")
        if self.picoammeter_mv_per_pa <= 0:
            raise ValueError("Picoammeter mV/pA must be greater than zero.")
        if self.picoammeter_polarity == 0:
            raise ValueError("Picoammeter polarity must be nonzero.")


@dataclass(frozen=True, slots=True)
class RawSample:
    elapsed_ms: int
    drive_adc_counts: float
    current_adc_counts: float
    drive_adc_v: float
    current_adc_v: float


@dataclass(frozen=True, slots=True)
class DataPoint:
    elapsed_ms: int
    drive_voltage_v: float
    tube_current_pa: float
    drive_adc_v: float
    current_adc_v: float
    drive_adc_counts: float
    current_adc_counts: float


def parse_data_line(line: bytes | str) -> RawSample:
    """Parse `DATA,time_ms,drive_raw,current_raw,drive_v,current_v`."""

    if isinstance(line, bytes):
        text = line.decode("ascii", errors="strict").strip()
    else:
        text = line.strip()

    fields = [field.strip() for field in text.split(",")]
    if len(fields) != 6 or fields[0] != "DATA":
        raise ProtocolError("Expected a six-field DATA record.")

    try:
        elapsed_ms = int(fields[1])
        numeric = tuple(float(field) for field in fields[2:])
    except (TypeError, ValueError) as exc:
        raise ProtocolError("DATA record contains a non-numeric field.") from exc

    if elapsed_ms < 0 or not all(math.isfinite(value) for value in numeric):
        raise ProtocolError("DATA record contains an invalid numeric value.")

    drive_counts, current_counts, drive_v, current_v = numeric
    if not (-32768.0 <= drive_counts <= 32767.0):
        raise ProtocolError("Drive ADC count is outside the ADS1115 range.")
    if not (-32768.0 <= current_counts <= 32767.0):
        raise ProtocolError("Current ADC count is outside the ADS1115 range.")
    voltage_limit = config.ADS1115_FULL_SCALE_VOLTS * 1.01
    if abs(drive_v) > voltage_limit or abs(current_v) > voltage_limit:
        raise ProtocolError("ADC voltage is outside the configured full-scale range.")

    return RawSample(
        elapsed_ms=elapsed_ms,
        drive_adc_counts=drive_counts,
        current_adc_counts=current_counts,
        drive_adc_v=drive_v,
        current_adc_v=current_v,
    )


def convert_sample(raw: RawSample, calibration: Calibration) -> DataPoint:
    """Convert the paired ADC measurement into laboratory units."""

    drive_voltage_v = (
        raw.drive_adc_v * calibration.drive_scale + calibration.drive_offset_v
    )
    picoammeter_mv = (raw.current_adc_v - calibration.picoammeter_zero_v) * 1000.0
    tube_current_pa = (
        calibration.picoammeter_polarity
        * picoammeter_mv
        / calibration.picoammeter_mv_per_pa
    )
    return DataPoint(
        elapsed_ms=raw.elapsed_ms,
        drive_voltage_v=drive_voltage_v,
        tube_current_pa=tube_current_pa,
        drive_adc_v=raw.drive_adc_v,
        current_adc_v=raw.current_adc_v,
        drive_adc_counts=raw.drive_adc_counts,
        current_adc_counts=raw.current_adc_counts,
    )


class Dataset:
    """Thread-safe, append-only measurement storage until explicitly cleared."""

    def __init__(self, max_points: int = config.MAX_STORED_POINTS) -> None:
        if max_points < 1:
            raise ValueError("max_points must be positive")
        self._max_points = max_points
        self._points: list[DataPoint] = []
        self._lock = threading.Lock()

    def append(self, point: DataPoint) -> None:
        with self._lock:
            if len(self._points) >= self._max_points:
                raise OverflowError(
                    f"The safety limit of {self._max_points:,} stored points was reached."
                )
            self._points.append(point)

    def clear(self) -> None:
        with self._lock:
            self._points.clear()

    def snapshot(self) -> tuple[DataPoint, ...]:
        with self._lock:
            return tuple(self._points)

    def __len__(self) -> int:
        with self._lock:
            return len(self._points)


class AcquisitionController:
    """Small state machine shared by the GUI and automated tests."""

    def __init__(
        self,
        sender: Callable[[bytes], None],
        calibration: Calibration | None = None,
        dataset: Dataset | None = None,
    ) -> None:
        self._sender = sender
        self.calibration = calibration or Calibration()
        self.dataset = dataset if dataset is not None else Dataset()
        self.device_ready = False
        self.running = False
        self.malformed_lines = 0
        self.storage_full = False

    def mark_device_ready(self) -> None:
        self.device_ready = True

    def disconnect(self) -> None:
        self.device_ready = False
        self.running = False

    def start(self) -> None:
        if not self.device_ready:
            raise RuntimeError("A compatible Arduino is not connected.")
        self._sender(config.START_COMMAND)
        self.running = True

    def stop(self) -> None:
        # Pause locally first so records already in the UI queue are ignored.
        was_ready = self.device_ready
        self.running = False
        if was_ready:
            self._sender(config.STOP_COMMAND)

    def ingest(self, line: bytes | str) -> DataPoint | None:
        if not self.running:
            return None
        try:
            raw = parse_data_line(line)
            point = convert_sample(raw, self.calibration)
            self.dataset.append(point)
            return point
        except (ProtocolError, UnicodeError):
            self.malformed_lines += 1
            return None
        except OverflowError:
            self.storage_full = True
            self.running = False
            return None

    def confirm_and_clear(self, confirm: Callable[[], bool]) -> bool:
        if len(self.dataset) and not confirm():
            return False
        self.dataset.clear()
        self.storage_full = False
        return True


def downsample_for_display(
    points: Iterable[DataPoint], maximum: int = config.MAX_DISPLAY_POINTS
) -> tuple[list[float], list[float]]:
    """Return a representative plot view while retaining every export row."""

    selected = _select_points_for_display(points, maximum)
    return (
        [point.drive_voltage_v for point in selected],
        [point.tube_current_pa for point in selected],
    )


def downsample_for_strip_recorder(
    points: Iterable[DataPoint], maximum: int = config.MAX_DISPLAY_POINTS
) -> tuple[list[float], list[float], list[float]]:
    """Return elapsed time and both acquired channels for the strip view."""

    selected = _select_points_for_display(points, maximum)
    return (
        [point.elapsed_ms / 1000.0 for point in selected],
        [point.drive_voltage_v for point in selected],
        [point.tube_current_pa for point in selected],
    )


def _select_points_for_display(
    points: Iterable[DataPoint], maximum: int
) -> tuple[DataPoint, ...]:
    """Select evenly spaced points and always retain the newest sample."""

    materialized = tuple(points)
    if not materialized:
        return ()
    stride = max(1, math.ceil(len(materialized) / maximum))
    selected = materialized[::stride]
    if selected[-1] is not materialized[-1]:
        selected += (materialized[-1],)
    return selected
