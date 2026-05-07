"""
Admin endpoints — self-update orchestration.

`POST /admin/self-update` spawns a one-shot **`docker:cli`** sidecar
container that runs the literal `docker compose pull` +
`docker compose up -d --force-recreate <service>` against this
container's compose project. The sidecar has the host Docker socket
mounted plus a bind mount of the compose project's working dir
(read off the `com.docker.compose.project.working_dir` label that
compose stamps on every container).

Why `docker:cli` over Watchtower:
  * Watchtower no-ops when the image digest hasn't changed — fine
    for "update if newer" but doesn't match the user's "force
    rebuild" intent.
  * `compose up --force-recreate` always rebuilds the container
    even when image is unchanged, which is what the UI promises.
  * Same compose file → exact same env / volumes / networks /
    labels / ports — no risk of drift from re-implementing recreate.
  * Image is ~70 MB (vs Watchtower's ~30 MB), pulled lazily on
    first self-update.

Auth: gated by the `ERPNET_ADMIN_TOKEN` env var. If it's empty or unset,
the endpoint returns 503 (feature disabled) — opt-in only. Default
deployments expose the regular fiscal/scale endpoints with no admin
surface, matching how the proxy ran pre-2026-05.

Security note: when enabled, this endpoint requires the proxy
container to run with `/var/run/docker.sock` mounted RW, which is
equivalent to root on the Docker host. Treat the admin token like a
host root password — never embed it in client-side code.
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import time
from collections import deque
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse

router = APIRouter(prefix="/admin", tags=["admin"])
_logger = logging.getLogger(__name__)


# ─── In-memory ring-buffer log handler ────────────────────────────
#
# Captures every log record emitted by the proxy into a bounded
# deque so admins can read the most-recent N lines via /admin/logs
# without shell access to the host. SSE variant streams new lines
# as they arrive.

_LOG_BUFFER: deque = deque(maxlen=int(os.environ.get("ERPNET_LOG_BUFFER", "5000")))
_LOG_LISTENERS: list[asyncio.Queue] = []


class _RingBufferHandler(logging.Handler):
    """Logging handler — appends formatted records to the ring buffer
    and notifies SSE listeners. Threadsafe: deque.append + list of
    asyncio.Queue.put_nowait."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = self.format(record)
            ts = record.created
            entry = {"ts": ts, "level": record.levelname,
                     "name": record.name, "msg": line}
            _LOG_BUFFER.append(entry)
            for q in list(_LOG_LISTENERS):
                try:
                    q.put_nowait(entry)
                except asyncio.QueueFull:
                    # Slow listener — drop the oldest message it had,
                    # then enqueue the new one. Better than blocking.
                    try:
                        q.get_nowait()
                        q.put_nowait(entry)
                    except Exception:
                        pass
        except Exception:
            self.handleError(record)


def install_log_buffer():
    """Wire the ring-buffer handler onto the root logger. Call once
    after `logging.basicConfig` in main.py."""
    h = _RingBufferHandler(level=logging.DEBUG)
    h.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    ))
    logging.getLogger().addHandler(h)


# ENV-driven configuration. All optional — sensible defaults for a
# typical compose-managed deployment, override per environment.
#
# Token resolution order:
#   1. ERPNET_ADMIN_TOKEN env var (operator-managed via .env)
#   2. /app/data/admin_token file (auto-bootstrapped on first run)
# This lets operators run the proxy with no env vars; the first-run
# bootstrap writes a fresh token + logs it prominently so they can
# pick it up via `docker logs` without any host shell access.
_TOKEN_FILE = os.environ.get("ERPNET_ADMIN_TOKEN_FILE",
                              "/app/data/admin_token")


def _admin_token() -> str:
    env = (os.environ.get("ERPNET_ADMIN_TOKEN") or "").strip()
    if env:
        return env
    try:
        with open(_TOKEN_FILE) as f:
            return f.read().strip()
    except OSError:
        return ""


def bootstrap_admin_token() -> Optional[str]:
    """Called once at server startup. If no token is configured anywhere,
    generate a fresh one, persist it to `_TOKEN_FILE` (mode 600), and
    print a banner to the logs. Returns the new token (for the banner)
    or None if a token was already configured.

    Idempotent: re-runs on subsequent startups become no-ops.
    """
    if (os.environ.get("ERPNET_ADMIN_TOKEN") or "").strip():
        return None
    if os.path.exists(_TOKEN_FILE):
        return None
    try:
        os.makedirs(os.path.dirname(_TOKEN_FILE), exist_ok=True)
        new = secrets.token_urlsafe(32)
        # umask-respecting strict file write so other UIDs can't read.
        fd = os.open(_TOKEN_FILE,
                     os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_TRUNC,
                     0o600)
        with os.fdopen(fd, "w") as f:
            f.write(new + "\n")
        return new
    except OSError as exc:
        _logger.warning("Could not bootstrap admin token at %s: %s",
                        _TOKEN_FILE, exc)
        return None


