"""Live fusion: one capture+track worker per camera feeding the shared
association engine; a stepper thread advances the engine on the wall clock.

Each worker keeps only the LATEST frame (grab thread), so slow inference
never builds buffer lag -- same rule as the single-camera collector. Every
camera runs its own YOLO instance (independent ByteTrack state); the GPU
serializes the actual inference. Latest annotated frames are kept in memory
for the UI's thumbnail strip."""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import cv2

NODE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(NODE))

from watch import sibling_source, redact, PyAVCapture  # noqa: E402
from .associate import Engine  # noqa: E402
from .geometry import load_calibrations, resolve_device, site_frame  # noqa: E402
from .record import primary_source  # noqa: E402


def world_gates(cfg: dict, frame) -> list:
    """Count gates in world units. Geo sites write gates as lat/lon pairs;
    ref-px sites as reference pixels."""
    gates = []
    for g in cfg.get("counting", {}).get("gates", []):
        g = dict(g)
        if frame is not None:
            ax, ay = frame.to_local(*g["a"])
            bx, by = frame.to_local(*g["b"])
            g["a"], g["b"] = [ax, ay], [bx, by]
        gates.append(g)
    return gates

CLASS_IDS = [0, 1, 2, 3, 5, 7]


class _LatestFrame(threading.Thread):
    """Decode as fast as the stream produces; expose only the newest frame."""

    def __init__(self, url: str, name: str):
        super().__init__(daemon=True, name=f"grab-{name}")
        self.url = url
        self.frame = None
        self.t = 0.0
        self.lock = threading.Lock()
        self.stop = threading.Event()

    def run(self):
        while not self.stop.is_set():
            try:
                cap = PyAVCapture(self.url)
                while not self.stop.is_set():
                    ok, frame = cap.read()
                    if not ok:
                        break
                    with self.lock:
                        self.frame = frame
                        self.t = time.time()
                cap.release()
            except Exception as e:
                print(f"{self.name}: {type(e).__name__} -- reconnect in 5s")
            time.sleep(5)

    def latest(self):
        with self.lock:
            return (self.frame.copy(), self.t) if self.frame is not None \
                else (None, 0.0)


class CameraWorker(threading.Thread):
    def __init__(self, cam_id: str, url: str, cal, model_cfg: dict,
                 engine: Engine, elock: threading.Lock):
        super().__init__(daemon=True, name=f"track-cam{cam_id}")
        self.cam_id = cam_id
        self.cal = cal
        self.engine = engine
        self.elock = elock
        self.model_cfg = model_cfg
        self.grab = _LatestFrame(url, f"cam{cam_id}")
        self.annotated = None      # latest annotated jpeg bytes (UI thumbs)
        self.fps = 0.0
        self.stop = threading.Event()
        print(f"cam{cam_id}: {redact(url)}")

    def run(self):
        from ultralytics import YOLO
        model = YOLO(str(NODE / self.model_cfg.get("weights", "yolo11s.pt")))
        self.grab.start()
        last_t, last_thumb, n, t_fps = 0.0, 0.0, 0, time.time()
        while not self.stop.is_set():
            frame, t = self.grab.latest()
            if frame is None or t <= last_t:
                time.sleep(0.01)
                continue
            last_t = t
            res = model.track(
                frame, persist=True, verbose=False,
                imgsz=self.model_cfg.get("imgsz", 640),
                conf=self.model_cfg.get("conf", 0.15),
                classes=CLASS_IDS,
                device=resolve_device(self.model_cfg.get("device", "auto")),
                tracker=str(NODE / "bytetrack_road.yaml"))[0]
            if res.boxes is not None and res.boxes.id is not None:
                with self.elock:
                    for box, tid, cls in zip(res.boxes.xyxy.cpu().numpy(),
                                             res.boxes.id.cpu().numpy(),
                                             res.boxes.cls.cpu().numpy()):
                        x1, y1, x2, y2 = box
                        wx, wy = self.cal.project((x1 + x2) / 2, y2)
                        self.engine.observe(self.cam_id, int(tid), int(cls),
                                            wx, wy, t)
            n += 1
            if time.time() - t_fps >= 5:
                self.fps = n / (time.time() - t_fps)
                n, t_fps = 0, time.time()
            if time.time() - last_thumb >= 1.0:
                last_thumb = time.time()
                thumb = cv2.resize(res.plot(), (480, 270))
                ok, buf = cv2.imencode(".jpg", thumb,
                                       [cv2.IMWRITE_JPEG_QUALITY, 70])
                if ok:
                    self.annotated = buf.tobytes()


class Fusion:
    """Owns the engine, its lock, the workers, and the stepper."""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.cameras = [str(c) for c in cfg.get("cameras", [])]
        self.cals = load_calibrations(self.cameras)
        self.frame = site_frame(self.cals)   # None -> legacy ref-px world
        counting = cfg.get("counting", {})
        assoc = dict(cfg.get("association", {}))
        assoc["min_travel"] = counting.get(
            "min_travel", counting.get("min_travel_px", 60))
        self.engine = Engine(assoc, gates=world_gates(cfg, self.frame))
        self.elock = threading.Lock()
        src = primary_source()
        self.workers = [
            CameraWorker(ch, sibling_source(src, ch), self.cals[ch],
                         cfg.get("model", {}), self.engine, self.elock)
            for ch in self.cameras]
        self._stepper = threading.Thread(target=self._step_loop, daemon=True)
        self.started = time.time()

    def start(self):
        for w in self.workers:
            w.start()
        self._stepper.start()

    def _step_loop(self):
        bin_s = self.engine.bin_s
        while True:
            time.sleep(bin_s)
            with self.elock:
                self.engine.step(time.time())

    def state(self):
        now = time.time()
        with self.elock:
            objs = self.engine.snapshot(now)
            counts = {g: dict(d) for g, d in self.engine.counts.items()}
            events = self.engine.events[-25:]
        return {"t": now, "objects": objs, "counts": counts,
                "events": events,
                "cams": {w.cam_id: {"fps": round(w.fps, 1),
                                    "alive": w.grab.t > now - 5}
                         for w in self.workers}}
