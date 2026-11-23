"""YOLO object detection (person + cake) with onnxruntime.

The model is a COCO-pretrained YOLO11n, exported to ONNX once on a PC:
    yolo export model=yolo11n.pt format=onnx imgsz=320
and copied to models/yolo11n.onnx. The Pi only needs onnxruntime (pip),
numpy and OpenCV (apt), not ultralytics/PyTorch.

Usage (camera.py, step 3):
    import detector
    for d in detector.detect(frame):        # frame: numpy BGR image, any size
        print(d.label, d.confidence, d.box)  # box = (x1, y1, x2, y2) in frame pixels

This module never touches the camera. `python detector.py [image.jpg]` runs a
speed benchmark on a saved photo (default camera_test.jpg).
"""

import os
import threading
from collections import namedtuple

# onnxruntime from pip, numpy + OpenCV from apt (see the dependency list).
# On a PC without them the app still runs; detection is just unavailable.
try:
    import cv2
    import numpy as np
    import onnxruntime as ort
except ImportError:
    cv2 = np = ort = None

# --------------------------------------------------
# DETECTOR CONFIGURATION
# --------------------------------------------------

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MODEL_PATH = os.path.join(BASE_DIR, "models", "yolo11n.onnx")

# COCO class id -> our label. Every other class (dog, car, ...) is ignored.
TARGET_CLASSES = {
    0: "person",
    55: "cake",       # COCO has no "cupcake"; cupcakes come out as cake
    # 54: "donut",    # uncomment if cupcakes often show up as donuts
}

CONF_THRESHOLD = 0.45   # 0-1; raise it if you get false alarms, lower it if people are missed
NMS_IOU = 0.45          # overlap above which two boxes count as the same object
THREADS = 4             # Pi 4 has 4 cores

Detection = namedtuple("Detection", "label confidence box")   # box = (x1, y1, x2, y2)

# --------------------------------------------------
# MODEL (loaded once, on first use)
# --------------------------------------------------

_load_lock = threading.Lock()
_session = None
_input_name = None
_input_size = None   # (width, height) the model expects, read from the file


def available():
    """True if the libraries are installed and the model file exists."""
    return ort is not None and os.path.exists(MODEL_PATH)


def load():
    """Load the model. Called automatically by detect(); safe to call twice."""
    global _session, _input_name, _input_size

    with _load_lock:
        if _session is not None:
            return
        if ort is None:
            raise RuntimeError("onnxruntime / numpy / OpenCV not installed")
        if not os.path.exists(MODEL_PATH):
            raise FileNotFoundError(f"model not found: {MODEL_PATH}")

        opts = ort.SessionOptions()
        opts.intra_op_num_threads = THREADS
        session = ort.InferenceSession(MODEL_PATH, sess_options=opts,
                                       providers=["CPUExecutionProvider"])

        inp = session.get_inputs()[0]          # shape like [1, 3, 320, 320]
        h, w = inp.shape[2], inp.shape[3]
        if not isinstance(h, int) or not isinstance(w, int):
            h = w = 320                        # dynamic-size export: use 320

        _input_name = inp.name
        _input_size = (w, h)
        _session = session

# --------------------------------------------------
# DETECTION
# --------------------------------------------------

def _letterbox(frame):
    """Resize to the model size without stretching; pad the rest with grey.

    Returns the padded image plus the scale and padding, so boxes can be
    mapped back onto the original frame.
    """
    fh, fw = frame.shape[:2]
    iw, ih = _input_size
    scale = min(iw / fw, ih / fh)
    nw, nh = round(fw * scale), round(fh * scale)

    resized = cv2.resize(frame, (nw, nh), interpolation=cv2.INTER_LINEAR)
    pad_x, pad_y = (iw - nw) // 2, (ih - nh) // 2

    canvas = np.full((ih, iw, 3), 114, dtype=np.uint8)   # 114 = YOLO's padding grey
    canvas[pad_y:pad_y + nh, pad_x:pad_x + nw] = resized
    return canvas, scale, pad_x, pad_y


