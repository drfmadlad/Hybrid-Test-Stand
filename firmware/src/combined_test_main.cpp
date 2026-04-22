/**
 * Combined Test — Throttle Servo + BMP388 + Lambda (AFR)
 * Target: Teensy 4.1
 *
 * Wiring:
 *   --- Servo (DFRobot SER0060) ---
 *   Brown  (GND)    → Common GND
 *   Red    (VCC)    → 5–8.4V external supply (NOT Teensy 5V)
 *   Yellow (Signal) → Teensy Pin 23
 *
 *   --- BMP388 (I2C) ---
 *   VIN → Teensy 3.3V
 *   GND → GND
 *   SCK → Teensy Pin 19 (SCL0)
 *   SDI → Teensy Pin 18 (SDA0)
 *   SDO → GND          (I2C address 0x76)
 *   CS  → 3.3V or NC
 *
 *   --- Lambda sensor (analog) ---
 *   Signal → Teensy A0 (Pin 14)   [0–2.02V range]
 *   GND    → GND
 *
 * Serial Commands (2000000 baud):
 *   --- Servo ---
 *   a<0-270>    — Absolute angle        (e.g. a135)
 *   t<0-100>    — Throttle percent      (e.g. t50)
 *   u<500-2500> — Raw pulse µs          (e.g. u1500)
 *   w           — Sweep full range
 *   z           — Closed throttle (0%)
 *   f           — Full throttle (100%)
 *   setmin      — Save current pos as closed-throttle
 *   setmax      — Save current pos as full-throttle
 *   --- Sensors ---
 *   r           — Single reading (BMP388 + lambda)
 *   c           — Continuous 1 Hz readings (any key to stop)
 *   p<hPa>      — Set sea-level pressure ref (e.g. p1013.25)
 *   --- General ---
 *   s           — Status (servo + sensor settings)
 *   h           — Help
 */

#include <Arduino.h>
#include <Wire.h>
#include <Servo.h>
#include <Adafruit_BMP3XX.h>

// =============================================================================
// Config
// =============================================================================

static constexpr uint32_t SERIAL_BAUD       = 2000000;
static constexpr uint32_t PRINT_INTERVAL_MS = 1000;

// Servo
static constexpr int   SERVO_PIN    = 23;
static constexpr int   SERVO_MIN_US = 500;
static constexpr int   SERVO_MAX_US = 2500;
static constexpr float SERVO_DEG    = 270.0f;
static constexpr int   SERVO_CTR_US = (SERVO_MIN_US + SERVO_MAX_US) / 2;

// Lambda ADC
static constexpr int   LAMBDA_PIN      = A0;   // Pin 14
static constexpr int   ADC_BITS        = 12;
static constexpr float ADC_VREF        = 3.3f;
static constexpr float LAMBDA_V_MIN    = 0.0f;
static constexpr float LAMBDA_V_MAX    = 2.02f;
static constexpr float LAMBDA_AT_V_MIN = 0.68f;
static constexpr float LAMBDA_AT_V_MAX = 1.36f;

// =============================================================================
// State
// =============================================================================

Servo throttleServo;
int   throttleMinUs = SERVO_MIN_US;
int   throttleMaxUs = SERVO_MAX_US;
int   currentUs     = SERVO_CTR_US;

Adafruit_BMP3XX bmp;
float seaLevelPressureHpa = 1013.25f;

// =============================================================================
// Servo
// =============================================================================

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
        0.0f, 100.0f
    );
}

