"""trafficwatch -- roadside vehicle & pedestrian counter.

Pipeline: webcam -> YOLO11 detection -> ByteTrack tracking -> directional
line-crossing counter -> SQLite (events + heartbeats) + audit snapshots.

Counting rules (what makes the numbers trustworthy):
1. Only CONFIRMED tracks count: seen >= min_frames, net horizontal travel
   >= min_travel_frac of frame width. Parked cars and box jitter can cross
   the line a thousand times and never satisfy the travel requirement.
2. A crossing whose track hasn't met the travel bar YET is held as PENDING
   and counted (with its original crossing timestamp) once travel is met --
   slow movers and tracks that spawn near the line are not undercounted.
3. Each track id counts at most once.
4. Every counted event stores an annotated snapshot, so accuracy is auditable
   against ground truth instead of assumed.

Run:  python watch.py [--show] [--config config.yaml]
      python watch.py --list-cameras
"""
from __future__ import annotations

import argparse
import signal
import sys
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import cv2
import yaml

import db as dbm

HERE = Path(__file__).resolve().parent


# --------------------------------------------------------------------------
# Camera: a grab thread keeps only the LATEST frame, so slow inference never
# builds up buffer lag -- every processed frame is "now", and its wall-clock
# timestamp is the truth we store.
# --------------------------------------------------------------------------
class Camera:
    def __init__(self, source: int, width: int, height: int):
        self.cap = cv2.VideoCapture(source, cv2.CAP_AVFOUNDATION)
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        if not self.cap.isOpened():
            raise RuntimeError(f"could not open camera source {source}")
        ok, frame = self.cap.read()
        if not ok:
            raise RuntimeError(f"camera {source} opened but returned no frame")
        self._frame = frame
        self._ts = time.time()
        self._seq = 0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self):
        while not self._stop.is_set():
            ok, frame = self.cap.read()
            if not ok:
                time.sleep(0.05)
                continue
            with self._lock:
                self._frame = frame
                self._ts = time.time()
                self._seq += 1

    def latest(self):
        """Block until a frame newer than the last one handed out arrives."""
        last = getattr(self, "_handed", -1)
        while True:
            with self._lock:
                if self._seq != last:
                    self._handed = self._seq
                    return self._frame.copy(), self._ts
            if self._stop.is_set():
                return None, None
            time.sleep(0.002)

    def close(self):
        self._stop.set()
        self._thread.join(timeout=2)
        self.cap.release()


class FileSource:
    """Video-file source for backtesting: frames are read sequentially and
    timestamps are synthesized from the file's fps so counting behaves as it
    would live."""

    def __init__(self, path: str):
        self.cap = cv2.VideoCapture(path)
        if not self.cap.isOpened():
            raise RuntimeError(f"could not open video file {path}")
        self.fps = self.cap.get(cv2.CAP_PROP_FPS) or 30.0
        self._t0 = time.time()
        self._n = 0

    def latest(self):
        ok, frame = self.cap.read()
        if not ok:
            return None, None
        ts = self._t0 + self._n / self.fps
        self._n += 1
        return frame, ts

    def close(self):
        self.cap.release()


def list_cameras():
    for idx in range(4):
        cap = cv2.VideoCapture(idx, cv2.CAP_AVFOUNDATION)
        if cap.isOpened():
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1920)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 1080)
            ok, frame = cap.read()
            if ok:
                h, w = frame.shape[:2]
                print(f"  index {idx}: {w}x{h}")
            cap.release()
    print("done probing indices 0-3")


# --------------------------------------------------------------------------
# Per-track counting state
# --------------------------------------------------------------------------
@dataclass
class Track:
    first_x: float
    first_ts: float
    last_x: float = 0.0
    last_ts: float = 0.0
    frames: int = 0
    conf_sum: float = 0.0
    conf_max: float = 0.0
    class_votes: Counter = field(default_factory=Counter)
    prev_side: int = 0            # -1 left of line, +1 right, 0 unknown
    counted: bool = False
    pending: dict | None = None   # crossing seen, travel bar not yet met

    @property
    def cls(self) -> str:
        return self.class_votes.most_common(1)[0][0]

    @property
    def conf_mean(self) -> float:
        return self.conf_sum / max(self.frames, 1)


