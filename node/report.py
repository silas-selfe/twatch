"""Summarize trafficwatch data with honesty about coverage.

Every hourly row includes uptime %, because a count of zero means nothing
unless the collector was actually running. Hours with partial uptime are
flagged so you never mistake downtime for quiet roads.

Usage:
  python report.py                 # today, local time
  python report.py --days 7        # last 7 days
  python report.py --csv out.csv   # export hourly table
"""
from __future__ import annotations

import argparse
import csv
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent


def local_hour(ts_utc: str) -> str:
    dt = datetime.fromisoformat(ts_utc)
    return dt.astimezone().strftime("%Y-%m-%d %H:00")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=1)
    ap.add_argument("--csv", default=None)
    ap.add_argument("--db", default=None)
    args = ap.parse_args()

    from watch import load_config
    cfg = load_config(str(HERE / "config.yaml"))
    db_path = args.db or str(HERE / cfg["output"]["db"])
    hb_secs = cfg["output"]["heartbeat_seconds"]
    if not Path(db_path).exists():
        sys.exit(f"no database at {db_path} -- run watch.py first")

    con = sqlite3.connect(db_path)
    since = (datetime.now(timezone.utc) - timedelta(days=args.days)).isoformat()

    events = con.execute(
        "SELECT ts_utc, class, direction FROM events WHERE ts_utc >= ?"
        " ORDER BY ts_utc", (since,)).fetchall()
    beats = con.execute(
        "SELECT ts_utc FROM heartbeats WHERE ts_utc >= ?", (since,)).fetchall()

    # hourly aggregation in local time
    hours: dict[str, dict] = defaultdict(lambda: defaultdict(int))
    directions, classes = set(), set()
    for ts, cls, direction in events:
        h = local_hour(ts)
        hours[h][(cls, direction)] += 1
        classes.add(cls)
        directions.add(direction)

    coverage: dict[str, float] = defaultdict(float)
    for (ts,) in beats:
        coverage[local_hour(ts)] += hb_secs / 3600 * 100

    all_hours = sorted(set(hours) | set(coverage))
    if not all_hours:
        sys.exit(f"no data in the last {args.days} day(s)")

    cols = sorted(classes)
    print(f"\ntrafficwatch -- last {args.days} day(s), local time")
    header = f"{'hour':<17}{'uptime%':>8}" + "".join(f"{c:>12}" for c in cols) \
        + f"{'total':>8}"
    print(header)
    print("-" * len(header))
    rows_out = []
    for h in all_hours:
        cov = min(coverage[h], 100.0)
        by_class = defaultdict(int)
        for (cls, _d), n in hours[h].items():
            by_class[cls] += n
        total = sum(by_class.values())
        flag = "" if cov >= 95 else "  (partial)" if cov > 0 else "  (DOWN)"
        print(f"{h:<17}{cov:>7.0f}%" +
              "".join(f"{by_class[c]:>12}" for c in cols) +
              f"{total:>8}{flag}")
        rows_out.append({"hour": h, "uptime_pct": round(cov, 1),
                         **{c: by_class[c] for c in cols}, "total": total})

    print("\nby direction:")
    dir_tot = defaultdict(int)
    for h in hours.values():
        for (cls, direction), n in h.items():
            dir_tot[(direction, cls)] += n
    for (direction, cls), n in sorted(dir_tot.items()):
        print(f"  {direction:<18}{cls:<12}{n}")

    if args.csv:
        with open(args.csv, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows_out[0].keys()))
            w.writeheader()
            w.writerows(rows_out)
        print(f"\nwrote {args.csv}")


if __name__ == "__main__":
    main()
