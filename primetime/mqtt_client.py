import json
import threading
import time

import paho.mqtt.client as mqtt

import db

# --------------------------------------------------
# MQTT CONFIGURATION
# --------------------------------------------------

MQTT_BROKER = "127.0.0.1"
MQTT_PORT = 1883

MQTT_USERNAME = None
MQTT_PASSWORD = None

READING_TIMEOUT = 30  # ESP32 publishes every 10 s; older readings are hidden

# --------------------------------------------------
# MQTT TOPIC DEFINITIONS (grouped by kind of message)
# --------------------------------------------------

# Availability: payload "online" / "offline" (retained, Last Will = "offline")
#   status/house1/primetime1/sensor   ~ Sensor ESP can communicate to Raspberry Pi
#   status/house1/primetime1/actuator ~ Actuator ESP can communicate to Raspberry Pi
#   status/house1/primetime1/camera   ~ Camera thread on the Pi is running
STATUS_TOPICS = {
    "status/house1/primetime1/sensor": "sensor_online",
    "status/house1/primetime1/actuator": "actuator_online",
    "status/house1/primetime1/camera": "camera_online",
}

# Readings from the sensor ESP: payload is a number
#   sensors/house1/primetime1/temperature ~ Temperature data (°F)
#   sensors/house1/primetime1/humidity    ~ Humidity data (%)
SENSOR_TOPICS = {
    "sensors/house1/primetime1/temperature": "temperature",
    "sensors/house1/primetime1/humidity": "humidity",
}

# One-shot events: payload is small JSON (not retained)
#   event/house1/primetime1/doorbell ~ Pi asks the actuator to sound the buzzer
#   event/house1/primetime1/motion   ~ Pi asks the actuator to turn on the LED
#   event/house1/primetime1/human    ~ Camera identifies a human
#   event/house1/primetime1/cupcake  ~ Camera identifies a cupcake
EVENT_TOPICS = {
    "event/house1/primetime1/doorbell": "doorbell",
    "event/house1/primetime1/motion": "motion",
    "event/house1/primetime1/human": "human",
    "event/house1/primetime1/cupcake": "cupcake",
}

# --------------------------------------------------
# SHARED DATA (always access under data_lock)
# --------------------------------------------------

data_lock = threading.Lock()

sensor_data = {
    "temperature": None,   # float, or None if no reading yet
    "humidity": None,
}

device_status = {
    "sensor_online": False,
    "actuator_online": False,
    "camera_online": False,
}

last_seen = {}             # topic -> time.monotonic() of the last reading
mqtt_connected = False

# --------------------------------------------------
# MQTT CALLBACKS
# --------------------------------------------------

def on_connect(client, userdata, flags, reason_code, properties):
    global mqtt_connected

    if reason_code == 0:
        with data_lock:
            mqtt_connected = True
        print("Connected to MQTT broker.")

        # Subscribing here means we re-subscribe after every reconnect.
        topics = list(STATUS_TOPICS) + list(SENSOR_TOPICS) + list(EVENT_TOPICS)
        for topic in topics:
            client.subscribe(topic, qos=1)
            print("Subscribed to:", topic)
    else:
        with data_lock:
            mqtt_connected = False
        print("MQTT connection failed:", reason_code)


def on_disconnect(client, userdata, disconnect_flags, reason_code, properties):
    global mqtt_connected

    print("Disconnected from MQTT broker.")

    with data_lock:
        mqtt_connected = False
        for key in device_status:
            device_status[key] = False


def on_message(client, userdata, message):
    topic = message.topic

    try:
        payload = message.payload.decode("utf-8").strip()
    except UnicodeDecodeError:
        print("Invalid message encoding:", topic)
        return

    print(f"MQTT received: {topic} = {payload}")

    # One-shot events: saved to SQLite (outside data_lock, it's file I/O)
    if topic in EVENT_TOPICS:
        _save_event(EVENT_TOPICS[topic], payload)
        return

    with data_lock:

        # Device availability
        if topic in STATUS_TOPICS:
            key = STATUS_TOPICS[topic]
            device_status[key] = payload.upper() == "ONLINE"

        # Sensor readings
        elif topic in SENSOR_TOPICS:
            key = SENSOR_TOPICS[topic]
            try:
                sensor_data[key] = round(float(payload), 1)
                last_seen[topic] = time.monotonic()
            except ValueError:
                print("Bad numeric payload:", topic, payload)


def _save_event(event_type, payload):
    """Store one event in the database.

    Payload is small JSON like {"type":"human","ts":1732400000,"snapshot":"snaps/1732400000.jpg"},
    but a plain string (e.g. "pressed") or empty payload also works: ts defaults to now.
    """
    try:
        details = json.loads(payload) if payload else {}
    except json.JSONDecodeError:
        details = {}
    if not isinstance(details, dict):
        details = {}

    try:
        ts = int(details.get("ts") or time.time())
    except (TypeError, ValueError):
        ts = int(time.time())

    # db.py stores snapshot paths relative to static/ ("snaps/x.jpg");
    # accept "static/snaps/x.jpg" too.
    snapshot = details.get("snapshot")
    if isinstance(snapshot, str):
        snapshot = snapshot.removeprefix("/").removeprefix("static/")
    else:
        snapshot = None

    try:
        db.add_event(event_type, ts, snapshot)
    except Exception as e:   # never let a DB error kill the MQTT thread
        print("[mqtt] could not save event:", event_type, e)

# --------------------------------------------------
# MQTT CLIENT SETUP
# --------------------------------------------------

client = mqtt.Client(
    mqtt.CallbackAPIVersion.VERSION2,
    client_id="primetime_web_app",
)

client.on_connect = on_connect
client.on_disconnect = on_disconnect
client.on_message = on_message

client.reconnect_delay_set(min_delay=1, max_delay=10)

if MQTT_USERNAME:
    client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)

_started = False


def start():
    """Connect in the background. Call once from app.py before app.run()."""
    global _started

    if _started:
        return
    _started = True

    # connect_async + loop_start: returns immediately, and keeps retrying
    # in the background if the broker isn't up yet.
    client.connect_async(MQTT_BROKER, MQTT_PORT, keepalive=30)
    client.loop_start()


def stop():
    """Disconnect cleanly (optional, e.g. on shutdown)."""
    client.disconnect()
    client.loop_stop()


def get_state():
    """Snapshot of everything the dashboard needs, as one flat dict.

    Readings older than READING_TIMEOUT (or never received) are None.
    """
    now = time.monotonic()

    with data_lock:
        readings = sensor_data.copy()
        devices = device_status.copy()
        seen = last_seen.copy()
        connected = mqtt_connected

    for topic, key in SENSOR_TOPICS.items():
        if topic not in seen or now - seen[topic] >= READING_TIMEOUT:
            readings[key] = None

    return {
        "temperature": readings["temperature"],
        "humidity": readings["humidity"],
        "mqtt": connected,
        "sensor": devices["sensor_online"],
        "actuator": devices["actuator_online"],
        "camera": devices["camera_online"],
    }


# Quick manual test: python mqtt_client.py, then publish with mosquitto_pub
if __name__ == "__main__":
    start()
    try:
        while True:
            time.sleep(5)
            print("state:", get_state())
    except KeyboardInterrupt:
        stop()
