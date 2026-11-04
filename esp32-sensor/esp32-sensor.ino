/*
  Embedded IoT Project
  Board: ESP32-WROOM-DA
*/

#include <Adafruit_AHTX0.h>
#include <Arduino.h>
#include <PubSubClient.h>
#include <WiFi.h>
#include <Wire.h>
#include "esp32-sensor-secrets.h"

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"
#include "freertos/semphr.h"

// PINS
const int TRIG_PIN = 5;
const int ECHO_PIN = 18;
const int LIGHT_DO_PIN = 19;
const int BUTTON_PIN = 26;
const int I2C_SDA_PIN = 27;
const int I2C_SCL_PIN = 33;
const int WIFI_LED_PIN = 2;                         // onboard LED, on while WiFi is connected

// RTOS CONFIG
const uint32_t AHT_POLL_MS = 10000;
const uint32_t AHT_STACK_BYTES = 6144;
const UBaseType_t AHT_PRIORITY = 1;
const uint32_t BUTTON_STACK_BYTES = 3072;
const UBaseType_t BUTTON_PRIORITY = 2;              // above sensor tasks
const uint32_t BUTTON_DEBOUNCE_MS = 50;
const uint32_t MOTION_STACK_BYTES = 4096;
const UBaseType_t MOTION_PRIORITY = 1;
const uint32_t MOTION_POLL_MS = 150;
const int MOTION_CONFIRM = 3;
const uint32_t MOTION_COOLDOWN_MS = 10000;
const float DETECTION_IN = 24.0;
const int DARK_SIGNAL = HIGH;
const BaseType_t SENSOR_CORE = 1;                   // WiFi stack lives on core 0
const uint32_t NET_STACK_BYTES = 8192;
const UBaseType_t NET_PRIORITY = 1;
const BaseType_t NET_CORE = 0;
const uint32_t NET_RETRY_MS = 5000;
const uint32_t NET_QUEUE_WAIT_MS = 100;
const char *STATUS_TOPIC = "status/house1/primetime1";

WiFiClient wifiClient;
PubSubClient mqtt(wifiClient);

// MQTT EVENT QUEUE
struct MqttMessage {
  char topic[48];
  char payload[96];
};

TaskHandle_t buttonTaskHandle;
QueueHandle_t mqttQueue;
Adafruit_AHTX0 aht;

bool publishValue(const char *topic, float value) {
  MqttMessage msg;
  strlcpy(msg.topic, topic, sizeof(msg.topic));
  snprintf(msg.payload, sizeof(msg.payload), "%.1f", value);
  return xQueueSend(mqttQueue, &msg, 0) == pdTRUE;
}

bool publishEvent(const char *topic, const char *payload) {
  MqttMessage msg;
  strlcpy(msg.topic, topic, sizeof(msg.topic));
  strlcpy(msg.payload, payload, sizeof(msg.payload));
  return xQueueSend(mqttQueue, &msg, 0) == pdTRUE;
}

/**
 * @brief Ensures it is safe to access memory, preventing risk of crash.
 */
void IRAM_ATTR buttonISR() {
  BaseType_t woken = pdFALSE;
  vTaskNotifyGiveFromISR(buttonTaskHandle, &woken);
  if(woken) portYIELD_FROM_ISR();
}

float measureDistanceInches() {
  digitalWrite(TRIG_PIN, LOW);
  delayMicroseconds(2);
  digitalWrite(TRIG_PIN, HIGH);
  delayMicroseconds(10);
  digitalWrite(TRIG_PIN, LOW);

  unsigned long duration = pulseIn(ECHO_PIN, HIGH, 30000);
  if (duration == 0) return -1.0;
  return duration * 0.0133 / 2.0;
}

/**
 * @brief Checks for a button press and publishes event. Only publishes on first contact
 * 
 * @param *pvParameters Thread parameters initialized when creating task
 */
void buttonTask(void *pvParameters) {
  for (;;) {
    ulTaskNotifyTake(pdTRUE, portMAX_DELAY);        // sleep until ISR fires

    vTaskDelay(pdMS_TO_TICKS(BUTTON_DEBOUNCE_MS));  // let contact settle
    if (digitalRead(BUTTON_PIN) == LOW) {
      bool ok = publishEvent("event/house1/primetime1/doorbell", "pressed");
      Serial.printf("Button pressed (%s)\n", ok ? "queued" : "DROPPED");

      while(digitalRead(BUTTON_PIN) == LOW)
        vTaskDelay(pdMS_TO_TICKS(10));
      vTaskDelay(pdMS_TO_TICKS(BUTTON_DEBOUNCE_MS));
    }
    ulTaskNotifyTake(pdTRUE, 0);
  }
}

/**
 * @brief sChecks if it is DARK and if there is motion and fires event if true
 * 
 * @param *pvParameters Thread parameters initialized when creating task
 */
