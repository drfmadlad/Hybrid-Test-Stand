/**
 * SLF3S-4000B Fuel Flow Sensor — Integration Test
 * Target: Teensy 4.1
 *
 * Wiring:
 *   Sensor Pin 1 (IRQn) → GND  (interrupt not used)
 *   Sensor Pin 2 (SDA)  → Teensy Pin 18 (SDA0) + 4.7 kΩ pull-up to 3.3V
 *   Sensor Pin 3 (VDD)  → Teensy 3.3V  (DO NOT use 5V)
 *   Sensor Pin 4 (GND)  → GND
 *   Sensor Pin 5 (SCL)  → Teensy Pin 19 (SCL0) + 4.7 kΩ pull-up to 3.3V
 *   Sensor Pin 6 (n.c.) → GND or leave floating
 *
 *   Connector: Molex PicoBlade 6-pin (1.25 mm pitch)
 *   I2C address: 0x08 (default)
 *
 * Serial Commands (2000000 baud):
 *   r   — Single reading (flow + temp + flags)
 *   c   — Continuous reading at ~100 Hz (any key to stop)
 *   i   — Read product ID and serial number
 *   x   — Soft reset (general call 0x00 + 0x06)
 *   s   — Status
 *   h   — Help
 */

#include <Arduino.h>
#include <Wire.h>

// =============================================================================
// Config
// =============================================================================

static constexpr uint32_t SERIAL_BAUD    = 2000000;
static constexpr uint8_t  SLF3S_ADDR    = 8;       // 0x08
static constexpr float    SCALE_FLOW    = 32.0f;   // ml/min per LSB
static constexpr float    SCALE_TEMP    = 200.0f;  // °C per LSB
static constexpr uint32_t READ_INTERVAL_MS = 10;   // ~100 Hz

// Fluid calibration — change to 0x36,0x15 for IPA
static constexpr uint8_t  CMD_START_H  = 0x36;
static constexpr uint8_t  CMD_START_L  = 0x08;  // H2O

// =============================================================================
// CRC-8 (poly 0x31, init 0xFF)
// =============================================================================

static uint8_t crc8(uint8_t b1, uint8_t b2) {
    uint8_t crc = 0xFF;
    uint8_t data[2] = {b1, b2};
    for (int i = 0; i < 2; i++) {
        crc ^= data[i];
        for (int b = 0; b < 8; b++) {
            crc = (crc & 0x80) ? (crc << 1) ^ 0x31 : (crc << 1);
        }
    }
    return crc;
}

// =============================================================================
// Sensor control
// =============================================================================

static bool sensorRunning = false;

static bool startContinuous() {
    Wire.beginTransmission(SLF3S_ADDR);
    Wire.write(CMD_START_H);
    Wire.write(CMD_START_L);
    uint8_t err = Wire.endTransmission();
    if (err != 0) {
        Serial.print("# ERROR: I2C write failed (err="); Serial.print(err); Serial.println(")");
        return false;
    }
    sensorRunning = true;
    delay(50); // 12 ms first sample + 38 ms warm-up
    return true;
}

static void stopContinuous() {
    Wire.beginTransmission(SLF3S_ADDR);
    Wire.write(0x3F);
    Wire.write(0xF9);
    Wire.endTransmission();
    sensorRunning = false;
    delayMicroseconds(500);
}

static void softReset() {
    // General call reset: address 0x00, command 0x06
    Wire.beginTransmission(0x00);
    Wire.write(0x06);
    Wire.endTransmission();
    sensorRunning = false;
    delay(25); // sensor takes up to 25 ms to reset
}

// =============================================================================
// Reading
// =============================================================================

struct FlowReading {
    float    flow_ml_min;
    float    temp_c;
    uint16_t flags;
    bool     crc_ok;
};

