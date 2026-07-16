-- Central store for the trafficwatch fleet.
-- Target: database "twatch", schema "monitor_traffic" (AWS RDS Postgres).
-- Apply as an admin user:  psql "$DSN" -f central_schema.sql
--
-- Design: nodes ship ONLY hourly summaries + liveness. Raw events and
-- snapshots stay on each node's local SQLite (full fidelity, audit trail).
-- All writes from nodes are idempotent upserts, so retries after network
-- loss are always safe.

CREATE SCHEMA IF NOT EXISTS monitor_traffic;
SET search_path TO monitor_traffic;

-- One row per camera deployment. Calibration lives on the node; this is
-- the registry that makes cross-site analytics interpretable.
CREATE TABLE IF NOT EXISTS sites (
    site_id       text PRIMARY KEY,          -- e.g. 'home-window', 'pi-elm-st'
    label         text NOT NULL,
    timezone      text NOT NULL DEFAULT 'America/New_York',
    lat           double precision,
    lon           double precision,
    -- what the camera-relative directions mean at THIS site
    dir_ltr_label text NOT NULL DEFAULT 'left_to_right',
    dir_rtl_label text NOT NULL DEFAULT 'right_to_left',
    notes         text,
    created_at    timestamptz NOT NULL DEFAULT now()
);

-- One row per (site, local hour, class, direction). The unit of analytics.
CREATE TABLE IF NOT EXISTS hourly_summary (
    site_id        text NOT NULL REFERENCES sites(site_id),
    hour_start     timestamptz NOT NULL,     -- start of the hour, UTC
    class          text NOT NULL,            -- car/truck/bus/motorcycle/person/bicycle
    direction      text NOT NULL,            -- camera-relative ltr/rtl
    n              integer NOT NULL,
    median_speed   real,                     -- px/s, camera-relative, uncalibrated
    conf_mean      real,
    -- coverage travels WITH the data so a count is never read without it
    uptime_pct     real NOT NULL,
    node_version   text,
    shipped_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (site_id, hour_start, class, direction)
);
CREATE INDEX IF NOT EXISTS idx_hourly_summary_hour ON hourly_summary (hour_start);

-- Liveness, one row per node ship cycle. Lets the fleet dashboard say
-- "pi-elm-st has not reported in 3 hours" without confusing that with
-- zero traffic.
CREATE TABLE IF NOT EXISTS node_heartbeat (
    site_id       text NOT NULL REFERENCES sites(site_id),
    reported_at   timestamptz NOT NULL DEFAULT now(),
    node_version  text,
    collector_fps real,
    local_events  bigint,                    -- total rows in the node's SQLite
    disk_free_gb  real,
    PRIMARY KEY (site_id, reported_at)
);

-- Convenience view: coverage-honest totals per site per local day.
CREATE OR REPLACE VIEW daily_by_site AS
SELECT
    s.site_id,
    s.label,
    (h.hour_start AT TIME ZONE s.timezone)::date AS local_date,
    h.class,
    sum(h.n)                                    AS n,
    avg(h.uptime_pct)                           AS mean_uptime_pct,
    count(DISTINCT h.hour_start)                AS hours_reported
FROM hourly_summary h
JOIN sites s USING (site_id)
GROUP BY 1, 2, 3, 4;

-- Least-privilege role for nodes. Create one login per node from this:
--   CREATE ROLE node_home_window LOGIN PASSWORD '...' IN ROLE tw_node;
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'tw_node') THEN
        CREATE ROLE tw_node NOLOGIN;
    END IF;
END $$;
GRANT USAGE ON SCHEMA monitor_traffic TO tw_node;
GRANT SELECT, INSERT, UPDATE ON hourly_summary TO tw_node;
GRANT INSERT ON node_heartbeat TO tw_node;
GRANT SELECT ON sites TO tw_node;
