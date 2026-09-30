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

RUN useradd --create-home --uid 1000 wisar \
    && chown -R wisar:wisar /opt/wisar

USER wisar

EXPOSE 8000

# One sync worker. WORK_DIR is created once per process and every analysis
# writes the same filenames, so a second worker or thread overwrites a run
# that is still in flight. Timeout covers a slow 3DEP fetch plus Dijkstra.
CMD ["gunicorn", \
     "--bind", "0.0.0.0:8000", \
     "--workers", "1", \
     "--threads", "1", \
     "--timeout", "600", \
     "--graceful-timeout", "30", \
     "--access-logfile", "-", \
     "--error-logfile", "-", \
     "server:app"]
