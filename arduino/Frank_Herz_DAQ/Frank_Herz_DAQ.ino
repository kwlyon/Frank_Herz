/*
  Universal Modern Lab acquisition shield firmware

  This sketch preserves the complete SerialPlotter V4 Recorder interface and
  adds an opt-in paired mode for the Franck-Hertz application.

  Power-on/default mode (backward compatible):
    - "run" emits: millis(),legacy_value_mV
    - "1x" selects ADS AIN0; "10x" selects ADS AIN1
    - all legacy commands, EEPROM scaling, and SoftwareSerial passthrough remain

  Franck-Hertz mode:
    - "mode,paired" selects paired AIN0/AIN2 acquisition
    - "run" emits:
      DATA,time_ms,drive_raw,current_raw,drive_adc_v,current_adc_v

  ADS1115 channel wiring on the revision-3 Dual Acquisition Board:
    J1 / first BNC:  AIN0 lower-gain path, AIN1 higher-gain path
    J3 / second BNC: AIN2 lower-gain path, AIN3 higher-gain path
*/

#include <Adafruit_ADS1X15.h>
#include <EEPROM.h>
#include <SoftwareSerial.h>
#include <Wire.h>

// ======= Original hardware/serial configuration =======
const uint32_t HW_BAUD = 115200;
const uint32_t SW_BAUD = 9600;
const uint8_t SW_RX_PIN = A0;  // external device -> Arduino
const uint8_t SW_TX_PIN = A1;  // Arduino -> external device
const char HW_TERM_CHAR = '\n';
const char SW_TERM_CHAR = '\n';

const char DEVICE_NAME[] = "Modern Lab Data Acquisition Shield";
const char PROTOCOL_CAPABILITY[] = "#protocol,franck-hertz-paired,2";

// ======= ADS1115 configuration =======
const uint8_t ADS1115_ADDRESS = 0x49;
const uint8_t LEGACY_1X_CHANNEL = 0;   // J1 lower-gain path
const uint8_t LEGACY_10X_CHANNEL = 1;  // J1 higher-gain path
const uint8_t DRIVE_CHANNEL = 0;    // J1 lower-gain path
const uint8_t CURRENT_CHANNEL = 2;  // J3 lower-gain path
const double ADS_VOLTS_PER_COUNT = 0.0000625;  // GAIN_TWO, +/-2.048 V

// ======= Original acquisition defaults =======
uint32_t acquisitionDelay = 100;
uint16_t samplesToAverage = 1;
uint8_t legacyChannel = LEGACY_1X_CHANNEL;
bool acquisitionRunning = false;
uint32_t lastSampleMs = 0;

enum StreamMode : uint8_t { LEGACY_MODE, PAIRED_MODE };
StreamMode streamMode = LEGACY_MODE;  // restart is always SerialPlotter-safe

// ======= Original persistent scale behavior =======
const double PREC_REF = 1.8;
double scaleFactor = 1.0;
const int SCALE_ADDR = 0;

SoftwareSerial swSer(SW_RX_PIN, SW_TX_PIN);
Adafruit_ADS1115 ads;

String commandBuffer;
const size_t MAX_COMMAND_LENGTH = 256;

void loadScaleFromEEPROM() {
  double stored;
  EEPROM.get(SCALE_ADDR, stored);
  if (isnan(stored) || stored == 0.0) {
    stored = PREC_REF / (double)1024;
    EEPROM.put(SCALE_ADDR, stored);
  }
  scaleFactor = stored;
}

void saveScaleToEEPROM(double value) {
  double current;
  EEPROM.get(SCALE_ADDR, current);
  if (current != value) EEPROM.put(SCALE_ADDR, value);
}

void printIdentity() {
  Serial.println(DEVICE_NAME);
  Serial.println(PROTOCOL_CAPABILITY);
}

void printIdn() {
  printIdentity();
  Serial.println(F("Commands:"));
  Serial.println(F("  run                 -> start stream"));
  Serial.println(F("  delay,<ms>          -> set delay (ms>0)"));
  Serial.println(F("  avg,<n>             -> set averages (n>=1)"));
  Serial.println(F("  1x                  -> legacy AIN0 selection"));
  Serial.println(F("  10x                 -> legacy AIN1 selection"));
  Serial.println(F("  scale,<val>         -> set legacy scale (persist)"));
  Serial.println(F("  sf                  -> print legacy scale"));
  Serial.println(F("  stop                -> stop stream"));
  Serial.println(F("  mode,legacy         -> SerialPlotter records"));
  Serial.println(F("  mode,paired         -> Franck-Hertz records"));
  Serial.println(F("  mode?               -> print active mode"));
  Serial.println(F("  idn?                -> print this list"));
}

void emitLegacySample(uint32_t now) {
  uint16_t count = samplesToAverage < 1 ? 1 : samplesToAverage;
  double millivoltSum = 0.0;
  for (uint16_t index = 0; index < count; ++index) {
    int16_t raw = ads.readADC_SingleEnded(legacyChannel);
    millivoltSum += ads.computeVolts(raw) * 1000.0;
  }
  // Preserve V4 Recorder semantics: averaged ADC millivolts multiplied by the
  // EEPROM-backed scale factor, emitted as "time,value".
  const double value = (millivoltSum / (double)count) * scaleFactor;
  Serial.print(now);
  Serial.print(',');
  Serial.print(value, 6);
  Serial.print(HW_TERM_CHAR);
}

