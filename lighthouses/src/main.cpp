#include <Arduino.h>
#include <Wire.h>
#include <ArduinoJson.h>
#include <driver/i2s.h>
#include <io_pin_remap.h>

#ifndef NODE_NAME
#define NODE_NAME "FSS-N01"
#endif

#define I2C_SDA_PIN   D10
#define I2C_SCL_PIN   D11
#define TF_LUNA_ADDR  0x10

#define AMP_I2S_PORT  I2S_NUM_1
#define AMP_BCLK_PIN  D4
#define AMP_LRC_PIN   D5
#define AMP_DIN_PIN   D6

#ifndef TF_LUNA_INT_PIN
#define TF_LUNA_INT_PIN -1
#endif

const uint32_t I2C_CLOCK_HZ = 100000;
const uint8_t LIDAR_READ_RETRIES = 3;
const unsigned long INITIAL_DEBUG_SCAN_MS = 10000; // Continuously scan I2C for 10s on boot
const unsigned long DEBUG_SCAN_INTERVAL_MS = 200;

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
bool tf_luna_int_enabled = false;
bool audio_ready = false;
char last_audio_cmd[16] = "NONE";
unsigned long last_audio_cmd_ms = 0;
bool intercom_enabled = false;

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

void setupAudioOutput() {
    int8_t bclk_gpio = digitalPinToGPIONumber(AMP_BCLK_PIN);
    int8_t lrc_gpio = digitalPinToGPIONumber(AMP_LRC_PIN);
    int8_t din_gpio = digitalPinToGPIONumber(AMP_DIN_PIN);

    if (bclk_gpio < 0 || lrc_gpio < 0 || din_gpio < 0) {
        Serial.printf(
            "[%s] Audio pin remap failed: D4->%d D5->%d D6->%d\n",
            NODE_NAME,
            (int)bclk_gpio,
            (int)lrc_gpio,
            (int)din_gpio
        );
        audio_ready = false;
        return;
    }

    i2s_config_t amp_config = {
        .mode = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_TX),
        .sample_rate = 16000,
        .bits_per_sample = I2S_BITS_PER_SAMPLE_16BIT,
        .channel_format = I2S_CHANNEL_FMT_RIGHT_LEFT,
        .communication_format = I2S_COMM_FORMAT_STAND_I2S,
        .intr_alloc_flags = ESP_INTR_FLAG_LEVEL1,
        .dma_buf_count = 4,
        .dma_buf_len = 256,
        .use_apll = false,
        .tx_desc_auto_clear = true,
        .fixed_mclk = 0
    };

    i2s_pin_config_t amp_pins = {
        .bck_io_num = bclk_gpio,
        .ws_io_num = lrc_gpio,
        .data_out_num = din_gpio,
        .data_in_num = I2S_PIN_NO_CHANGE
    };

    esp_err_t install_err = i2s_driver_install(AMP_I2S_PORT, &amp_config, 0, NULL);
    if (install_err != ESP_OK) {
        Serial.printf("[%s] Audio init failed (driver): %d\n", NODE_NAME, (int)install_err);
        audio_ready = false;
        return;
    }

    esp_err_t pin_err = i2s_set_pin(AMP_I2S_PORT, &amp_pins);
    if (pin_err != ESP_OK) {
        Serial.printf("[%s] Audio init failed (pins): %d\n", NODE_NAME, (int)pin_err);
        i2s_driver_uninstall(AMP_I2S_PORT);
        audio_ready = false;
        return;
    }

    Serial.printf(
        "[%s] Audio GPIO map: BCLK D4->%d, LRC D5->%d, DIN D6->%d\n",
        NODE_NAME,
        (int)bclk_gpio,
        (int)lrc_gpio,
        (int)din_gpio
    );

    audio_ready = true;
}

void playTone(int frequency, int duration_ms, float amplitudePct = 1.0f) {
    if (!audio_ready || frequency <= 0 || duration_ms <= 0) return;

    const int sample_rate = 16000;
    const int total_samples = (sample_rate * duration_ms) / 1000;
    const float clampedPct = constrain(amplitudePct, 0.0f, 1.0f);
    const int16_t maxVol = (int16_t)(10000.0f * clampedPct);
    const int half_period = max(1, sample_rate / max(1, frequency * 2));
    size_t bytes_written = 0;

    for (int i = 0; i < total_samples; i++) {
        int16_t sample = ((i / half_period) % 2 == 0) ? maxVol : -maxVol;
        uint32_t frame = (((uint32_t)(uint16_t)sample) << 16) | (uint16_t)sample;
        i2s_write(AMP_I2S_PORT, &frame, sizeof(frame), &bytes_written, portMAX_DELAY);
    }
}

