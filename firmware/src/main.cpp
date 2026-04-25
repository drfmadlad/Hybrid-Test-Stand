/**
 * Hybrid Test Stand — Unified Firmware
 * Target: Teensy 4.1
 *
 * Combines:
 *   - CAN bus sniffer with on-board J1939 decoding  (RPM, coolant, battery, fuel)
 *   - Throttle servo control   (DFRobot SER0060, 270 deg, Pin 23)
 *   - BMP388 barometric sensor (I2C 0x76)
 *   - Lambda / AFR analog      (A0, 0-2.02 V)
 *   - SLF3S-4000B flow sensor  (I2C 0x08)
 *
 * Wiring:
 *   CAN2       Pin 0 (RX), Pin 1 (TX) -> transceiver -> vehicle harness
 *   Servo      Pin 23 (signal), external 5-8.4 V supply
 *   BMP388     I2C 0x76 — Pin 18 (SDA0), Pin 19 (SCL0)
 *   SLF3S      I2C 0x08 — Pin 18 (SDA0), Pin 19 (SCL0)  [shared bus]
 *   Lambda     A0 (Pin 14), 0-2.02 V range
 *
 * Serial protocol (2 Mbps USB):
 *   Telemetry : $T,ms,rpm,coolant_c,batt_v,fuel_pct,lambda,bmp_c,bmp_hpa,bmp_alt_m,flow_ml,servo_pct
 *   CAN raw   : $C,ms,seq,0xID,ext,len,d0..d7  (when enabled with 'd')
 *   Status    : # ...
 *
 * Commands:
 *   Servo : a<deg>  t<pct>  u<us>  w(sweep)  z(close)  f(full)  setmin  setmax
 *   CAN   : 1(125k) 2(250k) 5(500k) m(1M)  r(reset seq)
 *   Flow  : i(product ID)  x(soft reset)
 *   BMP   : p<hPa> (sea-level ref)
 *   Mode  : d(toggle CAN raw dump)
 *   General: s(status)  h(help)
 */

#include <Arduino.h>
#include <FlexCAN_T4.h>
#include <Wire.h>
#include <Servo.h>
#include <Adafruit_BMP3XX.h>

// =============================================================================
// Configuration
// =============================================================================

static constexpr uint32_t SERIAL_BAUD       = 2000000;
static constexpr uint32_t TELEMETRY_INTERVAL = 100;   // ms  (10 Hz)
static constexpr uint32_t FLOW_READ_INTERVAL = 10;    // ms  (~100 Hz)
static constexpr size_t   RING_SIZE          = 512;    // power of 2

// Servo (DFRobot SER0060, 270 deg)
static constexpr int   SERVO_PIN    = 23;
static constexpr int   SERVO_MIN_US = 500;
static constexpr int   SERVO_MAX_US = 2500;
static constexpr float SERVO_DEG    = 270.0f;
static constexpr int   SERVO_CTR_US = (SERVO_MIN_US + SERVO_MAX_US) / 2;

// Lambda ADC
static constexpr int   LAMBDA_PIN      = A0;
static constexpr int   ADC_BITS        = 12;
static constexpr float ADC_VREF        = 3.3f;
static constexpr float LAMBDA_V_MIN    = 0.0f;
static constexpr float LAMBDA_V_MAX    = 2.02f;
static constexpr float LAMBDA_AT_V_MIN = 0.68f;
static constexpr float LAMBDA_AT_V_MAX = 1.36f;

// SLF3S flow sensor
static constexpr uint8_t SLF3S_ADDR  = 0x08;
static constexpr float   SCALE_FLOW  = 32.0f;   // ml/min per LSB
static constexpr float   SCALE_TEMP  = 200.0f;   // deg-C per LSB

// =============================================================================
// CAN Bus — Ring Buffer + FlexCAN
// =============================================================================

FlexCAN_T4<CAN2, RX_SIZE_256, TX_SIZE_16> can;
static uint32_t canBaudRate = 250000;

static CAN_message_t ringBuf[RING_SIZE];
volatile static size_t ringHead = 0;
volatile static size_t ringTail = 0;
static uint32_t ringOverflows  = 0;
static uint32_t seqNum         = 0;
static bool     canRawDump     = false;

