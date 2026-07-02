# syntax=docker/dockerfile:1
FROM python:3.14-slim

# Faster, quieter Python in a container.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /data

# Install dependencies first so this layer is cached across code changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Persistent data (config.yaml + bbs.db) lives here and is mounted as a
# volume by docker-compose, so it survives container rebuilds/restarts.
VOLUME ["/data"]

# Run the BBS. main.py is expected to load /data/config.yaml (see compose).
CMD ["python", "main.py"]