static bool readSensor(FlowReading& out) {
    if (!sensorRunning) {
        Serial.println("# ERROR: Sensor not running — send 'r' or 'c' to start.");
        return false;
    }

    Wire.requestFrom((uint8_t)SLF3S_ADDR, (uint8_t)9);
    if (Wire.available() < 9) {
        Serial.println("# ERROR: Incomplete I2C read (got fewer than 9 bytes)");
        return false;
    }

    uint8_t flow_msb = Wire.read();
    uint8_t flow_lsb = Wire.read();
    uint8_t flow_crc = Wire.read();
    uint8_t temp_msb = Wire.read();
    uint8_t temp_lsb = Wire.read();
    uint8_t temp_crc = Wire.read();
    uint8_t flag_msb = Wire.read();
    uint8_t flag_lsb = Wire.read();
    uint8_t flag_crc = Wire.read();

    bool crc_ok = (crc8(flow_msb, flow_lsb) == flow_crc) &&
                  (crc8(temp_msb, temp_lsb) == temp_crc) &&
                  (crc8(flag_msb, flag_lsb) == flag_crc);

    out.flow_ml_min = (int16_t)((flow_msb << 8) | flow_lsb) / SCALE_FLOW;
    out.temp_c      = (int16_t)((temp_msb << 8) | temp_lsb) / SCALE_TEMP;
    out.flags       = (uint16_t)((flag_msb << 8) | flag_lsb);
    out.crc_ok      = crc_ok;
    return true;
}

// =============================================================================
// Commands
// =============================================================================

static void takeSingleReading() {
    if (!sensorRunning) {
        Serial.println("# Starting sensor...");
        if (!startContinuous()) return;
    }

    FlowReading r;
    if (!readSensor(r)) return;

    bool high_flow     = (r.flags >> 1) & 0x01;
    bool exp_smoothing = (r.flags >> 5) & 0x01;

    Serial.println("# ----------------------------------------");
    Serial.print("# Flow      : "); Serial.print(r.flow_ml_min, 3); Serial.println(" ml/min");
    Serial.print("# Temp      : "); Serial.print(r.temp_c, 2);      Serial.println(" °C");
    Serial.print("# Flags     : 0x"); Serial.println(r.flags, HEX);
    if (high_flow)     Serial.println("#   [!] HIGH FLOW — reading exceeded ±1000 ml/min limit");
    if (exp_smoothing) Serial.println("#   [!] EXP SMOOTHING active — read rate too slow (<10 Hz)");
    if (!r.crc_ok)     Serial.println("#   [!] CRC MISMATCH — data may be corrupt");
    Serial.println("# ----------------------------------------");
}

static void runContinuous() {
    if (!sensorRunning) {
        Serial.println("# Starting sensor...");
        if (!startContinuous()) return;
    }

    Serial.println("# Continuous mode at ~100 Hz — press any key to stop");
    Serial.println("# t_ms,flow_ml_min,temp_c,flags,crc_ok");

    uint32_t lastRead = millis();
    while (true) {
        if (Serial.available()) { Serial.read(); break; }
        if (millis() - lastRead >= READ_INTERVAL_MS) {
            lastRead = millis();
            FlowReading r;
            if (!readSensor(r)) break;
            Serial.print(millis());
            Serial.print(",");
            Serial.print(r.flow_ml_min, 3);
            Serial.print(",");
            Serial.print(r.temp_c, 2);
            Serial.print(",0x");
            Serial.print(r.flags, HEX);
            Serial.print(",");
            Serial.println(r.crc_ok ? "ok" : "ERR");
        }
    }
    Serial.println("# Continuous mode stopped.");
}

static void readProductId() {
    // Must be idle — stop first if running
    if (sensorRunning) stopContinuous();

    // Two consecutive 16-bit write commands
    Wire.beginTransmission(SLF3S_ADDR);
    Wire.write(0x36); Wire.write(0x7C);
    Wire.endTransmission(false);

    Wire.beginTransmission(SLF3S_ADDR);
    Wire.write(0xE1); Wire.write(0x02);
    Wire.endTransmission(false);

    // Read 18 bytes: 4×(2 data + 1 CRC) = product number (32-bit) + serial (64-bit)
    Wire.requestFrom((uint8_t)SLF3S_ADDR, (uint8_t)18);
    if (Wire.available() < 18) {
        Serial.println("# ERROR: Could not read product ID (fewer than 18 bytes returned)");
        return;
    }

    uint8_t buf[18];
    for (int i = 0; i < 18; i++) buf[i] = Wire.read();

    bool crc_ok = true;
    crc_ok &= (crc8(buf[0], buf[1]) == buf[2]);
    crc_ok &= (crc8(buf[3], buf[4]) == buf[5]);
    crc_ok &= (crc8(buf[6], buf[7]) == buf[8]);
    crc_ok &= (crc8(buf[9], buf[10]) == buf[11]);
    crc_ok &= (crc8(buf[12], buf[13]) == buf[14]);
    crc_ok &= (crc8(buf[15], buf[16]) == buf[17]);

    uint32_t product_num = ((uint32_t)buf[0] << 24) | ((uint32_t)buf[1] << 16) |
                           ((uint32_t)buf[3] << 8)  |  (uint32_t)buf[4];
    uint64_t serial_num  = ((uint64_t)buf[6] << 56) | ((uint64_t)buf[7] << 48) |
                           ((uint64_t)buf[9] << 40) | ((uint64_t)buf[10] << 32) |
                           ((uint64_t)buf[12] << 24)| ((uint64_t)buf[13] << 16) |
                           ((uint64_t)buf[15] << 8) |  (uint64_t)buf[16];

    Serial.println("# ----------------------------------------");
    Serial.print("# Product number : 0x"); Serial.println(product_num, HEX);
    Serial.print("#   (expected 0x07030501 for SLF3S-4000B, lower 8 bits = revision)");
    Serial.println();
    // Print serial as two 32-bit halves (Arduino doesn't support uint64 printf)
    Serial.print("# Serial number  : 0x");
    Serial.print((uint32_t)(serial_num >> 32), HEX);
    Serial.println((uint32_t)(serial_num & 0xFFFFFFFF), HEX);
    if (!crc_ok) Serial.println("#   [!] CRC MISMATCH on product ID data");
    Serial.println("# ----------------------------------------");
}