class Counterline:
    def __init__(self, cfg: dict, frame_w: int, con, run_id: int, snap):
        c = cfg["counting"]
        self.line_x = c["line_frac"] * frame_w
        self.min_travel = c["min_travel_frac"] * frame_w
        self.min_frames = c["min_frames"]
        self.labels = c["direction_labels"]
        self.tracks: dict[int, Track] = {}
        self.last_seen: dict[int, float] = {}
        self.con = con
        self.run_id = run_id
        self.snap = snap  # callable(event_id_placeholder) -> path, or None
        self.counts = Counter()

    def update(self, tid: int, cx: float, conf: float, cls: str, ts: float,
               frame) -> bool:
        """Feed one tracked detection; returns True if an event was counted."""
        t = self.tracks.get(tid)
        if t is None:
            t = self.tracks[tid] = Track(first_x=cx, first_ts=ts)
        self.last_seen[tid] = ts
        t.frames += 1
        t.conf_sum += conf
        t.conf_max = max(t.conf_max, conf)
        t.class_votes[cls] += 1
        t.last_x, t.last_ts = cx, ts

        side = -1 if cx < self.line_x else 1
        crossed = t.prev_side != 0 and side != t.prev_side and not t.counted
        t.prev_side = side

        if crossed and t.pending is None:
            direction = "left_to_right" if side > 0 else "right_to_left"
            t.pending = {"ts": ts, "direction": direction}

        if t.pending and not t.counted:
            travel = abs(t.last_x - t.first_x)
            if travel >= self.min_travel and t.frames >= self.min_frames:
                self._count(tid, t, frame)
                return True
        return False

    def _count(self, tid: int, t: Track, frame):
        t.counted = True
        duration = max(t.last_ts - t.first_ts, 1e-3)
        speed = abs(t.last_x - t.first_x) / duration
        ts_utc = datetime.fromtimestamp(t.pending["ts"], timezone.utc)
        direction = self.labels.get(t.pending["direction"], t.pending["direction"])
        snap_path = self.snap(frame, t.cls, direction, ts_utc) if self.snap else None
        dbm.insert_event(
            self.con, self.run_id,
            ts_utc.isoformat(timespec="milliseconds"), tid, t.cls, direction,
            t.conf_mean, t.conf_max, t.frames, duration, speed, snap_path,
        )
        self.counts[(t.cls, direction)] += 1

    def prune(self, now: float, ttl: float = 10.0):
        for tid, seen in list(self.last_seen.items()):
            if now - seen > ttl:
                self.last_seen.pop(tid, None)
                self.tracks.pop(tid, None)

    @property
    def active(self) -> int:
        return len(self.tracks)


# --------------------------------------------------------------------------
# Snapshots
# --------------------------------------------------------------------------
class Snapshotter:
    def __init__(self, cfg: dict):
        out = cfg["output"]
        self.root = HERE / out["snapshots_dir"]
        self.max_w = out["snapshot_max_width"]
        self.enabled = out["snapshot_events"]
        self.keep_days = out["snapshot_keep_days"]
        self.root.mkdir(exist_ok=True)
        self._n = 0

    def cleanup(self):
        cutoff = time.time() - self.keep_days * 86400
        removed = 0
        for day_dir in self.root.glob("[0-9]" * 8):
            for f in day_dir.iterdir():
                if f.stat().st_mtime < cutoff:
                    f.unlink()
                    removed += 1
            if not any(day_dir.iterdir()):
                day_dir.rmdir()
        if removed:
            print(f"snapshot cleanup: removed {removed} files older than "
                  f"{self.keep_days} days")

    def save_event(self, annotated_frame, cls: str, direction: str,
                   ts_utc: datetime) -> str | None:
        if not self.enabled:
            return None
        day = ts_utc.strftime("%Y%m%d")
        d = self.root / day
        d.mkdir(exist_ok=True)
        self._n += 1
        name = f"{ts_utc.strftime('%H%M%S_%f')[:-3]}_{cls}_{direction}_{self._n}.jpg"
        path = d / name
        h, w = annotated_frame.shape[:2]
        if w > self.max_w:
            scale = self.max_w / w
            annotated_frame = cv2.resize(annotated_frame,
                                         (self.max_w, int(h * scale)))
        cv2.imwrite(str(path), annotated_frame,
                    [cv2.IMWRITE_JPEG_QUALITY, 80])
        return str(path.relative_to(HERE))

    def save_scene(self, frame) -> None:
        d = self.root / "scene"
        d.mkdir(exist_ok=True)
        name = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S") + ".jpg"
        cv2.imwrite(str(d / name), frame, [cv2.IMWRITE_JPEG_QUALITY, 80])