void handleAudioCommand(const String& command) {
    snprintf(last_audio_cmd, sizeof(last_audio_cmd), "%s", command.c_str());
    last_audio_cmd_ms = millis();

    if (command == "PING") {
        // Detection notification ping (audible but shorter than siren).
        playTone(1100, 120, 0.55f);
        delay(40);
        playTone(900, 120, 0.55f);
        return;
    }

    if (command == "INTERCOM_ON") {
        intercom_enabled = true;
        // Reserve the audio path for the live mic/intercom channel without triggering
        // a local proximity chirp. The actual USB mic can plug in later and use this flag.
        playTone(500, 80, 0.06f);
        return;
    }

    if (command == "INTERCOM_OFF") {
        intercom_enabled = false;
        playTone(340, 80, 0.04f);
        return;
    }

    if (command == "BEEP") {
        // Standard operator beep.
        playTone(800, 200, 0.80f);
        return;
    }

    if (command == "SIREN") {
        // Predator alarm burst.
        for (int i = 0; i < 3; i++) {
            playTone(1400, 400, 1.0f);
            playTone(600, 400, 1.0f);
        }
        return;
    }
}

void checkSerialCommands() {
    static String line;
    while (Serial.available() > 0) {
        char c = (char)Serial.read();
        if (c == '\n' || c == '\r') {
            if (line.length() > 0) {
                line.trim();
                line.toUpperCase();
                handleAudioCommand(line);
                line = "";
            }
            continue;
        }
        line += c;
    }
}

void IRAM_ATTR handleLidarInterrupt() {
    newLidarDataReady = true;
}

unsigned long sequence_num  = 0;
unsigned long last_transmit = 0;
unsigned long last_debug_scan_ms = 0;
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

    Serial.printf(
        "[%s] Pin map (logical D-pins): TF_SDA=%d TF_SCL=%d AMP_BCLK=%d AMP_LRC=%d AMP_DIN=%d TF_INT=%d\n",
        NODE_NAME,
        (int)I2C_SDA_PIN,
        (int)I2C_SCL_PIN,
        (int)AMP_BCLK_PIN,
        (int)AMP_LRC_PIN,
        (int)AMP_DIN_PIN,
        (int)TF_LUNA_INT_PIN
    );

    Wire.begin(I2C_SDA_PIN, I2C_SCL_PIN);
    Wire.setClock(I2C_CLOCK_HZ);
    runI2CScan();

    if (TF_LUNA_INT_PIN >= 0) {
        pinMode(TF_LUNA_INT_PIN, INPUT_PULLUP);
        attachInterrupt(digitalPinToInterrupt(TF_LUNA_INT_PIN), handleLidarInterrupt, RISING);
        tf_luna_int_enabled = true;
    }

    setupAudioOutput();

    if (audio_ready) {
        // Startup chirp confirms amp path is alive immediately after boot.
        playTone(650, 110, 0.70f);
        delay(40);
        playTone(980, 110, 0.70f);
        // Distinct 3-tone startup chirp to confirm audio path is alive.
        playTone(650, 80, 0.60f);
        delay(80);
        playTone(880, 80, 0.60f);
        delay(80);
        playTone(1200, 100, 0.70f);
    }

    Serial.printf("[%s] Ready.\n", NODE_NAME);
}

void loop() {
    checkSerialCommands();

    unsigned long now = millis();

    // For the first 10s, repeatedly re-scan I2C at a controlled cadence.
    if (now < INITIAL_DEBUG_SCAN_MS && (now - last_debug_scan_ms) >= DEBUG_SCAN_INTERVAL_MS) {
        runI2CScan();
        last_debug_scan_ms = now;
    }

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
    doc["tf_luna_int_pin"] = TF_LUNA_INT_PIN;
    doc["tf_luna_int_enabled"] = tf_luna_int_enabled;
    doc["audio_ready"] = audio_ready;
    doc["last_audio_cmd"] = last_audio_cmd;
    doc["last_audio_cmd_ms"] = last_audio_cmd_ms;
    doc["supply_note"]  = "TF-Luna needs >=4.5V";

    serializeJson(doc, Serial);
    Serial.println();
}