static void doSoftReset() {
    Serial.println("# Sending soft reset (general call 0x00 + 0x06)...");
    softReset();
    Serial.println("# Reset complete. Sensor is now idle.");
}

static void printStatus() {
    Serial.println();
    Serial.println("# === SLF3S-4000B Status ===");
    Serial.print("# Sensor state : "); Serial.println(sensorRunning ? "Running" : "Idle");
    Serial.print("# I2C address  : 0x"); Serial.println(SLF3S_ADDR, HEX);
    Serial.print("# Fluid cal    : "); Serial.println((CMD_START_L == 0x08) ? "H2O (water)" : "IPA");
    Serial.println("# Read rate    : ~100 Hz (10 ms interval)");
    Serial.println("# Scale flow   : raw / 32  = ml/min");
    Serial.println("# Scale temp   : raw / 200 = °C");
    Serial.println("# ===========================");
    Serial.println();
}

static void printHelp() {
    Serial.println();
    Serial.println("# === Commands ===");
    Serial.println("# r  — Single reading (flow + temp + flags)");
    Serial.println("# c  — Continuous ~100 Hz CSV (any key stops)");
    Serial.println("# i  — Read product ID and serial number");
    Serial.println("# x  — Soft reset (sensor → idle)");
    Serial.println("# s  — Status");
    Serial.println("# h  — This help");
    Serial.println("# =================");
    Serial.println();
}

// =============================================================================
// Command parser
// =============================================================================

static String serialBuf;

static void handleCommand(const String& cmd) {
    String c = cmd;
    c.trim();
    if (c.length() == 0) return;

    switch (c.charAt(0)) {
        case 'r': case 'R': takeSingleReading(); break;
        case 'c': case 'C': runContinuous();     break;
        case 'i': case 'I': readProductId();     break;
        case 'x': case 'X': doSoftReset();       break;
        case 's': case 'S': printStatus();       break;
        case 'h': case 'H': case '?': printHelp(); break;
        default:
            Serial.print("# Unknown command: "); Serial.println(c);
            Serial.println("# Type 'h' for help");
            break;
    }
}

static void readSerial() {
    while (Serial.available()) {
        char ch = Serial.read();
        if (ch == '\n') {
            handleCommand(serialBuf);
            serialBuf = "";
        } else if (ch != '\r') {
            serialBuf += ch;
        }
    }
}

// =============================================================================
// Setup & Loop
// =============================================================================

void setup() {
    Serial.begin(SERIAL_BAUD);
    uint32_t t = millis();
    while (!Serial && millis() - t < 3000) {}

    Serial.println();
    Serial.println("# ============================================");
    Serial.println("# Teensy 4.1 — SLF3S-4000B Flow Sensor Test");
    Serial.println("# ============================================");

    Wire.begin();
    Wire.setClock(400000); // 400 kHz I2C
    Serial.println("# I2C: initialized at 400 kHz");

    delay(25); // Sensor power-up time

    Serial.println("# Sensor: power-up delay complete");
    Serial.println("# Type 'i' to read product ID, 'r' for a reading, 'h' for help.");
    Serial.println();
}

void loop() {
    readSerial();
}
