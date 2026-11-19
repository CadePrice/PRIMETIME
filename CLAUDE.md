# PRIMETIME – Smart Doorbell (Proof of Concept)

## How we work (read first)
- **The user writes the code. Claude guides.** Explain the next step, give hints, and review what the user writes. Write full code or files only when the user explicitly asks.
- **Make no changes to this repo unless the user explicitly asks for them.** Reading and searching are fine.
- Go one step at a time. Wait for the user's code before moving on.

## Progress
Current task: the dashboard page, `primetime/templates/dashboard.html`.
1. [x] Skeleton: doctype, head (charset, viewport, title), and body sections for header, device-availability, sensor-dashboard (weather + live-feed), quotes and company-blurb.
2. [ ] Header: logo, notification bell link with a badge span, and a username button with a hidden "Sign out" dropdown. Claude has shown example markup in chat; the user is adding it.
3. [ ] Content cards: device status, weather, live feed, quote and blurb.
4. [ ] CSS in `static/` (linked with `url_for('static', ...)`).
5. [ ] Minimal `app.py` route to serve the dashboard.

Notes:
- Templates live in `primetime/templates/` (Flask default). `primetime/pages/` is a leftover with an empty file and can be deleted.
- `archive/` holds the old `app/` prototype.

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
├── quotes.json      # quote bank for the quote generator
├── static/          # css, js, assets, snaps/
└── templates/       # login.html, dashboard.html, notification.html
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
| event/house1/primetime1/doorbell | Pi | Actuator ESP | Notification bell event (4) |
| event/house1/primetime1/human | Pi | Pi | Notification bell event (4) |
| event/house1/primetime1/motion | Pi | Actuator ESP | Notification bell event (4) |
| sensors/house1/primetime1/temperature | Sensor ESP | Pi | Weather card (2) |
| sensors/house1/primetime1/humidity | Sensor ESP | Pi | Weather card (2) |

Still to settle: the sources of the doorbell and motion events. If the physical button or motion sensor is on an ESP,
that ESP should be the source and the Pi a recipient. Consider separate cmd/... topics for
commands to the actuator (sound, floodlight).

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
