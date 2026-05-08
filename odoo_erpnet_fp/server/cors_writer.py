"""Fleet-driven CORS writer for Traefik dynamic config.

The fleet UI on iot.mcpworks.net carries a per-proxy list of browser
origins that may call this proxy's API. The list arrives as
`cors_origins` on every heartbeat response. This module rewrites the
mounted Traefik dynamic.yml in place so the file watcher hot-reloads
the new policy without a container restart.

Without this, every new Odoo client that wants to talk to a proxy
from browser code had to be hand-edited into dynamic.yml on the proxy
host — easy to forget on new shop deploys and silently surfaces as
"Timeout: no response from browser within 90s".
"""
from __future__ import annotations

import hashlib
import logging
import os
import tempfile
from pathlib import Path

import yaml

_logger = logging.getLogger(__name__)

# Where the proxy mounts Traefik's dynamic config (RW). Traefik mounts
# the same host file RO at /etc/traefik/dynamic.yml.
_DYN_PATH = Path(os.getenv(
    "ERPNET_FP_TRAEFIK_DYNAMIC", "/app/traefik/dynamic.yml"))

_last_hash: str = ""


def _hash_origins(origins: list[str]) -> str:
    payload = "\n".join(sorted({o for o in origins if o})).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _atomic_write(path: Path, text: str) -> None:
    """Write `text` to `path` atomically — temp file in the same dir,
    fsync, then rename. Traefik never observes a half-written file.
    """
    tmp = tempfile.NamedTemporaryFile(
        "w", delete=False, dir=path.parent, prefix=".cors-",
        suffix=".tmp", encoding="utf-8")
    try:
        tmp.write(text)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp.close()
        os.replace(tmp.name, path)
    except Exception:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
        raise


def apply_cors_origins(origins: list[str]) -> bool:
    """Apply the fleet-supplied CORS origins to dynamic.yml.

    Returns True when a rewrite happened, False when the list is
    unchanged or when the file is missing/unreadable. Empty `origins`
    is treated as "fleet has nothing to say" — the on-disk list is
    preserved (so an unconfigured fleet UI does not wipe pre-existing
    origins from a manual deploy).
    """
    global _last_hash
    if not origins:
        return False
    if not _DYN_PATH.exists():
        _logger.warning(
            "CORS writer: %s missing — bind mount the host's "
            "docker/traefik/dynamic.yml at this path to enable "
            "fleet-driven CORS sync.", _DYN_PATH)
        return False
    h = _hash_origins(origins)
    if h == _last_hash:
        return False
    try:
        with _DYN_PATH.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError) as exc:
        _logger.warning("CORS writer: cannot read %s: %s", _DYN_PATH, exc)
        return False
    middlewares = (data.setdefault("http", {})
                       .setdefault("middlewares", {}))
    cors = middlewares.setdefault("cors", {}).setdefault("headers", {})
    cors["accessControlAllowOriginList"] = sorted({o for o in origins if o})
    cors.setdefault("accessControlAllowCredentials", True)
    cors.setdefault("addVaryHeader", True)
    try:
        text = yaml.safe_dump(data, default_flow_style=False, sort_keys=False)
        _atomic_write(_DYN_PATH, text)
    except (OSError, yaml.YAMLError) as exc:
        _logger.warning("CORS writer: cannot write %s: %s", _DYN_PATH, exc)
        return False
    _last_hash = h
    _logger.info("CORS allow-list updated from fleet: %d origin(s)",
                 len(set(origins)))
    return True
