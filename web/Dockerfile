# twatch web UI -- reads the central store, no camera, no torch. Tiny.
# Build from the repo root:  docker build -f web/Dockerfile .
FROM python:3.11-slim-bookworm

WORKDIR /app
COPY web/requirements.txt web/requirements.txt
RUN pip install --no-cache-dir -r web/requirements.txt

COPY web/ web/

ARG GIT_SHA=dev
ENV TW_VERSION=${GIT_SHA} \
    PYTHONUNBUFFERED=1

EXPOSE 8080
CMD ["uvicorn", "web.app:app", "--host", "0.0.0.0", "--port", "8080"]
