# PRIMETIME – Smart Doorbell (Proof of Concept)

## How we work (read first)
- **The user writes the code. Claude guides.** Explain the next step, give hints, and review what the user writes. Write full code or files only when the user explicitly asks.
- **Make no changes to this repo unless the user explicitly asks for them.** Reading and searching are fine.
- Go one step at a time. Wait for the user's code before moving on.

## Progress
Current status (2026-11-23): core system works on the Pi. Sensor + actuator ESPs, live dashboard (Socket.IO),
live camera feed popup, event snapshots, and YOLO person/cake detection creating notifications are all tested
on hardware. Next up: cupcake → EDP sound (step 1 below), then tuning, quotes/blurb, login.

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

- [x] Hardware test round 2 on the Pi: live updates and `cmd/#` forwarding work.
- [x] Actuator ESP firmware (user wrote it): subscribes to `cmd/.../floodlight` + `cmd/.../buzzer`, LED + buzzer work.
- [x] `camera.py` (Camera Module 3 **Wide**, imx708_wide, via picamera2): one capture thread owns the camera (1280x720 RGB888 = BGR, 15 fps); publishes `status/.../camera` retained (publishes "offline" first at startup to clear a stale retained "online", re-publishes every 30 s; no LWT); retries open every 5 s. JPEG-encodes only while live-feed viewers exist. `mjpeg_frames()` generator, `save_snapshot(event_type, frame=None)` → `"snaps/<ms>_<type>.jpg"` or None. `python camera.py` saves `camera_test.jpg` (no detection/MQTT; stop app.py first).
- [x] Live feed: `/video_feed` MJPEG route in `app.py`; `<dialog id="feed-dialog">` popup in `dashboard.html` (styles in `dashboard.css`); `live.js` sets the img src on open, removes it on close/Esc/backdrop click (stops the stream), closes the popup if the camera goes offline.
- [x] Event snapshots: `mqtt_client.set_snapshot_taker()` hook (registered in `app.py` with `camera.save_snapshot`; a hook because camera imports mqtt_client). `_save_event()` takes a snapshot when the payload has none (doorbell/motion from the ESP).
- [x] `detector.py`: COCO-pretrained YOLO11n exported to ONNX on the PC (`yolo export model=yolo11n.pt format=onnx imgsz=320`), file at `models/yolo11n.onnx`, run with onnxruntime (no ultralytics/torch on the Pi). `detect(frame)` → `[Detection(label, confidence, box)]`, letterbox + per-class NMS, only `TARGET_CLASSES` {0: person, 55: cake}, `CONF_THRESHOLD` 0.45. `python detector.py [img]` = benchmark + `detector_test.jpg` with boxes. Measured on the Pi 4: ~117 ms/detection at 320. Tests: face 0.79, hand alone 0.64 (COCO counts it as person), printed paper cupcake detected, cupcake on a phone screen not detected.
- [x] Detection thread in `camera.py`: ~3 checks/s (`DETECT_INTERVAL_S` 0.3); event when seen above `DETECT_EVENTS` threshold (person 0.60 → "human", cake 0.45 → "cupcake") in `CONFIRM_CHECKS` 3 in a row; once per visit (re-arms after `LEAVE_S` 10 s unseen) and `COOLDOWN_S` 30 s per type. Snapshot with box drawn (`DRAW_BOXES`), publishes JSON `{type, ts, snapshot, confidence}` to `event/.../human|cupcake`, which mqtt_client saves like any other event. Tested working on the Pi.

Next:
1. [ ] Cupcake → EDP sound: add `"cupcake"` to `EVENT_COMMANDS` with a new `cmd/` topic (e.g. `cmd/house1/primetime1/sound`), actuator subscribes and plays it. Add the topic to the MQTT table.
2. [ ] Detection tuning at the real door: person threshold (raise to ~0.7 if hands alone trigger), `CONFIRM_CHECKS`, `LEAVE_S`/`COOLDOWN_S`. If small/far cupcakes are missed, re-export at `imgsz=480` (~250 ms expected; just replace the .onnx, no code change). If live feed stutters, `detector.THREADS = 3`. Watch Pi temp (`vcgencmd measure_temp`).
3. [ ] Optional: Camera Module 3 continuous autofocus in `_open_camera()` (AfMode continuous); boxes on the live feed.
4. [ ] Quote card (quotes.json) and company blurb.
5. [ ] Login (Flask-Login); replace the temp `username` in `app.py`'s context processor; Socket.IO connect handler has a TODO to reject unauthenticated clients; protect `/video_feed` too.
6. [ ] Optional: `requirements.txt` + `SETUP.md` with the setup below.

