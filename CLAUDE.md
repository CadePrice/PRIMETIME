# PRIMETIME – Smart Doorbell (Proof of Concept)

## How we work (read first)
- **The user writes the code. Claude guides.** Explain the next step, give hints, and review what the user writes. Write full code or files only when the user explicitly asks.
- **Make no changes to this repo unless the user explicitly asks for them.** Reading and searching are fine.
- Go one step at a time. Wait for the user's code before moving on.

## Progress
Current status: MQTT works end to end on the Pi (sensor ESP → Mosquitto → app). Round 2 hardware test in progress: sensor status-topic fix, live updates (Socket.IO), Pi forwarding cmd/ topics. Actuator sketch not written yet.

Done:
- [x] Dashboard skeleton, shared header (`templates/_header.html`: logo → dashboard, bell → /notifications with unread badge, username menu with Sign out), device availability, weather card, live feed card. CSS in `static/css/dashboard.css`.
- [x] `weather.py`: `LOCATION_NAME`/`LAT`/`LON`, `weather_bucket(code, is_day)`, `get_weather_bucket()` (Open-Meteo via urllib, cached 15 min, never raises). Card shows location + icon per bucket.
- [x] `mqtt_client.py`: topics grouped as STATUS/SENSOR/EVENT, `start()`, `stop()`, `get_state()` (readings older than 30 s → None). Events saved via `db.add_event()`.
- [x] `db.py`: `EVENT_INFO` (icon/title/desc per type), `init_db()`, `add_event()` (prunes to 20 + deletes JPEGs), `get_latest()`, `unread_count()`, `mark_all_read()`. Snapshot paths stored relative to `static/`.
- [x] `app.py`: dashboard passes MQTT state ("---" when no reading), device states + error codes, unread count; `/notifications` route (get_latest, then mark_all_read); time filters `clock_time`/`short_date`/`iso_time`; runs with `debug=True, use_reloader=False` (restart by hand after .py changes).
- [x] `notification.html` + `static/css/notifications.css`: icon | title + desc | snapshot | timestamp, NEW marker for unread.
- [x] Hardware test round 1 on the Pi: sensor ESP readings and doorbell/motion events reach the app. Fixed the sensor's `STATUS_TOPIC` (was `status/house1/primetime1`, now `.../sensor`).
- [x] Pi forwards events to the actuator: `EVENT_COMMANDS` in `mqtt_client.py` (motion → cmd/.../floodlight, doorbell → cmd/.../buzzer).
- [x] Flask-SocketIO (`async_mode="threading"`): `mqtt_client.set_listener()` → `app.push_state()` emits `"state"` (built by `live_state()`) on every MQTT change, on connect, and every 5 s. `static/js/live.js` (both pages) updates status lights, error codes, live-feed state, readings and badge; the notifications page reloads on a new event. App runs via `socketio.run(...)`. Socket.IO JS client loads from CDN.