void emitPairedSample(uint32_t now) {
  double driveSum = 0.0;
  double currentSum = 0.0;
  uint16_t count = samplesToAverage < 1 ? 1 : samplesToAverage;

  // Alternating the multiplexed reads minimizes time skew between the BNCs.
  for (uint16_t index = 0; index < count; ++index) {
    driveSum += ads.readADC_SingleEnded(DRIVE_CHANNEL);
    currentSum += ads.readADC_SingleEnded(CURRENT_CHANNEL);
  }

  const double driveRaw = driveSum / (double)count;
  const double currentRaw = currentSum / (double)count;

  Serial.print(F("DATA,"));
  Serial.print(now);
  Serial.print(',');
  Serial.print(driveRaw, 3);
  Serial.print(',');
  Serial.print(currentRaw, 3);
  Serial.print(',');
  Serial.print(driveRaw * ADS_VOLTS_PER_COUNT, 6);
  Serial.print(',');
  Serial.println(currentRaw * ADS_VOLTS_PER_COUNT, 6);
}

void maybeStreamSample() {
  if (!acquisitionRunning) return;
  uint32_t now = millis();
  if (now - lastSampleMs < acquisitionDelay) return;

  if (streamMode == PAIRED_MODE) {
    emitPairedSample(now);
  } else {
    emitLegacySample(now);
  }
  lastSampleMs = now;
}

bool parseCommand(const String& originalLine) {
  String line = originalLine;
  line.trim();

  if (line.equalsIgnoreCase("idn?")) {
    printIdn();
    return true;
  }
  if (line.equalsIgnoreCase("run")) {
    acquisitionRunning = true;
    lastSampleMs = 0;
    Serial.println(F("#run"));
    return true;
  }
  if (line.equalsIgnoreCase("stop")) {
    acquisitionRunning = false;
    Serial.println(F("#stop"));
    return true;
  }
  if (line.startsWith("delay,")) {
    long requested = line.substring(6).toInt();
    if (requested > 0) {
      acquisitionDelay = (uint32_t)requested;
      Serial.print(F("#delay"));
      Serial.println(acquisitionDelay);
      return true;
    }
    return false;
  }
  if (line.startsWith("avg,")) {
    long requested = line.substring(4).toInt();
    if (requested >= 1) {
      samplesToAverage = (uint16_t)requested;
      Serial.print(F("#avg"));
      Serial.println(samplesToAverage);
      return true;
    }
    return false;
  }
  if (line.equalsIgnoreCase("1x")) {
    streamMode = LEGACY_MODE;
    legacyChannel = LEGACY_1X_CHANNEL;
    ads.setDataRate(RATE_ADS1115_128SPS);
    Serial.println(F("#1x"));
    return true;
  }
  if (line.equalsIgnoreCase("10x")) {
    streamMode = LEGACY_MODE;
    legacyChannel = LEGACY_10X_CHANNEL;
    ads.setDataRate(RATE_ADS1115_128SPS);
    Serial.println(F("#10x"));
    return true;
  }
  if (line.startsWith("scale,")) {
    double requested = atof(line.substring(6).c_str());
    if (requested > 0.0) {
      scaleFactor = requested;
      saveScaleToEEPROM(requested);
      Serial.print(F("#scale"));
      Serial.println(scaleFactor, 6);
      return true;
    }
    return false;
  }
  if (line.equalsIgnoreCase("sf")) {
    Serial.print(F("Scale: "));
    Serial.println(scaleFactor, 6);
    return true;
  }
  if (line.equalsIgnoreCase("mode,legacy")) {
    streamMode = LEGACY_MODE;
    ads.setDataRate(RATE_ADS1115_128SPS);
    Serial.println(F("#mode,legacy"));
    return true;
  }
  if (line.equalsIgnoreCase("mode,paired")) {
    streamMode = PAIRED_MODE;
    ads.setDataRate(RATE_ADS1115_860SPS);
    Serial.println(F("#mode,paired"));
    return true;
  }
  if (line.equalsIgnoreCase("mode?")) {
    Serial.println(streamMode == PAIRED_MODE ? F("#mode,paired") : F("#mode,legacy"));
    return true;
  }
  return false;
}

void serviceSerial() {
  while (Serial.available() > 0) {
    char incoming = (char)Serial.read();
    if (incoming == '\r' && HW_TERM_CHAR == '\n') continue;
    if (commandBuffer.length() < MAX_COMMAND_LENGTH) commandBuffer += incoming;

    if (incoming == HW_TERM_CHAR) {
      String line = commandBuffer;
      line.trim();
      if (!parseCommand(line)) {
        // Preserve the V4 Recorder bridge to the external serial device.
        swSer.print(line);
        swSer.print(SW_TERM_CHAR);
      }
      commandBuffer = "";
    }
  }

  while (swSer.available() > 0) {
    int incoming = swSer.read();
    if (incoming >= 0) Serial.write((uint8_t)incoming);
  }
}

void setup() {
  Serial.begin(HW_BAUD);
  swSer.begin(SW_BAUD);
  Wire.begin();
  // Preserve the original startup handshake timing: identify before ADS init.
  printIdentity();

  if (!ads.begin(ADS1115_ADDRESS)) {
    Serial.println(F("Failed to initialize ADS."));
    while (true) {
      serviceSerial();
      delay(10);
    }
  }
  ads.setGain(GAIN_TWO);
  analogReference(EXTERNAL);  // retained for compatibility with the V4 sketch
  loadScaleFromEEPROM();
}

void loop() {
  serviceSerial();
  maybeStreamSample();
}
