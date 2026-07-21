"""Find IP cameras on your network and OPT selected ones into twatch.

Privacy model, stated plainly: this tool only LISTS what it can see and
saves one local preview frame per camera so you can tell them apart.
Nothing is monitored, recorded, or shipped anywhere unless you explicitly
select a camera at the prompt -- and then only that camera, counting
objects on that view. Preview images live in ./discovered/ on this
machine; delete the folder whenever you like.

Typical flows:

  # scan the local network (ONVIF multicast + RTSP port probe), preview,
  # pick cameras, generate per-camera configs:
  python discover.py --user admin

  # you already know the stream URL (e.g. your NVR's channel URL):
  python discover.py --url "rtsp://user:pass@192.168.1.50:554/h264Preview_01_main"

Selected cameras get a directory each under ./cameras/<site-id>/ with
site.yaml (identity + calibration, safe to share) and .env (stream URL
with credentials + central DSN, chmod 600, never commit), plus a
generated compose.cameras.yaml that runs one collector+shipper pair per
selected camera.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import getpass
import os
import re
import socket
import stat
import sys
import threading
import uuid
from pathlib import Path

import cv2

HERE = Path(__file__).resolve().parent

# common RTSP path templates by vendor; first match wins per host
RTSP_PATHS = [
    "",                                     # mediamtx / generic
    "/stream1",                             # TP-Link Tapo, generic
    "/Streaming/Channels/101",              # Hikvision main stream
    "/h264Preview_01_main",                 # Reolink
    "/cam/realmonitor?channel=1&subtype=0", # Dahua / Amcrest
    "/live",
    "/videoMain",                           # Foscam
    "/media/video1",
    "/ch01/0",
]

WS_DISCOVERY_PROBE = f"""<?xml version="1.0" encoding="UTF-8"?>
<e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope"
    xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing"
    xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery"
    xmlns:dn="http://www.onvif.org/ver10/network/wsdl">
  <e:Header>
    <w:MessageID>uuid:{uuid.uuid4()}</w:MessageID>
    <w:To e:mustUnderstand="true">urn:schemas-xmlsoap-org:ws:2005:04:discovery</w:To>
    <w:Action e:mustUnderstand="true">http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</w:Action>
  </e:Header>
  <e:Body>
    <d:Probe><d:Types>dn:NetworkVideoTransmitter</d:Types></d:Probe>
  </e:Body>
</e:Envelope>"""


def local_subnet() -> list[str]:
    """Best-effort /24 around this machine's primary interface."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
    finally:
        s.close()
    base = ip.rsplit(".", 1)[0]
    return [f"{base}.{i}" for i in range(1, 255)]


def onvif_discover(timeout: float = 3.0) -> set[str]:
    """WS-Discovery multicast probe; returns responding camera IPs."""
    found = set()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.settimeout(timeout)
    try:
        sock.sendto(WS_DISCOVERY_PROBE.encode(), ("239.255.255.250", 3702))
        while True:
            try:
                _, addr = sock.recvfrom(65535)
                found.add(addr[0])
            except socket.timeout:
                break
    except OSError:
        pass
    finally:
        sock.close()
    return found


def port_open(ip: str, port: int = 554, timeout: float = 0.4) -> bool:
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except OSError:
        return False


def scan_rtsp_hosts() -> set[str]:
    hosts = local_subnet()
    found = set()
    with concurrent.futures.ThreadPoolExecutor(max_workers=64) as ex:
        for ip, ok in zip(hosts, ex.map(port_open, hosts)):
            if ok:
                found.add(ip)
    return found


def try_stream(url: str, timeout: float = 8.0):
    """Open url via PyAV, grab one frame. Returns (frame_bgr, w, h) or None.
    (opencv-python cannot open RTSP; PyAV's bundled FFmpeg can.)"""
    import av
    try:
        c = av.open(url, options={"rtsp_transport": "tcp"},
                    timeout=(timeout, timeout))
    except Exception:
        return None
    try:
        for frame in c.decode(video=0):
            img = frame.to_ndarray(format="bgr24")
            return img, img.shape[1], img.shape[0]
    except Exception:
        return None
    finally:
        c.close()
    return None


def redact(url: str) -> str:
    return re.sub(r"//[^/@]+@", "//***@", url)


def probe_host(ip: str, user: str | None, pw: str | None):
    cred = f"{user}:{pw}@" if user else ""
    for path in RTSP_PATHS:
        url = f"rtsp://{cred}{ip}:554{path}"
        hit = try_stream(url, timeout=6)
        if hit:
            return url, hit
    return None


def save_preview(name: str, frame) -> Path:
    d = HERE / "discovered"
    d.mkdir(exist_ok=True)
    p = d / f"{name}.jpg"
    cv2.imwrite(str(p), frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
    return p


def emit_camera(site_id: str, url: str, label: str):
    d = HERE / "cameras" / site_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "site.yaml").write_text(f"""site:
  id: {site_id}
  label: "{label}"

model:
  weights: yolo11n.pt
  imgsz: 640

counting:
  line_frac: 0.50          # calibrate with a scene snapshot or --show
  direction_labels:
    left_to_right: left_to_right
    right_to_left: right_to_left

output:
  db: /data/traffic.db
  snapshots_dir: /data/snapshots
""")
    env = d / ".env"
    env.write_text(
        f"TW_CAMERA_SOURCE={url}\n"
        "TW_CENTRAL_DSN=postgresql://node_" + site_id.replace("-", "_")
        + ":PASSWORD@YOUR-RDS-ENDPOINT:5432/twatch?sslmode=require\n")
    env.chmod(stat.S_IRUSR | stat.S_IWUSR)
    return d


