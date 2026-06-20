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
        socat \
        curl \
        openssl \
        opensc \
        libengine-pkcs11-openssl \
        pkcs11-provider \
        libpcsclite1 \
        pcscd \
    && rm -rf /var/lib/apt/lists/*
# libengine-pkcs11-openssl → за `openssl cms` подпис (/kep/sign, работи).
# pkcs11-provider → за curl mutual-TLS (/nssi): curl 8 + OpenSSL 3 махнаха
# ENGINE API, така че mutual-TLS иска pkcs11 PROVIDER (openssl.cnf), не engine.

WORKDIR /app

# Install Python deps first (cache layer)
COPY pyproject.toml /app/
COPY README.md /app/
COPY odoo_erpnet_fp /app/odoo_erpnet_fp
# Optional pip extras. Empty by default → slim, pure-fiscal image.
# Build access-control images with `--build-arg EXTRAS=zen` to pull
# zen-engine (offline ZEN access decisions). Multiple comma-separated
# extras allowed, e.g. EXTRAS=zen,onvif.
ARG EXTRAS=""
RUN pip install --no-cache-dir ".$( [ -n "$EXTRAS" ] && printf '[%s]' "$EXTRAS" )"

# Default config dir — bind-mount /etc/odoo-erpnet-fp:/app/config:ro to override
RUN mkdir -p /app/config /app/certs
VOLUME ["/app/config", "/app/certs"]

EXPOSE 8001

# tini reaps zombie children + forwards signals
ENTRYPOINT ["/usr/bin/tini", "--", "odoo-erpnet-fp"]
CMD ["--config", "/app/config/config.yaml"]
