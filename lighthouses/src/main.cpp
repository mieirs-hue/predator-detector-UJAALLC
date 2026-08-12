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

volatile bool newLidarDataReady = false;

void IRAM_ATTR handleLidarInterrupt() {
    newLidarDataReady = true;
}

unsigned long sequence_num  = 0;
unsigned long last_transmit = 0;
const int TRANSMIT_INTERVAL = 100;

// 9-byte I2C frame; fall back to 2-byte if no 0x59 header (I2C native mode)
int get_TFLuna_Distance() {
    Wire.beginTransmission(TF_LUNA_ADDR);
    Wire.write(0x01);
    Wire.endTransmission(false);

    byte n = Wire.requestFrom((int)TF_LUNA_ADDR, 9);
    if (n == 9) {
        uint8_t buf[9];
        for (int i = 0; i < 9; i++) buf[i] = Wire.read();
        if (buf[0] == 0x59 && buf[1] == 0x59)
            return (int)((buf[3] << 8) | buf[2]);
        return (int)((buf[1] << 8) | buf[0]);
    }
    return -1;
}

void setup() {
    Serial.begin(921600);
    delay(1000);

    Wire.begin(I2C_SDA_PIN, I2C_SCL_PIN);
    Wire.setClock(400000);

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

    serializeJson(doc, Serial);
    Serial.println();
}
