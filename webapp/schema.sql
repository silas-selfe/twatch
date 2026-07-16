-- Web UI additions to the central store. Apply after central_schema.sql:
--   psql "$ADMIN_DSN" -f webapp/schema.sql
SET search_path TO monitor_traffic;

CREATE TABLE IF NOT EXISTS users (
    username   text PRIMARY KEY,
    pw_hash    text NOT NULL,             -- argon2id
    label      text,
    is_admin   boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now()
);

-- Read-only web role: sees analytics + manages logins, can never write
-- traffic data. Create the login with:
--   CREATE ROLE twatch_web LOGIN PASSWORD '...' IN ROLE tw_web;
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'tw_web') THEN
        CREATE ROLE tw_web NOLOGIN;
    END IF;
END $$;
GRANT USAGE ON SCHEMA monitor_traffic TO tw_web;
GRANT SELECT ON sites, hourly_summary, node_heartbeat TO tw_web;
GRANT SELECT ON users TO tw_web;
