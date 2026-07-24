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
import glob
import os
import re
import signal
import sys
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import cv2
import numpy as np
import yaml

import db as dbm

HERE = Path(__file__).resolve().parent

# camera backend per platform: AVFoundation on macOS, V4L2 on Linux (Pi)
CAP_BACKEND = (cv2.CAP_AVFOUNDATION if sys.platform == "darwin"
               else cv2.CAP_V4L2 if sys.platform.startswith("linux")
               else cv2.CAP_ANY)


def _deep_merge(base: dict, overlay: dict) -> dict:
    for k, v in overlay.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v
    return base


def load_config(path: str) -> dict:
    """config.yaml holds fleet defaults; a per-deployment site.yaml
    (TW_SITE_CONFIG, default ./site.yaml) overlays site identity and
    calibration on top. Image updates never touch the overlay."""
    cfg = yaml.safe_load(Path(path).read_text())
    site_path = os.environ.get("TW_SITE_CONFIG")
    if site_path:
        # explicitly configured -> missing file is a deployment error, and
        # silently running with default identity/paths would corrupt data
        if not Path(site_path).exists():
            sys.exit(f"TW_SITE_CONFIG={site_path} does not exist -- refusing "
                     "to run with default site identity")
        _deep_merge(cfg, yaml.safe_load(Path(site_path).read_text()) or {})
    elif (HERE / "site.yaml").exists():
        _deep_merge(cfg, yaml.safe_load((HERE / "site.yaml").read_text()) or {})
    return cfg


# --------------------------------------------------------------------------
# Camera: a grab thread keeps only the LATEST frame, so slow inference never
# builds up buffer lag -- every processed frame is "now", and its wall-clock
# timestamp is the truth we store.
# --------------------------------------------------------------------------
def redact(source) -> str:
    """Camera URLs may embed credentials -- never let them reach a log."""
    import re
    return re.sub(r"//[^/@]+@", "//***@", str(source))


class PyAVCapture:
    """Read frames from an rtsp/http URL via PyAV (its bundled FFmpeg speaks
    RTSP; opencv-python\'s does not). Exposes the cv2.VideoCapture surface the
    grab thread uses: isOpened / read / release."""

    def __init__(self, url: str):
        import av
        self._av = av
        # (open_timeout, read_timeout) seconds -> never hang on a dead camera
        self._c = av.open(url, options={"rtsp_transport": "tcp"}, timeout=(10, 5))
        self._gen = self._c.decode(video=0)

    def isOpened(self) -> bool:
        return self._c is not None

    def read(self):
        try:
            frame = next(self._gen)
            return True, frame.to_ndarray(format="bgr24")
        except Exception:
            return False, None  # drop/EOF -> Camera loop triggers reconnect

    def release(self):
        try:
            self._c.close()
        except Exception:
            pass


def open_source(source, width: int, height: int):
    """Local device index -> OpenCV + platform backend; rtsp/http URL -> PyAV."""
    if isinstance(source, str) and "://" in source:
        return PyAVCapture(source)
    cap = cv2.VideoCapture(source, CAP_BACKEND)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
    return cap


# built-in / laptop cameras -- de-prioritized so 'auto' prefers an external cam
BUILTIN_RE = re.compile(r"facetime|built[\s-]?in|isight|integrated", re.I)


def camera_list() -> list[tuple[int, str]]:
    """[(cv2 index, device name)] of local video cameras, best-effort named.
    macOS: AVFoundation (index order matches cv2). Linux: /sys v4l names."""
    if sys.platform == "darwin":
        try:
            import AVFoundation  # pyobjc; macOS only
            devs = AVFoundation.AVCaptureDevice.devicesWithMediaType_(
                AVFoundation.AVMediaTypeVideo)
            named = [(i, str(d.localizedName())) for i, d in enumerate(devs)]
            if named:
                return named
        except Exception:
            pass  # fall through to probing
    elif sys.platform.startswith("linux"):
        out = []
        for p in sorted(glob.glob("/sys/class/video4linux/video*")):
            try:
                idx = int(p.rsplit("video", 1)[1])
                out.append((idx, (Path(p) / "name").read_text().strip()))
            except Exception:
                pass
        if out:
            return out
    # fallback: probe indices, generic names
    found = []
    for i in range(6):
        cap = cv2.VideoCapture(i, CAP_BACKEND)
        if cap.isOpened():
            found.append((i, f"camera {i}"))
            cap.release()
    return found


