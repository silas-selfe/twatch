# trafficwatch

Roadside vehicle & pedestrian counter: webcam → YOLO11 + ByteTrack →
directional line-crossing counts → SQLite. Built so the stored data
*accurately represents the real world*, not just whatever the detector said.

Now fleet-ready: each camera is a containerized **node** (Raspberry Pi or any
Linux box) keeping full-fidelity data locally and shipping hourly summaries
to a central Postgres (`twatch` database, `monitor_traffic` schema) with an
idempotent store-and-forward protocol — offline nodes backfill safely when
they reconnect. See [deploy/PI_SETUP.md](deploy/PI_SETUP.md) for bringing up
a node, [central_schema.sql](central_schema.sql) for the central store, and
[site.yaml.example](site.yaml.example) for per-camera identity/calibration.
Images build multi-arch on every push (`ghcr.io/silas-selfe/twatch`);
Watchtower on each node picks them up within 30 minutes.

## Why the numbers can be trusted

| Threat to accuracy | Defense |
|---|---|
| Double counting | Each track id counts at most once, at the moment it crosses the line |
| Parked cars / box jitter | A track must accumulate net horizontal travel ≥ 10% of frame width before its crossing counts |
| Slow movers / tracks spawning near the line | Crossings are held *pending* and counted (with original timestamp) once travel is met — no undercount |
| Occlusion (trees) splitting tracks | ByteTrack with 90-frame track buffer + low-confidence second-pass association |
| Ghost tracks | New tracks only start from detections ≥ 0.6 confidence |
| Downtime mistaken for zero traffic | Heartbeat row every 15 s; the report prints uptime % per hour and flags partial/DOWN hours |
| Silent meaning drift when settings change | Every run stores its full config + model version in the `runs` table |
| Unverifiable accuracy | Every counted event saves an annotated snapshot — spot-audit against reality any time |
| Camera got bumped | Hourly scene snapshot in `snapshots/scene/` |

## Run

```bash
cd ~/Documents/Projects/ObjectDetection/trafficwatch
./run.sh              # headless collector; auto-restarts on crash, blocks idle sleep
./run.sh --show       # with live annotated window (press q to quit)
```

First run from a new terminal app will trigger the macOS camera-permission
dialog — grant it. `python watch.py --list-cameras` probes device indices if
the external webcam isn't index 0.

## Calibrate (do this once, with `--show`)

1. The orange vertical line is the count line (`counting.line_frac` in
   `config.yaml`). Put it where traffic is unobstructed — away from trees and
   parked cars.
2. Rename directions in `config.yaml → counting.direction_labels` to the real
   bearings (e.g. `left_to_right: northbound`).
3. Watch for 10 minutes; every counted event prints and saves a snapshot.

## Audit accuracy (recommended before trusting analytics)

Manually count traffic for a fixed window (e.g. 15 min) while the collector
runs, then compare with `python report.py`. Snapshots in `snapshots/YYYYMMDD/`
show every counted event; false positives are visible there, and misses show
up as the gap between your manual count and the report. Tune `model.imgsz`
(larger helps distant pedestrians) or the confidence thresholds in
`bytetrack_road.yaml`, and re-audit. Known caveat: night-time recall drops —
per-event confidence is stored so low-light data can be filtered or
re-weighted at analysis time.

## Analyze

```bash
.venv/bin/python report.py            # today's hourly table + direction totals
.venv/bin/python report.py --days 7   # last week
.venv/bin/python report.py --csv out.csv
```

Export everything to Excel (About / Events / Hourly / Daily sheets):

```bash
.venv/bin/python export_xlsx.py            # writes traffic.xlsx
.venv/bin/python export_xlsx.py --out ~/Desktop/traffic.xlsx
```

The summary sheets use live COUNTIFS formulas over the raw Events sheet, and
the Hourly/Daily sheets carry a measured uptime % column — hours below 100%
undercount reality, so filter on it before comparing periods.

Or query `traffic.db` directly — `events` is one row per counted road user
(UTC timestamp, class, direction, confidence, duration, px/s speed, snapshot
path). Backtest any recorded clip with `python watch.py --source clip.mp4`.

## Files

- `watch.py` — collector (capture thread → YOLO11s on MPS → ByteTrack → counter)
- `config.yaml` — all settings that affect what the data means
- `bytetrack_road.yaml` — tracker tuning for occlusion-heavy fixed camera
- `db.py` / `report.py` — storage and reporting
- `run.sh` — supervised long-running entry point