def emit_compose(site_ids: list[str]):
    blocks = []
    for sid in site_ids:
        for svc, cmd in (("collector", ""), ("shipper", '\n    command: ["python", "/app/aggregator.py", "--loop"]')):
            blocks.append(f"""  {sid}-{svc}:
    image: ghcr.io/silas-selfe/twatch:latest
    restart: unless-stopped{cmd}
    env_file: cameras/{sid}/.env
    volumes:
      - ./cameras/{sid}/site.yaml:/config/site.yaml:ro
      - {sid}-data:/data""")
    vols = "\n".join(f"  {sid}-data:" for sid in site_ids)
    out = HERE / "compose.cameras.yaml"
    out.write_text(
        "# Generated by discover.py -- one collector+shipper per OPTED-IN camera.\n"
        "# Network cameras need no device passthrough; this runs anywhere Docker does.\n"
        "services:\n" + "\n".join(blocks) + f"""
  watchtower:
    image: containrrr/watchtower
    restart: unless-stopped
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
    command: --interval 1800 --cleanup

volumes:
{vols}
""")
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", help="skip scanning; test this exact stream URL")
    ap.add_argument("--user", help="camera/NVR username for RTSP auth")
    ap.add_argument("--password", help="password (omit to be prompted securely)")
    args = ap.parse_args()

    user, pw = args.user, args.password
    if user and not pw:
        pw = getpass.getpass(f"RTSP password for {user!r}: ")

    print(__doc__.split("Typical flows")[0])

    candidates = []  # (name, url, w, h)
    if args.url:
        hit = try_stream(args.url, timeout=10)
        if not hit:
            sys.exit(f"could not read a frame from {redact(args.url)} -- check "
                     "URL/credentials (and that the stream is H.264, not H.265)")
        _, w, h = hit
        name = re.sub(r"[^a-z0-9]+", "-", redact(args.url).split("//")[-1].lower())[:40]
        save_preview(name, hit[0])
        candidates.append((name, args.url, w, h))
    else:
        print("scanning: ONVIF multicast + RTSP port probe on your /24 ...")
        hosts = sorted(onvif_discover() | scan_rtsp_hosts())
        if not hosts:
            sys.exit("no RTSP/ONVIF hosts found. If your cameras sit behind an "
                     "NVR, scan may only find the NVR itself -- use --url with "
                     "a channel URL from the NVR's manual instead.")
        print(f"found {len(hosts)} candidate host(s): {', '.join(hosts)}")
        if not user:
            print("note: no --user given; only unauthenticated streams will preview")
        for ip in hosts:
            print(f"  probing {ip} ...", end="", flush=True)
            hit = probe_host(ip, user, pw)
            if hit:
                url, (frame, w, h) = hit
                save_preview(ip.replace(".", "-"), frame)
                candidates.append((ip.replace(".", "-"), url, w, h))
                print(f" OK  {w}x{h}  {redact(url)}")
            else:
                print(" no readable stream")

    if not candidates:
        sys.exit("no readable streams found")

    print("\nPreview frames saved to ./discovered/ -- LOOK AT THEM before "
          "enabling anything.\nOnly cameras you select below will ever be used;"
          " the rest are never touched.\n")
    for i, (name, url, w, h) in enumerate(candidates):
        print(f"  [{i}] {redact(url)}  ({w}x{h})  preview: discovered/{name}.jpg")

    picks = input("\nenable which? (comma-separated numbers, empty = none): ").strip()
    if not picks:
        print("nothing enabled. previews remain in ./discovered/ -- delete at will.")
        return
    site_ids = []
    for idx in [int(x) for x in picks.split(",")]:
        name, url, w, h = candidates[idx]
        sid = input(f"site id for [{idx}] {redact(url)} (e.g. driveway-east): ").strip()
        label = input(f"human label (e.g. 'Driveway facing east'): ").strip() or sid
        d = emit_camera(sid, url, label)
        site_ids.append(sid)
        print(f"  wrote {d}/site.yaml and {d}/.env (edit the DSN in .env!)")
    compose = emit_compose(site_ids)
    print(f"""
wrote {compose}

next steps per camera:
  1. edit cameras/<id>/.env -> real TW_CENTRAL_DSN (node role)
  2. register the site + create its DB role (deploy/PI_SETUP.md has the SQL)
  3. calibrate: TW_SITE_CONFIG=cameras/<id>/site.yaml TW_CAMERA_SOURCE=<url> \\
       python watch.py --show   (adjust counting.line_frac)
  4. docker compose -f compose.cameras.yaml up -d
""")


if __name__ == "__main__":
    main()
