# Franck–Hertz Data Acquisition

This project provides the experiment-specific Franck–Hertz acquisition
application and the dual-channel Arduino firmware used with it. The Windows
title is **Franck–Hertz Data Acquisition**. Its Franck–Hertz X–Y characteristic,
two-channel strip recorder, Excel export, live plot autoscaling, measurement
cursor, and simulator are all retained.

Version 2.0 supports the current dual-channel hardware only. The same firmware
may also be copied into other acquisition projects, but this desktop application
remains specifically configured for the Franck–Hertz experiment. The retired analog
gain-switching hardware and its compatibility protocol are not part of this
firmware or desktop application.

## Hardware architecture

The acquisition board uses an ADS1115 at I2C address `0x49`:

| Application channel | Connector | ADS1115 differential pair | Default divider |
|---|---|---:|---:|
| Channel A / drive monitor | J1 | AIN0 − AIN1 | 909 kΩ high, 101 kΩ low |
| Channel B / current monitor | J3 | AIN2 − AIN3 | 909 kΩ high, 101 kΩ low |

The ADS1115 multiplexes its inputs, so the readings are sequential rather than
simultaneous. Its PGA setting is written immediately before every AIN0−AIN1 and
AIN2−AIN3 conversion. This allows the two differential channels to use
independent ranges cleanly even though they share one converter. The shared
analog bias is common-mode and therefore cancels instead of becoming a false
nonzero signal.

Supported bipolar full-scale settings are:

- ±6.144 V
- ±4.096 V
- ±2.048 V
- ±1.024 V
- ±0.512 V
- ±0.256 V

These are converter transfer-function ranges, not permission to exceed the
ADS1115 pin limits. Keep each physical ADC input pin between ground and the ADC
supply limit even when measuring a signed differential voltage. Never connect a
high-voltage experimental output directly to the acquisition board; use the
intended monitor output and a properly rated divider.

## Voltage conversion and divider configuration

The firmware performs the complete physical conversion for each sample:

```text
signed differential ADC counts → pair voltage using that sample's active PGA range
           → connector voltage using that channel's resistor divider
```

For either differential pair:

```text
Vin = Vdiff × (Rhigh + Rlow) / Rlow
```

The four divider resistances are stored in a versioned, checksummed EEPROM
structure. Invalid, uninitialized, or incompatible EEPROM data is replaced once
with the factory values. Unchanged configurations are not rewritten.

Factory values, in ohms:

```text
RAhigh = 909000    RAlow = 101000
RBhigh = 909000    RBlow = 101000
```

The Python application receives the already corrected connector voltages. Its
default drive correction is 1.0 and therefore does not rescale them. It uses the
reported active PGA ranges only to reconstruct and export the ADC-node voltages
from the signed raw counts. With equal bias on both inputs, zero differential
signal is reported as approximately zero rather than the bias voltage.

## ADC autoranging

Autoranging runs in the firmware, independently for Channels A and B. This keeps
clipping protection beside the ADC and does not depend on desktop timing.

- At 90% of the current range, the channel immediately moves one range wider.
- At 98%, it jumps directly to the widest setting.
- A narrower setting must contain the signal below 55% of its full scale for 20
  consecutive records before it is selected.
- An eight-record cooldown follows a change; an urgent widening is still allowed.
- The maximum absolute conversion within an averaging group drives the decision,
  so averaging cannot hide a near-clipping sample.

The `DATA` row is transmitted before a resulting `#range` notification. Thus the
desktop interprets that row with the range used to acquire it and applies the new
range to the following row.

## Serial protocol version 4

The firmware starts at 115200 baud and identifies itself with:

```text
Modern Lab Dual-Channel Data Acquisition
#protocol,modern-lab-dual-channel,4
```

Acquisition rows are:

```text
DATA,time_ms,channel_a_raw,channel_b_raw,channel_a_input_v,channel_b_input_v
```

The two final fields are calibrated connector voltages. The raw fields are
averaged signed ADS1115 counts.

### Acquisition and range commands

| Command | Result |
|---|---|
| `idn?` | Report identity and protocol capability |
| `run` / `stop` | Start or pause streaming |
| `delay,N` | Set record interval to 0–60000 ms |
| `avg,N` | Average 1–64 conversion pairs per row |
| `autorange?` | Report automatic-range state and both active ranges |
| `autorange,1` / `autorange,0` | Enable or disable automatic ranging |
| `range?` | Report both active ranges |
| `range,A,V` | Set Channel A manually while autoranging is off |
| `range,B,V` | Set Channel B manually while autoranging is off |
| `range,both,V` | Set both channels manually while autoranging is off |

`V` must be one of `6.144`, `4.096`, `2.048`, `1.024`, `0.512`, or `0.256`.
Range reports use `#range,A=2.048,B=0.512`; automatic-range reports use
`#autorange,1` or `#autorange,0`. Invalid commands return a descriptive `ERR,...`
line and do not change the active configuration.

### Divider commands

| Command | Result |
|---|---|
| `dividers?` | Report all resistances, multipliers, and current external full scales |
| `RAhigh,N` | Set Channel A high-side resistance in ohms |
| `RAlow,N` | Set Channel A low-side resistance in ohms |
| `RBhigh,N` | Set Channel B high-side resistance in ohms |
| `RBlow,N` | Set Channel B low-side resistance in ohms |
| `defaults` | Restore and save all four factory divider values |

Resistances must be integer ohm values from 1 through 100,000,000, and the
resulting divider multiplier must not exceed 1000.

## Desktop controls

Install the source dependencies and start the app with:

```powershell
python -m pip install -r requirements.txt
python app.py
```

Choose a serial port or **SIMULATOR (no hardware)** and select **Connect**.

- **Plot mode** changes between the strip recorder and Franck–Hertz X–Y view
  without changing or clearing acquisition.
- **Auto-scale live** controls only the Matplotlib axes. Home resumes live fitting;
  Pan or Zoom holds the chosen plot limits.
- **ADC Autorange** controls only the ADS1115 electrical ranges.
- With ADC Autorange selected, the Channel A and Channel B dropdowns are disabled
  for editing but continuously display the ranges reported by the firmware.
- With ADC Autorange cleared, both range dropdowns are independently selectable.
- The measurement cursor remains available in X–Y mode and can be hidden or
  dragged to a nearby acquired point.

Excel exports retain the original measurement/raw columns and append the active
Channel A and Channel B ADC range for every row. This makes raw ADC-node voltages
auditable even when the firmware changes ranges during a run.

## Simulator

The simulator uses the production version-4 protocol and implements the same
manual-range and autorange state machine. Its Franck–Hertz trace has a rising
space-charge baseline, visible random noise, and mercury peaks separated by about
4.9 V over a 0–30 V triangular sweep.

## Tests and builds

Run the source checks with:

```powershell
python -m unittest discover -s tests -v
python app.py --smoke-test
python app.py --gui-smoke-test
```

Compile the Uno firmware with Arduino CLI:

```powershell
arduino-cli compile --fqbn arduino:avr:uno arduino/Frank_Herz_DAQ
```

Build the Windows executable with the repository's existing PyInstaller spec:

```powershell
python -m PyInstaller --noconfirm Frank_Herz.spec
```

The packaged application is produced at `dist/Frank_Herz/Frank_Herz.exe`.