void motionTask(void *pvParameters) {
  TickType_t lastWake = xTaskGetTickCount();
  TickType_t lastEvent = 0;
  bool everFired = false;
  int confirmed = 0;

  for(;;) {
    float dist = measureDistanceInches();
    bool detected = (dist > 0 && dist <= DETECTION_IN);
    bool dark = (digitalRead(LIGHT_DO_PIN) == DARK_SIGNAL);

    confirmed = (detected && dark) ? confirmed + 1 : 0;

    bool cooled = !everFired || 
      (xTaskGetTickCount() - lastEvent) >= pdMS_TO_TICKS(MOTION_COOLDOWN_MS);

    // DEBUGGING LOG Serial.printf("MOTION: (%f) | DETECT: (%d) | DARK: (%d) | COOLED: (%d)\n", dist, detected, dark, cooled);

    if(confirmed >= MOTION_CONFIRM && cooled) {
      bool ok = publishEvent("event/house1/primetime1/motion", "detected");
      Serial.printf("Night motion at %.1f in (%s)\n", dist, ok ? "queued" : "DROPPED");
      lastEvent = xTaskGetTickCount();
      everFired = true;
      confirmed = 0;
    }

    vTaskDelayUntil(&lastWake, pdMS_TO_TICKS(MOTION_POLL_MS));
  }
}

/**
 * @brief Publishes temperature (℉) and humidity data to MQTT on interval
 * 
 * @param *pvParameters Thread parameters initialized when creating task
 */
void tempHumidityTask(void *pvParameters) {
  TickType_t lastWake = xTaskGetTickCount();

  for(;;) {
    sensors_event_t humidity, temp;
    aht.getEvent(&humidity, &temp);

    if(!isnan(temp.temperature) && !isnan(humidity.relative_humidity)) {
        float tempF = temp.temperature * 9.0 / 5.0 + 32.0;
        //bool okT = publishValue("sensor/house1/primetime1/temperature", tempF);
        bool okT = publishValue("sensors/temperature", tempF);
        bool okH = publishValue("sensor/house1/primetime1/humidity",
                                humidity.relative_humidity);
        Serial.printf("Temp: %.1f F (%s) | Humidity: %.1f %% (%s)\n",
                      tempF, okT ? "queued" : "DROPPED",
                      humidity.relative_humidity, okH ? "queued" : "DROPPED");
    }

    vTaskDelayUntil(&lastWake, pdMS_TO_TICKS(AHT_POLL_MS));
  }
}

void networkTask(void *pvParameters) {
  mqtt.setServer(MQTT_HOST, MQTT_PORT);

  WiFi.mode(WIFI_STA);
  WiFi.setAutoReconnect(true);

  // TEMP: log the ESP32's MAC address, remove once no longer needed
  Serial.printf("[TEMP] ESP32 MAC: %s\n", WiFi.macAddress().c_str());

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

    // MQTT
    if(!mqtt.connected()) {
      Serial.println("MQTT connecting...");
      if(mqtt.connect(MQTT_CLIENTID, STATUS_TOPIC, 1, true, "offline")) {
        mqtt.publish(STATUS_TOPIC, "online", true);
        Serial.println("MQTT connected");
      } else {
        Serial.printf("MQTT failed, rc=%d\n", mqtt.state());
        vTaskDelay(pdMS_TO_TICKS(NET_RETRY_MS));
        continue;
      }
    }

    // Drain the queue
    MqttMessage msg;
    while(xQueueReceive(mqttQueue, &msg, pdMS_TO_TICKS(NET_QUEUE_WAIT_MS)) == pdTRUE) {
      if(!mqtt.publish(msg.topic, msg.payload))
        Serial.printf("Publish failed: %s\n", msg.topic);
    }
    mqtt.loop();

  }
}

void setup()
{
  Serial.begin(115200);
  delay(1000);
  Wire.begin(I2C_SDA_PIN, I2C_SCL_PIN);

  mqttQueue = xQueueCreate(10, sizeof(MqttMessage));

  // WiFi Thread
  pinMode(WIFI_LED_PIN, OUTPUT);
  digitalWrite(WIFI_LED_PIN, LOW);
  xTaskCreatePinnedToCore(networkTask, "network", NET_STACK_BYTES,
                          NULL, NET_PRIORITY, NULL, NET_CORE);

  // Button Thread
  pinMode(BUTTON_PIN, INPUT_PULLUP);
  xTaskCreatePinnedToCore(buttonTask, "button", BUTTON_STACK_BYTES,
                          NULL, BUTTON_PRIORITY, &buttonTaskHandle, SENSOR_CORE);
  attachInterrupt(digitalPinToInterrupt(BUTTON_PIN), buttonISR, FALLING);

  // Motion Thread
  pinMode(TRIG_PIN, OUTPUT);
  pinMode(ECHO_PIN, INPUT);
  pinMode(LIGHT_DO_PIN, INPUT);
  digitalWrite(TRIG_PIN, LOW);
  xTaskCreatePinnedToCore(motionTask, "motion", MOTION_STACK_BYTES,
                          NULL, MOTION_PRIORITY, NULL, SENSOR_CORE);

  // Temperature/Humidity Thread
  if(!aht.begin()) {
    Serial.println("AHT10 not found, check wiring. Temp/humidity disabled.");
  } else {
    xTaskCreatePinnedToCore(tempHumidityTask, "aht10", AHT_STACK_BYTES,
                            NULL, AHT_PRIORITY, NULL, SENSOR_CORE);
  }
}

void loop() {
  vTaskDelete(NULL);
}