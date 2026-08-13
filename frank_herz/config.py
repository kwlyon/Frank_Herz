"""Hardware and calibration settings.

These constants are intentionally kept together so a laboratory technician can
calibrate the setup without searching through the GUI or serial code.
"""

# Serial protocol. These values must match Frank_Herz_DAQ.ino.
BAUD_RATE = 115_200
# The banner identifies the physical Modern Lab shield and intentionally matches
# SerialPlotter. A separate capability line identifies paired Franck-Hertz data.
HANDSHAKE_BANNER = "Modern Lab Data Acquisition Shield"
PROTOCOL_CAPABILITY = "#protocol,franck-hertz-paired,1"
IDENTIFY_COMMAND = b"idn?\n"
START_COMMAND = b"run\n"
STOP_COMMAND = b"stop\n"
LINE_ENDING = b"\n"
HANDSHAKE_TIMEOUT_SECONDS = 10.0
IDENTIFY_RETRY_MS = 1_000

# Firmware acquisition defaults.
DEFAULT_AVERAGES = 10
DEFAULT_SAMPLE_INTERVAL_MS = 50

# ADS1115 setup used by the firmware: GAIN_TWO gives a +/-2.048 V full scale
# and 62.5 uV/count. The shield's single-ended inputs must stay non-negative.
ADS1115_I2C_ADDRESS = 0x49
ADS1115_FULL_SCALE_VOLTS = 2.048
ADS1115_VOLTS_PER_COUNT = 0.000_062_5
DRIVE_ADC_CHANNEL = 0
CURRENT_ADC_CHANNEL = 2

# ---------------- Laboratory calibration (edit these) ----------------
# Actual tube drive voltage = measured shield voltage * scale + offset.
# The SerialPlotter project contains no documented high-voltage divider ratio,
# so the safe default is unity. Set this to the verified external divider ratio
# before measuring a drive voltage above the ADC input range.
DRIVE_VOLTAGE_SCALE = 1.0
DRIVE_VOLTAGE_OFFSET_V = 0.0

# Picoammeter analog-output calibration. The requested working assumption is
# 1 mV per pA. Tube current is calculated after subtracting the zero voltage.
PICOAMMETER_MV_PER_PA = 1.0
PICOAMMETER_ZERO_V = 0.0
PICOAMMETER_POLARITY = 1.0
# ----------------------------------------------------------------------

MAX_STORED_POINTS = 2_000_000
MAX_DISPLAY_POINTS = 20_000
UI_UPDATE_MS = 40
SERIAL_READ_CHUNK = 2048
SIMULATOR_PORT = "SIMULATOR (no hardware)"
