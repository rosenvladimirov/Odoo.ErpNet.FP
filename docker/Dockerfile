FROM python:3.12-slim

# Native deps:
#   pyserial            — none
#   uvicorn[standard]   — pulls httptools/uvloop wheels
#   evdev (HID readers) — needs libudev1 at runtime + headers to compile
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        tini \
        libudev1 \
        gcc \
        libudev-dev \
        python3-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first (cache layer)
COPY pyproject.toml /app/
COPY README.md /app/
COPY odoo_erpnet_fp /app/odoo_erpnet_fp
RUN pip install --no-cache-dir .

# Default config dir — bind-mount /etc/odoo-erpnet-fp:/app/config:ro to override
RUN mkdir -p /app/config /app/certs
VOLUME ["/app/config", "/app/certs"]

EXPOSE 8001

# tini reaps zombie children + forwards signals
ENTRYPOINT ["/usr/bin/tini", "--", "odoo-erpnet-fp"]
CMD ["--config", "/app/config/config.yaml"]
