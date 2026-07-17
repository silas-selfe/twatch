"""SQLite layer for trafficwatch.

Design rules that keep the data honest:
- events are raw, one row per counted road user; aggregation happens at query
  time so no analytical choice is baked into storage.
- heartbeats record that the system was ALIVE, so an hour with no events is
  distinguishable from an hour the collector was down.
- every run stores the exact config + model used, so threshold changes never
  silently redefine what the numbers mean.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY,
    started_utc TEXT NOT NULL,
    ended_utc   TEXT,
    model       TEXT,
    config_json TEXT
);
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY,
    run_id      INTEGER NOT NULL REFERENCES runs(id),
    ts_utc      TEXT NOT NULL,
    track_id    INTEGER,
    class       TEXT NOT NULL,
    direction   TEXT NOT NULL,
    conf_mean   REAL,
    conf_max    REAL,
    frames_seen INTEGER,
    duration_s  REAL,
    speed_px_s  REAL,
    snapshot    TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts_utc);
CREATE TABLE IF NOT EXISTS heartbeats (
    id            INTEGER PRIMARY KEY,
    run_id        INTEGER NOT NULL REFERENCES runs(id),
    ts_utc        TEXT NOT NULL,
    fps           REAL,
    frames        INTEGER,
    active_tracks INTEGER
);
CREATE INDEX IF NOT EXISTS idx_heartbeats_ts ON heartbeats(ts_utc);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def connect(path: str) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(SCHEMA)
    return con


def start_run(con: sqlite3.Connection, model: str, config: dict) -> int:
    cur = con.execute(
        "INSERT INTO runs (started_utc, model, config_json) VALUES (?, ?, ?)",
        (utcnow(), model, json.dumps(config, default=str)),
    )
    con.commit()
    return cur.lastrowid


def end_run(con: sqlite3.Connection, run_id: int) -> None:
    con.execute("UPDATE runs SET ended_utc = ? WHERE id = ?", (utcnow(), run_id))
    con.commit()


def insert_event(con: sqlite3.Connection, run_id: int, ts_utc: str, track_id: int,
                 cls: str, direction: str, conf_mean: float, conf_max: float,
                 frames_seen: int, duration_s: float, speed_px_s: float,
                 snapshot: str | None) -> int:
    cur = con.execute(
        "INSERT INTO events (run_id, ts_utc, track_id, class, direction, conf_mean,"
        " conf_max, frames_seen, duration_s, speed_px_s, snapshot)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (run_id, ts_utc, track_id, cls, direction, round(conf_mean, 3),
         round(conf_max, 3), frames_seen, round(duration_s, 2),
         round(speed_px_s, 1), snapshot),
    )
    con.commit()
    return cur.lastrowid


def insert_heartbeat(con: sqlite3.Connection, run_id: int, fps: float,
                     frames: int, active_tracks: int) -> None:
    con.execute(
        "INSERT INTO heartbeats (run_id, ts_utc, fps, frames, active_tracks)"
        " VALUES (?, ?, ?, ?, ?)",
        (run_id, utcnow(), round(fps, 1), frames, active_tracks),
    )
    con.commit()
