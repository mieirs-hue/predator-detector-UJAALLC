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

void IRAM_ATTR handleLidarInterrupt() {
    newLidarDataReady = true;
}

unsigned long sequence_num  = 0;
unsigned long last_transmit = 0;
const int TRANSMIT_INTERVAL = 100;

// Robust TF-Luna read path for marginal supply: retries + dual frame parsing
int get_TFLuna_Distance() {
    for (uint8_t attempt = 0; attempt < LIDAR_READ_RETRIES; attempt++) {
        Wire.beginTransmission(TF_LUNA_ADDR);
        Wire.write(0x01);
        uint8_t tx = Wire.endTransmission(false);
        last_i2c_error = tx;
        if (tx == 0) {
            byte n = Wire.requestFrom((int)TF_LUNA_ADDR, 9);
            if (n == 9) {
                uint8_t buf[9];
                for (int i = 0; i < 9; i++) buf[i] = Wire.read();
                if (buf[0] == 0x59 && buf[1] == 0x59) {
                    i2c_ok_count++;
                    return (int)((buf[3] << 8) | buf[2]);
                }
                // I2C native mode fallback (dist low/high in first two bytes)
                int d = (int)((buf[1] << 8) | buf[0]);
                if (d > 0 && d < 1200) {
                    i2c_ok_count++;
                    return d;
                }
            }
        }

        // Retry with 2-byte direct register read fallback.
        Wire.beginTransmission(TF_LUNA_ADDR);
        Wire.write(0x00);
        if (Wire.endTransmission(false) == 0) {
            byte n2 = Wire.requestFrom((int)TF_LUNA_ADDR, 2);
            if (n2 == 2) {
                uint8_t lo = Wire.read();
                uint8_t hi = Wire.read();
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

    StaticJsonDocument<128> doc;
    doc["node_id"]      = NODE_NAME;
    doc["timestamp_ms"] = now;
    doc["sequence"]     = sequence_num++;
    doc["distance_cm"]  = dist;
    doc["status"]       = (dist >= 0) ? "OK" : "SENSOR_ERR";
    doc["i2c_error"]    = last_i2c_error;
    doc["i2c_ok_count"] = i2c_ok_count;
    doc["i2c_fail_count"] = i2c_fail_count;
    doc["supply_note"]  = "TF-Luna needs >=4.5V";

    serializeJson(doc, Serial);
    Serial.println();
}