Next:
1. [ ] Hardware test round 2: reflash sensor ESP, `pip install flask-socketio simple-websocket` on the Pi, check live updates and `cmd/#` forwarding.
2. [ ] Actuator ESP sketch: own `MQTT_CLIENTID` (different from the sensor's), `STATUS_TOPIC` `.../actuator`, its own secrets header, subscribe to `cmd/.../floodlight` + `cmd/.../buzzer` on every connect, callback notifies an LED/buzzer task (no delays in the callback). Remove the copied sensor tasks.
3. [ ] Quote card (quotes.json) and company blurb.
4. [ ] `camera.py`: capture thread, detection, snapshots, MJPEG live feed, camera status.
5. [ ] Login (Flask-Login); replace the temp `username` in `app.py`'s context processor.

Notes:
- `archive/` holds the old `app/` prototype.
- Add `events.db` and `static/snaps/` to `.gitignore`.

## Overview
Locally hosted smart doorbell web app (ESET 456 course project), proof-of-concept stage.
Everything runs on a Raspberry Pi with an attached camera module. Two ESP32 breadboards
(sensor node and actuator node) connect directly to a Mosquitto MQTT broker on the Pi.
The web app is accessed on localhost.

```
ESP32 (sensor) ──┐
                 ├──► Mosquitto broker (on Pi) ◄──► Flask app (on Pi) ──► Browser (localhost)
ESP32 (actuator)─┘                                    └── Pi camera + OpenCV
```

## Stack
- Python, Flask, Flask-Login (single hard-coded user, password hash in .env, no sign-up)
- paho-mqtt (the app is an MQTT *client*; Mosquitto is the separate broker)
- Flask-SocketIO for live updates to the browser (status lights, weather, bell count)
- SQLite for notification events; snapshots saved as JPEGs in static/snaps/
- OpenCV (+ picamera2 if needed) for human/cupcake detection
- MJPEG stream endpoint for the live camera feed
- Jinja templates + plain JS + a light CSS framework (Pico.css or Bootstrap)
- Open-Meteo (no API key) for the weather icon, cached every 10–15 min
- Single Python process: Flask + MQTT thread + camera thread. Only ONE process may open the camera.

## Project structure
```
primetime/
├── app.py           # Flask app: routes, login, Socket.IO, starts MQTT + camera threads
├── mqtt_client.py   # paho client: subscribes to status/#, event/#, sensors/#; updates state, saves events, emits to browser
├── camera.py        # camera init, capture thread, human/cupcake detection, snapshots, publishes events, MJPEG frames
├── db.py            # SQLite helpers (events.db): add event, get latest 20, mark read, prune old rows + JPEGs
├── weather.py       # Open-Meteo weather icon (location constants, cached fetch)
├── quotes.json      # quote bank for the quote generator
├── static/          # css, js, assets, snaps/
└── templates/       # login.html, dashboard.html, notification.html, _header.html (shared)
```

## Pages / UI
1. **Login**: username + password for the single existing account.
2. **Dashboard**:
   - Header: "PRIMETIME" logo (left), notification bell with unread badge (shows "9+" above 9),
     username (click → dropdown with Sign out).
   - Device availability (1): app connection and doorbell connection indicators.
     Green = all connected, yellow = partially connected, red = no connection.
     Error codes: 1 = sensor, 2 = actuator.
   - Weather card (2): temp + humidity from the sensor ESP over MQTT; weather icon from the web API.
   - Live feed block (5), to the right of the weather card: clickable, opens the live feed only
     if the camera status is online (disabled otherwise).
   - Quote generator (6) below: random quote from quotes.json (Chris Hansen quotes).
   - Company blurb (7) at the bottom: blurb for a hotline against child predators.
3. **Notifications page** (4): opened from the bell. Lists the 20 most recent events, each with a
   camera screenshot and a timestamp. Opening it marks events read.

Event types and icons: human detected, floodlight activated, doorbell pressed,
cupcake detected (cupcake also triggers the iconic EDP sound on the actuator).

## MQTT topics
| Topic | Source | Recipient | Purpose |
|---|---|---|---|
| status/house1/primetime1/actuator | Actuator ESP | Pi | Actuator availability (1) |
| status/house1/primetime1/camera | Pi | Pi | Camera availability (1) |
| status/house1/primetime1/sensor | Sensor ESP | Pi | Sensor availability (1) |
| event/house1/primetime1/cupcake | Pi | Pi | Notification bell event (4) |
| event/house1/primetime1/doorbell | Sensor ESP | Pi | Notification bell event (4); Pi forwards cmd/.../buzzer |
| event/house1/primetime1/human | Pi | Pi | Notification bell event (4) |
| event/house1/primetime1/motion | Sensor ESP | Pi | Notification bell event (4); Pi forwards cmd/.../floodlight |
| sensors/house1/primetime1/temperature | Sensor ESP | Pi | Weather card (2) |
| sensors/house1/primetime1/humidity | Sensor ESP | Pi | Weather card (2) |
| cmd/house1/primetime1/floodlight | Pi | Actuator ESP | Turn on the LED (sent on motion) |
| cmd/house1/primetime1/buzzer | Pi | Actuator ESP | Sound the buzzer (sent on doorbell) |

The sensor ESP has the doorbell button and the night-motion sensor (ultrasonic + light sensor).
The Pi never republishes to event/ topics (it subscribes to them, which would loop). It sends
actuator commands on cmd/ topics instead, mapped in `EVENT_COMMANDS` in `mqtt_client.py`.
Command payload is "on"; commands are not retained.

## Design rules
- Status topics: each node publishes "online" with retain on connect and sets a Last Will of
  "offline". The camera thread does the same for status/.../camera. Error codes can go in a JSON payload.
- Event payloads are small JSON, e.g. {"type":"human","ts":<unix>,"snapshot":"static/snaps/<ts>.jpg"}.
  Never send images over MQTT.
- Do not retain event topics.
- Camera detections are debounced with a cooldown (a few seconds) so one person ≠ many events.
- Camera events are published to the local broker even though camera.py is in the app, so the
  actuator ESP can react and all events flow through one handler.
- Keep only the newest 20 events; delete pruned rows and their JPEGs.
- Every route except /login requires login; Socket.IO connections check the session too.
