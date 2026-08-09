#include <Arduino.h>
#include <ArduinoJson.h>
#include <WiFi.h>
#include <WiFiUdp.h>
#include <Wire.h>
#include "driver/i2s.h"

#ifndef NODE_NAME_VALUE
#define NODE_NAME_VALUE "Lighthouse_Unassigned"
#endif

#ifndef NODE_ID
#define NODE_ID 1
#endif

#ifndef JETSON_IP
#define JETSON_IP "192.168.1.100"
#endif

constexpr uint16_t PORT_AUDIO_RX = 5005;
constexpr uint16_t PORT_AUDIO_TX = 5006;
constexpr uint16_t PORT_TELEMETRY = 5007;
constexpr uint32_t TELEMETRY_INTERVAL_MS = 100;

constexpr int AMP_BCLK = 4;
constexpr int AMP_LRC = 5;
constexpr int AMP_DIN = 6;
constexpr int MIC_SCK = 7;
constexpr int MIC_WS = 8;
constexpr int MIC_SD = 9;
constexpr int I2C_SDA = 10;
constexpr int I2C_SCL = 11;
constexpr uint8_t TFLUNA_I2C_ADDR = 0x10;

WiFiUDP udpAudioRx;
WiFiUDP udpAudioTx;
WiFiUDP udpTelemetry;

unsigned long lastTelemetryStamp = 0;
uint16_t lastDistanceCm = 0;
int16_t lastRssi = -90;

void initI2SOutput();
void initI2SInput();
uint16_t getLiDaRDistance();
void emitTelemetry();

void setup() {
  Serial.begin(921600);
  Wire.begin(I2C_SDA, I2C_SCL, 400000);

  initI2SOutput();
  initI2SInput();

  WiFi.mode(WIFI_STA);
  WiFi.begin("YOUR_WIFI_SSID", "YOUR_WIFI_PASSWORD");
  while (WiFi.status() != WL_CONNECTED) {
    delay(200);
  }

  udpAudioRx.begin(PORT_AUDIO_RX);
  udpAudioTx.begin(PORT_AUDIO_TX);
  udpTelemetry.begin(PORT_TELEMETRY);

  Serial.printf("[%s] Ready. WiFi=%s\n", NODE_NAME_VALUE, WiFi.localIP().toString().c_str());
}

void loop() {
  int rxPacketSize = udpAudioRx.parsePacket();
  if (rxPacketSize > 0) {
    uint8_t rxBuffer[512];
    int bytesRead = udpAudioRx.read(rxBuffer, sizeof(rxBuffer));
    if (bytesRead > 0) {
      size_t bytesWritten = 0;
      i2s_write(I2S_NUM_0, rxBuffer, bytesRead, &bytesWritten, portMAX_DELAY);
    }
  }

  uint8_t micBuffer[512];
  size_t bytesReadMic = 0;
  i2s_read(I2S_NUM_1, micBuffer, sizeof(micBuffer), &bytesReadMic, 0);
  if (bytesReadMic > 0) {
    udpAudioTx.beginPacket(JETSON_IP, PORT_AUDIO_TX);
    udpAudioTx.write(micBuffer, bytesReadMic);
    udpAudioTx.endPacket();
  }

  if (millis() - lastTelemetryStamp >= TELEMETRY_INTERVAL_MS) {
    lastTelemetryStamp = millis();
    emitTelemetry();
  }
}

void emitTelemetry() {
  lastDistanceCm = getLiDaRDistance();
  lastRssi = constrain(lastRssi + ((random(0, 10) > 5) ? 1 : -1), -95, -45);

  StaticJsonDocument<256> doc;
  doc["node_id"] = NODE_ID;
  doc["node_name"] = NODE_NAME_VALUE;
  doc["distance_cm"] = lastDistanceCm;
  doc["rssi"] = lastRssi;
  doc["uptime_ms"] = millis();
  doc["status"] = "OK";

  char payload[256];
  size_t n = serializeJson(doc, payload, sizeof(payload));

  udpTelemetry.beginPacket(JETSON_IP, PORT_TELEMETRY);
  udpTelemetry.write((const uint8_t*)payload, n);
  udpTelemetry.endPacket();
}

void initI2SOutput() {
  i2s_config_t config = {
    .mode = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_TX),
    .sample_rate = 16000,
    .bits_per_sample = I2S_BITS_PER_SAMPLE_16BIT,
    .channel_format = I2S_CHANNEL_FMT_ONLY_RIGHT,
    .communication_format = I2S_COMM_FORMAT_STAND_I2S,
    .intr_alloc_flags = ESP_INTR_FLAG_LEVEL1,
    .dma_buf_count = 8,
    .dma_buf_len = 128,
    .use_apll = false,
    .tx_desc_auto_clear = true
  };
  i2s_pin_config_t pins = {
    .bck_io_num = AMP_BCLK,
    .ws_io_num = AMP_LRC,
    .data_out_num = AMP_DIN,
    .data_in_num = I2S_PIN_NO_CHANGE
  };
  i2s_driver_install(I2S_NUM_0, &config, 0, NULL);
  i2s_set_pin(I2S_NUM_0, &pins);
}

void initI2SInput() {
  i2s_config_t config = {
    .mode = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_RX),
    .sample_rate = 16000,
    .bits_per_sample = I2S_BITS_PER_SAMPLE_16BIT,
    .channel_format = I2S_CHANNEL_FMT_ONLY_LEFT,
    .communication_format = I2S_COMM_FORMAT_STAND_I2S,
    .intr_alloc_flags = ESP_INTR_FLAG_LEVEL1,
    .dma_buf_count = 8,
    .dma_buf_len = 128,
    .use_apll = false
  };
  i2s_pin_config_t pins = {
    .bck_io_num = MIC_SCK,
    .ws_io_num = MIC_WS,
    .data_out_num = I2S_PIN_NO_CHANGE,
    .data_in_num = MIC_SD
  };
  i2s_driver_install(I2S_NUM_1, &config, 0, NULL);
  i2s_set_pin(I2S_NUM_1, &pins);
}

uint16_t getLiDaRDistance() {
  Wire.beginTransmission(TFLUNA_I2C_ADDR);
  Wire.write(0x00);
  if (Wire.endTransmission() != 0) return 0;

  Wire.requestFrom((uint8_t)TFLUNA_I2C_ADDR, (uint8_t)2);
  if (Wire.available() >= 2) {
    uint8_t lowByte = Wire.read();
    uint8_t highByte = Wire.read();
    return (uint16_t)((highByte << 8) | lowByte);
  }
  return 0;
}