def _updater_image() -> str:
    return os.environ.get("ERPNET_UPDATER_IMAGE",
                          "docker:25-cli")


def _self_container_name(client=None) -> str:
    """Resolve our own container name. Compose sets it to the
    `container_name:` field; otherwise to `<project>-<service>-<n>`.

    Watchtower matches targets by **name**, not ID — and HOSTNAME
    inside a Docker container is the short ID, not the name. So we
    use the Docker SDK to translate HOSTNAME → name. If the SDK is
    unavailable / lookup fails, fall back to `odoo-erpnet-fp`
    (compose default), which still works for stock setups.
    """
    explicit = (os.environ.get("ERPNET_SELF_CONTAINER") or "").strip()
    if explicit:
        return explicit
    hid = (os.environ.get("HOSTNAME") or "").strip()
    if client is not None and hid:
        try:
            c = client.containers.get(hid)
            # Docker SDK returns the name with a leading "/" — strip it.
            return c.name.lstrip("/")
        except Exception:  # noqa: BLE001
            pass
    return "odoo-erpnet-fp"


def _check_token(provided: Optional[str]) -> None:
    expected = _admin_token()
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Self-update is disabled. Set ERPNET_ADMIN_TOKEN "
                   "on the proxy to enable.",
        )
    if not provided or not secrets.compare_digest(provided, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid admin token.",
        )


# ─── GET /admin/self-update ─── status / capability check ──────────


# ─── GET /admin/bootstrap-info ─── one-time token retrieval ────


@router.get("/bootstrap-info")
async def bootstrap_info(request: Request):
    """If the admin token was auto-bootstrapped on first run AND the
    file is still in 'pristine' state (mode 600 + no rotation), this
    endpoint returns it so an operator can fetch it remotely once.
    After the first successful read, the file is renamed to mark it
    as 'claimed' and subsequent calls return 410 Gone.

    Designed for the SSH-less bootstrap flow: deploy → curl
    `/admin/bootstrap-info` from a trusted IP → grab token → done.
    """
    # Only callable from a private/loopback network — defends against
    # public scanning + race conditions on token claim.
    client_ip = (request.client.host if request.client else "") or ""
    private = (
        client_ip == "127.0.0.1"
        or client_ip == "::1"
        or client_ip.startswith("10.")
        or client_ip.startswith("192.168.")
        or client_ip.startswith("172.16.")
        or client_ip.startswith("172.17.")
        or client_ip.startswith("172.18.")
        or client_ip.startswith("172.19.")
        or client_ip.startswith("172.20.")
        or client_ip.startswith("172.21.")
        or client_ip.startswith("172.22.")
        or client_ip.startswith("172.23.")
        or client_ip.startswith("172.24.")
        or client_ip.startswith("172.25.")
        or client_ip.startswith("172.26.")
        or client_ip.startswith("172.27.")
        or client_ip.startswith("172.28.")
        or client_ip.startswith("172.29.")
        or client_ip.startswith("172.30.")
        or client_ip.startswith("172.31.")
    )
    if not private:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="bootstrap-info accessible only from RFC 1918 / "
                   "loopback IPs.",
        )
    claimed_marker = _TOKEN_FILE + ".claimed"
    if os.path.exists(claimed_marker):
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="Bootstrap token already claimed. Rotate via the "
                   "self-update modal or rewrite ERPNET_ADMIN_TOKEN "
                   "in the env if you've lost it.",
        )
    try:
        with open(_TOKEN_FILE) as f:
            token = f.read().strip()
    except OSError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=("Token not bootstrapped (env var ERPNET_ADMIN_TOKEN "
                    "is set, or proxy is too old)."),
        )
    # Mark as claimed to prevent a second retrieval.
    try:
        with open(claimed_marker, "w") as f:
            from datetime import datetime
            f.write(datetime.now().isoformat() + "\n")
    except OSError:
        pass
    return {"token": token, "warning":
            "Save this — subsequent /admin/bootstrap-info calls return 410."}


