#include <Arduino.h>

void setup() {
  Serial.begin(115200);
  delay(1000);
  Serial.println("ESP32-S3 Lighthouse Booting...");
  // Initialize RF sniffing here
}

void loop() {
  // Read and transmit telemetry here
  delay(100);
}
