# Builder image for the Windows installer of the ErpNet.FP proxy.
#
# Mirrors the pattern used by hid2serial/packaging/windows/Dockerfile —
# Ubuntu base + NSIS + curl + Python 3.12 (for pip-download of
# Windows wheels). No Wine, no MSVC: we don't compile anything for
# Windows, we just stage Python embeddable + pre-downloaded wheels +
# our source tree and let NSIS bundle them into a setup.exe.

FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
        nsis \
        curl \
        unzip \
        ca-certificates \
        python3 \
        python3-pip \
        python3-venv \
        python3-wheel \
    && rm -rf /var/lib/apt/lists/*

# Ubuntu 24.04 ships pip 24.x which already handles the --platform /
# --python-version / --abi triplet correctly. We do not upgrade pip
# (PEP 668 system-Python protection makes that needlessly painful);
# `pip download` runs fine with the distro version.

WORKDIR /work
