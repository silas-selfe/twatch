"""Ship hourly summaries from this node's local SQLite to the central
Postgres (database "twatch", schema "monitor_traffic").

Protocol -- built so an unreliable node still produces trustworthy central
data:
- Only COMPLETE hours ship (the in-progress hour never does).
- Every write is an idempotent upsert keyed (site_id, hour_start, class,
  direction); retries and re-ships are always safe.
- The local `ship_state` table records what has shipped. Network down for a
  day? The next successful cycle backfills every missed hour from SQLite --
  nothing is lost, nothing is double-counted.
- Hours with zero events still ship a coverage row per class ('__coverage__')
  so the central store can distinguish "no traffic" from "node offline".

Env:
  TW_CENTRAL_DSN   postgres://user:pass@host:5432/twatch  (required to ship)
  TW_SITE_CONFIG   optional site.yaml overlay path (same as watch.py)
  TW_VERSION       node code version stamp (set by the container image)

Usage:
  python aggregator.py --once      # ship everything unshipped, then exit
  python aggregator.py --loop      # ship on the hour, forever
  python aggregator.py --dry-run   # show what would ship, no Postgres needed
"""
from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import statistics
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from watch import load_config, HERE

SCHEMA = "monitor_traffic"


def utc_hour(ts_utc: str) -> datetime:
    dt = datetime.fromisoformat(ts_utc)
    return dt.replace(minute=0, second=0, microsecond=0)


def connect_local(cfg) -> sqlite3.Connection:
    con = sqlite3.connect(str(HERE / cfg["output"]["db"]))
    con.execute("""CREATE TABLE IF NOT EXISTS ship_state (
        hour_start TEXT PRIMARY KEY,
        shipped_at TEXT NOT NULL)""")
    con.commit()
    return con


def unshipped_hours(con, hb_secs: int) -> list[datetime]:
    """Complete hours with ANY heartbeat that have not shipped yet."""
    now_hour = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    shipped = {r[0] for r in con.execute("SELECT hour_start FROM ship_state")}
    hours = set()
    for (ts,) in con.execute("SELECT ts_utc FROM heartbeats"):
        h = utc_hour(ts)
        if h < now_hour and h.isoformat() not in shipped:
            hours.add(h)
    return sorted(hours)


def summarize_hour(con, hour: datetime, hb_secs: int) -> tuple[list, float]:
    """Rows of (class, direction, n, median_speed, conf_mean) + uptime_pct."""
    lo, hi = hour.isoformat(), (hour + timedelta(hours=1)).isoformat()
    groups = defaultdict(lambda: {"n": 0, "speeds": [], "confs": []})
    for cls, direction, sp, conf in con.execute(
            "SELECT class, direction, speed_px_s, conf_mean FROM events"
            " WHERE ts_utc >= ? AND ts_utc < ?", (lo, hi)):
        g = groups[(cls, direction)]
        g["n"] += 1
        g["speeds"].append(sp)
        g["confs"].append(conf)
    n_hb = con.execute(
        "SELECT count(*) FROM heartbeats WHERE ts_utc >= ? AND ts_utc < ?",
        (lo, hi)).fetchone()[0]
    uptime = min(n_hb * hb_secs / 3600 * 100, 100.0)
    rows = [(cls, d, g["n"],
             round(statistics.median(g["speeds"]), 1) if g["speeds"] else None,
             round(sum(g["confs"]) / len(g["confs"]), 3) if g["confs"] else None)
            for (cls, d), g in sorted(groups.items())]
    if not rows:  # empty hour still ships its coverage
        rows = [("__coverage__", "none", 0, None, None)]
    return rows, round(uptime, 1)


