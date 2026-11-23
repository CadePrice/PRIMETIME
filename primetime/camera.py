"""Pi Camera Module 3: one capture thread owns the camera.

Only this thread ever opens the camera. Everything else (the MJPEG live feed,
the detection thread) reads the newest frame it stores:

    capture thread ──► _frame (numpy, BGR)  ──► detection thread (YOLO, ~3 checks/s)
                   │                               └─► snapshot + event/.../human|cupcake
                   └─► _jpeg  (bytes)       ──► mjpeg_frames() ──► /video_feed

Camera availability is published to status/house1/primetime1/camera (retained),
so it reaches the dashboard through mqtt_client like the ESP statuses do.
If the camera can't be opened or stops delivering frames, the thread marks it
offline and retries every RETRY_S seconds.
"""

import json
import os
import threading
import time

import db
import detector
import mqtt_client

# picamera2 and OpenCV come from apt on the Pi (python3-picamera2, python3-opencv).
# On a dev PC without them the app still runs; the camera just stays offline.
try:
    from picamera2 import Picamera2
except ImportError:
    Picamera2 = None

try:
    import cv2
except ImportError:
    cv2 = None

# --------------------------------------------------
# CAMERA CONFIGURATION
# --------------------------------------------------

FRAME_SIZE = (1280, 720)    # width, height of the frames we capture
FPS = 15                    # capture rate; also the max live feed rate
JPEG_QUALITY = 75           # 0-100; lower = smaller stream, less CPU
SNAPSHOT_QUALITY = 90       # event photos are saved once, so they can be sharper
RETRY_S = 5                 # wait between attempts to (re)open the camera
FRAME_TIMEOUT_S = 3         # a viewer gives up if no frame arrives in this time
STATUS_REPUBLISH_S = 30     # re-send status in case the broker restarted

STATUS_TOPIC = "status/house1/primetime1/camera"
EVENT_TOPIC_PREFIX = "event/house1/primetime1/"   # + "human" / "cupcake"

# ----- Detection (tune these on the Pi) -----
DETECT_INTERVAL_S = 0.3     # time between checks (~3/s); one check takes ~120 ms on the Pi 4
CONFIRM_CHECKS = 3          # seen in this many checks in a row before an event fires (~1 s)
LEAVE_S = 10                # not seen for this long = gone; seeing it again is a new event
COOLDOWN_S = 30             # minimum time between two events of the same type
DRAW_BOXES = True           # draw the detection box on the event snapshot

# detector label -> (event type, minimum confidence for an event).
# These sit on top of detector.CONF_THRESHOLD (0.45), which is the floor.
DETECT_EVENTS = {
    "person": ("human", 0.60),     # tests: face 0.79, a hand alone 0.64
    "cake": ("cupcake", 0.45),
}

# --------------------------------------------------
# SHARED DATA (always access under _cond)
# --------------------------------------------------

_cond = threading.Condition()   # a lock + "new frame" signal for viewers
_frame = None                   # newest frame, numpy array (BGR, OpenCV order)
_jpeg = None                    # newest frame as JPEG, only while someone watches
_frame_id = 0                   # goes up by one per captured frame
_viewers = 0                    # open /video_feed streams
_online = False

_stop = threading.Event()
_threads = []

# --------------------------------------------------
# STATUS
# --------------------------------------------------

def _publish_status(online):
    # QoS 1 messages are queued by paho if the broker isn't connected yet.
    mqtt_client.client.publish(STATUS_TOPIC, "online" if online else "offline",
                               qos=1, retain=True)


def _set_online(online):
    global _online

    with _cond:
        changed = _online != online
        _online = online
        _cond.notify_all()          # wake viewers so they notice "offline"

    if changed or not online:
        print("[camera]", "online" if online else "offline")
    _publish_status(online)


def is_online():
    with _cond:
        return _online

# --------------------------------------------------
# SNAPSHOTS (event photos for the notifications page)
# --------------------------------------------------

