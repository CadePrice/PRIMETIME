"""Pi Camera Module 3: one capture thread owns the camera.

Only this thread ever opens the camera. Everything else (the MJPEG live feed,
detection later) reads the newest frame it stores:

    capture thread ──► _frame (numpy, BGR)  ──► detection (later)
                   └─► _jpeg  (bytes)       ──► mjpeg_frames() ──► /video_feed

Camera availability is published to status/house1/primetime1/camera (retained),
so it reaches the dashboard through mqtt_client like the ESP statuses do.
If the camera can't be opened or stops delivering frames, the thread marks it
offline and retries every RETRY_S seconds.
"""

import threading
import time

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
RETRY_S = 5                 # wait between attempts to (re)open the camera
FRAME_TIMEOUT_S = 3         # a viewer gives up if no frame arrives in this time
STATUS_REPUBLISH_S = 30     # re-send status in case the broker restarted

STATUS_TOPIC = "status/house1/primetime1/camera"

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
_thread = None

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

        # (Detection will hook in here in a later step.)

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


def start():
    """Start the capture thread. Call once from app.py, after mqtt_client.start()."""
    global _thread

    if _thread is not None:
        return
    _stop.clear()
    _thread = threading.Thread(target=_run, name="camera", daemon=True)
    _thread.start()


def stop():
    """Stop the capture thread and release the camera (optional, e.g. on shutdown)."""
    global _thread

    _stop.set()
    if _thread is not None:
        _thread.join(timeout=5)
        _thread = None

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
if __name__ == "__main__":
    start()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        with _cond:
            frame = _frame
        if frame is not None:
            cv2.imwrite("camera_test.jpg", frame)
            print("Saved camera_test.jpg", frame.shape)
            break
        time.sleep(0.2)
    else:
        print("No frame within 10 s")
    stop()
