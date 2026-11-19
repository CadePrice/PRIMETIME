from flask import Flask, render_template, jsonify
import paho.mqtt.client as mqtt
import threading
import time
from collections import deque

app = Flask(__name__)

# --------------------------------------------------
# MQTT CONFIGURATION
# --------------------------------------------------

MQTT_BROKER = "127.0.0.1"
MQTT_PORT = 1883

MQTT_USERNAME = None
MQTT_PASSWORD = None

SENSOR_TIMEOUT = 5
ACTUATOR_TIMEOUT = 5

# --------------------------------------------------
# SHARED DATA
# --------------------------------------------------

data_lock = threading.Lock()

sensor_data = {
    "button": "--",
    "temperature": "--",
    "humidity": "--",
    "light_value": "--",
    "light_status": "--",
    "distance": "--",
    "object_status": "--"
}

actuator_data = {
    "buzzer": "UNKNOWN",
    "light": "UNKNOWN"
}

device_status = {
    "sensor_online": False,
    "actuator_online": False
}

last_seen = {}
last_command = {
    "buzzer": None,
    "light": None
}

mqtt_connected = False

start_time = time.time()


# --------------------------------------------------
# MQTT TOPIC DEFINITIONS
# --------------------------------------------------

SENSOR_TOPICS = {
    "sensors/house1/primetime1/temperature": "temperature",
    "sensors/house1/primetime1/humidity": "humidity",
}

# One-shot events: queued as alerts instead of stored as state.
EVENT_TOPICS = {
    "event/house1/primetime1/doorbell": "doorbell",
    "event/house1/primetime1/motion": "motion",
}

HISTORY_LENGTH = 5
READING_TIMEOUT = 30  # ESP32 publishes every 10 s

history = {
    "temperature": deque(maxlen=HISTORY_LENGTH),
    "humidity": deque(maxlen=HISTORY_LENGTH),
}

events = deque(maxlen=20)
event_counter = 0

ACTUATOR_TOPICS = {
    "actuators/buzzer/status": "buzzer",
    "actuators/light/status": "light"
}

DEVICE_TOPICS = {
    "status/house1/primetime1": "sensor_online",
    "devices/actuator/availability": "actuator_online"
}

CONTROL_TOPICS = [
    "sensors/light/status",
    "sensors/object/status"
]

# --------------------------------------------------
# MQTT AUTOMATIC CONTROL
# --------------------------------------------------

def automatic_control():

    with data_lock:

        sensor_online = device_status["sensor_online"]
        actuator_online = device_status["actuator_online"]

        light_fresh = (
            "sensors/light/status" in last_seen
            and time.monotonic() - last_seen["sensors/light/status"]
            < SENSOR_TIMEOUT
        )

        object_fresh = (
            "sensors/object/status" in last_seen
            and time.monotonic() - last_seen["sensors/object/status"]
            < SENSOR_TIMEOUT
        )

        sensor_ready = (
            sensor_online
            and light_fresh
            and object_fresh
        )

        # Safe default: turn both actuators OFF
        # unless valid, fresh sensor readings exist.

        if sensor_ready:

            desired_light = (
                "ON" if sensor_data["light_status"] == "DARK"
                else "OFF"
            )

            desired_buzzer = (
                "ON" if sensor_data["object_status"] == "OBJECT"
                else "OFF"
            )

        else:

            desired_light = "OFF"
            desired_buzzer = "OFF"

        commands = {
            "light": desired_light,
            "buzzer": desired_buzzer
        }

        actuator_is_online = actuator_online
        commands_to_send = []

        if actuator_is_online:

            for actuator, command in commands.items():

                if last_command[actuator] != command:

                    last_command[actuator] = command
                    commands_to_send.append((actuator, command))

    # Publish outside the lock.

    for actuator, command in commands_to_send:

        topic = f"actuators/{actuator}/set"

        try:

            result = client.publish(
                topic,
                command,
                qos=1,
                retain=False
            )

            if result.rc != mqtt.MQTT_ERR_SUCCESS:

                print(f"Failed to publish {topic}: {result.rc}")

                with data_lock:
                    last_command[actuator] = None

            else:

                print(f"Automatic command: {topic} = {command}")

        except Exception as error:

            print("MQTT publish error:", error)

            with data_lock:
                last_command[actuator] = None


# --------------------------------------------------
# WATCHDOG
# --------------------------------------------------

def control_watchdog():

    while True:

        try:
            automatic_control()

        except Exception as error:
            print("Control watchdog error:", error)

        time.sleep(1)


# --------------------------------------------------
# MQTT CALLBACKS
# --------------------------------------------------

def on_connect(client, userdata, flags, reason_code, properties):

    global mqtt_connected

    if reason_code == 0:

        mqtt_connected = True

        print("Connected to MQTT broker.")

        topics = (
            list(SENSOR_TOPICS.keys())
            + list(EVENT_TOPICS.keys())
            + list(ACTUATOR_TOPICS.keys())
            + list(DEVICE_TOPICS.keys())
        )

        for topic in topics:
            client.subscribe(topic, qos=1)
            print("Subscribed to:", topic)

        with data_lock:

            last_command["buzzer"] = None
            last_command["light"] = None

        automatic_control()

    else:

        mqtt_connected = False
        print("MQTT connection failed:", reason_code)


