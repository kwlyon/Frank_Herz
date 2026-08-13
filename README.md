# Franck-Hertz Data Acquisition

`Frank_Herz` is a standalone Windows/Tkinter application for acquiring the characteristic tube current versus accelerating (drive) voltage in a Franck-Hertz teaching experiment. It reuses the proven serial workflow and Modern Lab Arduino acquisition shield from `SerialPlotter`, but records paired voltages and plots an XY characteristic instead of voltage versus time.

The existing `SerialPlotter` project is not modified by this project.

## Hardware audit and channel assignments

The reference hardware is the revision 3 **Dual Acquisition Board** in `SerialPlotter`. Its ADS1115 is a multiplexed 16-bit ADC at I2C address `0x49`. It can measure four single-ended inputs; measurements on different inputs are sequential rather than truly simultaneous.

The two independent low-gain signal paths used here are:

| Experiment signal | Shield connection | ADS1115 input | Firmware field |
| --- | --- | --- | --- |
| Tube drive monitor | J1 low-gain/1× path | AIN0 | `drive_*` |
| Picoammeter analog output | J3 low-gain/1× path | AIN2 | `current_*` |

The companion higher-gain paths reach AIN1 and AIN3 and are not used by paired mode. Each paired record is produced by alternating AIN0 and AIN2 reads inside the averaging loop, minimizing channel-to-channel time skew.

The ADS1115 has four inputs numbered AIN0 through AIN3 (there is no AIN4).
Schematic tracing shows that the first BNC feeds AIN0/AIN1 and the second BNC
feeds AIN2/AIN3. A comment in the legacy V4 Recorder labels AIN0 and AIN2 as
`1x` and `10x`, but its actual code selects channel numbers 0 and 1. The
universal firmware preserves that real legacy behavior: `1x` reads AIN0 and
`10x` reads AIN1, the two gain paths for the first BNC. Franck-Hertz paired mode
instead reads AIN0 and AIN2, the lower-gain paths for the two separate BNCs.

The ADS1115 uses `GAIN_TWO`, a ±2.048 V converter range with 62.5 µV/count. In single-ended operation, signals must remain between ground and the allowed positive input limit. **Never connect the high-voltage tube drive directly to the shield.** Use an isolated/appropriate monitor output or a properly rated external divider, and verify the voltage at the shield input with a meter.

## Calibration

All laboratory calibration settings are grouped near the top of [`frank_herz/config.py`](frank_herz/config.py):

```python
DRIVE_VOLTAGE_SCALE = 1.0
DRIVE_VOLTAGE_OFFSET_V = 0.0
PICOAMMETER_MV_PER_PA = 1.0
PICOAMMETER_ZERO_V = 0.0
PICOAMMETER_POLARITY = 1.0
```

The `SerialPlotter` source and shield files do not contain a documented high-voltage divider ratio for the tube-drive monitor. Unity is therefore the safe default: the x-axis initially reports the voltage measured at the shield input. If a verified 40:1 monitor divider is used, for example, set `DRIVE_VOLTAGE_SCALE = 40.0` and rebuild the executable.

The working picoammeter assumption requested for this project is `1 mV = 1 pA`. The application calculates:

```text
tube current (pA) = polarity × (ADC volts − zero volts) × 1000 / (mV per pA)
```

Confirm the picoammeter's analog-output specification and update the constant before quantitative work. The older shield note about `1 mV per µA` describes a different PMT/current-input configuration, not the external picoammeter assumed here.

Every Excel export includes the calibration values plus the raw ADS1115 counts and ADC voltages, so a dataset can be recalibrated later.

## Arduino firmware and serial protocol

Install the Arduino libraries:

- `Adafruit ADS1X15`
- its normal `Adafruit BusIO` dependency

Open and upload [`arduino/Frank_Herz_DAQ/Frank_Herz_DAQ.ino`](arduino/Frank_Herz_DAQ/Frank_Herz_DAQ.ino) to the Arduino Uno. This is one universal firmware image for both applications; stations do not need to reflash a device when moving it between `SerialPlotter` and `Frank_Herz`.

The firmware deliberately starts in legacy mode after every reset. It therefore
works with the unmodified `SerialPlotter` handshake and two-field parser. The
Franck-Hertz application verifies the firmware capability, explicitly requests
paired mode, waits for confirmation, and only then enables acquisition.

Compatibility is:

| Client | Mode selection | Channels | Stream record |
| --- | --- | --- | --- |
| Existing `SerialPlotter` / `recorder4.py` | Default legacy mode; `1x` or `10x` also selects legacy mode | AIN0 (`1x`) or AIN1 (`10x`) on J1 | `time_ms,value` |
| `Frank_Herz` 1.0.2+ | Sends `mode,paired` after capability verification | AIN0 on J1 plus AIN2 on J3 | `DATA,time_ms,drive_raw,current_raw,drive_adc_v,current_adc_v` |

The original V4 Recorder behavior retained in legacy mode includes:

