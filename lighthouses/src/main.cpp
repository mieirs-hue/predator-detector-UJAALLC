#include <Arduino.h>
#include <Wire.h>
#include <driver/i2s.h>
#include <ArduinoJson.h>

// NODE_NAME is set per-board via platformio.ini: -D NODE_NAME=\"FSS-N01\"
#ifndef NODE_NAME
#define NODE_NAME "FSS-N01"
#endif

// TF-Luna LiDAR (I2C)
#define I2C_SDA_PIN   10
#define I2C_SCL_PIN   11
#define TF_LUNA_ADDR  0x10

// INMP441 Microphone (I2S IN — Port 0)
#define MIC_I2S_PORT  I2S_NUM_0
#define MIC_SCK_PIN   7
#define MIC_WS_PIN    8
#define MIC_SD_PIN    9

// MAX98357A Amplifier (I2S OUT — Port 1)
#define AMP_I2S_PORT  I2S_NUM_1
#define AMP_BCLK_PIN  4
#define AMP_LRC_PIN   5
#define AMP_DIN_PIN   6

// TF-Luna hardware interrupt on D3 (avoids MIC_SD conflict on D9)
#define TF_LUNA_INT   3

volatile bool newLidarDataReady = false;

void IRAM_ATTR handleLidarInterrupt() {
    newLidarDataReady = true;
}

unsigned long sequence_num  = 0;
unsigned long last_transmit = 0;
const int TRANSMIT_INTERVAL = 50; // 20 Hz heartbeat; ISR overrides when data arrives sooner

void setup_I2C() {
    Wire.begin(I2C_SDA_PIN, I2C_SCL_PIN);
    Wire.setClock(400000);
}

void setup_Microphone() {
    i2s_config_t mic_config = {
        .mode                 = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_RX),
        .sample_rate          = 16000,
        .bits_per_sample      = I2S_BITS_PER_SAMPLE_32BIT,
        .channel_format       = I2S_CHANNEL_FMT_ONLY_LEFT,
        .communication_format = I2S_COMM_FORMAT_STAND_I2S,
        .intr_alloc_flags     = ESP_INTR_FLAG_LEVEL1,
        .dma_buf_count        = 4,
        .dma_buf_len          = 512,
        .use_apll             = false,
        .tx_desc_auto_clear   = false,
        .fixed_mclk           = 0
    };
    i2s_pin_config_t mic_pins = {
        .bck_io_num   = MIC_SCK_PIN,
        .ws_io_num    = MIC_WS_PIN,
        .data_out_num = I2S_PIN_NO_CHANGE,
        .data_in_num  = MIC_SD_PIN
    };
    i2s_driver_install(MIC_I2S_PORT, &mic_config, 0, NULL);
    i2s_set_pin(MIC_I2S_PORT, &mic_pins);
}

void setup_Amplifier() {
    i2s_config_t amp_config = {
        .mode                 = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_TX),
        .sample_rate          = 16000,
        .bits_per_sample      = I2S_BITS_PER_SAMPLE_16BIT,
        .channel_format       = I2S_CHANNEL_FMT_ONLY_RIGHT,
        .communication_format = I2S_COMM_FORMAT_STAND_I2S,
        .intr_alloc_flags     = ESP_INTR_FLAG_LEVEL1,
        .dma_buf_count        = 8,
        .dma_buf_len          = 128,
        .use_apll             = false,
        .tx_desc_auto_clear   = true,
        .fixed_mclk           = 0
    };
    i2s_pin_config_t amp_pins = {
        .bck_io_num   = AMP_BCLK_PIN,
        .ws_io_num    = AMP_LRC_PIN,
        .data_out_num = AMP_DIN_PIN,
        .data_in_num  = I2S_PIN_NO_CHANGE
    };
    i2s_driver_install(AMP_I2S_PORT, &amp_config, 0, NULL);
    i2s_set_pin(AMP_I2S_PORT, &amp_pins);
}

// 9-byte frame with 0x59/0x59 header validation
int get_TFLuna_Distance() {
    Wire.beginTransmission(TF_LUNA_ADDR);
    Wire.write(0x01);
    Wire.endTransmission(false);

    byte received = Wire.requestFrom((int)TF_LUNA_ADDR, 9);
    if (received == 9) {
        uint8_t buf[9];
        for (int i = 0; i < 9; i++) buf[i] = Wire.read();
        if (buf[0] == 0x59 && buf[1] == 0x59) {
            return (int)((buf[3] << 8) | buf[2]);
        }
    }
    return -1;
}

int get_Acoustic_Level() {
    int32_t sample_buffer[64];
    size_t bytes_read = 0;
    i2s_read(MIC_I2S_PORT, &sample_buffer, sizeof(sample_buffer), &bytes_read, 0);

    int samples = bytes_read / sizeof(int32_t);
    if (samples == 0) return 0;

    long long sum_sq = 0;
    for (int i = 0; i < samples; i++) {
        int32_t val = sample_buffer[i] >> 14;
        sum_sq += (long long)val * val;
    }
    return (int)sqrt((double)(sum_sq / samples));
}

void setup() {
    Serial.begin(921600);
    while (!Serial) { delay(10); }

    setup_I2C();

    pinMode(TF_LUNA_INT, INPUT_PULLUP);
    attachInterrupt(digitalPinToInterrupt(TF_LUNA_INT), handleLidarInterrupt, RISING);

    setup_Microphone();
    setup_Amplifier();

    delay(500);
    Serial.printf("[%s] Initialized. Serial=921600 I2C=400kHz\n", NODE_NAME);
}

void loop() {
    unsigned long now = millis();

    bool due = newLidarDataReady || (now - last_transmit >= TRANSMIT_INTERVAL);
    if (!due) return;

    newLidarDataReady = false;
    last_transmit = now;

    int distance_cm = get_TFLuna_Distance();
    int audio_rms   = get_Acoustic_Level();

    StaticJsonDocument<200> doc;
    doc["node_id"]      = NODE_NAME;
    doc["timestamp_ms"] = now;
    doc["sequence"]     = sequence_num++;
    doc["distance_cm"]  = distance_cm;
    doc["audio_rms"]    = audio_rms;
    doc["status"]       = (distance_cm >= 0) ? "OK" : "SENSOR_ERR";

    serializeJson(doc, Serial);
    Serial.println();
}
