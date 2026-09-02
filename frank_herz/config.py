"""Hardware and calibration settings.

These constants are intentionally kept together so a laboratory technician can
calibrate the setup without searching through the GUI or serial code.
"""

# Serial protocol. These values must match Frank_Herz_DAQ.ino.
BAUD_RATE = 115_200
HANDSHAKE_BANNER = "Modern Lab Dual-Channel Data Acquisition"
PROTOCOL_CAPABILITY = "#protocol,modern-lab-dual-channel,4"
IDENTIFY_COMMAND = b"idn?\n"
START_COMMAND = b"run\n"
STOP_COMMAND = b"stop\n"
AUTORANGE_QUERY_COMMAND = b"autorange?\n"
RANGE_QUERY_COMMAND = b"range?\n"
LINE_ENDING = b"\n"
HANDSHAKE_TIMEOUT_SECONDS = 10.0
IDENTIFY_RETRY_MS = 1_000

# Firmware acquisition defaults.
DEFAULT_AVERAGES = 10
DEFAULT_SAMPLE_INTERVAL_MS = 50

# ADS1115 programmable differential full-scale ranges, ordered widest to
# narrowest. Each physical input pin must remain between ground and the ADC
# supply even though the conversion result is signed.
ADS1115_I2C_ADDRESS = 0x49
ADC_RANGE_VOLTS = (6.144, 4.096, 2.048, 1.024, 0.512, 0.256)
DEFAULT_ADC_RANGE_VOLTS = 2.048
DRIVE_ADC_PAIR = (0, 1)
CURRENT_ADC_PAIR = (2, 3)

# ---------------- Laboratory calibration (edit these) ----------------
# Protocol v4 reports the external connector voltage after applying the
# EEPROM-backed resistor divider in firmware. These values are optional
# laboratory corrections applied after that firmware conversion.
DRIVE_VOLTAGE_SCALE = 1.0
DRIVE_VOLTAGE_OFFSET_V = 0.0

# Picoammeter analog-output calibration. Current (pA) equals output (mV)
# divided by this value after subtracting the zero voltage.
PICOAMMETER_MV_PER_PA = 10.0
PICOAMMETER_ZERO_V = 0.0
PICOAMMETER_POLARITY = 1.0
# ----------------------------------------------------------------------

MAX_STORED_POINTS = 2_000_000
MAX_DISPLAY_POINTS = 20_000
UI_UPDATE_MS = 40
SERIAL_READ_CHUNK = 2048
SIMULATOR_PORT = "SIMULATOR (no hardware)"

# Simulator model. Both virtual inputs start with the firmware's standard 10:1
# dividers; autoranging widens Channel A in time to follow the full 30 V sweep.
SIMULATOR_DRIVE_MAX_VOLTS = 30.0
SIMULATOR_SWEEP_PERIOD_SECONDS = 12.0
MERCURY_EXCITATION_VOLTS = 4.9
SIMULATOR_FIRST_PEAK_VOLTS = 5.2
MAX_DIVIDER_MULTIPLIER = 1_000.0

# Firmware-equivalent autorange behavior used by the simulator.
AUTORANGE_WIDEN_FRACTION = 0.90
AUTORANGE_SATURATION_FRACTION = 0.98
AUTORANGE_NARROW_FRACTION = 0.55
AUTORANGE_NARROW_RECORDS = 20
AUTORANGE_COOLDOWN_RECORDS = 8
