#include <Adafruit_ADS1X15.h>
#include <EEPROM.h>
#include <Wire.h>

// Modern Lab dual-channel data-acquisition firmware.
// Channel A is ADS1115 AIN0 (J1); channel B is ADS1115 AIN2 (J3).

Adafruit_ADS1115 ads;

const unsigned long SERIAL_BAUD = 115200;
const uint8_t ADS1115_I2C_ADDRESS = 0x49;
const uint8_t CHANNEL_A_INPUT = 0;
const uint8_t CHANNEL_B_INPUT = 2;

const uint16_t DEFAULT_ACQUISITION_DELAY_MS = 100;
const uint8_t DEFAULT_AVERAGE_COUNT = 1;
const uint8_t MAX_AVERAGE_COUNT = 64;

const uint32_t DEFAULT_R_A_HIGH = 909000UL;
const uint32_t DEFAULT_R_A_LOW = 101000UL;
const uint32_t DEFAULT_R_B_HIGH = 909000UL;
const uint32_t DEFAULT_R_B_LOW = 101000UL;

const uint32_t MIN_RESISTANCE_OHMS = 1UL;
const uint32_t MAX_RESISTANCE_OHMS = 100000000UL;
const double MAX_DIVIDER_MULTIPLIER = 1000.0;

const uint32_t CONFIG_MAGIC = 0x4D4C4456UL;  // "MLDV"
const uint16_t CONFIG_VERSION = 2;
const int CONFIG_EEPROM_ADDRESS = 0;

struct DividerConfig {
  uint32_t magic;
  uint16_t version;
  uint16_t size;
  uint32_t RAhigh;
  uint32_t RAlow;
  uint32_t RBhigh;
  uint32_t RBlow;
  uint32_t checksum;
};

uint32_t RAhigh = DEFAULT_R_A_HIGH;
uint32_t RAlow = DEFAULT_R_A_LOW;
uint32_t RBhigh = DEFAULT_R_B_HIGH;
uint32_t RBlow = DEFAULT_R_B_LOW;

enum AdcRangeIndex : uint8_t {
  RANGE_6_144 = 0,
  RANGE_4_096 = 1,
  RANGE_2_048 = 2,
  RANGE_1_024 = 3,
  RANGE_0_512 = 4,
  RANGE_0_256 = 5,
  RANGE_COUNT = 6
};

const uint8_t DEFAULT_RANGE_INDEX = RANGE_2_048;
const int32_t AUTORANGE_WIDEN_COUNTS = 29490L;       // 90% of full scale
const int32_t AUTORANGE_SATURATION_COUNTS = 32112L;  // 98% of full scale
const double AUTORANGE_NARROW_FRACTION = 0.55;
const uint8_t AUTORANGE_NARROW_RECORDS = 20;
const uint8_t AUTORANGE_COOLDOWN_RECORDS = 8;

uint8_t activeRange[2] = {DEFAULT_RANGE_INDEX, DEFAULT_RANGE_INDEX};
uint8_t narrowPersistence[2] = {0, 0};
uint8_t rangeCooldown[2] = {0, 0};
bool autorangeEnabled = true;

bool acquisitionRunning = false;
uint16_t acquisitionDelayMs = DEFAULT_ACQUISITION_DELAY_MS;
uint8_t averageCount = DEFAULT_AVERAGE_COUNT;
unsigned long lastAcquisitionMs = 0;

const size_t COMMAND_BUFFER_SIZE = 128;
char commandBuffer[COMMAND_BUFFER_SIZE];
size_t commandLength = 0;
bool commandOverflow = false;

uint32_t fnvByte(uint32_t hash, uint8_t value) {
  hash ^= value;
  return hash * 16777619UL;
}

uint32_t fnvUint16(uint32_t hash, uint16_t value) {
  hash = fnvByte(hash, (uint8_t)(value & 0xFF));
  return fnvByte(hash, (uint8_t)((value >> 8) & 0xFF));
}