# --------------------------------------------------------------------------
# Main loop
# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(HERE / "config.yaml"))
    ap.add_argument("--show", action="store_true",
                    help="display annotated live window")
    ap.add_argument("--list-cameras", action="store_true")
    ap.add_argument("--max-seconds", type=float, default=None,
                    help="stop after N seconds (testing)")
    ap.add_argument("--source", default=None,
                    help="override camera: a video file path for backtesting")
    args = ap.parse_args()

    if args.list_cameras:
        list_cameras()
        return

    cfg = yaml.safe_load(Path(args.config).read_text())
    from ultralytics import YOLO  # deferred: slow import

    model_cfg = cfg["model"]
    model = YOLO(model_cfg["weights"])
    class_ids = list(cfg["classes"].values())
    id_to_name = {v: k for k, v in cfg["classes"].items()}

    if args.source:
        cam = FileSource(args.source)
    else:
        cam = Camera(cfg["camera"]["source"], cfg["camera"]["width"],
                     cfg["camera"]["height"])
    frame, _ = cam.latest()
    frame_h, frame_w = frame.shape[:2]
    print(f"camera open: {frame_w}x{frame_h}")

    con = dbm.connect(str(HERE / cfg["output"]["db"]))
    run_id = dbm.start_run(con, model_cfg["weights"], cfg)
    snap = Snapshotter(cfg)
    snap.cleanup()
    counter = Counterline(cfg, frame_w, con, run_id, snap.save_event)
    line_x = int(counter.line_x)

    hb_every = cfg["output"]["heartbeat_seconds"]
    scene_every = cfg["output"]["scene_snapshot_minutes"] * 60
    last_hb = last_scene = time.time()
    frames = 0
    fps_window: list[float] = []

    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())

    t_start = time.time()
    print(f"run {run_id} started; count line at x={line_x}px; ctrl-c to stop")

    try:
        while not stop.is_set():
            if args.max_seconds and time.time() - t_start > args.max_seconds:
                break
            frame, ts = cam.latest()
            if frame is None:
                break
            t0 = time.time()
            results = model.track(
                frame, persist=True, verbose=False,
                conf=model_cfg["conf"], imgsz=model_cfg["imgsz"],
                device=model_cfg["device"], classes=class_ids,
                tracker=str(HERE / "bytetrack_road.yaml"),
            )
            frames += 1
            fps_window.append(time.time() - t0)
            if len(fps_window) > 60:
                fps_window.pop(0)

            r = results[0]
            annotated = None
            counted_now = False
            if r.boxes is not None and r.boxes.id is not None:
                ids = r.boxes.id.int().tolist()
                confs = r.boxes.conf.tolist()
                clss = r.boxes.cls.int().tolist()
                xyxy = r.boxes.xyxy.tolist()
                for tid, cf, ci, box in zip(ids, confs, clss, xyxy):
                    cx = (box[0] + box[2]) / 2
                    if annotated is None:
                        annotated = r.plot(line_width=2)
                        cv2.line(annotated, (line_x, 0), (line_x, frame_h),
                                 (0, 200, 255), 2)
                    counted_now |= counter.update(
                        tid, cx, cf, id_to_name.get(ci, str(ci)), ts, annotated)

            now = time.time()
            counter.prune(now)

            if now - last_hb >= hb_every:
                fps = 1.0 / (sum(fps_window) / len(fps_window)) if fps_window else 0
                dbm.insert_heartbeat(con, run_id, fps, frames, counter.active)
                last_hb = now
                total = sum(counter.counts.values())
                print(f"[{datetime.now().strftime('%H:%M:%S')}] "
                      f"fps={fps:.1f} frames={frames} active={counter.active} "
                      f"counted={total}")

            if now - last_scene >= scene_every:
                snap.save_scene(annotated if annotated is not None else frame)
                last_scene = now

            if args.show:
                disp = annotated if annotated is not None else frame
                if annotated is None:
                    disp = frame.copy()
                    cv2.line(disp, (line_x, 0), (line_x, frame_h),
                             (0, 200, 255), 2)
                cv2.imshow("trafficwatch", disp)
                if cv2.waitKey(1) & 0xFF == ord("q"):
                    break
    finally:
        dbm.end_run(con, run_id)
        cam.close()
        if args.show:
            cv2.destroyAllWindows()
        total = sum(counter.counts.values())
        print(f"\nrun {run_id} ended: {frames} frames, {total} events")
        for (cls, direction), n in sorted(counter.counts.items()):
            print(f"  {cls:12s} {direction:15s} {n}")


if __name__ == "__main__":
    main()