def resolve_source(source):
    """Turn a config source into what open_source wants:
    int index -> itself; rtsp/http URL -> itself; 'auto' -> the external
    camera's index (refuses if only a built-in laptop camera is present);
    a name substring -> its index (refuses if that camera is absent)."""
    if isinstance(source, int):
        return source
    s = str(source).strip()
    if "://" in s:
        return s
    if s.isdigit():
        return int(s)
    cams = camera_list()
    if s.lower() == "auto":
        external = [(i, n) for i, n in cams if not BUILTIN_RE.search(n)]
        if external:
            i, n = external[0]
            print(f"camera auto-select: index {i} ({n})")
            return i
        # Only a built-in laptop camera is present. It faces the room, not a
        # road: opening it silently would record the user AND produce junk
        # counts. Refuse -- run.sh retries every 10s, so re-plugging the USB
        # camera recovers on its own. Set camera.source explicitly to override.
        avail = [n for _, n in cams] or ["(no cameras detected)"]
        sys.exit(
            f"camera 'auto': no external camera found (available: {avail}).\n"
            "  -> plug the USB camera back in (run.sh will pick it up), or\n"
            "  -> set camera.source explicitly (index / name / rtsp:// URL)\n"
            "     if you really want the built-in camera.")
    for i, n in cams:  # name substring match (stable across index reordering)
        if s.lower() in n.lower():
            print(f"camera '{s}' -> index {i} ({n})")
            return i
    # a NAMED camera that is absent must never silently become a different
    # camera (e.g. the laptop's) -- fail; run.sh retries every 10s until it
    # reappears, and meanwhile no false "healthy coverage" data is produced
    sys.exit(f"camera '{s}' not found (available: {[n for _, n in cams]}) -- "
             "is it plugged in? retrying via run.sh, or fix camera.source")


def sibling_source(primary, spec):
    """A view-only source. A bare number reuses the primary URL with the
    channel swapped (rtsp://.../ch07/1 + '3' -> .../ch03/1), so NVR channels
    need no credentials on the command line. Anything else is used verbatim."""
    spec = str(spec).strip()
    if spec.isdigit() and isinstance(primary, str):
        m = re.search(r"/ch(\d+)(/|_)", primary)
        if m:
            width = len(m.group(1))
            return (primary[:m.start(1)] + str(int(spec)).zfill(width)
                    + primary[m.end(1):])
        m = re.search(r"channel=(\d+)", primary)
        if m:
            return primary[:m.start(1)] + spec + primary[m.end(1):]
    return spec