uint32_t fnvUint32(uint32_t hash, uint32_t value) {
  for (uint8_t shift = 0; shift < 32; shift += 8) {
    hash = fnvByte(hash, (uint8_t)((value >> shift) & 0xFF));
  }
  return hash;
}

uint32_t dividerConfigChecksum(const DividerConfig &config) {
  uint32_t hash = 2166136261UL;
  hash = fnvUint32(hash, config.magic);
  hash = fnvUint16(hash, config.version);
  hash = fnvUint16(hash, config.size);
  hash = fnvUint32(hash, config.RAhigh);
  hash = fnvUint32(hash, config.RAlow);
  hash = fnvUint32(hash, config.RBhigh);
  hash = fnvUint32(hash, config.RBlow);
  return hash;
}

bool dividerPairIsValid(uint32_t highSide, uint32_t lowSide) {
  if (highSide < MIN_RESISTANCE_OHMS || highSide > MAX_RESISTANCE_OHMS ||
      lowSide < MIN_RESISTANCE_OHMS || lowSide > MAX_RESISTANCE_OHMS) {
    return false;
  }
  const double multiplier = ((double)highSide + (double)lowSide) / (double)lowSide;
  return multiplier >= 1.0 && multiplier <= MAX_DIVIDER_MULTIPLIER;
}

bool dividerConfigIsValid(const DividerConfig &config) {
  return config.magic == CONFIG_MAGIC &&
         config.version == CONFIG_VERSION &&
         config.size == sizeof(DividerConfig) &&
         dividerPairIsValid(config.RAhigh, config.RAlow) &&
         dividerPairIsValid(config.RBhigh, config.RBlow) &&
         config.checksum == dividerConfigChecksum(config);
}

DividerConfig currentDividerConfig() {
  DividerConfig config;
  config.magic = CONFIG_MAGIC;
  config.version = CONFIG_VERSION;
  config.size = sizeof(DividerConfig);
  config.RAhigh = RAhigh;
  config.RAlow = RAlow;
  config.RBhigh = RBhigh;
  config.RBlow = RBlow;
  config.checksum = dividerConfigChecksum(config);
  return config;
}

void saveDividerConfig() {
  const DividerConfig config = currentDividerConfig();
  DividerConfig stored;
  EEPROM.get(CONFIG_EEPROM_ADDRESS, stored);
  const bool unchanged =
      stored.magic == config.magic &&
      stored.version == config.version &&
      stored.size == config.size &&
      stored.RAhigh == config.RAhigh &&
      stored.RAlow == config.RAlow &&
      stored.RBhigh == config.RBhigh &&
      stored.RBlow == config.RBlow &&
      stored.checksum == config.checksum;
  if (!unchanged) {
    EEPROM.put(CONFIG_EEPROM_ADDRESS, config);
  }
}

void restoreDefaultDividers(bool saveToEeprom) {
  RAhigh = DEFAULT_R_A_HIGH;
  RAlow = DEFAULT_R_A_LOW;
  RBhigh = DEFAULT_R_B_HIGH;
  RBlow = DEFAULT_R_B_LOW;
  if (saveToEeprom) {
    saveDividerConfig();
  }
}

void loadDividerConfig() {
  DividerConfig config;
  EEPROM.get(CONFIG_EEPROM_ADDRESS, config);
  if (!dividerConfigIsValid(config)) {
    restoreDefaultDividers(true);
    return;
  }
  RAhigh = config.RAhigh;
  RAlow = config.RAlow;
  RBhigh = config.RBhigh;
  RBlow = config.RBlow;
}

adsGain_t gainForRange(uint8_t rangeIndex) {
  switch (rangeIndex) {
    case RANGE_6_144: return GAIN_TWOTHIRDS;
    case RANGE_4_096: return GAIN_ONE;
    case RANGE_2_048: return GAIN_TWO;
    case RANGE_1_024: return GAIN_FOUR;
    case RANGE_0_512: return GAIN_EIGHT;
    default: return GAIN_SIXTEEN;
  }
}