def save_snapshot(event_type, frame=None):
    """Save a frame as static/snaps/<ms>_<event_type>.jpg.

    frame: the image to save; None = the camera's newest frame.
    Returns the path relative to static/ ("snaps/..."), the format db.py stores,
    or None if the camera is offline / has no frame yet / the write failed.
    """
    if frame is None:
        with _cond:
            if not _online or _frame is None:
                return None
            # capture_array() gives a new array each frame, so holding a reference
            # is enough: the capture thread never changes this one.
            frame = _frame

    # Slow part (JPEG encode + disk write) happens outside the lock, so the
    # capture thread and live feed viewers are never held up.
    name = f"{time.time_ns() // 1_000_000}_{event_type}.jpg"   # ms: no clashes within a second
    try:
        os.makedirs(db.SNAPS_DIR, exist_ok=True)
        ok = cv2.imwrite(os.path.join(db.SNAPS_DIR, name), frame,
                         [cv2.IMWRITE_JPEG_QUALITY, SNAPSHOT_QUALITY])
    except Exception as e:
        print("[camera] snapshot failed:", e)
        return None

    if not ok:
        print("[camera] snapshot failed: imwrite returned False")
        return None
    return f"snaps/{name}"

# --------------------------------------------------
# CAPTURE THREAD
# --------------------------------------------------

def _open_camera():
    """Return a started Picamera2, or None if the camera isn't available."""
    cam = None
    try:
        cam = Picamera2()   # raises if no camera is connected
        config = cam.create_video_configuration(
            # "RGB888" is stored in BGR byte order, which is what OpenCV expects.
            main={"size": FRAME_SIZE, "format": "RGB888"},
            controls={"FrameRate": FPS},
        )
        cam.configure(config)
        cam.start()
        return cam
    except Exception as e:
        print("[camera] could not open camera:", e)
        _close_camera(cam)
        return None


def _close_camera(cam):
    if cam is None:
        return
    try:
        cam.stop()
    except Exception:
        pass
    try:
        cam.close()
    except Exception:
        pass


def _capture_loop(cam):
    """Grab frames until stopped or the camera fails (raises)."""
    global _frame, _jpeg, _frame_id

    last_status = time.monotonic()

    while not _stop.is_set():
        frame = cam.capture_array("main")   # blocks until the next frame

        # Only spend CPU on JPEG encoding while someone has the live feed open.
        jpeg = None
        if _viewers > 0:
            ok, buf = cv2.imencode(".jpg", frame,
                                   [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
            if ok:
                jpeg = buf.tobytes()

        with _cond:
            _frame = frame
            _jpeg = jpeg
            _frame_id += 1
            _cond.notify_all()              # wake every waiting viewer

        now = time.monotonic()
        if now - last_status >= STATUS_REPUBLISH_S:
            _publish_status(True)
            last_status = now


def _run():
    global _frame, _jpeg

    if Picamera2 is None or cv2 is None:
        print("[camera] picamera2/OpenCV not installed: camera disabled")
        _set_online(False)
        return

    # Clear a stale retained "online" left over from the last run.
    _set_online(False)

    while not _stop.is_set():
        cam = _open_camera()
        if cam is None:
            _stop.wait(RETRY_S)
            continue

        _set_online(True)
        try:
            _capture_loop(cam)
        except Exception as e:   # never let a camera error kill the thread
            print("[camera] capture failed:", e)
        finally:
            _close_camera(cam)
            with _cond:
                _frame = None
                _jpeg = None
            _set_online(False)

        _stop.wait(RETRY_S)


def start(detection=True):
    """Start the capture thread and, if possible, the detection thread.
    Call once from app.py, after mqtt_client.start()."""
    if _threads:
        return
    _stop.clear()

    targets = [("camera", _run)]
    if detection:
        if Picamera2 is None or cv2 is None:
            pass   # no camera libraries: _run prints why
        elif not detector.available():
            print("[detect] disabled: onnxruntime missing or no model at", detector.MODEL_PATH)
        else:
            targets.append(("detect", _detect_run))

    for name, target in targets:
        t = threading.Thread(target=target, name=name, daemon=True)
        t.start()
        _threads.append(t)


def stop():
    """Stop the threads and release the camera (optional, e.g. on shutdown)."""
    _stop.set()
    for t in _threads:
        t.join(timeout=5)
    _threads.clear()

# --------------------------------------------------
# DETECTION THREAD
# --------------------------------------------------
# Per label (person, cake), an event fires when:
#   1. it is seen above its DETECT_EVENTS confidence in CONFIRM_CHECKS checks in a row,
#   2. it isn't already "present" (an event fires once per visit, not every check),
#   3. COOLDOWN_S has passed since the last event of that type.
# It stops being "present" after LEAVE_S seconds without being seen.

def _fire_event(event_type, frame, det):
    """Snapshot + publish. mqtt_client receives our own message and saves the
    event like any other, so all events flow through one handler."""
    if DRAW_BOXES:
        frame = frame.copy()   # never draw on the shared frame
        x1, y1, x2, y2 = det.box
        cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 0, 255), 3)
        cv2.putText(frame, f"{det.label} {det.confidence:.2f}", (x1 + 6, max(y1 - 10, 24)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 255), 2)

    snapshot = save_snapshot(event_type, frame)
    payload = json.dumps({
        "type": event_type,
        "ts": int(time.time()),
        "snapshot": snapshot,              # "snaps/....jpg" or null
        "confidence": det.confidence,
    })
    mqtt_client.client.publish(EVENT_TOPIC_PREFIX + event_type, payload, qos=1)
    print(f"[detect] {event_type}: {det.label} {det.confidence:.2f} -> {snapshot}")


