# Deploying a trafficwatch node (Windows, native)

Windows nodes run natively (no Docker required): Python venv + CUDA torch
when an NVIDIA GPU is present. `node\run.ps1` mirrors `node/run.sh` —
collector foreground, hourly shipper background, crash restart, idle-sleep
blocking via `SetThreadExecutionState`.

## One-time setup

```powershell
# 1. clone + venv (Python 3.10+; `py -0` lists installed versions)
git clone https://github.com/silas-selfe/twatch.git
cd twatch
py -3.11 -m venv .venv

# 2. torch first, from the CUDA index matching your driver
#    (`nvidia-smi` top-right shows the max CUDA version; cu126 for 12.6).
#    Skip the index-url for CPU-only machines.
.venv\Scripts\python -m pip install torch torchvision --index-url https://download.pytorch.org/whl/cu126

# 3. the node requirements (includes pygrabber for DirectShow camera names)
.venv\Scripts\python -m pip install -r node\requirements.txt

# 4. identity + calibration: create node\site.yaml (see node\site.yaml.example)
#    site.id unique per deployment; register it in the central sites table.

# 5. central DB credential (INSERT-only node role) — skip to collect locally only
Copy-Item node\.env.example node\.env   # then edit: real TW_CENTRAL_DSN
```

## Run

```powershell
cd node
.\run.ps1 --show     # live annotated window; q or ctrl-c to stop
.\run.ps1            # headless
```

Camera selection works as on macOS/Linux: `camera.source: auto` prefers an
external USB camera (virtual cameras — OBS, NVIDIA Broadcast — are
de-prioritized), `--list-cameras` prints DirectShow names,
`--pick-camera` opens the click-to-select preview, and `rtsp://` URLs go
straight to PyAV. Windows uses the DirectShow backend so enumerated names
line up with OpenCV indices.

If PowerShell refuses to run the script:
`Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` (once).

## Auto-start on boot (optional)

Task Scheduler → Create Task → run
`powershell -NoProfile -ExecutionPolicy Bypass -File D:\path\to\twatch\node\run.ps1`
at log on, "Run whether user is logged on or not". Headless mode only
(`--show` needs a desktop session).

## Notes

- GPU: `model.device: auto` resolves to `cuda` when the CUDA wheels see the
  driver. Verify with `.venv\Scripts\python -c "import torch; print(torch.cuda.is_available(), torch.cuda.get_device_name(0))"`.
- Multi-GPU boxes: `cuda` uses device 0 (CUDA orders fastest-first by
  default). Pin with `model.device: cuda:1` if needed.
- Updates are `git pull` + restart — Watchtower/GHCR images are the
  Linux/Docker path; a native Windows node updates from the repo.