double fullScaleVoltsForRange(uint8_t rangeIndex) {
  switch (rangeIndex) {
    case RANGE_6_144: return 6.144;
    case RANGE_4_096: return 4.096;
    case RANGE_2_048: return 2.048;
    case RANGE_1_024: return 1.024;
    case RANGE_0_512: return 0.512;
    default: return 0.256;
  }
}

double voltsPerCountForRange(uint8_t rangeIndex) {
  return fullScaleVoltsForRange(rangeIndex) / 32768.0;
}

bool parseRangeValue(const char *text, uint8_t &rangeIndex) {
  if (text == NULL || *text == '\0') return false;
  char *endPointer;
  const double requested = strtod(text, &endPointer);
  if (*endPointer != '\0') return false;
  for (uint8_t index = 0; index < RANGE_COUNT; ++index) {
    if (fabs(requested - fullScaleVoltsForRange(index)) < 0.0005) {
      rangeIndex = index;
      return true;
    }
  }
  return false;
}

void printRangeValue(uint8_t rangeIndex) {
  Serial.print(fullScaleVoltsForRange(rangeIndex), 3);
}

void printRangeStatus() {
  Serial.print(F("#range,A="));
  printRangeValue(activeRange[0]);
  Serial.print(F(",B="));
  printRangeValue(activeRange[1]);
  Serial.println();
}

void printAutorangeStatus() {
  Serial.print(F("#autorange,"));
  Serial.println(autorangeEnabled ? 1 : 0);
}

double dividerMultiplier(uint32_t highSide, uint32_t lowSide) {
  return ((double)highSide + (double)lowSide) / (double)lowSide;
}

double externalVoltage(int32_t rawCount, uint8_t rangeIndex,
                       uint32_t highSide, uint32_t lowSide) {
  const double adcVoltage = (double)rawCount * voltsPerCountForRange(rangeIndex);
  return adcVoltage * dividerMultiplier(highSide, lowSide);
}

void printDividerStatus() {
  const double multiplierA = dividerMultiplier(RAhigh, RAlow);
  const double multiplierB = dividerMultiplier(RBhigh, RBlow);
  Serial.print(F("#dividers,RAhigh="));
  Serial.print((unsigned long)RAhigh);
  Serial.print(F(",RAlow="));
  Serial.print((unsigned long)RAlow);
  Serial.print(F(",RBhigh="));
  Serial.print((unsigned long)RBhigh);
  Serial.print(F(",RBlow="));
  Serial.print((unsigned long)RBlow);
  Serial.print(F(",A_multiplier="));
  Serial.print(multiplierA, 6);
  Serial.print(F(",B_multiplier="));
  Serial.print(multiplierB, 6);
  Serial.print(F(",A_full_scale_v="));
  Serial.print(fullScaleVoltsForRange(activeRange[0]) * multiplierA, 6);
  Serial.print(F(",B_full_scale_v="));
  Serial.println(fullScaleVoltsForRange(activeRange[1]) * multiplierB, 6);
}

int32_t absoluteCount(int16_t value) {
  const int32_t widened = value;
  return widened < 0 ? -widened : widened;
}

