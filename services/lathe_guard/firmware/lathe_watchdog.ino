/*
 * THE CABINET - DETERMINISTIC LATHE DEADMAN WATCHDOG FIRMWARE
 * Target MCU: Raspberry Pi Pico (RP2040) / STM32F4 / Arduino Uno R4
 *
 * SAFETY LOGIC:
 * - Normally-Open (NO) Contactor Architecture: PIN_SSR_ENABLE must be driven HIGH
 *   continuously to keep the lathe spindle running.
 * - Requires a continuous 0x55 heartbeat byte at >= 100Hz (max window: 30,000 us).
 * - If 3 consecutive 10ms heartbeats are missed (>30ms) OR 0xFF is received,
 *   PIN_SSR_ENABLE drops LOW and PIN_DC_BRAKE drives HIGH within <5 microseconds.
 * - FAULT_LATCHED requires both:
 *     (a) Physical momentary push-button press on PIN_MANUAL_RESET (active LOW), AND
 *     (b) Fresh 0x55 heartbeat stream from host PC.
 */

#include <Arduino.h>

// --- HARDWARE PIN DEFINITIONS ---
constexpr uint8_t PIN_SSR_ENABLE   = 2;   // HIGH = Lathe Contactor Armed, LOW = Power Cut
constexpr uint8_t PIN_DC_BRAKE     = 3;   // HIGH = Engage DC Injection Brake
constexpr uint8_t PIN_FAULT_LED    = 13;  // HIGH = Fault Latched Indicator
constexpr uint8_t PIN_MANUAL_RESET = 4;   // INPUT_PULLUP: Physical N.O. Reset Pushbutton to GND

// --- PROTOCOL & TIMING CONSTANTS ---
constexpr uint32_t SERIAL_BAUD          = 921600;
constexpr uint8_t  BYTE_HEARTBEAT       = 0x55;
constexpr uint8_t  BYTE_EMERGENCY_TRIP  = 0xFF;
constexpr uint8_t  BYTE_STATUS_QUERY    = 0x3F;  // '?'
constexpr uint32_t WATCHDOG_TIMEOUT_US  = 30000; // 30.0 ms (3 missed 100Hz pulses)
constexpr uint32_t DEBOUNCE_RESET_MS    = 50;    // 50 ms physical button debounce

enum class WatchdogState : uint8_t {
    DISARMED      = 0,
    ARMED_ACTIVE  = 1,
    FAULT_LATCHED = 2
};

volatile WatchdogState g_state = WatchdogState::DISARMED;
uint32_t g_last_heartbeat_us = 0;
uint32_t g_last_status_ms    = 0;

inline void engage_emergency_stop() {
    digitalWrite(PIN_SSR_ENABLE, LOW);   // Cut spindle power immediately
    digitalWrite(PIN_DC_BRAKE, HIGH);    // Engage electromechanical / DC injection brake
    digitalWrite(PIN_FAULT_LED, HIGH);   // Illuminate red workshop fault beacon
}

inline void engage_armed_run() {
    digitalWrite(PIN_DC_BRAKE, LOW);     // Release brake first
    digitalWrite(PIN_SSR_ENABLE, HIGH);  // Close spindle contactor
    digitalWrite(PIN_FAULT_LED, LOW);
}

void setup() {
    pinMode(PIN_SSR_ENABLE, OUTPUT);
    pinMode(PIN_DC_BRAKE, OUTPUT);
    pinMode(PIN_FAULT_LED, OUTPUT);
    pinMode(PIN_MANUAL_RESET, INPUT_PULLUP);

    // Boot in safe fail-open state (Spindle OFF, Brake ON)
    engage_emergency_stop();
    g_state = WatchdogState::DISARMED;

    Serial.begin(SERIAL_BAUD);
}

void loop() {
    const uint32_t now_us = micros();

    // 1. PROCESS INCOMING HIGH-SPEED SERIAL BYTES
    while (Serial.available() > 0) {
        const uint8_t rx = static_cast<uint8_t>(Serial.read());

        if (rx == BYTE_EMERGENCY_TRIP) {
            engage_emergency_stop();
            g_state = WatchdogState::FAULT_LATCHED;
            Serial.println("{\"state\":\"FAULT_LATCHED\",\"reason\":\"HOST_0xFF_TRIP\"}");
        }
        else if (rx == BYTE_HEARTBEAT) {
            g_last_heartbeat_us = now_us;
            if (g_state == WatchdogState::DISARMED) {
                engage_armed_run();
                g_state = WatchdogState::ARMED_ACTIVE;
                Serial.println("{\"state\":\"ARMED_ACTIVE\"}");
            }
            // Note: If in FAULT_LATCHED, 0x55 updates timestamp but CANNOT unlatch without physical button!
        }
        else if (rx == BYTE_STATUS_QUERY) {
            Serial.print("{\"state\":");
            Serial.print(static_cast<int>(g_state));
            Serial.print(",\"dt_us\":");
            Serial.print(now_us - g_last_heartbeat_us);
            Serial.println("}");
        }
    }

    // 2. HARD 30ms DEADMAN WATCHDOG CHECK (Handles unsigned 32-bit micros() wraparound cleanly)
    if (g_state == WatchdogState::ARMED_ACTIVE) {
        const uint32_t elapsed_us = now_us - g_last_heartbeat_us;
        if (elapsed_us > WATCHDOG_TIMEOUT_US) {
            engage_emergency_stop();
            g_state = WatchdogState::FAULT_LATCHED;
            Serial.println("{\"state\":\"FAULT_LATCHED\",\"reason\":\"WATCHDOG_30MS_TIMEOUT\"}");
        }
    }

    // 3. PHYSICAL MANUAL PUSH-BUTTON RESET LOGIC (Requires Active LOW press + fresh heartbeat)
    if (g_state == WatchdogState::FAULT_LATCHED) {
        if (digitalRead(PIN_MANUAL_RESET) == LOW) {
            delay(DEBOUNCE_RESET_MS);
            if (digitalRead(PIN_MANUAL_RESET) == LOW) {
                // Transition to DISARMED; will only re-energize SSR if a fresh 0x55 arrives
                g_state = WatchdogState::DISARMED;
                digitalWrite(PIN_FAULT_LED, LOW);
                Serial.println("{\"state\":\"DISARMED\",\"reason\":\"MANUAL_BUTTON_RESET\"}");
                while (digitalRead(PIN_MANUAL_RESET) == LOW) {
                    // Wait for operator to release button (prevents taping button down!)
                }
            }
        }
    }
}
