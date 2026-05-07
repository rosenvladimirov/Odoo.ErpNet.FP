"""
Admin endpoints — self-update orchestration.

`POST /admin/self-update` triggers an out-of-band container recreate
by spawning a one-shot **Watchtower** container with the local Docker
socket mounted. Watchtower handles all the recreate semantics that
`docker compose` would: pulls the configured image, stops + removes
the targeted container, and creates a fresh one with the same env /
volumes / labels / networks. Once the recreate completes, the new
container has the new image.

Why Watchtower (containrrr/watchtower):
  * Battle-tested orchestrator — reproduces compose recreate semantics
    correctly (preserves networks aliased by name, copies labels, etc.)
  * One-shot mode `--run-once` — exits as soon as the update is done,
    leaves no daemon behind
  * Tiny image (~30 MB), pulled lazily on first self-update

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

import logging
import os
import secrets
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Request, status

router = APIRouter(prefix="/admin", tags=["admin"])
_logger = logging.getLogger(__name__)


# ENV-driven configuration. All optional — sensible defaults for a
# typical compose-managed deployment, override per environment.
def _admin_token() -> str:
    return (os.environ.get("ERPNET_ADMIN_TOKEN") or "").strip()


def _watchtower_image() -> str:
    return os.environ.get("ERPNET_WATCHTOWER_IMAGE",
                          "containrrr/watchtower:latest")


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
        "watchtower_image": _watchtower_image(),
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
    image = _watchtower_image()

    # Watchtower one-shot: pulls latest of our image, stops + removes
    # the target, recreates it with the same compose-injected config,
    # then exits. `--cleanup` removes the OLD image so disk doesn't
    # accumulate stale layers.
    try:
        client.images.pull(image)
    except Exception as exc:  # noqa: BLE001
        _logger.warning("Failed to pre-pull watchtower image %s: %s",
                        image, exc)
        # Proceed anyway — `containers.run` will pull on demand.

    try:
        wt = client.containers.run(
            image,
            command=[
                "--run-once",
                "--cleanup",
                "--include-restarting",
                target,
            ],
            volumes={
                "/var/run/docker.sock": {
                    "bind": "/var/run/docker.sock",
                    "mode": "rw",
                },
            },
            detach=True,
            remove=False,  # keep around until manual cleanup so logs are inspectable
            name=f"odoo-erpnet-fp-updater-{secrets.token_hex(3)}",
        )
    except Exception as exc:  # noqa: BLE001
        _logger.exception("Watchtower spawn failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to spawn watchtower: {exc}",
        )

    return {
        "status": "scheduled",
        "watchtower_container": wt.id[:12],
        "watchtower_name": wt.name,
        "target": target,
        "image": image,
        "message": "Watchtower one-shot scheduled. Poll /healthz to "
                   "detect the new version coming online.",
    }
