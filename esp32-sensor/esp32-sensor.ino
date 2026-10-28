/*


*/

#include <Arduino.h>
#include <Wire.h>
#include <Adafruit_AHTX0.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"
#include "freertos/semphr.h"

// PINS
const int I2C_SDA_PIN = 27;
const int I2C_SCL_PIN = 33;

// RTOS CONFIG
const uint32_t AHT_POLL_MS = 10000;
const uint32_t AHT_STACK_BYTES = 6144;
const UBaseType_t AHT_PRIORITY = 1;
const BaseType_t SENSOR_CORE = 1;       // WiFi stack lives on core 0

// MQTT EVENT QUEUE
struct MqttMessage {
  char topic[48];
  char payload[96];
};

QueueHandle_t mqttQueue;

Adafruit_AHTX0 aht;

bool publishValue(const char *topic, float value) {
  MqttMessage msg;
  strlcpy(msg.topic, topic, sizeof(msg.topic));
  snprintf(msg.payload, sizeof(msg.payload), "%.1f", value);
  return xQueueSend(mqttQueue, &msg, 0) == pdTRUE;
}

void tempHumidityTask(void *pvParameters) {
  TickType_t lastWake = xTaskGetTickCount();

  for(;;) {
    sensors_event_t humidity, temp;
    aht.getEvent(&humidity, &temp);

    if(!isnan(temp.temperature) && !isnan(humidity.relative_humidity)) {
        float tempF = temp.temperature * 9.0 / 5.0 + 32.0;
        bool okT = publishValue("sensor/house1/primetime1/temperature", tempF);
        bool okH = publishValue("sensor/house1/primetime1/humidity",
                                humidity.relative_humidity);
        Serial.printf("Temp: %.1f F (%s) | Humidity: %.1f %% (%s)\n",
                      tempF, okT ? "queued" : "DROPPED",
                      humidity.relative_humidity, okH ? "queued" : "DROPPED");
    }

    vTaskDelayUntil(&lastWake, pdMS_TO_TICKS(AHT_POLL_MS));
  }
}

void setup()
{
  Serial.begin(115200);
  delay(1000);
  Wire.begin(I2C_SDA_PIN, I2C_SCL_PIN);

  if(!aht.begin()) {
    Serial.println("AHT10 not found, check wiring");
    while (true) vTaskDelay(portMAX_DELAY);
  }

  mqttQueue = xQueueCreate(10, sizeof(MqttMessage));

  xTaskCreatePinnedToCore(tempHumidityTask, "aht10", AHT_STACK_BYTES,
                          NULL, AHT_PRIORITY, NULL, SENSOR_CORE);
}

void loop() {
  vTaskDelete(NULL);
}