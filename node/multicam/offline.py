"""Offline replay: run the SAME engine that live fusion uses over recorded
clips. This is how association changes get validated -- record once, replay
cheaply, compare multi-camera global-id counts and eyeball the rendered map
before anything touches the live path.

Outputs (data/clips/): world_tracks.csv (cached tracking pass),
global_tracks.csv, map_trails.jpg, map_view.mp4"""
from __future__ import annotations

import csv
from collections import defaultdict
from pathlib import Path

import cv2
import numpy as np

from .associate import Engine, CLS_NAME
from .geometry import NODE, load_calibrations, resolve_device

CLIPS = NODE / "data" / "clips"
CLASS_IDS = [0, 1, 2, 3, 5, 7]


def track_clips(cameras, model_cfg, force=False) -> Path:
    """YOLO+ByteTrack per clip (fresh tracker each), project bottom-centers
    -> world_tracks.csv. Cached: reruns only with force=True."""
    out = CLIPS / "world_tracks.csv"
    if out.exists() and not force:
        print(f"using cached {out} (--retrack to redo)")
        return out
    from ultralytics import YOLO
    cals = load_calibrations(cameras)
    rows = []
    for ch in cameras:
        times = {}
        with open(CLIPS / f"ch{ch}_times.csv") as f:
            for r in csv.DictReader(f):
                times[int(r["frame"])] = float(r["t_unix"])
        model = YOLO(str(NODE / model_cfg.get("weights", "yolo11s.pt")))
        results = model.track(
            source=str(CLIPS / f"ch{ch}.mp4"), stream=True,
            imgsz=model_cfg.get("imgsz", 960),
            conf=model_cfg.get("conf", 0.15),
            classes=CLASS_IDS,
            device=resolve_device(model_cfg.get("device", "auto")),
            verbose=False, tracker=str(NODE / "bytetrack_road.yaml"))
        n = 0
        for i, res in enumerate(results):
            if res.boxes is None or res.boxes.id is None:
                continue
            for box, tid, cls, cf in zip(res.boxes.xyxy.cpu().numpy(),
                                         res.boxes.id.cpu().numpy(),
                                         res.boxes.cls.cpu().numpy(),
                                         res.boxes.conf.cpu().numpy()):
                x1, y1, x2, y2 = box
                wx, wy = cals[ch].project((x1 + x2) / 2, y2)
                rows.append([ch, i, f"{times.get(i, 0):.3f}", int(tid),
                             int(cls), f"{cf:.3f}", f"{wx:.1f}", f"{wy:.1f}"])
                n += 1
        print(f"ch{ch}: {n} track-frames")
    with open(out, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["cam", "frame", "t", "tid", "cls", "conf", "wx", "wy"])
        w.writerows(rows)
    return out


def replay(cameras, cfg, ref_path: Path, render=True):
    """Feed world_tracks.csv through the live engine in time order."""
    rows = []
    with open(CLIPS / "world_tracks.csv") as f:
        for r in csv.DictReader(f):
            rows.append((float(r["t"]), r["cam"], int(r["tid"]),
                         int(r["cls"]), float(r["wx"]), float(r["wy"])))
    rows.sort()
    from .fusion import world_gates
    from .geometry import site_frame
    cals = load_calibrations(cameras)
    counting = cfg.get("counting", {})
    assoc = dict(cfg.get("association", {}))
    assoc["min_travel"] = counting.get(
        "min_travel", counting.get("min_travel_px", 60))
    eng = Engine(assoc, gates=world_gates(cfg, site_frame(cals)))

    t0 = rows[0][0]
    bin_s = eng.bin_s
    next_step = t0 + bin_s
    snapshots = []  # (t, engine snapshot) for rendering
    gid_hist = defaultdict(list)
    for t, cam, tid, cls, x, y in rows:
        while t >= next_step:
            eng.step(next_step)
            for s in eng.snapshot(next_step):
                gid_hist[s["gid"]].append((next_step, s["x"], s["y"], s["cams"]))
            next_step += bin_s
        eng.observe(cam, tid, cls, x, y, t)
    eng.step(next_step)

    multi = {g: obs for g, obs in gid_hist.items()
             if len({c for _, _, _, cams in obs for c in cams}) > 1}
    print(f"\n{len(gid_hist)} global ids, {len(multi)} seen by 2+ cameras")
    for g, obs in sorted(multi.items()):
        cams = sorted({c for _, _, _, cs in obs for c in cs})
        print(f"  gid {g}: {'+'.join('cam' + c for c in cams)}"
              f"  {obs[-1][0] - obs[0][0]:.1f}s")
    for g_id, dirs in eng.counts.items():
        print(f"gate '{g_id}': " + ", ".join(f"{d}={n}" for d, n in dirs.items()))

    with open(CLIPS / "global_tracks.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["gid", "t", "x", "y", "cams"])
        for g, obs in sorted(gid_hist.items()):
            for t, x, y, cams in obs:
                w.writerow([g, f"{t:.3f}", x, y, "+".join(cams)])

    if not render:
        return
    ref = cv2.imread(str(ref_path))
    rng = np.random.default_rng(3)
    colors = {g: tuple(int(v) for v in rng.integers(60, 255, 3))
              for g in gid_hist}
    trails = ref.copy()
    for g, obs in gid_hist.items():
        pts = [(int(x), int(y)) for _, x, y, _ in obs]
        thick = 3 if g in multi else 1
        for p, q in zip(pts, pts[1:]):
            if np.hypot(q[0] - p[0], q[1] - p[1]) < 120:
                cv2.line(trails, p, q, colors[g], thick)
    cv2.imwrite(str(CLIPS / "map_trails.jpg"), trails,
                [cv2.IMWRITE_JPEG_QUALITY, 90])
    print(f"wrote {CLIPS / 'map_trails.jpg'}")