bool updateChannelAutorange(uint8_t channelIndex, int32_t maximumMagnitude) {
  if (!autorangeEnabled) return false;

  uint8_t current = activeRange[channelIndex];
  uint8_t requested = current;

  if (maximumMagnitude >= AUTORANGE_SATURATION_COUNTS && current != RANGE_6_144) {
    requested = RANGE_6_144;
  } else if (maximumMagnitude >= AUTORANGE_WIDEN_COUNTS && current > RANGE_6_144) {
    requested = current - 1;
  }

  if (requested != current) {
    activeRange[channelIndex] = requested;
    narrowPersistence[channelIndex] = 0;
    rangeCooldown[channelIndex] = AUTORANGE_COOLDOWN_RECORDS;
    return true;
  }

  if (rangeCooldown[channelIndex] > 0) {
    --rangeCooldown[channelIndex];
    narrowPersistence[channelIndex] = 0;
    return false;
  }

  if (current + 1 < RANGE_COUNT) {
    const double measuredVolts =
        maximumMagnitude * voltsPerCountForRange(current);
    const double narrowerLimit =
        fullScaleVoltsForRange(current + 1) * AUTORANGE_NARROW_FRACTION;
    if (measuredVolts <= narrowerLimit) {
      if (narrowPersistence[channelIndex] < AUTORANGE_NARROW_RECORDS) {
        ++narrowPersistence[channelIndex];
      }
      if (narrowPersistence[channelIndex] >= AUTORANGE_NARROW_RECORDS) {
        activeRange[channelIndex] = current + 1;
        narrowPersistence[channelIndex] = 0;
        rangeCooldown[channelIndex] = AUTORANGE_COOLDOWN_RECORDS;
        return true;
      }
    } else {
      narrowPersistence[channelIndex] = 0;
    }
  } else {
    narrowPersistence[channelIndex] = 0;
  }
  return false;
}

void acquireAndEmitRecord() {
  const uint8_t sampledRangeA = activeRange[0];
  const uint8_t sampledRangeB = activeRange[1];
  int32_t sumA = 0;
  int32_t sumB = 0;
  int32_t maximumA = 0;
  int32_t maximumB = 0;

  for (uint8_t sampleIndex = 0; sampleIndex < averageCount; ++sampleIndex) {
    ads.setGain(gainForRange(sampledRangeA));
    const int16_t rawA = ads.readADC_SingleEnded(CHANNEL_A_INPUT);
    ads.setGain(gainForRange(sampledRangeB));
    const int16_t rawB = ads.readADC_SingleEnded(CHANNEL_B_INPUT);
    sumA += rawA;
    sumB += rawB;
    const int32_t magnitudeA = absoluteCount(rawA);
    const int32_t magnitudeB = absoluteCount(rawB);
    if (magnitudeA > maximumA) maximumA = magnitudeA;
    if (magnitudeB > maximumB) maximumB = magnitudeB;
  }

  const int32_t averagedA = sumA / averageCount;
  const int32_t averagedB = sumB / averageCount;
  Serial.print(F("DATA,"));
  Serial.print(millis());
  Serial.print(',');
  Serial.print(averagedA);
  Serial.print(',');
  Serial.print(averagedB);
  Serial.print(',');
  Serial.print(externalVoltage(averagedA, sampledRangeA, RAhigh, RAlow), 6);
  Serial.print(',');
  Serial.println(externalVoltage(averagedB, sampledRangeB, RBhigh, RBlow), 6);

  const bool rangeChangedA = updateChannelAutorange(0, maximumA);
  const bool rangeChangedB = updateChannelAutorange(1, maximumB);
  if (rangeChangedA || rangeChangedB) {
    printRangeStatus();
  }
}

char *trimWhitespace(char *text) {
  while (*text != '\0' && isspace((unsigned char)*text)) ++text;
  char *end = text + strlen(text);
  while (end > text && isspace((unsigned char)*(end - 1))) --end;
  *end = '\0';
  return text;
}

bool parseUnsignedLong(const char *text, uint32_t &value) {
  if (text == NULL || *text == '\0') return false;
  char *endPointer;
  const unsigned long parsed = strtoul(text, &endPointer, 10);
  if (*endPointer != '\0') return false;
  value = (uint32_t)parsed;
  return true;
}

void resetAutorangeHistory() {
  for (uint8_t index = 0; index < 2; ++index) {
    narrowPersistence[index] = 0;
    rangeCooldown[index] = 0;
  }
}