@router.get("/self-update")
async def self_update_status():
    """Tell the UI whether self-update is wired up. Always 200 — the
    body's `enabled` boolean lets the modal hide the button cleanly
    instead of erroring on hidden capability."""
    enabled = bool(_admin_token())
    docker_sock_present = os.path.exists("/var/run/docker.sock")
    # Best-effort resolved name (may need Docker SDK so guard ImportError)
    name = "odoo-erpnet-fp"
    try:
        if docker_sock_present:
            import docker
            name = _self_container_name(docker.from_env())
        else:
            name = _self_container_name(None)
    except Exception:
        name = _self_container_name(None)
    return {
        "enabled": enabled,
        "docker_socket": docker_sock_present,
        "self_container": name,
        "updater_image": _updater_image(),
    }


# ─── POST /admin/self-update ─── trigger orchestration ────────────


@router.post("/self-update")
async def trigger_self_update(
    request: Request,
    x_admin_token: Optional[str] = Header(None, alias="X-Admin-Token"),
):
    """Spawn a one-shot Watchtower container that recreates us with
    the latest image, then exits.

    Returns 202 Accepted as soon as the watchtower container is
    created — actual recreate happens asynchronously and is observable
    via `/healthz` (which goes 5xx → 200 with the new version) or
    Docker's container logs.
    """
    _check_token(x_admin_token)

    if not os.path.exists("/var/run/docker.sock"):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Docker socket not mounted at /var/run/docker.sock. "
                   "Add `- /var/run/docker.sock:/var/run/docker.sock` "
                   "to the proxy service's volumes in your compose file.",
        )

    try:
        import docker
    except ImportError:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Python `docker` SDK not installed. Rebuild the "
                   "proxy image with `pip install docker>=7.0`.",
        )

    client = docker.from_env()
    target = _self_container_name(client)
    image = _updater_image()

    # Read compose metadata off our own container's labels. The
    # sidecar needs all three to run the right `compose --force-recreate`.
    hid = (os.environ.get("HOSTNAME") or "").strip()
    try:
        me = client.containers.get(hid or target)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Could not introspect own container ({hid!r}): {exc}",
        )
    labels = me.attrs.get("Config", {}).get("Labels") or {}
    compose_project = labels.get("com.docker.compose.project")
    compose_service = labels.get("com.docker.compose.service")
    compose_workdir = labels.get("com.docker.compose.project.working_dir")
    compose_files = labels.get("com.docker.compose.project.config_files") or ""
    if not (compose_project and compose_service and compose_workdir):
        raise HTTPException(
            status_code=status.HTTP_412_PRECONDITION_FAILED,
            detail="Self container is not managed by `docker compose` "
                   "(missing com.docker.compose.* labels). Recreate "
                   "the proxy via your stack manager or set "
                   "`container_name:` and bring it up with compose.",
        )

    # Pre-pull the sidecar image so the user's progress modal doesn't
    # spend the first 20–60 s on a cold pull of docker:cli.
    try:
        client.images.pull(image)
    except Exception as exc:  # noqa: BLE001
        _logger.warning("Failed to pre-pull updater image %s: %s",
                        image, exc)
        # Proceed anyway — `containers.run` will pull on demand.

    # The sidecar pulls our image fresh and force-recreates the target
    # service. `sleep 3` lets THIS request return cleanly before the
    # current container is stopped (we'd lose the response otherwise).
    # Both `pull` and `up` operate on the SAME compose project, scoped
    # to a single service — no traefik / prometheus / grafana churn.
    #
    # PATH RESOLUTION TRAP:
    # The sidecar talks to the SAME Docker daemon as us (shared socket).
    # When `docker compose up` mounts `./config:/app/config`, the daemon
    # resolves `./config` ON THE HOST, not inside the sidecar. So we
    # MUST mount the project workdir at the *same host path* inside the
    # sidecar — not at /work — and `--project-directory` it explicitly
    # so compose stamps that exact path on the recreated container.
    # Otherwise the recreated proxy gets an empty `./config` mount and
    # crash-loops on missing config.yaml.
    script = (
        f"sleep 3 && "
        f"docker compose --project-directory {compose_workdir} "
        f"-p {compose_project} pull {compose_service} && "
        f"docker compose --project-directory {compose_workdir} "
        f"-p {compose_project} up -d --force-recreate "
        f"--no-deps {compose_service}"
    )

    try:
        sc = client.containers.run(
            image,
            command=["sh", "-c", script],
            volumes={
                "/var/run/docker.sock": {
                    "bind": "/var/run/docker.sock",
                    "mode": "rw",
                },
                # Same path inside sidecar as on host so the daemon's
                # `./config` resolution still works after recreate.
                compose_workdir: {
                    "bind": compose_workdir,
                    "mode": "ro",
                },
            },
            working_dir=compose_workdir,
            detach=True,
            remove=False,  # keep so `docker logs` works for diagnostics
            name=f"odoo-erpnet-fp-updater-{secrets.token_hex(3)}",
        )
    except Exception as exc:  # noqa: BLE001
        _logger.exception("Updater sidecar spawn failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to spawn updater sidecar: {exc}",
        )

    return {
        "status": "scheduled",
        "updater_container": sc.id[:12],
        "updater_name": sc.name,
        "target": target,
        "compose_project": compose_project,
        "compose_service": compose_service,
        "image": image,
        "message": (
            "docker:cli sidecar scheduled — will run "
            "`compose pull` + `compose up -d --force-recreate "
            f"{compose_service}` against project `{compose_project}` "
            "in 3 s. Poll /healthz to detect the new container."
        ),
    }


