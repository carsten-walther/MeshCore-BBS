# syntax=docker/dockerfile:1
FROM python:3.14-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    BBS_CONFIG=/config/config.yaml

WORKDIR /app

# Install dependencies first so this layer is cached across code changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source.
COPY app/ .

# /config — config.yaml (mount as a read-only bind mount from the host)
# /data   — bbs.db and log files (mount as a writable volume)
VOLUME ["/config", "/data"]

CMD ["python", "main.py"]