void setDividerValue(const char *name, uint32_t value) {
  uint32_t candidateRAhigh = RAhigh;
  uint32_t candidateRAlow = RAlow;
  uint32_t candidateRBhigh = RBhigh;
  uint32_t candidateRBlow = RBlow;

  if (strcmp(name, "RAhigh") == 0) candidateRAhigh = value;
  else if (strcmp(name, "RAlow") == 0) candidateRAlow = value;
  else if (strcmp(name, "RBhigh") == 0) candidateRBhigh = value;
  else if (strcmp(name, "RBlow") == 0) candidateRBlow = value;
  else {
    Serial.println(F("ERR,UNKNOWN_DIVIDER"));
    return;
  }

  if (!dividerPairIsValid(candidateRAhigh, candidateRAlow) ||
      !dividerPairIsValid(candidateRBhigh, candidateRBlow)) {
    Serial.println(F("ERR,INVALID_RESISTOR,valid range is 1..100000000 ohms and multiplier <= 1000"));
    return;
  }

  RAhigh = candidateRAhigh;
  RAlow = candidateRAlow;
  RBhigh = candidateRBhigh;
  RBlow = candidateRBlow;
  saveDividerConfig();
  Serial.print(F("OK,"));
  Serial.print(name);
  Serial.print(',');
  Serial.println((unsigned long)value);
  printDividerStatus();
}

void handleRangeCommand(char *arguments) {
  if (autorangeEnabled) {
    Serial.println(F("ERR,AUTORANGE_ENABLED,disable autorange before setting a manual range"));
    return;
  }
  char *savePointer;
  char *channel = strtok_r(arguments, ",", &savePointer);
  char *rangeText = strtok_r(NULL, ",", &savePointer);
  char *extra = strtok_r(NULL, ",", &savePointer);
  if (channel == NULL || rangeText == NULL || extra != NULL) {
    Serial.println(F("ERR,RANGE_FORMAT,use range,A|B|both,6.144|4.096|2.048|1.024|0.512|0.256"));
    return;
  }
  channel = trimWhitespace(channel);
  rangeText = trimWhitespace(rangeText);
  uint8_t requestedRange;
  if (!parseRangeValue(rangeText, requestedRange)) {
    Serial.println(F("ERR,INVALID_RANGE,valid values are 6.144,4.096,2.048,1.024,0.512,0.256"));
    return;
  }
  if (strcmp(channel, "A") == 0 || strcmp(channel, "a") == 0) {
    activeRange[0] = requestedRange;
  } else if (strcmp(channel, "B") == 0 || strcmp(channel, "b") == 0) {
    activeRange[1] = requestedRange;
  } else if (strcmp(channel, "both") == 0) {
    activeRange[0] = requestedRange;
    activeRange[1] = requestedRange;
  } else {
    Serial.println(F("ERR,INVALID_CHANNEL,use A, B, or both"));
    return;
  }
  resetAutorangeHistory();
  Serial.print(F("OK,range,"));
  Serial.print(channel);
  Serial.print(',');
  printRangeValue(requestedRange);
  Serial.println();
  printRangeStatus();
}