static inline bool ringPush(const CAN_message_t& msg) {
    size_t next = (ringHead + 1) & (RING_SIZE - 1);
    if (next == ringTail) { ringOverflows++; return false; }
    ringBuf[ringHead] = msg;
    __asm__ volatile("dsb");
    ringHead = next;
    return true;
}

static inline bool ringPop(CAN_message_t& out) {
    if (ringHead == ringTail) return false;
    out = ringBuf[ringTail];
    __asm__ volatile("dsb");
    ringTail = (ringTail + 1) & (RING_SIZE - 1);
    return true;
}

void onReceive(const CAN_message_t& msg) { ringPush(msg); }

void initCAN(uint32_t baud) {
    can.begin();
    can.setBaudRate(baud);
    can.setMaxMB(16);
    can.enableFIFO();
    can.enableFIFOInterrupt();
    for (int i = 0; i < 14; i++) can.setMB((FLEXCAN_MAILBOX)i, RX, STD);
    can.setMB(MB14, RX, EXT);
    can.setMB(MB15, RX, EXT);
    can.onReceive(onReceive);
    Serial.print("# CAN2 @ "); Serial.print(baud / 1000); Serial.println(" kbps");
}

// =============================================================================
// J1939 Decoded Values  (updated from CAN frames)
// =============================================================================

static float canRPM     = NAN;   // engine speed
static float canCoolant = NAN;   // deg-C
static float canBattV   = NAN;   // volts
static float canFuelPct = NAN;   // percent

static void j1939Decode(uint32_t canId, const uint8_t* d, uint8_t len) {
    uint8_t pf = (canId >> 16) & 0xFF;
    uint16_t pgn;
    if (pf >= 0xF0) {
        pgn = ((uint16_t)pf << 8) | ((canId >> 8) & 0xFF);
    } else {
        pgn = (uint16_t)pf << 8;
    }

    switch (pgn) {
        case 0xF004:   // EEC1 — Engine Speed (SPN 190)
            if (len >= 5) {
                uint16_t raw = (uint16_t)d[4] << 8 | d[3];
                if (raw != 0xFFFF) canRPM = raw * 0.125f;
            }
            break;
        case 0xFF66:   // Polaris proprietary RPM (1 RPM/bit)
            if (len >= 2) {
                uint16_t raw = (uint16_t)d[1] << 8 | d[0];
                if (raw != 0xFFFF) canRPM = (float)raw;
            }
            break;
        case 0xFEEE:   // ET1 — Coolant Temperature (SPN 110)
            if (len >= 1 && d[0] != 0xFF)
                canCoolant = (float)d[0] - 40.0f;
            break;
        case 0xFEF7:   // VEP — Battery Voltage (SPN 168, bytes 5-6)
            if (len >= 6) {
                uint16_t raw = (uint16_t)d[5] << 8 | d[4];
                if (raw != 0xFFFF) canBattV = raw * 0.05f;
            }
            break;
        case 0xFEFC:   // Instrument cluster — Fuel Level (byte 1, 0.4 %/bit)
            if (len >= 2 && d[1] != 0xFF)
                canFuelPct = d[1] * 0.4f;
            break;
    }
}

// =============================================================================
// Servo
// =============================================================================

Servo throttleServo;
static int throttleMinUs = SERVO_MIN_US;
static int throttleMaxUs = SERVO_MAX_US;
static int currentUs     = SERVO_CTR_US;

void setMicroseconds(int us) {
    us = constrain(us, SERVO_MIN_US, SERVO_MAX_US);
    currentUs = us;
    throttleServo.writeMicroseconds(us);
}

void setAngle(float deg) {
    deg = constrain(deg, 0.0f, SERVO_DEG);
    setMicroseconds((int)(SERVO_MIN_US + (deg / SERVO_DEG) * (SERVO_MAX_US - SERVO_MIN_US)));
}

void setThrottlePercent(float pct) {
    pct = constrain(pct, 0.0f, 100.0f);
    setMicroseconds((int)(throttleMinUs + (pct / 100.0f) * (throttleMaxUs - throttleMinUs)));
}

