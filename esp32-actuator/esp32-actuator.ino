/*
  Embedded IoT Project - Actuator node
  Board: ESP32-WROOM-DA

  Listens for commands from the Pi:
    cmd/house1/primetime1/buzzer = "on"     -> buzzer on GPIO 26 (PWM) for 3 seconds
    cmd/house1/primetime1/floodlight = "on" -> LED on GPIO 25 for 15 seconds
*/

#include <Arduino.h>
#include <PubSubClient.h>
#include <WiFi.h>
#include "esp32-actuator-secrets.h"

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

// PINS
const int BUZZER_PIN = 26;
const int FLOODLIGHT_PIN = 33;
const int WIFI_LED_PIN = 2;                         // onboard LED, on while WiFi is connected

// BUZZER PWM
const uint32_t BUZZER_FREQ_HZ = 2000;               // tone frequency
const uint8_t BUZZER_RES_BITS = 8;
const uint8_t BUZZER_CHANNEL = 0;                   // only used on core 2.x
const uint32_t BUZZER_ON_MS = 3000;

// FLOODLIGHT
const uint32_t FLOODLIGHT_ON_MS = 15000;

// RTOS CONFIG
const uint32_t BUZZER_STACK_BYTES = 2048;
const UBaseType_t BUZZER_PRIORITY = 2;
const uint32_t FLOODLIGHT_STACK_BYTES = 2048;
const UBaseType_t FLOODLIGHT_PRIORITY = 2;
const BaseType_t ACTUATOR_CORE = 1;                 // WiFi stack lives on core 0
const uint32_t NET_STACK_BYTES = 8192;
const UBaseType_t NET_PRIORITY = 1;
const BaseType_t NET_CORE = 0;
const uint32_t NET_RETRY_MS = 5000;
const uint32_t NET_LOOP_MS = 10;

// MQTT TOPICS
const char *STATUS_TOPIC = "status/house1/primetime1/actuator";
const char *BUZZER_TOPIC = "cmd/house1/primetime1/buzzer";
const char *FLOODLIGHT_TOPIC = "cmd/house1/primetime1/floodlight";

WiFiClient wifiClient;
PubSubClient mqtt(wifiClient);
TaskHandle_t buzzerTaskHandle;
TaskHandle_t floodlightTaskHandle;

// PWM helpers that work on both ESP32 Arduino core 2.x and 3.x
void buzzerOff();

void buzzerInit() {
#if ESP_ARDUINO_VERSION_MAJOR >= 3
  ledcAttach(BUZZER_PIN, BUZZER_FREQ_HZ, BUZZER_RES_BITS);
#else
  ledcSetup(BUZZER_CHANNEL, BUZZER_FREQ_HZ, BUZZER_RES_BITS);
  ledcAttachPin(BUZZER_PIN, BUZZER_CHANNEL);
#endif
  buzzerOff();
}

void buzzerOn() {
#if ESP_ARDUINO_VERSION_MAJOR >= 3
  ledcWriteTone(BUZZER_PIN, BUZZER_FREQ_HZ);
#else
  ledcWriteTone(BUZZER_CHANNEL, BUZZER_FREQ_HZ);
#endif
}

void buzzerOff() {
#if ESP_ARDUINO_VERSION_MAJOR >= 3
  ledcWrite(BUZZER_PIN, 0);
#else
  ledcWrite(BUZZER_CHANNEL, 0);
#endif
}

/**
 * @brief Sleeps until the MQTT callback notifies it, then buzzes for BUZZER_ON_MS.
 *
 * @param *pvParameters Thread parameters initialized when creating task
 */
void buzzerTask(void *pvParameters) {
  for(;;) {
    ulTaskNotifyTake(pdTRUE, portMAX_DELAY);        // sleep until a command arrives

    Serial.println("Buzzer ON");
    buzzerOn();
    vTaskDelay(pdMS_TO_TICKS(BUZZER_ON_MS));
    buzzerOff();
    Serial.println("Buzzer OFF");

    ulTaskNotifyTake(pdTRUE, 0);                    // drop commands that came in while buzzing
  }
}

/**
 * @brief Sleeps until the MQTT callback notifies it, then lights the LED for FLOODLIGHT_ON_MS.
 *
 * @param *pvParameters Thread parameters initialized when creating task
 */