def _detect_run():
    try:
        detector.load()
    except Exception as e:
        print("[detect] could not load model:", e)
        return
    print("[detect] running")

    tracks = {label: {"streak": 0, "present": False, "last_seen": 0.0, "last_fired": None}
              for label in DETECT_EVENTS}
    last_id = -1

    while not _stop.wait(DETECT_INTERVAL_S):
        with _cond:
            frame, frame_id, online = _frame, _frame_id, _online

        if not online or frame is None or frame_id == last_id:
            for t in tracks.values():
                t["streak"] = 0            # a gap breaks "in a row"
            continue
        last_id = frame_id

        try:
            detections = detector.detect(frame)
        except Exception as e:             # never let a model error kill the thread
            print("[detect] failed:", e)
            _stop.wait(RETRY_S)
            continue

        # Best detection per label that clears its event threshold
        # (detect() returns the highest confidence first).
        best = {}
        for d in detections:
            rule = DETECT_EVENTS.get(d.label)
            if rule and d.confidence >= rule[1] and d.label not in best:
                best[d.label] = d

        now = time.monotonic()
        for label, t in tracks.items():
            if label in best:
                t["streak"] += 1
                t["last_seen"] = now
            else:
                t["streak"] = 0
                if t["present"] and now - t["last_seen"] >= LEAVE_S:
                    t["present"] = False   # gone: the next sighting is a new visit

            cooled = t["last_fired"] is None or now - t["last_fired"] >= COOLDOWN_S
            if not t["present"] and t["streak"] >= CONFIRM_CHECKS and cooled:
                t["present"] = True
                t["last_fired"] = now
                try:
                    _fire_event(DETECT_EVENTS[label][0], frame, best[label])
                except Exception as e:
                    print("[detect] could not fire event:", e)

# --------------------------------------------------
# LIVE FEED (MJPEG)
# --------------------------------------------------

def mjpeg_frames():
    """Generator for the /video_feed route: yields JPEGs as multipart chunks.

    Use with:
        Response(camera.mjpeg_frames(),
                 mimetype="multipart/x-mixed-replace; boundary=frame")

    Ends by itself if the camera goes offline. When the browser closes the
    stream, Flask closes the generator and the `finally` lowers the viewer count.
    """
    global _viewers

    with _cond:
        _viewers += 1
    try:
        last_id = -1
        while True:
            with _cond:
                got_frame = _cond.wait_for(
                    lambda: not _online or (_frame_id != last_id and _jpeg is not None),
                    timeout=FRAME_TIMEOUT_S,
                )
                if not got_frame or not _online:
                    return
                last_id = _frame_id
                jpeg = _jpeg

            yield (b"--frame\r\n"
                   b"Content-Type: image/jpeg\r\n"
                   b"Content-Length: " + str(len(jpeg)).encode() + b"\r\n\r\n"
                   + jpeg + b"\r\n")
    finally:
        with _cond:
            _viewers -= 1


# Quick manual test on the Pi: python camera.py  → saves camera_test.jpg
# (no detection, no MQTT; stop app.py first)
if __name__ == "__main__":
    start(detection=False)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        with _cond:
            frame, frame_id = _frame, _frame_id
        if frame is not None and frame_id >= 2 * FPS:   # ~2 s: let auto-exposure settle
            cv2.imwrite("camera_test.jpg", frame)
            print("Saved camera_test.jpg", frame.shape)
            break
        time.sleep(0.2)
    else:
        print("No frame within 10 s")
    stop()