float getCurrentAngle() {
    return (float)(currentUs - SERVO_MIN_US) / (SERVO_MAX_US - SERVO_MIN_US) * SERVO_DEG;
}

float getCurrentThrottlePercent() {
    if (throttleMaxUs == throttleMinUs) return 0.0f;
    return constrain(
        (float)(currentUs - throttleMinUs) / (throttleMaxUs - throttleMinUs) * 100.0f,
        0.0f, 100.0f);
}

void runSweep() {
    Serial.println("# Sweep: 0% -> 100% -> 0%");
    uint32_t start = millis();
    while (millis() - start < 2000) {
        setMicroseconds((int)(throttleMinUs +
            (millis() - start) / 2000.0f * (throttleMaxUs - throttleMinUs)));
        delay(20);
    }
    setMicroseconds(throttleMaxUs);
    delay(200);
    start = millis();
    while (millis() - start < 2000) {
        setMicroseconds((int)(throttleMaxUs +
            (millis() - start) / 2000.0f * (throttleMinUs - throttleMaxUs)));
        delay(20);
    }
    setMicroseconds(throttleMinUs);
    Serial.println("# Sweep complete.");
}

// =============================================================================
// Lambda ADC
// =============================================================================

float readLambdaVoltage() {
    uint32_t sum = 0;
    for (int i = 0; i < 16; i++) sum += analogRead(LAMBDA_PIN);
    return (sum / 16.0f) / ((1 << ADC_BITS) - 1) * ADC_VREF;
}

float voltageToLambda(float v) {
    float lam = LAMBDA_AT_V_MIN +
        (v - LAMBDA_V_MIN) / (LAMBDA_V_MAX - LAMBDA_V_MIN) *
        (LAMBDA_AT_V_MAX - LAMBDA_AT_V_MIN);
    return constrain(lam, LAMBDA_AT_V_MIN, LAMBDA_AT_V_MAX);
}

// =============================================================================
// BMP388
// =============================================================================

Adafruit_BMP3XX bmp;
static float seaLevelPressureHpa = 1013.25f;
static bool  bmpOk = false;

// =============================================================================
// SLF3S Flow Sensor
// =============================================================================

static bool  flowRunning = false;
static float flowRate    = NAN;   // ml/min
static float flowTemp    = NAN;   // deg-C

static uint8_t flowCRC8(uint8_t b1, uint8_t b2) {
    uint8_t crc = 0xFF;
    uint8_t data[2] = {b1, b2};
    for (int i = 0; i < 2; i++) {
        crc ^= data[i];
        for (int b = 0; b < 8; b++)
            crc = (crc & 0x80) ? (crc << 1) ^ 0x31 : (crc << 1);
    }
    return crc;
}

static bool flowStartContinuous() {
    Wire.beginTransmission(SLF3S_ADDR);
    Wire.write(0x36);
    Wire.write(0x08);   // H2O calibration
    uint8_t err = Wire.endTransmission();
    if (err != 0) {
        Serial.print("# Flow sensor I2C error: "); Serial.println(err);
        return false;
    }
    flowRunning = true;
    delay(50);
    return true;
}

static void flowStopContinuous() {
    Wire.beginTransmission(SLF3S_ADDR);
    Wire.write(0x3F);
    Wire.write(0xF9);
    Wire.endTransmission();
    flowRunning = false;
    delayMicroseconds(500);
}

static bool flowRead() {
    if (!flowRunning) return false;
    Wire.requestFrom((uint8_t)SLF3S_ADDR, (uint8_t)9);
    if (Wire.available() < 9) return false;

    uint8_t b[9];
    for (int i = 0; i < 9; i++) b[i] = Wire.read();

    bool ok = (flowCRC8(b[0], b[1]) == b[2]) &&
              (flowCRC8(b[3], b[4]) == b[5]) &&
              (flowCRC8(b[6], b[7]) == b[8]);
    if (ok) {
        flowRate = (int16_t)((b[0] << 8) | b[1]) / SCALE_FLOW;
        flowTemp = (int16_t)((b[3] << 8) | b[4]) / SCALE_TEMP;
    }
    return ok;
}

