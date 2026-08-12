#include <Arduino.h>
#include <Wire.h>
#include <ArduinoJson.h>

#ifndef NODE_NAME
#define NODE_NAME "FSS-N01"
#endif

#define I2C_SDA_PIN   10
#define I2C_SCL_PIN   11
#define TF_LUNA_ADDR  0x10
#define TF_LUNA_INT   3

const uint32_t I2C_CLOCK_HZ = 100000;
const uint8_t LIDAR_READ_RETRIES = 3;

volatile bool newLidarDataReady = false;
volatile int last_i2c_error = 0;
unsigned long i2c_fail_count = 0;
unsigned long i2c_ok_count = 0;
uint8_t last_i2c_n = 0;
uint8_t last_i2c_b0 = 0;
uint8_t last_i2c_b1 = 0;
uint8_t last_i2c_mode = 0;
uint8_t i2c_scan_count = 0;
uint8_t i2c_scan_first_addr = 0;
uint8_t i2c_scan_last_addr = 0;
bool i2c_scan_tf_luna_present = false;
char i2c_scan_addrs[96] = "";

void runI2CScan() {
    i2c_scan_count = 0;
    i2c_scan_first_addr = 0;
    i2c_scan_last_addr = 0;
    i2c_scan_tf_luna_present = false;
    i2c_scan_addrs[0] = '\0';

    size_t used = 0;
    for (uint8_t addr = 1; addr < 127; addr++) {
        Wire.beginTransmission(addr);
        uint8_t err = Wire.endTransmission();
        if (err != 0) continue;

        if (i2c_scan_count == 0) i2c_scan_first_addr = addr;
        i2c_scan_last_addr = addr;
        i2c_scan_count++;
        if (addr == TF_LUNA_ADDR) i2c_scan_tf_luna_present = true;

        if (used < sizeof(i2c_scan_addrs) - 1) {
            int wrote = snprintf(
                i2c_scan_addrs + used,
                sizeof(i2c_scan_addrs) - used,
                "%s0x%02X",
                (used == 0) ? "" : ",",
                addr
            );
            if (wrote > 0) used += (size_t)wrote;
            if (used >= sizeof(i2c_scan_addrs)) {
                i2c_scan_addrs[sizeof(i2c_scan_addrs) - 1] = '\0';
                break;
            }
        }
    }
}

int parseDistanceFrom9(const uint8_t* buf) {
    // TF-Luna UART-compatible frame over I2C bridge.
    if (buf[0] == 0x59 && buf[1] == 0x59) {
        return (int)((buf[3] << 8) | buf[2]);
    }
    // TF-Luna native I2C register-style payload fallback.
    return (int)((buf[1] << 8) | buf[0]);
}

void IRAM_ATTR handleLidarInterrupt() {
    newLidarDataReady = true;
}

unsigned long sequence_num  = 0;
unsigned long last_transmit = 0;
const int TRANSMIT_INTERVAL = 100;

// Robust TF-Luna read path for marginal supply: retries + dual frame parsing
int get_TFLuna_Distance() {
    for (uint8_t attempt = 0; attempt < LIDAR_READ_RETRIES; attempt++) {
        // Mode 1: direct 9-byte read (some TF-Luna builds respond this way in I2C mode).
        last_i2c_mode = 1;
        byte n0 = Wire.requestFrom((int)TF_LUNA_ADDR, 9);
        last_i2c_n = n0;
        if (n0 == 9) {
            uint8_t buf0[9];
            for (int i = 0; i < 9; i++) buf0[i] = Wire.read();
            last_i2c_b0 = buf0[0];
            last_i2c_b1 = buf0[1];
            int d0 = parseDistanceFrom9(buf0);
            if (d0 > 0 && d0 < 1200) {
                i2c_ok_count++;
                return d0;
            }
        }

        // Mode 2: set register pointer to 0x01, then read 9 bytes.
        last_i2c_mode = 2;
        Wire.beginTransmission(TF_LUNA_ADDR);
        Wire.write(0x01);
        uint8_t tx = Wire.endTransmission(false);
        last_i2c_error = tx;
        if (tx == 0) {
            byte n = Wire.requestFrom((int)TF_LUNA_ADDR, 9);
            last_i2c_n = n;
            if (n == 9) {
                uint8_t buf[9];
                for (int i = 0; i < 9; i++) buf[i] = Wire.read();
                last_i2c_b0 = buf[0];
                last_i2c_b1 = buf[1];
                int d = parseDistanceFrom9(buf);
                if (d > 0 && d < 1200) {
                    i2c_ok_count++;
                    return d;
                }
            }
        }

        // Mode 3: 2-byte direct register read fallback.
        last_i2c_mode = 3;
        Wire.beginTransmission(TF_LUNA_ADDR);
        Wire.write(0x00);
        if (Wire.endTransmission(false) == 0) {
            byte n2 = Wire.requestFrom((int)TF_LUNA_ADDR, 2);
            last_i2c_n = n2;
            if (n2 == 2) {
                uint8_t lo = Wire.read();
                uint8_t hi = Wire.read();
                last_i2c_b0 = lo;
                last_i2c_b1 = hi;
                int d2 = (int)((hi << 8) | lo);
                if (d2 > 0 && d2 < 1200) {
                    i2c_ok_count++;
                    return d2;
                }
            }
        }

        delay(3);
    }

    // Re-init bus after repeated failures to recover from brownout-induced lockups.
    Wire.end();
    delay(1);
    Wire.begin(I2C_SDA_PIN, I2C_SCL_PIN);
    Wire.setClock(I2C_CLOCK_HZ);
    i2c_fail_count++;
    return -1;
}

void setup() {
    Serial.begin(921600);
    delay(1000);

    Wire.begin(I2C_SDA_PIN, I2C_SCL_PIN);
    Wire.setClock(I2C_CLOCK_HZ);
    runI2CScan();

    pinMode(TF_LUNA_INT, INPUT_PULLUP);
    attachInterrupt(digitalPinToInterrupt(TF_LUNA_INT), handleLidarInterrupt, RISING);

    Serial.printf("[%s] Ready.\n", NODE_NAME);
}

void loop() {
    unsigned long now = millis();
    bool due = newLidarDataReady || (now - last_transmit >= TRANSMIT_INTERVAL);
    if (!due) return;

    newLidarDataReady = false;
    last_transmit = now;

    int dist = get_TFLuna_Distance();

    StaticJsonDocument<384> doc;
    doc["node_id"]      = NODE_NAME;
    doc["timestamp_ms"] = now;
    doc["sequence"]     = sequence_num++;
    doc["distance_cm"]  = dist;
    doc["status"]       = (dist >= 0) ? "OK" : "SENSOR_ERR";
    doc["i2c_error"]    = last_i2c_error;
    doc["i2c_ok_count"] = i2c_ok_count;
    doc["i2c_fail_count"] = i2c_fail_count;
    doc["i2c_last_n"]   = last_i2c_n;
    doc["i2c_last_b0"]  = last_i2c_b0;
    doc["i2c_last_b1"]  = last_i2c_b1;
    doc["i2c_last_mode"] = last_i2c_mode;
    doc["i2c_scan_count"] = i2c_scan_count;
    doc["i2c_scan_first_addr"] = i2c_scan_first_addr;
    doc["i2c_scan_last_addr"] = i2c_scan_last_addr;
    doc["i2c_scan_tf_luna_present"] = i2c_scan_tf_luna_present;
    doc["i2c_scan_addrs"] = i2c_scan_addrs;
    doc["supply_note"]  = "TF-Luna needs >=4.5V";

    serializeJson(doc, Serial);
    Serial.println();
}