def on_disconnect(
    client,
    userdata,
    disconnect_flags,
    reason_code,
    properties
):

    global mqtt_connected

    mqtt_connected = False

    print("Disconnected from MQTT broker.")

    with data_lock:

        device_status["sensor_online"] = False
        device_status["actuator_online"] = False

        last_command["buzzer"] = None
        last_command["light"] = None


def on_message(client, userdata, message):

    topic = message.topic

    try:
        payload = message.payload.decode("utf-8").strip()

    except UnicodeDecodeError:

        print("Invalid message encoding:", topic)
        return

    normalized = payload.upper()

    print(f"MQTT received: {topic} = {payload}")

    global event_counter

    with data_lock:

        # One-shot events (doorbell, motion)

        if topic in EVENT_TOPICS:

            event_counter += 1
            events.append({
                "id": event_counter,
                "type": EVENT_TOPICS[topic],
                "time": time.strftime("%H:%M:%S")
            })

        # Sensor data

        elif topic in SENSOR_TOPICS:

            key = SENSOR_TOPICS[topic]

            if key in history:

                try:
                    history[key].append({
                        "time": time.strftime("%H:%M:%S"),
                        "value": round(float(payload), 1)
                    })
                except ValueError:
                    print("Bad numeric payload:", topic, payload)

            if key in [
                "button",
                "light_status",
                "object_status"
            ]:

                sensor_data[key] = normalized

            else:

                sensor_data[key] = payload

            last_seen[topic] = time.monotonic()

        # Actuator feedback

        elif topic in ACTUATOR_TOPICS:

            key = ACTUATOR_TOPICS[topic]

            actuator_data[key] = normalized
            last_seen[topic] = time.monotonic()

        # Device availability

        elif topic in DEVICE_TOPICS:

            key = DEVICE_TOPICS[topic]

            is_online = normalized == "ONLINE"

            device_status[key] = is_online

            if key == "sensor_online" and not is_online:

                for control_topic in CONTROL_TOPICS:
                    last_seen.pop(control_topic, None)

            if key == "actuator_online" and not is_online:

                last_command["buzzer"] = None
                last_command["light"] = None

    automatic_control()


# --------------------------------------------------
# MQTT CLIENT SETUP
# --------------------------------------------------

client = mqtt.Client(
    mqtt.CallbackAPIVersion.VERSION2,
    client_id="raspberry_pi_web_controller"
)

client.on_connect = on_connect
client.on_disconnect = on_disconnect
client.on_message = on_message

client.reconnect_delay_set(
    min_delay=1,
    max_delay=10
)

if MQTT_USERNAME:

    client.username_pw_set(
        MQTT_USERNAME,
        MQTT_PASSWORD
    )


# --------------------------------------------------
# FLASK API
# --------------------------------------------------

def get_data():

    current_time = time.monotonic()

    with data_lock:

        sensors = sensor_data.copy()
        actuators = actuator_data.copy()
        devices = device_status.copy()
        seen = last_seen.copy()
        hist = {k: list(v) for k, v in history.items()}
        recent_events = list(events)

    # Hide stale sensor readings.

    for topic, key in SENSOR_TOPICS.items():

        if (
            topic not in seen
            or current_time - seen[topic] >= READING_TIMEOUT
        ):

            sensors[key] = "--"

    # Hide stale actuator feedback.

    for topic, key in ACTUATOR_TOPICS.items():

        if (
            topic not in seen
            or current_time - seen[topic] >= ACTUATOR_TIMEOUT
        ):

            actuators[key] = "UNKNOWN"

    uptime = int(time.time() - start_time)

    return {
        "sensors": sensors,

        "history": hist,

        "events": recent_events,

        "actuators": actuators,

        "connections": {
            "mqtt": mqtt_connected,
            "sensor": devices["sensor_online"],
            "actuator": devices["actuator_online"]
        },

        "uptime": uptime
    }


@app.route("/")
def index():

    return render_template("index.html")


@app.route("/api/data")
def api_data():

    return jsonify(get_data())


# --------------------------------------------------
# APPLICATION STARTUP
# --------------------------------------------------

if __name__ == "__main__":

    print("Starting Raspberry Pi Sensor Control System...")

    # Start MQTT connection.

    client.connect_async(
        MQTT_BROKER,
        MQTT_PORT,
        keepalive=30
    )

    client.loop_start()

    # Start automatic-control watchdog.

    watchdog_thread = threading.Thread(
        target=control_watchdog,
        daemon=True
    )

    watchdog_thread.start()

    print("Automatic-control watchdog started.")
    print("Flask dashboard starting...")

    try:

        app.run(
            host="0.0.0.0",
            port=5000,
            debug=False,
            threaded=True
        )

    finally:

        client.disconnect()
        client.loop_stop()