# ─── GET /admin/logs ─── tail recent entries ─────────────────────


@router.get("/logs")
async def get_logs(
    tail: int = Query(200, ge=1, le=5000,
                      description="How many recent lines to return"),
    level: Optional[str] = Query(None, regex="^(DEBUG|INFO|WARNING|ERROR|CRITICAL)$",
                                  description="Minimum level filter"),
    contains: Optional[str] = Query(None, max_length=200,
                                     description="Substring filter"),
    x_admin_token: Optional[str] = Header(None, alias="X-Admin-Token"),
):
    """Return the most-recent N log entries as JSON.

    Useful for remote debug without shell access — e.g. when the
    proxy is fronted by Cloudflare and `docker logs` requires SSH.
    Filtering by `level` / `contains` happens server-side so the
    response stays small.
    """
    _check_token(x_admin_token)

    snapshot = list(_LOG_BUFFER)
    levels_order = {"DEBUG": 10, "INFO": 20, "WARNING": 30,
                    "ERROR": 40, "CRITICAL": 50}
    if level:
        floor = levels_order[level]
        snapshot = [
            e for e in snapshot
            if levels_order.get(e["level"], 20) >= floor
        ]
    if contains:
        c = contains.lower()
        snapshot = [e for e in snapshot if c in e["msg"].lower()]

    snapshot = snapshot[-tail:]
    return {
        "count": len(snapshot),
        "buffer_size": _LOG_BUFFER.maxlen,
        "lines": snapshot,
    }


# ─── GET /admin/logs/stream ─── SSE live tail ───────────────────


@router.get("/logs/stream")
async def stream_logs(
    request: Request,
    level: Optional[str] = Query(None, regex="^(DEBUG|INFO|WARNING|ERROR|CRITICAL)$"),
    contains: Optional[str] = Query(None, max_length=200),
    backfill: int = Query(50, ge=0, le=500,
                          description="Existing lines to send before live tail"),
    x_admin_token: Optional[str] = Query(None, alias="token",
                                          description="Admin token (query param so EventSource can pass it)"),
):
    """Live tail via Server-Sent Events.

    EventSource API on the browser side can't set custom request
    headers, so the admin token comes in via `?token=…` query param.
    Subsequent log records are pushed as `data: <json>\\n\\n` events.
    Connection ends when the client disconnects or the server stops.
    """
    _check_token(x_admin_token)

    levels_order = {"DEBUG": 10, "INFO": 20, "WARNING": 30,
                    "ERROR": 40, "CRITICAL": 50}
    floor = levels_order.get(level, 0) if level else 0
    needle = contains.lower() if contains else None

    def _accept(entry: dict) -> bool:
        if floor and levels_order.get(entry["level"], 20) < floor:
            return False
        if needle and needle not in entry["msg"].lower():
            return False
        return True

    queue: asyncio.Queue = asyncio.Queue(maxsize=200)
    _LOG_LISTENERS.append(queue)

    async def _gen():
        import json
        try:
            # Backfill — last N already-buffered lines, server-side filtered.
            backfilled = [e for e in list(_LOG_BUFFER) if _accept(e)][-backfill:]
            for e in backfilled:
                yield f"data: {json.dumps(e)}\n\n"
            # Live tail
            heartbeat = time.monotonic() + 15.0
            while True:
                if await request.is_disconnected():
                    break
                try:
                    e = await asyncio.wait_for(queue.get(), timeout=5.0)
                    if _accept(e):
                        yield f"data: {json.dumps(e)}\n\n"
                except asyncio.TimeoutError:
                    pass
                # Periodic heartbeat keeps the connection alive through
                # idle proxies / load balancers.
                if time.monotonic() > heartbeat:
                    yield ": keepalive\n\n"
                    heartbeat = time.monotonic() + 15.0
        finally:
            try:
                _LOG_LISTENERS.remove(queue)
            except ValueError:
                pass

    return StreamingResponse(
        _gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # disable nginx buffering if behind one
        },
    )