def ship(cfg, dry_run: bool = False) -> int:
    site = cfg["site"]["id"]
    version = os.environ.get("TW_VERSION", "dev")
    hb_secs = cfg["output"]["heartbeat_seconds"]
    local = connect_local(cfg)
    hours = unshipped_hours(local, hb_secs)
    if not hours and dry_run:
        print(f"[{datetime.now():%H:%M}] nothing to ship")
        return 0

    if dry_run:
        for h in hours:
            rows, up = summarize_hour(local, h, hb_secs)
            for r in rows:
                print(f"  {h.isoformat()} {up:5.1f}% {r}")
        print(f"dry-run: {len(hours)} hour(s) would ship for site {site!r}")
        return len(hours)

    dsn = os.environ.get("TW_CENTRAL_DSN")
    if not dsn:
        sys.exit("TW_CENTRAL_DSN is not set -- cannot ship (use --dry-run to inspect)")
    if "YOUR-RDS-ENDPOINT" in dsn or ":PASSWORD@" in dsn:
        sys.exit("TW_CENTRAL_DSN still contains .env.example placeholders -- "
                 "edit node/.env with your real endpoint and password")
    import psycopg
    with psycopg.connect(dsn, connect_timeout=15) as pg:
        with pg.cursor() as cur:
            # best-effort site registration; nodes may lack the grant
            try:
                cur.execute(
                    f"INSERT INTO {SCHEMA}.sites (site_id, label, dir_ltr_label, dir_rtl_label)"
                    " VALUES (%s, %s, %s, %s) ON CONFLICT (site_id) DO NOTHING",
                    (site, cfg["site"].get("label", site),
                     cfg["counting"]["direction_labels"].get("left_to_right", "left_to_right"),
                     cfg["counting"]["direction_labels"].get("right_to_left", "right_to_left")))
            except psycopg.errors.InsufficientPrivilege:
                pg.rollback()
                print(f"note: no INSERT grant on sites -- register {site!r} manually")
            shipped = 0
            for h in hours:
                rows, uptime = summarize_hour(local, h, hb_secs)
                for cls, d, n, med, conf in rows:
                    cur.execute(
                        f"""INSERT INTO {SCHEMA}.hourly_summary
                            (site_id, hour_start, class, direction, n,
                             median_speed, conf_mean, uptime_pct, node_version)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                            ON CONFLICT (site_id, hour_start, class, direction)
                            DO UPDATE SET n = EXCLUDED.n,
                                          median_speed = EXCLUDED.median_speed,
                                          conf_mean = EXCLUDED.conf_mean,
                                          uptime_pct = EXCLUDED.uptime_pct,
                                          node_version = EXCLUDED.node_version,
                                          shipped_at = now()""",
                        (site, h, cls, d, n, med, conf, uptime, version))
                pg.commit()  # per-hour commit; a mid-batch failure loses nothing
                local.execute(
                    "INSERT OR REPLACE INTO ship_state (hour_start, shipped_at)"
                    " VALUES (?, ?)",
                    (h.isoformat(), datetime.now(timezone.utc).isoformat()))
                local.commit()
                shipped += 1
            # node liveness for the fleet view
            n_events = local.execute("SELECT count(*) FROM events").fetchone()[0]
            free_gb = shutil.disk_usage(str(HERE)).free / 1e9
            cur.execute(
                f"INSERT INTO {SCHEMA}.node_heartbeat"
                " (site_id, node_version, local_events, disk_free_gb)"
                " VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
                (site, version, n_events, round(free_gb, 1)))
            pg.commit()
    print(f"[{datetime.now():%H:%M}] shipped {shipped} hour(s) for site {site!r}"
          if shipped else f"[{datetime.now():%H:%M}] nothing to ship; heartbeat sent")
    return shipped


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(HERE / "config.yaml"))
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true")
    mode.add_argument("--loop", action="store_true")
    mode.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    cfg = load_config(args.config)
    if args.dry_run:
        ship(cfg, dry_run=True)
        return
    if args.once:
        ship(cfg)
        return
    while True:  # --loop: fire a few minutes past each hour boundary
        try:
            ship(cfg)
        except Exception as e:  # network/db blips must never kill the shipper
            print(f"ship failed (will retry next hour): {e}", file=sys.stderr)
        now = datetime.now(timezone.utc)
        nxt = now.replace(minute=3, second=0, microsecond=0)
        if nxt <= now:
            nxt += timedelta(hours=1)
        time.sleep((nxt - now).total_seconds())


if __name__ == "__main__":
    main()