Notes:
- `archive/` holds the old `app/` prototype.
- `.gitignore` (repo root) has `events.db`, `static/snaps/`, `camera_test.jpg`, `detector_test.jpg`. `events.db` was committed before that: run `git rm --cached primetime/events.db` once.
- `camera.py` and `detector.py` are app modules (imported by app.py / camera.py), not test scripts; their `__main__` blocks are the manual tests.
- Ultralytics YOLO is AGPL-3.0 (fine for the course; matters only if distributed commercially).

## Setup
**Pi** (Raspberry Pi 4, no AI HAT; app at `~/Desktop/primetime`, venv `myenv`):
- apt: `mosquitto mosquitto-clients python3-picamera2 python3-opencv` (picamera2/OpenCV/numpy must come from apt).
- venv: `python3 -m venv --system-site-packages myenv` (so it sees the apt packages).
- pip: `flask flask-socketio simple-websocket paho-mqtt onnxruntime` (onnxruntime 1.32.0 works with apt numpy 2.2.4). Never pip-install numpy, opencv-python, picamera2 or ultralytics into myenv (shadows the apt versions, breaks picamera2). Check: `python -c "import numpy; print(numpy.__file__)"` must be under `/usr/lib/python3/dist-packages`.
- Model: copy `yolo11n.onnx` to `models/` (not in git).
- Camera: only one process may open it (stop app.py before `python camera.py` / `rpicam-hello`). The `imx708_wide ... No static properties` libcamera warnings are harmless.

**Windows dev PC**: `.venv` with `flask flask-socketio simple-websocket paho-mqtt`; camera and detection just stay disabled. Model export uses a separate venv (`yolo-export`, `pip install ultralytics onnx`).

**ESP32 (Arduino IDE)**: esp32 board package; sensor: PubSubClient, Adafruit AHTX0 (+ BusIO, Unified Sensor); actuator: PubSubClient. Each ESP needs its own `MQTT_CLIENTID`.

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
- picamera2 for capture; YOLO11n (ONNX, onnxruntime) for human/cupcake detection; OpenCV for image work
- MJPEG stream endpoint for the live camera feed
- Jinja templates + plain JS + a light CSS framework (Pico.css or Bootstrap)
- Open-Meteo (no API key) for the weather icon, cached every 10–15 min
- Single Python process: Flask + MQTT thread + camera capture thread + detection thread. Only ONE process may open the camera.

## Project structure
```
primetime/
├── app.py           # Flask app: routes, login, Socket.IO, starts MQTT + camera threads
├── mqtt_client.py   # paho client: subscribes to status/#, event/#, sensors/#; updates state, saves events, emits to browser
├── camera.py        # capture thread, camera status, detection thread (events), snapshots, MJPEG frames
├── detector.py      # YOLO11n ONNX inference: detect(frame) -> person/cake boxes (+ benchmark in __main__)
├── models/          # yolo11n.onnx (copied to the Pi by hand, not in git)
├── db.py            # SQLite helpers (events.db): add event, get latest 20, mark read, prune old rows + JPEGs
├── weather.py       # Open-Meteo weather icon (location constants, cached fetch)
├── quotes.json      # quote bank for the quote generator
├── static/          # css/, js/live.js (Socket.IO updates + live feed popup), snaps/
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
- Camera detections are debounced (N checks in a row, once per visit, per-type cooldown) so one person ≠ many events.
- Camera events are published to the local broker even though camera.py is in the app, so the
  actuator ESP can react and all events flow through one handler.
- Keep only the newest 20 events; delete pruned rows and their JPEGs.
- Every route except /login requires login; Socket.IO connections check the session too.
