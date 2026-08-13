/*
  Franck-Hertz dual-channel acquisition firmware

  Hardware: Modern Lab Dual Acquisition Board, ADS1115 at I2C address 0x49
  Paired inputs (sampled sequentially by the multiplexed ADS1115):
    AIN0 = J1 low-gain path = tube drive monitor
    AIN2 = J3 low-gain path = external picoammeter analog output

  Serial: 115200 baud, newline terminated ASCII
  Data: DATA,time_ms,drive_raw,current_raw,drive_adc_v,current_adc_v
  Commands: run | stop | avg,N | delay,N | idn?
*/

#include <Adafruit_ADS1X15.h>
#include <Wire.h>

const uint32_t BAUD_RATE = 115200;
const uint8_t ADS1115_ADDRESS = 0x49;
const uint8_t DRIVE_CHANNEL = 0;
const uint8_t CURRENT_CHANNEL = 2;
const float ADS_VOLTS_PER_COUNT = 0.0000625F;  // GAIN_TWO, +/-2.048 V
// The shared banner identifies the physical shield and matches SerialPlotter.
// The capability line below distinguishes this paired-channel protocol.
const char DEVICE_NAME[] = "Modern Lab Data Acquisition Shield";
const char PROTOCOL_CAPABILITY[] = "#protocol,franck-hertz-paired,1";

uint16_t samplesToAverage = 10;
uint32_t sampleIntervalMs = 50;
bool acquisitionRunning = false;
uint32_t lastSampleMs = 0;

Adafruit_ADS1115 ads;
char commandBuffer[48];
uint8_t commandLength = 0;

void printIdentity() {
  Serial.println(DEVICE_NAME);
  Serial.println(PROTOCOL_CAPABILITY);
}

void printAcknowledgement(const char* name, uint32_t value) {
  Serial.print('#');
  Serial.print(name);
  Serial.print(',');
  Serial.println(value);
}

void processCommand() {
  commandBuffer[commandLength] = '\0';

  if (strcasecmp(commandBuffer, "run") == 0) {
    acquisitionRunning = true;
    lastSampleMs = 0;
    Serial.println(F("#run"));
  } else if (strcasecmp(commandBuffer, "stop") == 0) {
    acquisitionRunning = false;
    Serial.println(F("#stop"));
  } else if (strncasecmp(commandBuffer, "avg,", 4) == 0) {
    long requested = atol(commandBuffer + 4);
    if (requested >= 1 && requested <= 1000) {
      samplesToAverage = (uint16_t)requested;
      printAcknowledgement("avg", samplesToAverage);
    } else {
      Serial.println(F("ERR,BAD_AVG"));
    }
  } else if (strncasecmp(commandBuffer, "delay,", 6) == 0) {
    long requested = atol(commandBuffer + 6);
    if (requested >= 10 && requested <= 10000) {
      sampleIntervalMs = (uint32_t)requested;
      printAcknowledgement("delay", sampleIntervalMs);
    } else {
      Serial.println(F("ERR,BAD_DELAY"));
    }
  } else if (strcasecmp(commandBuffer, "idn?") == 0) {
    printIdentity();
  } else if (commandLength > 0) {
    Serial.println(F("ERR,UNKNOWN_COMMAND"));
  }
  commandLength = 0;
}

void serviceSerial() {
  while (Serial.available() > 0) {
    char incoming = (char)Serial.read();
    if (incoming == '\r') continue;
    if (incoming == '\n') {
      processCommand();
    } else if (commandLength < sizeof(commandBuffer) - 1) {
      commandBuffer[commandLength++] = incoming;
    } else {
      commandLength = 0;
      Serial.println(F("ERR,COMMAND_TOO_LONG"));
    }
  }
}

void acquirePairedSample() {
  double driveSum = 0.0;
  double currentSum = 0.0;

  // ADS1115 is a multiplexed converter, so the two channels are read in quick
  // succession. Alternating them inside the averaging loop minimizes skew.
  for (uint16_t index = 0; index < samplesToAverage; ++index) {
    driveSum += ads.readADC_SingleEnded(DRIVE_CHANNEL);
    currentSum += ads.readADC_SingleEnded(CURRENT_CHANNEL);
  }

  const double driveRaw = driveSum / samplesToAverage;
  const double currentRaw = currentSum / samplesToAverage;
  const double driveVolts = driveRaw * ADS_VOLTS_PER_COUNT;
  const double currentVolts = currentRaw * ADS_VOLTS_PER_COUNT;

  Serial.print(F("DATA,"));
  Serial.print(millis());
  Serial.print(',');
  Serial.print(driveRaw, 3);
  Serial.print(',');
  Serial.print(currentRaw, 3);
  Serial.print(',');
  Serial.print(driveVolts, 6);
  Serial.print(',');
  Serial.println(currentVolts, 6);
}

void setup() {
  Serial.begin(BAUD_RATE);
  Wire.begin();

  if (!ads.begin(ADS1115_ADDRESS)) {
    Serial.println(F("ERR,ADS1115_INIT"));
    while (true) {
      serviceSerial();
      delay(100);
    }
  }
  ads.setGain(GAIN_TWO);                // +/-2.048 V, 62.5 uV/count
  ads.setDataRate(RATE_ADS1115_860SPS); // fast paired sampling
  printIdentity();
}

void loop() {
  serviceSerial();
  if (!acquisitionRunning) return;

  uint32_t now = millis();
  if (lastSampleMs == 0 || now - lastSampleMs >= sampleIntervalMs) {
    acquirePairedSample();
    lastSampleMs = now;
  }
}