def camera_wall(main_frame, extras, width=1280):
    """Main (annotated) frame on top; view-only feeds tiled in a row below."""
    def fit(img, w, h):
        out = np.full((h, w, 3), 25, np.uint8)
        if img is None:
            cv2.putText(out, "no signal", (10, h // 2),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (120, 120, 120), 1)
            return out
        ih, iw = img.shape[:2]
        sc = min(w / iw, h / ih)
        rw, rh = max(int(iw * sc), 1), max(int(ih * sc), 1)
        y0, x0 = (h - rh) // 2, (w - rw) // 2
        out[y0:y0 + rh, x0:x0 + rw] = cv2.resize(img, (rw, rh))
        return out

    mh = int(width * main_frame.shape[0] / main_frame.shape[1])
    top = cv2.resize(main_frame, (width, mh))
    if not extras:
        return top
    n = len(extras)
    tw = width // n
    th = int(tw * 9 / 16)
    row = np.full((th, width, 3), 25, np.uint8)
    for i, (label, img) in enumerate(extras):
        tile = fit(img, tw, th)
        cv2.rectangle(tile, (0, 0), (tw - 1, th - 1), (70, 70, 70), 1)
        cv2.rectangle(tile, (0, 0), (tw, 18), (0, 0, 0), -1)
        cv2.putText(tile, label[:28], (5, 13), cv2.FONT_HERSHEY_SIMPLEX,
                    0.42, (200, 200, 200), 1)
        row[:, i * tw:i * tw + tile.shape[1]] = tile
    return np.vstack([top, row])


def set_config_source(config_path: str, value: str):
    """Persist camera.source in config.yaml, preserving comments."""
    text = Path(config_path).read_text()
    new, n = re.subn(r"(?m)^(\s*source:\s*).*$",
                     lambda m: f'{m.group(1)}"{value}"', text, count=1)
    if n:
        Path(config_path).write_text(new)


def pick_camera(config_path: str):
    """GUI: tile a live preview of every detected camera; click one (or press
    its number) to select. Writes the choice (by name) into config.yaml."""
    cams = camera_list()
    if not cams:
        print("no cameras detected")
        return
    TW, TH = 480, 270
    tiles = []
    for idx, name in cams:
        cap = cv2.VideoCapture(idx, CAP_BACKEND)
        frame = None
        if cap.isOpened():
            for _ in range(5):  # warm up; first frames are often blank
                ok, f = cap.read()
                if ok:
                    frame = f
        cap.release()
        tiles.append((idx, name, frame))
    cols = min(len(tiles), 2)
    rows = (len(tiles) + cols - 1) // cols
    canvas = np.full((rows * TH, cols * TW, 3), 30, np.uint8)
    boxes = []
    for k, (idx, name, frame) in enumerate(tiles):
        r, c = divmod(k, cols)
        x0, y0 = c * TW, r * TH
        thumb = (cv2.resize(frame, (TW, TH)) if frame is not None
                 else np.full((TH, TW, 3), 60, np.uint8))
        if frame is None:
            cv2.putText(thumb, "no preview", (20, TH // 2),
                        cv2.FONT_HERSHEY_SIMPLEX, 1, (200, 200, 200), 2)
        canvas[y0:y0 + TH, x0:x0 + TW] = thumb
        cv2.rectangle(canvas, (x0, y0), (x0 + TW, y0 + 26), (0, 0, 0), -1)
        cv2.putText(canvas, f"[{k}] {name}", (8, y0 + 19),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1)
        cv2.rectangle(canvas, (x0, y0), (x0 + TW - 1, y0 + TH - 1), (90, 90, 90), 1)
        boxes.append((x0, y0, x0 + TW, y0 + TH, idx, name))
    sel = {}
    def on_mouse(event, x, y, *_):
        if event == cv2.EVENT_LBUTTONDOWN:
            for (x0, y0, x1, y1, idx, name) in boxes:
                if x0 <= x < x1 and y0 <= y < y1:
                    sel["v"] = (idx, name)
    win = "twatch - click a camera to use it  (0-9 to pick, ESC to cancel)"
    cv2.namedWindow(win)
    cv2.setMouseCallback(win, on_mouse)
    while True:
        cv2.imshow(win, canvas)
        key = cv2.waitKey(30) & 0xFF
        if key == 27:  # ESC
            break
        if ord("0") <= key <= ord("9") and key - ord("0") < len(tiles):
            t = tiles[key - ord("0")]
            sel["v"] = (t[0], t[1])
        if "v" in sel:
            break
    cv2.destroyAllWindows()
    if "v" not in sel:
        print("cancelled -- config unchanged")
        return
    idx, name = sel["v"]
    set_config_source(config_path, name)
    print(f"selected {name!r} (index {idx}); wrote camera.source to {config_path}")


class Camera:
    def __init__(self, source, width: int, height: int):
        self.source, self.width, self.height = source, width, height
        self.cap = open_source(source, width, height)
        if not self.cap.isOpened():
            raise RuntimeError(f"could not open camera source {redact(source)}")
        ok, frame = self.cap.read()
        if not ok:
            raise RuntimeError(f"camera {redact(source)} opened but returned no frame")
        self._frame = frame
        self._ts = time.time()
        self._seq = 0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self):
        fails = 0
        while not self._stop.is_set():
            ok, frame = self.cap.read()
            if not ok:
                fails += 1
                if fails >= 100:  # ~5s dead -> reopen (rtsp drops, NVR reboots)
                    print(f"camera {redact(self.source)} unresponsive -- reconnecting")
                    self.cap.release()
                    time.sleep(2)
                    self.cap = open_source(self.source, self.width, self.height)
                    fails = 0
                time.sleep(0.05)
                continue
            fails = 0
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

    def peek(self):
        """Non-blocking: newest frame we already have (or None). View-only
        feeds use this so a slow/stalled camera can never hold up counting."""
        with self._lock:
            return (self._frame.copy() if self._frame is not None else None), self._ts

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
    cams = camera_list()
    if not cams:
        print("no local cameras detected")
        return
    print("local cameras (set camera.source to an index or a name substring):")
    for idx, name in cams:
        tag = " [built-in]" if BUILTIN_RE.search(name) else ""
        cap = cv2.VideoCapture(idx, CAP_BACKEND)
        res = ""
        if cap.isOpened():
            ok, frame = cap.read()
            if ok:
                h, w = frame.shape[:2]
                res = f"  {w}x{h}"
        cap.release()
        print(f"  index {idx}: {name}{tag}{res}")
    print("'auto' (the default) prefers an external camera over the built-in one.")


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
        try:
            return str(path.relative_to(HERE))
        except ValueError:  # data volume outside the code dir (container)
            return str(path)

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
    ap.add_argument("--list-cameras", action="store_true",
                    help="list local cameras with names, then exit")
    ap.add_argument("--pick-camera", action="store_true",
                    help="visual picker: click the camera to use (writes config)")
    ap.add_argument("--max-seconds", type=float, default=None,
                    help="stop after N seconds (testing)")
    ap.add_argument("--source", default=None,
                    help="override camera: a video file path for backtesting")
    ap.add_argument("--also-show", default=None, metavar="LIST",
                    help="extra VIEW-ONLY feeds shown beside the counted one "
                         "(comma-separated NVR channel numbers, e.g. 1,2,3, or "
                         "full URLs). Never detected, tracked, counted, or shipped.")
    args = ap.parse_args()

    if args.list_cameras:
        list_cameras()
        return
    if args.pick_camera:
        pick_camera(args.config)
        return

    cfg = load_config(args.config)
    from ultralytics import YOLO  # deferred: slow import

    model_cfg = cfg["model"]
    if model_cfg.get("device", "auto") == "auto":
        import torch
        model_cfg["device"] = ("mps" if torch.backends.mps.is_available()
                               else "cuda" if torch.cuda.is_available()
                               else "cpu")
        print(f"device auto-selected: {model_cfg['device']}")
    model = YOLO(model_cfg["weights"])
    class_ids = list(cfg["classes"].values())
    id_to_name = {v: k for k, v in cfg["classes"].items()}

    # site.yaml may override tracker params (e.g. track_buffer scaled to the
    # node's real fps); materialize the merged tracker config for ultralytics
    tracker_path = HERE / "bytetrack_road.yaml"
    if cfg.get("tracker"):
        merged = yaml.safe_load(tracker_path.read_text())
        merged.update(cfg["tracker"])
        tracker_path = Path(os.environ.get("TW_RUNTIME_DIR", str(HERE))) / "tracker_runtime.yaml"
        tracker_path.write_text(yaml.safe_dump(merged))
        print(f"tracker overrides applied: {cfg['tracker']}")

    if args.source:
        cam = FileSource(args.source)
    else:
        src = os.environ.get("TW_CAMERA_SOURCE") or cfg["camera"]["source"]
        src = resolve_source(src)  # name/'auto' -> index; int/URL pass through
        cam = Camera(src, cfg["camera"]["width"], cfg["camera"]["height"])
        print(f"camera source: {redact(src)}")
    frame, _ = cam.latest()
    frame_h, frame_w = frame.shape[:2]
    print(f"camera open: {frame_w}x{frame_h}")

    # VIEW-ONLY feeds: displayed beside the counted camera, never fed to the
    # model and never written to the database. Purely a monitor wall.
    view_cams = []
    if args.also_show and args.show:
        primary_src = os.environ.get("TW_CAMERA_SOURCE") or cfg["camera"]["source"]
        for spec in [x for x in args.also_show.split(",") if x.strip()]:
            url = sibling_source(primary_src, spec)
            label = f"ch{spec.strip()}" if spec.strip().isdigit() else redact(url)
            try:
                view_cams.append((label, Camera(url, frame_w, frame_h)))
                print(f"view-only feed: {label} ({redact(url)})")
            except Exception as e:
                print(f"view-only feed {label} unavailable: {type(e).__name__}")
    elif args.also_show:
        print("--also-show needs --show (it only affects the live window)")

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
                tracker=str(tracker_path),
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
                if view_cams:
                    extras = []
                    for label, vc in view_cams:
                        vf, _ = vc.peek()
                        extras.append((label, vf))
                    disp = camera_wall(disp, extras)
                cv2.imshow("trafficwatch", disp)
                if cv2.waitKey(1) & 0xFF == ord("q"):
                    break
    finally:
        dbm.end_run(con, run_id)
        cam.close()
        for _lbl, vc in view_cams:
            vc.close()
        if args.show:
            cv2.destroyAllWindows()
        total = sum(counter.counts.values())
        print(f"\nrun {run_id} ended: {frames} frames, {total} events")
        for (cls, direction), n in sorted(counter.counts.items()):
            print(f"  {cls:12s} {direction:15s} {n}")


if __name__ == "__main__":
    main()
