"""Record N seconds from all configured cameras simultaneously, one thread
each. Every frame gets a wall-clock timestamp in a sidecar CSV -- the shared
clock that cross-camera association (and its offline validation) runs on.

Output: data/clips/ch<N>.mp4 + ch<N>_times.csv"""
from __future__ import annotations

import csv
import os
import sys
import threading
import time
from pathlib import Path

import cv2

NODE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(NODE))

from watch import sibling_source, redact, PyAVCapture  # noqa: E402

CLIPS = NODE / "data" / "clips"


def primary_source() -> str:
    src = os.environ.get("TW_CAMERA_SOURCE")
    if not src and (NODE / ".env").exists():
        for line in (NODE / ".env").read_text().splitlines():
            if line.strip().startswith("TW_CAMERA_SOURCE="):
                src = line.split("=", 1)[1].strip()
    if not src:
        sys.exit("TW_CAMERA_SOURCE not set (env or node/.env)")
    return src


def _record_one(url: str, ch: str, seconds: float):
    t_end = time.time() + seconds
    writer, times, n = None, [], 0
    while time.time() < t_end:
        try:
            cap = PyAVCapture(url)
            while time.time() < t_end:
                ok, frame = cap.read()
                if not ok:
                    break  # stream hiccup -> reconnect
                if writer is None:
                    writer = cv2.VideoWriter(
                        str(CLIPS / f"ch{ch}.mp4"),
                        cv2.VideoWriter_fourcc(*"mp4v"), 15,
                        (frame.shape[1], frame.shape[0]))
                writer.write(frame)
                times.append((n, time.time()))
                n += 1
            cap.release()
        except Exception as e:
            print(f"ch{ch}: {type(e).__name__}, reconnecting")
            time.sleep(1)
    if writer:
        writer.release()
    with open(CLIPS / f"ch{ch}_times.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["frame", "t_unix"])
        w.writerows(times)
    dur = times[-1][1] - times[0][1] if len(times) > 1 else 0
    fps = n / dur if dur else 0
    print(f"ch{ch}: {n} frames over {dur:.1f}s ({fps:.1f} fps) "
          f"-> {redact(url)}")


def record(cameras: list[str], seconds: float):
    CLIPS.mkdir(parents=True, exist_ok=True)
    src = primary_source()
    threads = [threading.Thread(target=_record_one,
                                args=(sibling_source(src, ch), ch, seconds))
               for ch in cameras]
    print(f"recording {seconds:.0f}s from {len(threads)} camera(s) ...")
    for t in threads:
        t.start()
    for t in threads:
        t.join()
