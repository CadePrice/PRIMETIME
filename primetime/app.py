from datetime import datetime

from flask import Flask, Response, render_template
from flask_socketio import SocketIO, emit

import camera
import db
import mqtt_client
from weather import LOCATION_NAME, get_weather_bucket

LIVE_REFRESH_S = 5   # also push state on a timer so stale readings turn into "---"

# Flask finds templates/ and static/ automatically because they sit next to this file.
app = Flask(__name__)

# "threading" mode: plays nicely with the paho MQTT thread, no eventlet/gevent needed.
socketio = SocketIO(app, async_mode="threading")

db.init_db()   # create the events table if needed (safe every start)


@app.context_processor
def inject_user():
    """Values every template gets (the shared header needs username)."""
    return {"username": "cade"}   # TEMP: replace with current_user once Flask-Login is added


# ----- Template filters for Unix timestamps (local time of the Pi) -----
@app.template_filter("clock_time")
def clock_time(ts):
    """1732400000 -> '7:42 PM'"""
    return datetime.fromtimestamp(ts).strftime("%I:%M %p").lstrip("0")


@app.template_filter("short_date")
def short_date(ts):
    """1732400000 -> 'Nov 23, 2024'"""
    d = datetime.fromtimestamp(ts)
    return f"{d:%b} {d.day}, {d.year}"


@app.template_filter("iso_time")
def iso_time(ts):
    """For the <time datetime="..."> attribute."""
    return datetime.fromtimestamp(ts).isoformat(timespec="seconds")


def device_view(state):
    """Turn mqtt_client.get_state() into the values the status indicators use."""
    camera_state = "online" if state["camera"] else "offline"

    # Doorbell = sensor ESP + actuator ESP.
    # Error codes: 1 = sensor offline, 2 = actuator offline.
    errors = []
    if not state["sensor"]:
        errors.append("1")
    if not state["actuator"]:
        errors.append("2")

    if not errors:
        doorbell_state = "online"
    elif len(errors) == 1:
        doorbell_state = "partial"
    else:
        doorbell_state = "offline"

    return {
        "camera_state": camera_state,        # online | offline
        "doorbell_state": doorbell_state,    # online | partial | offline
        "doorbell_error": ", ".join(errors), # e.g. "1" or "1, 2"
    }


def live_state():
    """Everything that changes while a page is open. Used by the dashboard
    route (first render) and by Socket.IO (live updates), so they always match."""
    state = mqtt_client.get_state()
    return {
        "temperature": state["temperature"],   # float, or None → "---"
        "humidity": state["humidity"],
        "unread": db.unread_count(),           # bell badge
        **device_view(state),
    }


@app.route("/")
def dashboard():
    return render_template(
        "dashboard.html",
        location=LOCATION_NAME,
        weather_icon=get_weather_bucket(),   # cached, never raises
        **live_state(),
    )


@app.route("/video_feed")
def video_feed():
    """MJPEG live feed: the browser shows it in a plain <img>.
    Reads frames from camera.py's capture thread; never opens the camera itself."""
    return Response(camera.mjpeg_frames(),
                    mimetype="multipart/x-mixed-replace; boundary=frame")


@app.route("/notifications")
def notifications():
    # Read first, then mark read: events with read == False are the ones
    # that are new on this visit, so the page can highlight them.
    events = db.get_latest()
    db.mark_all_read()
    return render_template("notification.html", events=events, unread=0)


@app.route("/login")
def login():
    return render_template("login.html")


# ----- Socket.IO: live updates -----
def push_state():
    """Send the current state to every open page."""
    socketio.emit("state", live_state())


@socketio.on("connect")
def on_socket_connect():
    # TODO (Flask-Login): return False here if not current_user.is_authenticated
    emit("state", live_state())   # just this new page


def refresh_loop():
    """Push on a timer too: a reading that stops arriving produces no MQTT
    message, so this is what turns it into "---" after READING_TIMEOUT."""
    while True:
        socketio.sleep(LIVE_REFRESH_S)
        push_state()


if __name__ == "__main__":
    mqtt_client.set_listener(push_state)   # push on every MQTT change/event
    mqtt_client.set_snapshot_taker(camera.save_snapshot)   # photo for ESP events
    mqtt_client.start()   # background MQTT thread; connects/retries on its own
    camera.start()        # capture thread (after MQTT: it publishes camera status)
    socketio.start_background_task(refresh_loop)

    # debug=True shows errors in the browser. use_reloader=False because the
    # auto-reloader runs this file twice, which would start two MQTT clients
    # and try to open the camera twice.
    socketio.run(app, debug=True, use_reloader=False)