void runSweep() {
    Serial.println("# Sweep: 0% -> 100% -> 0% (2s each way)");
    uint32_t start = millis();
    int startUs = throttleMinUs, endUs = throttleMaxUs;

    while (millis() - start < 2000) {
        setMicroseconds((int)(startUs + (millis() - start) / 2000.0f * (endUs - startUs)));
        delay(20);
    }
    setMicroseconds(throttleMaxUs);
    delay(200);

    start = millis();
    while (millis() - start < 2000) {
        setMicroseconds((int)(endUs + (millis() - start) / 2000.0f * (startUs - endUs)));
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
    float lambda = LAMBDA_AT_V_MIN +
                   (v - LAMBDA_V_MIN) / (LAMBDA_V_MAX - LAMBDA_V_MIN) *
                   (LAMBDA_AT_V_MAX - LAMBDA_AT_V_MIN);
    return constrain(lambda, LAMBDA_AT_V_MIN, LAMBDA_AT_V_MAX);
}

// =============================================================================
// Sensor reading
// =============================================================================

bool takeSingleReading() {
    if (!bmp.performReading()) {
        Serial.println("# ERROR: BMP388 read failed!");
        return false;
    }

    float lambdaV = readLambdaVoltage();

    Serial.println("# ----------------------------------------");
    Serial.print("# Temperature : "); Serial.print(bmp.temperature, 2);              Serial.println(" °C");
    Serial.print("# Pressure    : "); Serial.print(bmp.pressure / 100.0f, 4);        Serial.println(" hPa");
    Serial.print("# Altitude    : "); Serial.print(bmp.readAltitude(seaLevelPressureHpa), 2); Serial.println(" m");
    Serial.print("# Lambda V    : "); Serial.print(lambdaV, 3);                       Serial.println(" V");
    Serial.print("# Lambda      : "); Serial.print(voltageToLambda(lambdaV), 3);      Serial.println(" λ");
    Serial.println("# ----------------------------------------");
    return true;
}

void runContinuous() {
    Serial.println("# Continuous mode — press any key to stop");
    uint32_t lastPrint = 0;
    while (true) {
        if (Serial.available()) { Serial.read(); break; }
        if (millis() - lastPrint >= PRINT_INTERVAL_MS) {
            lastPrint = millis();
            takeSingleReading();
        }
    }
    Serial.println("# Continuous mode stopped.");
}

// =============================================================================
// Status & Help
// =============================================================================

void printStatus() {
    Serial.println();
    Serial.println("# === Servo Status ===");
    Serial.print("# Current pulse  : "); Serial.print(currentUs);                    Serial.println(" µs");
    Serial.print("# Current angle  : "); Serial.print(getCurrentAngle(), 1);         Serial.println("°");
    Serial.print("# Throttle %     : "); Serial.print(getCurrentThrottlePercent(), 1); Serial.println("%");
    Serial.print("# Min (closed)   : "); Serial.print(throttleMinUs);                Serial.println(" µs");
    Serial.print("# Max (full)     : "); Serial.print(throttleMaxUs);                Serial.println(" µs");
    Serial.println("# === Sensor Settings ===");
    Serial.print("# Sea-level ref  : "); Serial.print(seaLevelPressureHpa, 2);       Serial.println(" hPa");
    Serial.println("# BMP388 oversamp: pressure x8, temp x1, IIR coeff 3");
    Serial.println("# Lambda scale   : 0.00V = 0.68λ  /  2.02V = 1.36λ");
    Serial.println("# =======================");
    Serial.println();
}

void printHelp() {
    Serial.println();
    Serial.println("# === Commands ===");
    Serial.println("# -- Servo --");
    Serial.println("# a<deg>    — Absolute angle 0–270  (e.g. a135)");
    Serial.println("# t<pct>    — Throttle % 0–100      (e.g. t50)");
    Serial.println("# u<us>     — Raw pulse µs           (e.g. u1500)");
    Serial.println("# w         — Sweep 0%->100%->0%");
    Serial.println("# z         — Closed throttle (0%)");
    Serial.println("# f         — Full throttle (100%)");
    Serial.println("# setmin    — Save current pos as 0%");
    Serial.println("# setmax    — Save current pos as 100%");
    Serial.println("# -- Sensors --");
    Serial.println("# r         — Single reading");
    Serial.println("# c         — Continuous 1 Hz (any key stops)");
    Serial.println("# p<hPa>    — Set sea-level pressure (e.g. p1013.25)");
    Serial.println("# -- General --");
    Serial.println("# s         — Status");
    Serial.println("# h         — This help");
    Serial.println("# =================");
    Serial.println();
}

// =============================================================================
// Command Parser
// =============================================================================

static String serialBuf;

void handleCommand(const String& cmd) {
    String c = cmd;
    c.trim();
    if (c.length() == 0) return;

    if (c.equalsIgnoreCase("setmin")) {
        throttleMinUs = currentUs;
        Serial.print("# Closed-throttle set to "); Serial.print(throttleMinUs); Serial.println(" µs");
        return;
    }
    if (c.equalsIgnoreCase("setmax")) {
        throttleMaxUs = currentUs;
        Serial.print("# Full-throttle set to "); Serial.print(throttleMaxUs); Serial.println(" µs");
        return;
    }

    char prefix = c.charAt(0);
    switch (prefix) {
        // Servo
        case 'a': case 'A':
            setAngle(c.substring(1).toFloat());
            Serial.print("# Angle -> "); Serial.print(getCurrentAngle(), 1);
            Serial.print("°  ("); Serial.print(currentUs); Serial.println(" µs)");
            break;
        case 't': case 'T':
            setThrottlePercent(c.substring(1).toFloat());
            Serial.print("# Throttle -> "); Serial.print(getCurrentThrottlePercent(), 1);
            Serial.print("%  ("); Serial.print(currentUs); Serial.println(" µs)");
            break;
        case 'u': case 'U':
            setMicroseconds(c.substring(1).toInt());
            Serial.print("# Pulse -> "); Serial.print(currentUs); Serial.println(" µs");
            break;
        case 'w': case 'W': runSweep();           break;
        case 'z': case 'Z':
            setMicroseconds(throttleMinUs);
            Serial.println("# Closed throttle (0%)");
            break;
        case 'f': case 'F':
            setMicroseconds(throttleMaxUs);
            Serial.println("# Full throttle (100%)");
            break;
        // Sensors
        case 'r': case 'R': takeSingleReading();  break;
        case 'c': case 'C': runContinuous();      break;
        case 'p': case 'P':
            if (c.length() > 1) {
                float p = c.substring(1).toFloat();
                if (p > 800.0f && p < 1100.0f) {
                    seaLevelPressureHpa = p;
                    Serial.print("# Sea-level pressure set to ");
                    Serial.print(seaLevelPressureHpa, 2); Serial.println(" hPa");
                } else {
                    Serial.println("# ERROR: Pressure must be 800–1100 hPa");
                }
            } else {
                Serial.println("# Usage: p<hPa>  e.g. p1013.25");
            }
            break;
        // General
        case 's': case 'S': printStatus(); break;
        case 'h': case 'H': case '?': printHelp(); break;
        default:
            Serial.print("# Unknown command: "); Serial.println(c);
            Serial.println("# Type 'h' for help");
            break;
    }
}

void readSerial() {
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
    Serial.println("# Teensy 4.1 — Servo + BMP388 + Lambda Test");
    Serial.println("# ============================================");

    // Servo
    throttleServo.attach(SERVO_PIN, SERVO_MIN_US, SERVO_MAX_US);
    setMicroseconds(SERVO_CTR_US);
    Serial.println("# Servo: attached, center position (1500 µs)");

    // Lambda ADC
    analogReadResolution(ADC_BITS);
    pinMode(LAMBDA_PIN, INPUT);
    Serial.println("# Lambda ADC: ready on A0");

    // BMP388
    Wire.begin();
    if (!bmp.begin_I2C(0x76)) {
        Serial.println("# ERROR: BMP388 not found at 0x76 — check wiring. Halting.");
        while (true) { delay(1000); }
    }
    bmp.setTemperatureOversampling(BMP3_NO_OVERSAMPLING);
    bmp.setPressureOversampling(BMP3_OVERSAMPLING_8X);
    bmp.setIIRFilterCoeff(BMP3_IIR_FILTER_COEFF_3);
    bmp.setOutputDataRate(BMP3_ODR_50_HZ);
    Serial.println("# BMP388: found and configured");

    Serial.println("# Type 'h' for commands.");
    Serial.println();
}

void loop() {
    readSerial();
}
