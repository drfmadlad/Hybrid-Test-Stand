/**
 * Throttle Servo Test — DFRobot SER0060 (35kg)
 * Target: Teensy 4.1
 *
 * Hardware:
 *   Servo Brown (GND)     → Common GND
 *   Servo Red   (VCC)     → 5–8.4V external supply (NOT Teensy 5V)
 *   Servo Yellow (Signal) → Teensy Pin 23
 *
 * Serial Commands (2000000 baud, send with newline):
 *   a<0-270>    — Move to absolute angle  (e.g. "a135")
 *   t<0-100>    — Move to throttle %      (e.g. "t50")
 *   u<500-2500> — Set raw pulse µs        (e.g. "u1500")
 *   w           — Sweep full range (slow)
 *   z           — Go to closed-throttle position (min)
 *   f           — Go to full-throttle position  (max)
 *   setmin      — Save current position as closed-throttle (0%)
 *   setmax      — Save current position as full-throttle  (100%)
 *   s           — Show status & calibration
 *   h           — Help
 */

#include <Arduino.h>
#include <Servo.h>

// =============================================================================
// Config
// =============================================================================

static constexpr int   SERVO_PIN    = 23;
static constexpr int   SERVO_MIN_US = 500;
static constexpr int   SERVO_MAX_US = 2500;
static constexpr float SERVO_DEG    = 270.0f;
static constexpr int   SERVO_CTR_US = (SERVO_MIN_US + SERVO_MAX_US) / 2;  // 1500

static constexpr int DEFAULT_THROTTLE_MIN_US = 500;
static constexpr int DEFAULT_THROTTLE_MAX_US = 2500;

static constexpr uint32_t SERIAL_BAUD = 2000000;

// =============================================================================
// State
// =============================================================================

Servo throttleServo;

int   throttleMinUs = DEFAULT_THROTTLE_MIN_US;
int   throttleMaxUs = DEFAULT_THROTTLE_MAX_US;
int   currentUs     = SERVO_CTR_US;

// =============================================================================
// Servo Control
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

// =============================================================================
// Sweep
// =============================================================================

void runSweep() {
    Serial.println("# Sweep: 0% -> 100% -> 0% (2s each way)");

    uint32_t start = millis();
    int startUs = throttleMinUs, endUs = throttleMaxUs;

    while (millis() - start < 2000) {
        float t = (millis() - start) / 2000.0f;
        setMicroseconds((int)(startUs + t * (endUs - startUs)));
        delay(20);
    }
    setMicroseconds(throttleMaxUs);
    delay(200);

    start = millis();
    while (millis() - start < 2000) {
        float t = (millis() - start) / 2000.0f;
        setMicroseconds((int)(endUs + t * (startUs - endUs)));
        delay(20);
    }
    setMicroseconds(throttleMinUs);
    Serial.println("# Sweep complete.");
}

// =============================================================================
// Status & Help
// =============================================================================

void printStatus() {
    Serial.println();
    Serial.println("# === Throttle Servo Status ===");
    Serial.print("# Current pulse  : "); Serial.print(currentUs);           Serial.println(" µs");
    Serial.print("# Current angle  : "); Serial.print(getCurrentAngle(), 1); Serial.println("°");
    Serial.print("# Throttle %     : "); Serial.print(getCurrentThrottlePercent(), 1); Serial.println("%");
    Serial.println("# --- Calibration ---");
    Serial.print("# Min (closed)   : "); Serial.print(throttleMinUs); Serial.println(" µs");
    Serial.print("# Max (full)     : "); Serial.print(throttleMaxUs); Serial.println(" µs");
    Serial.println("# ============================");
    Serial.println();
}

void printHelp() {
    Serial.println();
    Serial.println("# === Throttle Servo Commands ===");
    Serial.println("# a<deg>    — Absolute angle 0–270   (e.g. a135)");
    Serial.println("# t<pct>    — Throttle percent 0–100 (e.g. t50)");
    Serial.println("# u<us>     — Raw pulse µs 500–2500  (e.g. u1500)");
    Serial.println("# w         — Sweep 0%->100%->0%");
    Serial.println("# z         — Go to closed throttle (0%)");
    Serial.println("# f         — Go to full throttle (100%)");
    Serial.println("# setmin    — Save current pos as closed-throttle");
    Serial.println("# setmax    — Save current pos as full-throttle");
    Serial.println("# s         — Show status");
    Serial.println("# h         — Show this help");
    Serial.println("# ================================");
    Serial.println();
}

// =============================================================================
// Command Parser
// =============================================================================

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
        case 'w': case 'W': runSweep(); break;
        case 'z': case 'Z':
            setMicroseconds(throttleMinUs);
            Serial.println("# Closed throttle (0%)");
            break;
        case 'f': case 'F':
            setMicroseconds(throttleMaxUs);
            Serial.println("# Full throttle (100%)");
            break;
        case 's': case 'S': printStatus(); break;
        case 'h': case 'H': case '?': printHelp(); break;
        default:
            Serial.print("# Unknown command: "); Serial.println(c);
            Serial.println("# Type 'h' for help");
            break;
    }
}

static String serialBuf;

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
    Serial.println("# ================================");
    Serial.println("# Throttle Servo Test — Teensy 4.1");
    Serial.println("# DFRobot SER0060 / 35kg");
    Serial.println("# ================================");

    throttleServo.attach(SERVO_PIN, SERVO_MIN_US, SERVO_MAX_US);

    // Start at center so movement is visible in both directions
    setMicroseconds(SERVO_CTR_US);

    Serial.println("# Servo attached. Starting at center position (1500 µs).");
    Serial.println("# Type 'h' for commands.");
    Serial.println();
}

void loop() {
    readSerial();
}