static void flowReadProductId() {
    if (flowRunning) flowStopContinuous();

    Wire.beginTransmission(SLF3S_ADDR);
    Wire.write(0x36); Wire.write(0x7C);
    Wire.endTransmission(false);
    Wire.beginTransmission(SLF3S_ADDR);
    Wire.write(0xE1); Wire.write(0x02);
    Wire.endTransmission(false);

    Wire.requestFrom((uint8_t)SLF3S_ADDR, (uint8_t)18);
    if (Wire.available() < 18) {
        Serial.println("# ERROR: Could not read flow sensor product ID");
        flowStartContinuous();
        return;
    }
    uint8_t buf[18];
    for (int i = 0; i < 18; i++) buf[i] = Wire.read();

    uint32_t prod = ((uint32_t)buf[0] << 24) | ((uint32_t)buf[1] << 16) |
                    ((uint32_t)buf[3] << 8)  |  (uint32_t)buf[4];
    Serial.print("# Flow sensor product: 0x"); Serial.println(prod, HEX);

    flowStartContinuous();
}

static void flowSoftReset() {
    Wire.beginTransmission(0x00);
    Wire.write(0x06);
    Wire.endTransmission();
    flowRunning = false;
    delay(25);
    Serial.println("# Flow sensor reset. Restarting...");
    flowStartContinuous();
}

// =============================================================================
// Telemetry Output
// =============================================================================

static float lastLambda   = NAN;
static float lastBmpTemp  = NAN;
static float lastBmpPress = NAN;
static float lastBmpAlt   = NAN;

static void printFloat(float v) {
    if (isnan(v)) Serial.print("nan");
    else          Serial.print(v, 2);
}

static void sendTelemetry() {
    // Sample BMP388
    if (bmpOk && bmp.performReading()) {
        lastBmpTemp  = bmp.temperature;
        lastBmpPress = bmp.pressure / 100.0f;
        lastBmpAlt   = bmp.readAltitude(seaLevelPressureHpa);
    }

    // Sample lambda
    lastLambda = voltageToLambda(readLambdaVoltage());

    // $T,ms,rpm,coolant,battv,fuel,lambda,bmptemp,bmphpa,bmpalt,flow,servo
    Serial.print("$T,");
    Serial.print(millis());                  Serial.print(',');
    printFloat(canRPM);                      Serial.print(',');
    printFloat(canCoolant);                  Serial.print(',');
    printFloat(canBattV);                    Serial.print(',');
    printFloat(canFuelPct);                  Serial.print(',');
    printFloat(lastLambda);                  Serial.print(',');
    printFloat(lastBmpTemp);                 Serial.print(',');
    printFloat(lastBmpPress);                Serial.print(',');
    printFloat(lastBmpAlt);                  Serial.print(',');
    printFloat(flowRate);                    Serial.print(',');
    printFloat(getCurrentThrottlePercent());
    Serial.println();
}

// =============================================================================
// CAN Raw Dump  (optional, toggled with 'd')
// =============================================================================

static void printCANFrame(const CAN_message_t& msg) {
    Serial.print("$C,");
    Serial.print(millis());   Serial.print(',');
    Serial.print(seqNum);     Serial.print(',');
    Serial.print("0x");
    if (!msg.flags.extended) {
        if (msg.id < 0x100) Serial.print('0');
        if (msg.id < 0x010) Serial.print('0');
    }
    Serial.print(msg.id, HEX); Serial.print(',');
    Serial.print(msg.flags.extended ? 1 : 0); Serial.print(',');
    Serial.print(msg.len);
    for (int i = 0; i < 8; i++) {
        Serial.print(',');
        if (i < msg.len) {
            if (msg.buf[i] < 0x10) Serial.print('0');
            Serial.print(msg.buf[i], HEX);
        } else {
            Serial.print("--");
        }
    }
    Serial.println();
}

// =============================================================================
// Serial Commands
// =============================================================================

static String serialBuf;