- 115200-baud USB serial and newline-terminated commands
- 9600-baud SoftwareSerial passthrough on A0/A1 for unrecognized commands
- ADS1115 address `0x49`, `GAIN_TWO`, and the original AIN0/AIN1 gain selection
- `run`, `stop`, `delay,N`, `avg,N`, `1x`, `10x`, `scale,N`, `sf`, and `idn?`
- the EEPROM-backed scale factor and its original `1.8 / 1024` initialization
- two-field `millis(),value` records with the original scaled-millivolt calculation

The firmware and Franck-Hertz Python application agree on:

- 115200 baud
- newline-terminated ASCII
- ADS1115 address `0x49`
- `GAIN_TWO` (±2.048 V, 62.5 µV/count)
- drive on AIN0 followed by picoammeter output on AIN2
- shared hardware banner: `Modern Lab Data Acquisition Shield` (the same banner used by `SerialPlotter`)
- paired-protocol capability: `#protocol,franck-hertz-paired,2`

The shared banner identifies the physical shield, not the application mode. The
Franck-Hertz app enables acquisition only after it receives the version-2
paired capability and the firmware acknowledges `#mode,paired`. If the original
single-channel Recorder firmware is installed, the app reports that the shield
was found and asks for the current universal firmware rather than presenting a
false ready state.

The data record is:

```text
DATA,time_ms,drive_raw,current_raw,drive_adc_v,current_adc_v
```

Example:

```text
DATA,1250,8000.000,3200.000,0.500000,0.200000
```

Supported commands are:

| Command | Meaning |
| --- | --- |
| `mode,paired` | Select paired Franck-Hertz records and reply `#mode,paired` |
| `mode,legacy` | Select legacy SerialPlotter records and reply `#mode,legacy` |
| `mode?` | Report the current stream mode |
| `run` | Start/resume records in the selected mode |
| `stop` | Pause records |
| `avg,N` | Set the number of ADC readings to average (`N >= 1`) |
| `delay,N` | Set the minimum record interval in milliseconds (`N > 0`) |
| `1x` | Select legacy mode and the J1 AIN0 path |
| `10x` | Select legacy mode and the J1 AIN1 path |
| `scale,N` / `sf` | Set or report the persistent legacy scaling factor |
| `idn?` | Repeat the shared device banner, capability, and command list |

Acknowledgements start with `#`; firmware errors start with `ERR,`. The app sends
periodic `idn?` queries while connecting, so it can recover if the Arduino's
one-time startup banner was emitted before the serial reader was ready. The
application ignores malformed records without crashing. If the serial link
fails, acquisition stops locally while the collected dataset remains intact. If
the Arduino resets and repeats its identity during an active run, the application
restores acquisition settings and resumes streaming.

## Run from source

Python 3.11 or later is recommended. Tkinter is included with the standard Windows Python installer.

```powershell
python -m pip install -r requirements.txt
python app.py
```

Choose the Arduino COM port and click **Connect**. A green indicator and “Arduino connected and ready” confirm the expected handshake. If hardware is unavailable, choose **SIMULATOR (no hardware)**; it uses exactly the production data protocol.

## Operation

1. Connect the drive monitor to J1 and the picoammeter analog output to J3, with a common signal reference appropriate for the shield.
2. Verify that both shield inputs remain within their safe range.
3. Select the COM port and click **Connect**.
4. Click **Start Acquisition** to append live `(Drive Voltage, Tube Current)` points.
5. Click **Stop Acquisition** to pause. Existing points remain displayed; **Start Acquisition** resumes appending to the same dataset.
6. Use the plot toolbar to go Home, move Back/Forward through views, Pan, box-Zoom, configure the plot, or save an image. Manual pan/zoom pauses live autoscaling so new samples do not overwrite the selected view.
7. Leave **Auto-scale live** selected to continuously fit all acquired data. Select **Home** or reselect **Auto-scale live** at any time to fit the full dataset again.
8. Click **Clear Data** only to begin a new dataset. A confirmation dialog is required before existing points are permanently removed.
9. Click **Export Data** and choose an `.xlsx` filename.

The workbook's primary columns are `Drive Voltage (V)` and `Tube Current (pA)`. It also includes elapsed time, both ADC voltages, both raw averaged counts, and a calibration worksheet.

## Test

The test suite covers channel parsing and units, malformed data, stop/resume retention, confirmation-gated clearing, connection loss, simulator acquisition, and a readable Excel export:

```powershell
python -m unittest discover -s tests -v
python app.py --smoke-test
python app.py --gui-smoke-test
```

The GUI smoke test opens the real interface, connects the simulator, acquires points, pauses without losing them, and closes automatically.

If Arduino CLI and the required board/library packages are installed, compile-check the firmware with:

```powershell
arduino-cli compile --fqbn arduino:avr:uno --build-path .arduino_build arduino/Frank_Herz_DAQ
```

## Build the Windows executable

```powershell
python -m pip install -r requirements-dev.txt
python -m PyInstaller --noconfirm Frank_Herz.spec
```

The executable is generated at `dist/Frank_Herz/Frank_Herz.exe`, with its required runtime files in the adjacent `_internal` folder. Keep the distribution folder together when copying it to the laboratory computer. Test the packaged acquisition/export path with:

```powershell
dist\Frank_Herz\Frank_Herz.exe --smoke-test
```

`build/` and `dist/` are intentionally ignored by Git. Rebuild from the versioned source and PyInstaller specification for the target laboratory computer.
