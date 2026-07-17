# trafficwatch node image: collector + shipper (multi-arch: arm64 Pi, amd64).
# Build from the repo root:  docker build -f node/Dockerfile .
# Data and per-site config live OUTSIDE the image:
#   /data    volume: sqlite db, snapshots, downloaded model weights
#   /config  bind mount: site.yaml (identity + calibration)
FROM python:3.11-slim-bookworm

# opencv runtime libs (ultralytics pulls full opencv-python)
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgl1 libglib2.0-0 && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY node/requirements.txt .
# CPU-only torch first: the default wheel bundles CUDA (5+ GB image) that no
# Pi or GPU-less node can use; CPU wheels keep the image Pi-friendly
RUN pip install --no-cache-dir torch torchvision \
        --index-url https://download.pytorch.org/whl/cpu && \
    pip install --no-cache-dir -r requirements.txt

COPY node/watch.py node/db.py node/aggregator.py node/report.py \
     node/config.yaml node/bytetrack_road.yaml ./

ARG GIT_SHA=dev
ENV TW_VERSION=${GIT_SHA} \
    TW_SITE_CONFIG=/config/site.yaml \
    TW_RUNTIME_DIR=/data \
    PYTHONUNBUFFERED=1

# workdir /data so model weights auto-download onto the persistent volume
WORKDIR /data
VOLUME ["/data"]

CMD ["python", "/app/watch.py"]
