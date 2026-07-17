# Deploying a trafficwatch camera node (Raspberry Pi)

Target: Pi 5 (Pi 4 works at lower fps), Raspberry Pi OS Lite 64-bit, any UVC
webcam. One node = three containers (collector, hourly shipper, watchtower
auto-updater) sharing a data volume.

## One-time, per Pi

```bash
# 1. flash Raspberry Pi OS Lite 64-bit; enable ssh; then on the Pi:
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER   # log out/in after this

# 2. node directory
mkdir ~/twatch && cd ~/twatch
curl -fsSLO https://raw.githubusercontent.com/silas-selfe/twatch/main/deploy/compose.yaml
curl -fsSL https://raw.githubusercontent.com/silas-selfe/twatch/main/node/site.yaml.example -o site.yaml

# 3. identity + calibration: edit site.yaml
#    - site.id: unique (e.g. pi-elm-st) -- register the same id in the
#      central sites table
#    - camera.source: usually 0 (/dev/video0); check with `ls /dev/video*`
#    - leave model yolo11n / imgsz 640 / tracker.track_buffer 15 for Pi CPU

# 4. central DB credential (INSERT-only node role):
echo 'TW_CENTRAL_DSN=postgresql://node_pi_elm_st:PASSWORD@your-rds-host:5432/twatch?sslmode=require' > .env
chmod 600 .env

# 5. go
docker compose up -d
docker compose logs -f collector   # watch it come up; ctrl-c to detach
```

## Calibrate the count line

The collector saves a scene snapshot to the data volume every hour
(`/data/snapshots/scene/`). Pull one and adjust `counting.line_frac` in
site.yaml so the line sits where traffic is unobstructed, then
`docker compose restart collector`. Do the 15-minute manual-count audit
(README) before trusting a new site's numbers.

## Registering the site centrally (once, as admin)

```sql
INSERT INTO monitor_traffic.sites (site_id, label, timezone, dir_ltr_label, dir_rtl_label)
VALUES ('pi-elm-st', 'Elm St facing north', 'America/New_York', 'northbound', 'southbound');
CREATE ROLE node_pi_elm_st LOGIN PASSWORD '...' IN ROLE tw_node;
```

## How updates work

Push to `main` -> GitHub Actions builds `ghcr.io/silas-selfe/twatch:latest`
(arm64 + amd64) -> Watchtower on every node pulls it within 30 minutes and
restarts the containers. site.yaml, the database, snapshots, and model
weights all live outside the image, so updates never touch them. Roll back
by pinning a previous SHA tag in compose.yaml.

## Health

- `docker compose ps` -- all three services Up
- Central: `SELECT * FROM monitor_traffic.node_heartbeat ORDER BY reported_at DESC LIMIT 5;`
- An hour missing entirely from hourly_summary = node was down (vs. rows
  with n=0 / `__coverage__` = healthy but quiet road).

## Pi performance notes

- yolo11n @ imgsz 640 on Pi 5 CPU is roughly 4-8 fps -- sufficient (a
  vehicle crossing takes ~3 s, so ByteTrack still gets 15+ looks at it).
  Keep `tracker.track_buffer` ~= 3 seconds x real fps.
- If a site needs more, the Hailo-8L AI kit or NCNN-exported weights
  (`yolo export model=yolo11n.pt format=ncnn`) both help materially.
