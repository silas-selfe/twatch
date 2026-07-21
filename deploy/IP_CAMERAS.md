# Using IP / network cameras (security cameras, NVRs)

A twatch node can count from any RTSP stream, not just a USB webcam — so an
existing security camera or NVR channel becomes a traffic sensor with no new
hardware. Network cameras need no device passthrough, so this runs anywhere
Docker does (a Mac, a Linux box, the server rack), not only on a Pi.

## The important part: you choose which cameras, one at a time

`discover.py` only *lists* what it can see and saves a local preview frame per
camera so you can tell them apart. **Nothing is monitored, recorded, or
shipped unless you explicitly select a camera at the prompt** — and then only
that one. Point it at a 7-camera system and enable the single one facing the
street; the other six are never touched.

```bash
cd node
../.venv/bin/python discover.py --user admin      # scans your LAN, prompts for password
#   -> probes ONVIF + RTSP, writes previews to node/discovered/, lists candidates
#   -> LOOK at the previews, then type the numbers of the ones to enable
#   -> generates cameras/<site-id>/{site.yaml,.env} + compose.cameras.yaml
```

If your cameras sit behind an NVR, a LAN scan often finds only the NVR. Use the
channel URL from the NVR's manual directly:

```bash
../.venv/bin/python discover.py --url "rtsp://user:pass@192.168.1.50:554/Streaming/Channels/101"
```

Common stream URL shapes by vendor (main stream): Hikvision
`/Streaming/Channels/101`, Reolink `/h264Preview_01_main`, Dahua/Amcrest
`/cam/realmonitor?channel=1&subtype=0`, Tapo/generic `/stream1`.

## Credentials never touch a committed file

The stream URL (which contains the camera password) lives only in
`cameras/<id>/.env` (`chmod 600`, gitignored) as `TW_CAMERA_SOURCE`. `site.yaml`
holds just identity + calibration and is safe to share. Camera URLs are
redacted (`rtsp://***@host`) in every log the node prints.

## Per selected camera, finish the wiring

1. `cameras/<id>/.env` -> set the real `TW_CENTRAL_DSN` (the node's DB role).
2. Register the site + create its DB role (SQL in `PI_SETUP.md`).
3. Calibrate the count line against this view:
   ```bash
   TW_SITE_CONFIG=cameras/<id>/site.yaml \
   TW_CAMERA_SOURCE="rtsp://..." ../.venv/bin/python watch.py --show
   ```
   (or read a scene snapshot the node saves hourly), adjust
   `counting.line_frac`, and do the 15-minute accuracy audit (README).
4. `docker compose -f compose.cameras.yaml up -d` — one collector + shipper per
   enabled camera, plus Watchtower for auto-updates.

## Notes

- **H.264 streams** work directly. Many cameras also offer H.265/HEVC; if a
  stream won't decode, switch that camera's codec to H.264 (or use its
  substream) in the camera/NVR settings.
- The node reconnects automatically if a stream drops (camera reboot, WiFi
  blip) — it reopens after ~5 s of dead frames.
- Most cameras expose a lower-res **substream**; for a Pi, pointing at the
  substream (e.g. `subtype=1`, `_sub`) is lighter and plenty for counting.