static void printHelp() {
    Serial.println("# === Commands ===");
    Serial.println("# Servo : a<deg> t<pct> u<us> w(sweep) z(close) f(full) setmin setmax");
    Serial.println("# CAN   : 1/2/5/m(baud)  r(reset seq)");
    Serial.println("# Flow  : i(product ID)   x(soft reset)");
    Serial.println("# BMP   : p<hPa>(sea-level ref)");
    Serial.println("# Mode  : d(toggle CAN raw dump)");
    Serial.println("# General: s(status) h(help)");
}

static void printStatus() {
    Serial.println("# === Status ===");
    Serial.print("# Servo   : "); Serial.print(getCurrentThrottlePercent(), 1);
    Serial.print("% ("); Serial.print(currentUs); Serial.println(" us)");
    Serial.print("#   min="); Serial.print(throttleMinUs);
    Serial.print(" max="); Serial.println(throttleMaxUs);
    Serial.print("# CAN     : "); Serial.print(canBaudRate / 1000);
    Serial.print(" kbps  frames="); Serial.print(seqNum);
    Serial.print("  overflows="); Serial.println(ringOverflows);
    Serial.print("# CAN dump: "); Serial.println(canRawDump ? "ON" : "OFF");
    Serial.print("# BMP388  : "); Serial.println(bmpOk ? "OK" : "NOT FOUND");
    Serial.print("# Flow    : "); Serial.println(flowRunning ? "Running" : "Idle");
    Serial.print("# J1939   : RPM="); printFloat(canRPM);
    Serial.print("  Coolant="); printFloat(canCoolant);
    Serial.print("  BattV="); printFloat(canBattV);
    Serial.print("  Fuel="); printFloat(canFuelPct);
    Serial.println();
}

