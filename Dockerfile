# WiSAR Terrain-Aware Range Rings
#
# Web process only. Cache builders (tools/build_*.py) are on the image so they
# can be run by hand; docker compose up does not build the snapshots.
# gdal-bin and osmium-tool are for those builders. Rasterio and pyogrio
# install from wheels. Fiona does too on amd64; on arm64 it is built here
# against the image GDAL because 1.10.1 has no linux/arm64 wheel.

FROM python:3.12-slim-bookworm

# libgdal-dev and g++ are here because Fiona 1.10.1 publishes no linux/arm64
# wheel. On that architecture pip builds it against this image's GDAL.
# amd64 still installs the pinned wheel.
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        g++ \
        gdal-bin \
        libgdal-dev \
        osmium-tool \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/wisar

COPY app/requirements.txt /opt/wisar/requirements.txt
ENV GDAL_CONFIG=/usr/bin/gdal-config
RUN pip install --no-cache-dir --only-binary=:all: --no-binary=fiona -r requirements.txt

COPY app/ /opt/wisar/
# The /api/v1 contract, served at /api/v1/openapi.json and used to validate requests.
COPY docs/openapi.json /opt/wisar/api/openapi.json

RUN useradd --create-home --uid 1000 wisar \
    && mkdir -p /var/wisar/jobs \
    && chown -R wisar:wisar /opt/wisar /var/wisar

USER wisar

EXPOSE 8000

# One worker process, several threads. Analyses never run in request threads:
# api/jobs.py runs every analysis (v1 jobs and the wrapped legacy
# /api/analyze* calls) on a single background thread, one at a time, because
# the pipeline writes fixed filenames into one per-process WORK_DIR. The
# extra threads only answer status polls, downloads and legacy callers
# waiting their turn. Do not raise --workers: the queue is per process.
# Timeout covers a legacy caller waiting behind queued analyses.
CMD ["gunicorn", \
     "--bind", "0.0.0.0:8000", \
     "--workers", "1", \
     "--threads", "8", \
     "--timeout", "600", \
     "--graceful-timeout", "30", \
     "--access-logfile", "-", \
     "--error-logfile", "-", \
     "wsgi:app"]
