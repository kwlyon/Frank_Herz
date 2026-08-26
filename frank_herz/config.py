"""Hardware and calibration settings.

These constants are intentionally kept together so a laboratory technician can
calibrate the setup without searching through the GUI or serial code.
"""

# Serial protocol. These values must match Frank_Herz_DAQ.ino.
BAUD_RATE = 115_200
# The banner identifies the physical Modern Lab shield and intentionally matches
# SerialPlotter. A separate capability line identifies paired Franck-Hertz data.
HANDSHAKE_BANNER = "Modern Lab Data Acquisition Shield"
PROTOCOL_CAPABILITY = "#protocol,franck-hertz-paired,2"
IDENTIFY_COMMAND = b"idn?\n"
PAIRED_MODE_COMMAND = b"mode,paired\n"
PAIRED_MODE_ACK = "#mode,paired"
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
# The assumed 0--30 V drive monitor maps 30 V to 2.000 V at the ADS1115, so
# its external divider/calibrated monitor scale is 15:1. Verify this ratio on
# the completed hardware before connecting the tube drive monitor.
DRIVE_VOLTAGE_SCALE = 15.0
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

# Simulator model. The 2.000 V endpoint stays below the ADS1115's 2.048 V
# full-scale limit and becomes 30 V after the configured 15:1 calibration.
SIMULATOR_DRIVE_ADC_MAX_VOLTS = 2.0
SIMULATOR_SWEEP_PERIOD_SECONDS = 12.0
MERCURY_EXCITATION_VOLTS = 4.9
SIMULATOR_FIRST_PEAK_VOLTS = 5.2