static void handleCommand(const String& cmd) {
    String c = cmd;
    c.trim();
    if (c.length() == 0) return;

    // Multi-char commands
    if (c.equalsIgnoreCase("setmin")) {
        throttleMinUs = currentUs;
        Serial.print("# Closed-throttle set to "); Serial.print(throttleMinUs); Serial.println(" us");
        return;
    }
    if (c.equalsIgnoreCase("setmax")) {
        throttleMaxUs = currentUs;
        Serial.print("# Full-throttle set to "); Serial.print(throttleMaxUs); Serial.println(" us");
        return;
    }

    char prefix = c.charAt(0);
    switch (prefix) {
        // ── Servo ────────────────────────────────────────────
        case 'a': case 'A':
            setAngle(c.substring(1).toFloat());
            Serial.print("# Angle -> "); Serial.print(getCurrentAngle(), 1);
            Serial.print(" deg ("); Serial.print(currentUs); Serial.println(" us)");
            break;
        case 't': case 'T':
            setThrottlePercent(c.substring(1).toFloat());
            Serial.print("# Throttle -> "); Serial.print(getCurrentThrottlePercent(), 1);
            Serial.print("% ("); Serial.print(currentUs); Serial.println(" us)");
            break;
        case 'u': case 'U':
            setMicroseconds(c.substring(1).toInt());
            Serial.print("# Pulse -> "); Serial.print(currentUs); Serial.println(" us");
            break;
        case 'w': case 'W': runSweep(); break;
        case 'z': case 'Z':
            setMicroseconds(throttleMinUs);
            Serial.println("# Closed throttle (0%)");
            break;
        case 'f': case 'F':
            setMicroseconds(throttleMaxUs);
            Serial.println("# Full throttle (100%)");
            break;
        // ── CAN baud ────────────────────────────────────────
        case '1': canBaudRate = 125000;  initCAN(canBaudRate); break;
        case '2': canBaudRate = 250000;  initCAN(canBaudRate); break;
        case '5': canBaudRate = 500000;  initCAN(canBaudRate); break;
        case 'm': case 'M': canBaudRate = 1000000; initCAN(canBaudRate); break;
        case 'r': case 'R':
            noInterrupts(); seqNum = 0; interrupts();
            Serial.println("# Sequence counter reset.");
            break;
        // ── Flow sensor ──────────────────────────────────────
        case 'i': case 'I': flowReadProductId(); break;
        case 'x': case 'X': flowSoftReset();     break;
        // ── BMP388 sea-level ref ─────────────────────────────
        case 'p': case 'P':
            if (c.length() > 1) {
                float p = c.substring(1).toFloat();
                if (p > 800.0f && p < 1100.0f) {
                    seaLevelPressureHpa = p;
                    Serial.print("# Sea-level pressure: ");
                    Serial.print(seaLevelPressureHpa, 2); Serial.println(" hPa");
                } else {
                    Serial.println("# ERROR: Pressure must be 800-1100 hPa");
                }
            }
            break;
        // ── Toggle CAN raw dump ──────────────────────────────
        case 'd': case 'D':
            canRawDump = !canRawDump;
            Serial.print("# CAN raw dump: "); Serial.println(canRawDump ? "ON" : "OFF");
            break;
        // ── General ──────────────────────────────────────────
        case 's': case 'S': printStatus(); break;
        case 'h': case 'H': case '?': printHelp(); break;
        default:
            Serial.print("# Unknown: "); Serial.println(c);
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
// Setup
// =============================================================================

void setup() {
    Serial.begin(SERIAL_BAUD);
    uint32_t t0 = millis();
    while (!Serial && millis() - t0 < 3000) {}
    delay(100);

    Serial.println("# ============================================");
    Serial.println("# Hybrid Test Stand — Unified Firmware");
    Serial.println("# Teensy 4.1");
    Serial.println("# ============================================");

    // I2C bus (shared by BMP388 + SLF3S)
    Wire.begin();
    Wire.setClock(400000);

    // Servo
    throttleServo.attach(SERVO_PIN, SERVO_MIN_US, SERVO_MAX_US);
    setMicroseconds(SERVO_CTR_US);
    Serial.println("# Servo: attached (center position)");

    // Lambda ADC
    analogReadResolution(ADC_BITS);
    pinMode(LAMBDA_PIN, INPUT);
    Serial.println("# Lambda ADC: ready on A0");

    // BMP388
    if (bmp.begin_I2C(0x76)) {
        bmp.setTemperatureOversampling(BMP3_NO_OVERSAMPLING);
        bmp.setPressureOversampling(BMP3_OVERSAMPLING_8X);
        bmp.setIIRFilterCoeff(BMP3_IIR_FILTER_COEFF_3);
        bmp.setOutputDataRate(BMP3_ODR_50_HZ);
        bmpOk = true;
        Serial.println("# BMP388: OK");
    } else {
        Serial.println("# BMP388: NOT FOUND (continuing without)");
    }

    // Flow sensor
    delay(25);
    if (flowStartContinuous()) {
        Serial.println("# SLF3S flow sensor: running");
    } else {
        Serial.println("# SLF3S flow sensor: NOT FOUND (continuing without)");
    }

    // CAN bus
    initCAN(canBaudRate);
    pinMode(LED_BUILTIN, OUTPUT);

    printHelp();
    Serial.println("# Telemetry: $T,ms,rpm,coolant,battv,fuel,lambda,bmptemp,bmphpa,bmpalt,flow,servo");
    Serial.println();
}

// =============================================================================
// Main Loop
// =============================================================================

void loop() {
    can.events();
    readSerial();

    // Drain CAN ring buffer + decode J1939
    CAN_message_t msg;
    while (ringPop(msg)) {
        seqNum++;
        if (msg.flags.extended) {
            j1939Decode(msg.id, msg.buf, msg.len);
        }
        if (canRawDump) printCANFrame(msg);
    }

    // Read flow sensor at ~100 Hz
    static uint32_t lastFlowRead = 0;
    if (flowRunning && millis() - lastFlowRead >= FLOW_READ_INTERVAL) {
        lastFlowRead = millis();
        flowRead();
    }

    // Send telemetry at 10 Hz
    static uint32_t lastTelemetry = 0;
    if (millis() - lastTelemetry >= TELEMETRY_INTERVAL) {
        lastTelemetry = millis();
        sendTelemetry();
    }

    // LED blink on CAN activity
    static uint32_t lastBlink = 0;
    if (millis() - lastBlink >= 100) {
        lastBlink = millis();
        digitalWrite(LED_BUILTIN, seqNum & 1);
    }
}
