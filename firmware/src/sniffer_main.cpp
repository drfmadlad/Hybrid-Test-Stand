/**
 * Polaris ATV CAN Bus Sniffer
 * Target: Teensy 4.1
 *
 * Streams every CAN frame over USB serial as plain CSV.
 * Feed into tools/dashboard.py — it logs everything to a .csv file on your PC
 * and shows a live-updating table at the same time.
 *
 * Serial output:
 *   Data lines : timestamp_ms,seq,0xID,ext,len,d0,d1,d2,d3,d4,d5,d6,d7
 *   Status lines: start with '#' (ignored by dashboard and analyze_can.py)
 *
 * Hardware (CAN2 on Teensy 4.1):
 *   Pin 0  → Transceiver RXD
 *   Pin 1  → Transceiver TXD
 *   GND    → Transceiver GND (and harness GND)
 *   Transceiver CANH/CANL → vehicle harness
 *
 * Serial commands (2 Mbps):
 *   h / ?   help
 *   1       125 kbps
 *   2       250 kbps  ← default (Polaris)
 *   5       500 kbps
 *   m       1000 kbps
 *   r       reset sequence counter
 */

#include <Arduino.h>
#include <FlexCAN_T4.h>

static constexpr uint32_t SERIAL_BAUD = 2000000;
static constexpr size_t   RING_SIZE   = 512;  // must be a power of 2

uint32_t canBaudRate = 250000;

FlexCAN_T4<CAN2, RX_SIZE_256, TX_SIZE_16> can;

// ── SPSC ring buffer ──────────────────────────────────────────────────────────
// ISR writes (ringHead), main loop reads (ringTail). No locking needed for
// single-producer / single-consumer. __asm__ volatile("dsb") prevents the
// Cortex-M7 from reordering the data store past the pointer advance.

static CAN_message_t ringBuf[RING_SIZE];
volatile static size_t ringHead = 0;
volatile static size_t ringTail = 0;
static uint32_t ringOverflows = 0;

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

// ── Sequence counter ──────────────────────────────────────────────────────────

static uint32_t seqNum = 0;

// ── Print one frame to serial as CSV ─────────────────────────────────────────
// Format: timestamp_ms,seq,0xID,ext,len,d0,d1,d2,d3,d4,d5,d6,d7

static void printFrameCSV(const CAN_message_t& msg) {
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

// ── ISR callback ─────────────────────────────────────────────────────────────

void onReceive(const CAN_message_t& msg) {
    ringPush(msg);
}

// ── CAN init ──────────────────────────────────────────────────────────────────

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
    can.mailboxStatus();
    Serial.print("# CAN2 @ ");
    Serial.print(baud / 1000);
    Serial.println(" kbps  (pin0=RX  pin1=TX)");
}

// ── Serial commands ───────────────────────────────────────────────────────────

void printHelp() {
    Serial.println("# Commands: h=help  1/2/5/m=baud  r=reset seq counter");
    Serial.println("# Data format: timestamp_ms,seq,id,ext,len,d0..d7");
    Serial.println("# Run: python tools/dashboard.py <COM_PORT>");
}

void handleSerial() {
    if (!Serial.available()) return;
    char c = Serial.read();
    switch (c) {
        case 'h': case 'H': case '?': printHelp();                              break;
        case '1': canBaudRate = 125000;  initCAN(canBaudRate);                  break;
        case '2': canBaudRate = 250000;  initCAN(canBaudRate);                  break;
        case '5': canBaudRate = 500000;  initCAN(canBaudRate);                  break;
        case 'm': case 'M': canBaudRate = 1000000; initCAN(canBaudRate);        break;
        case 'r': case 'R':
            noInterrupts(); seqNum = 0; interrupts();
            Serial.println("# Sequence counter reset.");
            break;
    }
}

// ── Entry points ──────────────────────────────────────────────────────────────

void setup() {
    Serial.begin(SERIAL_BAUD);
    uint32_t t0 = millis();
    while (!Serial && millis() - t0 < 3000) {}
    delay(100);

    Serial.println("# Polaris CAN Sniffer — USB CSV mode");
    Serial.println("# Connect dashboard.py to log and display live data.");
    printHelp();
    Serial.println();

    initCAN(canBaudRate);
    pinMode(LED_BUILTIN, OUTPUT);
    Serial.println("# Ready — streaming all frames.");
    Serial.println();
}

void loop() {
    can.events();
    handleSerial();

    // Drain ring buffer — print every frame as CSV
    CAN_message_t msg;
    while (ringPop(msg)) {
        seqNum++;
        printFrameCSV(msg);
    }

    // Blink LED on activity
    static uint32_t lastBlink = 0;
    if (millis() - lastBlink >= 100) {
        lastBlink = millis();
        digitalWrite(LED_BUILTIN, seqNum & 1);
    }
}