def detect(frame):
    """Find people and cakes in a BGR image. Returns a list of Detection,
    highest confidence first (empty list if nothing found)."""
    load()

    if frame.ndim == 3 and frame.shape[2] == 4:   # XRGB8888 frames: drop the 4th channel
        frame = frame[:, :, :3]
    fh, fw = frame.shape[:2]

    # 1. Pre-process: letterbox, BGR -> RGB, 0-255 -> 0-1, HWC -> 1xCxHxW
    img, scale, pad_x, pad_y = _letterbox(frame)
    blob = np.ascontiguousarray(img[:, :, ::-1].transpose(2, 0, 1)[None], dtype=np.float32) / 255.0

    # 2. Run the model. Output is (1, 84, N): per candidate box
    #    cx, cy, w, h (model pixels) + 80 class scores. N = 2100 at 320x320.
    out = _session.run(None, {_input_name: blob})[0][0]
    out = out.T                                   # -> (N, 84): one row per box

    # 3. Each box's best class; keep only our classes above the threshold
    scores = out[:, 4:]
    class_ids = scores.argmax(axis=1)
    confs = scores[np.arange(len(scores)), class_ids]

    keep = (confs >= CONF_THRESHOLD) & np.isin(class_ids, list(TARGET_CLASSES))
    if not keep.any():
        return []
    out, class_ids, confs = out[keep], class_ids[keep], confs[keep]

    # 4. Boxes: model pixels (center, size) -> frame pixels (corners)
    cx, cy, bw, bh = out[:, 0], out[:, 1], out[:, 2], out[:, 3]
    x1 = np.clip((cx - bw / 2 - pad_x) / scale, 0, fw)
    y1 = np.clip((cy - bh / 2 - pad_y) / scale, 0, fh)
    x2 = np.clip((cx + bw / 2 - pad_x) / scale, 0, fw)
    y2 = np.clip((cy + bh / 2 - pad_y) / scale, 0, fh)

    # 5. Non-max suppression per class: YOLO proposes many overlapping boxes
    #    for one object; keep only the best of each overlapping group.
    results = []
    for class_id in np.unique(class_ids):
        idx = np.flatnonzero(class_ids == class_id)
        rects = [[float(x1[i]), float(y1[i]), float(x2[i] - x1[i]), float(y2[i] - y1[i])] for i in idx]
        kept = cv2.dnn.NMSBoxes(rects, confs[idx].tolist(), CONF_THRESHOLD, NMS_IOU)
        for k in np.array(kept).flatten():
            i = idx[k]
            results.append(Detection(
                TARGET_CLASSES[int(class_id)],
                round(float(confs[i]), 2),
                (int(x1[i]), int(y1[i]), int(x2[i]), int(y2[i])),
            ))

    results.sort(key=lambda d: d.confidence, reverse=True)
    return results


# --------------------------------------------------
# BENCHMARK: python detector.py [image.jpg]
# --------------------------------------------------
if __name__ == "__main__":
    import sys
    import time

    RUNS = 10

    if ort is None:
        sys.exit("onnxruntime / numpy / OpenCV not installed")

    image_path = sys.argv[1] if len(sys.argv) > 1 else "camera_test.jpg"
    frame = cv2.imread(image_path)
    if frame is None:
        sys.exit(f"could not read {image_path} (take one with: python camera.py)")

    t0 = time.perf_counter()
    load()
    print(f"Model: {MODEL_PATH}")
    print(f"Input {_input_size[0]}x{_input_size[1]}, loaded in {(time.perf_counter() - t0) * 1000:.0f} ms")
    print(f"Image: {image_path} ({frame.shape[1]}x{frame.shape[0]})")

    detect(frame)   # warm-up: the first run is always slower

    times = []
    for _ in range(RUNS):
        t0 = time.perf_counter()
        detections = detect(frame)
        times.append((time.perf_counter() - t0) * 1000)

    print(f"\nDetections ({len(detections)}):")
    for d in detections:
        print(f"  {d.label:<7} {d.confidence:.2f}  box={d.box}")
    if not detections:
        print("  none (no person/cake above", CONF_THRESHOLD, ")")

    avg = sum(times) / len(times)
    print(f"\nTime over {RUNS} runs: avg {avg:.0f} ms, min {min(times):.0f} ms, "
          f"max {max(times):.0f} ms  (~{1000 / avg:.1f} detections/s)")

    # Save a copy with boxes drawn, to check them by eye
    for d in detections:
        x1, y1, x2, y2 = d.box
        cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 0, 255), 2)
        cv2.putText(frame, f"{d.label} {d.confidence:.2f}", (x1, max(y1 - 6, 12)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 255), 2)
    cv2.imwrite("detector_test.jpg", frame)
    print("Saved detector_test.jpg (boxes drawn)")
