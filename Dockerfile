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

# Run as a non-root user. UID/GID 1000 must be able to write the /data
# volume — chown the host directory accordingly (see docker-compose.yaml).
# Serial access is granted via group_add in the compose file, not here.
RUN useradd --create-home --uid 1000 bbs
USER bbs

# `restart: unless-stopped` catches a CRASHED process, not a HUNG one.
# The BBS touches /data/heartbeat every 30 s from inside its event loop;
# three missed beats mark the container unhealthy.
HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
    CMD python -c "import os,sys,time; sys.exit(0 if time.time()-os.path.getmtime('/data/heartbeat')<90 else 1)"

CMD ["python", "main.py"]
