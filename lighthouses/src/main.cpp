#include <Arduino.h>
#include <WiFi.h>
#include <esp_wifi.h>

// --- Configuration ---
// Unique Node Name. In production, you might set this via build flags per board.
#ifndef NODE_NAME
#define NODE_NAME "Lighthouse_Unassigned"
#endif

// We will eventually need credentials to connect to the Jetson's broker
// const char* ssid = "YOUR_WIFI_SSID";
// const char* password = "YOUR_WIFI_PASSWORD";
// const char* mqtt_server = "JETSON_IP_ADDRESS";

// --- Sniffing State ---
int sniffed_packets = 0;
unsigned long last_report_time = 0;
const unsigned long REPORT_INTERVAL_MS = 2000;

// --- Promiscuous Mode Callback ---
// This function is triggered for every raw WiFi packet received
void sniffer_callback(void* buf, wifi_promiscuous_pkt_type_t type) {
    wifi_promiscuous_pkt_t* pkt = (wifi_promiscuous_pkt_t*)buf;
    wifi_pkt_rx_ctrl_t ctrl = (wifi_pkt_rx_ctrl_t)pkt->rx_ctrl;
    
    // For now, just count. Later we extract MAC and RSSI.
    sniffed_packets++;
}

void setup() {
  Serial.begin(115200);
  delay(1000);
  
  Serial.println("\n==================================");
  Serial.printf("ESP32-S3 Lighthouse Booting...\n");
  Serial.printf("Node ID: %s\n", NODE_NAME);
  
  // Get and print MAC address (this will be the unique key in Jetson config)
  String mac = WiFi.macAddress();
  Serial.printf("MAC Address: %s\n", mac.c_str());
  Serial.println("==================================\n");

  // --- Initialize Promiscuous Mode ---
  // 1. Initialize WiFi in Station mode but don't connect
  WiFi.mode(WIFI_STA);
  WiFi.disconnect();
  delay(100);

  // 2. Set the promiscuous mode callback
  esp_wifi_set_promiscuous_rx_cb(&sniffer_callback);
  
  // 3. Enable promiscuous mode
  esp_wifi_set_promiscuous(true);
  
  // 4. Set the channel to monitor (e.g., channel 1)
  // Later you'll likely want to hop channels to catch all traffic
  esp_wifi_set_channel(1, WIFI_SECOND_CHAN_NONE);
  
  Serial.println("RF Sniffing Started on Channel 1.");
}

void loop() {
  // Report basic telemetry locally to serial for confirmation
  if (millis() - last_report_time > REPORT_INTERVAL_MS) {
      Serial.printf("[%s] Uptime: %lu ms | Packets sniffed last 2s: %d\n", NODE_NAME, millis(), sniffed_packets);
      sniffed_packets = 0;
      last_report_time = millis();
  }
}