void handleCommand(char *rawCommand) {
  char *command = trimWhitespace(rawCommand);
  if (*command == '\0') return;

  if (strcmp(command, "idn?") == 0) {
    Serial.println(F("Modern Lab Dual-Channel Data Acquisition"));
    Serial.println(F("#protocol,modern-lab-dual-channel,4"));
    return;
  }
  if (strcmp(command, "run") == 0) {
    acquisitionRunning = true;
    lastAcquisitionMs = 0;
    Serial.println(F("OK,run"));
    return;
  }
  if (strcmp(command, "stop") == 0) {
    acquisitionRunning = false;
    Serial.println(F("OK,stop"));
    return;
  }
  if (strcmp(command, "range?") == 0) {
    printRangeStatus();
    return;
  }
  if (strcmp(command, "autorange?") == 0) {
    printAutorangeStatus();
    printRangeStatus();
    return;
  }
  if (strcmp(command, "dividers?") == 0) {
    printDividerStatus();
    return;
  }
  if (strcmp(command, "defaults") == 0) {
    restoreDefaultDividers(true);
    Serial.println(F("OK,defaults"));
    printDividerStatus();
    return;
  }

  char *comma = strchr(command, ',');
  if (comma == NULL) {
    Serial.println(F("ERR,UNKNOWN_COMMAND"));
    return;
  }
  *comma = '\0';
  char *name = trimWhitespace(command);
  char *arguments = trimWhitespace(comma + 1);

  if (strcmp(name, "range") == 0) {
    handleRangeCommand(arguments);
    return;
  }
  if (strcmp(name, "autorange") == 0) {
    if (strcmp(arguments, "0") != 0 && strcmp(arguments, "1") != 0) {
      Serial.println(F("ERR,AUTORANGE_FORMAT,use autorange,0 or autorange,1"));
      return;
    }
    autorangeEnabled = strcmp(arguments, "1") == 0;
    resetAutorangeHistory();
    Serial.print(F("OK,autorange,"));
    Serial.println(autorangeEnabled ? 1 : 0);
    printAutorangeStatus();
    printRangeStatus();
    return;
  }
  if (strcmp(name, "delay") == 0) {
    uint32_t value;
    if (!parseUnsignedLong(arguments, value) || value > 60000UL) {
      Serial.println(F("ERR,INVALID_DELAY,valid range is 0..60000 ms"));
      return;
    }
    acquisitionDelayMs = (uint16_t)value;
    Serial.print(F("OK,delay,"));
    Serial.println(acquisitionDelayMs);
    return;
  }
  if (strcmp(name, "avg") == 0) {
    uint32_t value;
    if (!parseUnsignedLong(arguments, value) || value < 1 || value > MAX_AVERAGE_COUNT) {
      Serial.println(F("ERR,INVALID_AVERAGE,valid range is 1..64"));
      return;
    }
    averageCount = (uint8_t)value;
    Serial.print(F("OK,avg,"));
    Serial.println(averageCount);
    return;
  }
  if (strcmp(name, "RAhigh") == 0 || strcmp(name, "RAlow") == 0 ||
      strcmp(name, "RBhigh") == 0 || strcmp(name, "RBlow") == 0) {
    uint32_t value;
    if (!parseUnsignedLong(arguments, value)) {
      Serial.println(F("ERR,INVALID_RESISTOR,expected an integer number of ohms"));
      return;
    }
    setDividerValue(name, value);
    return;
  }
  Serial.println(F("ERR,UNKNOWN_COMMAND"));
}

void serviceSerial() {
  while (Serial.available() > 0) {
    const char incoming = (char)Serial.read();
    if (incoming == '\r') continue;
    if (incoming == '\n') {
      if (commandOverflow) {
        Serial.println(F("ERR,COMMAND_TOO_LONG"));
      } else {
        commandBuffer[commandLength] = '\0';
        handleCommand(commandBuffer);
      }
      commandLength = 0;
      commandOverflow = false;
    } else if (!commandOverflow) {
      if (commandLength + 1 < COMMAND_BUFFER_SIZE) {
        commandBuffer[commandLength++] = incoming;
      } else {
        commandOverflow = true;
      }
    }
  }
}

void setup() {
  Serial.begin(SERIAL_BAUD);
  Wire.begin();
  loadDividerConfig();

  Serial.println(F("Modern Lab Dual-Channel Data Acquisition"));
  Serial.println(F("#protocol,modern-lab-dual-channel,4"));
  if (!ads.begin(ADS1115_I2C_ADDRESS)) {
    Serial.println(F("ERR,ADS1115_NOT_FOUND"));
    while (true) {
      serviceSerial();
      delay(100);
    }
  }
  ads.setDataRate(RATE_ADS1115_860SPS);
  printAutorangeStatus();
  printRangeStatus();
  printDividerStatus();
}

void loop() {
  serviceSerial();
  if (!acquisitionRunning) return;

  const unsigned long now = millis();
  if (lastAcquisitionMs == 0 || now - lastAcquisitionMs >= acquisitionDelayMs) {
    lastAcquisitionMs = now;
    acquireAndEmitRecord();
  }
}