void floodlightTask(void *pvParameters) {
  for(;;) {
    ulTaskNotifyTake(pdTRUE, portMAX_DELAY);        // sleep until a command arrives

    Serial.println("Floodlight ON");
    digitalWrite(FLOODLIGHT_PIN, HIGH);
    vTaskDelay(pdMS_TO_TICKS(FLOODLIGHT_ON_MS));
    digitalWrite(FLOODLIGHT_PIN, LOW);
    Serial.println("Floodlight OFF");

    ulTaskNotifyTake(pdTRUE, 0);                    // drop commands that came in while lit
  }
}

/**
 * @brief Runs inside mqtt.loop(). Only notifies tasks, never delays.
 */
void mqttCallback(char *topic, byte *payload, unsigned int length) {
  bool isOn = (length == 2 && memcmp(payload, "on", 2) == 0);
  Serial.printf("MQTT %s -> %.*s\n", topic, (int)length, (const char *)payload);

  if(!isOn) return;
  if(strcmp(topic, BUZZER_TOPIC) == 0)
    xTaskNotifyGive(buzzerTaskHandle);
  else if(strcmp(topic, FLOODLIGHT_TOPIC) == 0)
    xTaskNotifyGive(floodlightTaskHandle);
}

void networkTask(void *pvParameters) {
  mqtt.setServer(MQTT_HOST, MQTT_PORT);
  mqtt.setCallback(mqttCallback);

  WiFi.mode(WIFI_STA);
  WiFi.setAutoReconnect(true);

  for(;;) {
    // WiFi
    digitalWrite(WIFI_LED_PIN, WiFi.status() == WL_CONNECTED ? HIGH : LOW);
    if(WiFi.status() != WL_CONNECTED) {
      Serial.printf("WiFi connecting to %s...\n", WIFI_SSID);
      WiFi.begin(WIFI_SSID);
      uint32_t start = millis();
      while(WiFi.status() != WL_CONNECTED && millis() - start < 15000)
        vTaskDelay(pdMS_TO_TICKS(250));
      digitalWrite(WIFI_LED_PIN, WiFi.status() == WL_CONNECTED ? HIGH : LOW);
      if(WiFi.status() != WL_CONNECTED) {
        Serial.println("WiFi failed, retrying");
        vTaskDelay(pdMS_TO_TICKS(NET_RETRY_MS));
        continue;
      }
      Serial.printf("WiFi connected, IP %s\n", WiFi.localIP().toString().c_str());
    }

    // MQTT (subscribe on every connect, a reconnect drops subscriptions)
    if(!mqtt.connected()) {
      Serial.println("MQTT connecting...");
      if(mqtt.connect(MQTT_CLIENTID, STATUS_TOPIC, 1, true, "offline")) {
        mqtt.publish(STATUS_TOPIC, "online", true);
        mqtt.subscribe(BUZZER_TOPIC);
        mqtt.subscribe(FLOODLIGHT_TOPIC);
        Serial.printf("MQTT connected, subscribed to %s and %s\n", BUZZER_TOPIC, FLOODLIGHT_TOPIC);
      } else {
        Serial.printf("MQTT failed, rc=%d\n", mqtt.state());
        vTaskDelay(pdMS_TO_TICKS(NET_RETRY_MS));
        continue;
      }
    }

    mqtt.loop();
    vTaskDelay(pdMS_TO_TICKS(NET_LOOP_MS));
  }
}

void setup()
{
  Serial.begin(115200);
  delay(1000);

  // Actuator threads are created before the network task so their handles are valid in the callback

  // Buzzer Thread
  buzzerInit();
  xTaskCreatePinnedToCore(buzzerTask, "buzzer", BUZZER_STACK_BYTES,
                          NULL, BUZZER_PRIORITY, &buzzerTaskHandle, ACTUATOR_CORE);

  // Floodlight Thread
  pinMode(FLOODLIGHT_PIN, OUTPUT);
  digitalWrite(FLOODLIGHT_PIN, LOW);
  xTaskCreatePinnedToCore(floodlightTask, "floodlight", FLOODLIGHT_STACK_BYTES,
                          NULL, FLOODLIGHT_PRIORITY, &floodlightTaskHandle, ACTUATOR_CORE);

  // WiFi Thread
  pinMode(WIFI_LED_PIN, OUTPUT);
  digitalWrite(WIFI_LED_PIN, LOW);
  xTaskCreatePinnedToCore(networkTask, "network", NET_STACK_BYTES,
                          NULL, NET_PRIORITY, NULL, NET_CORE);
}

void loop() {
  vTaskDelete(NULL);
}
