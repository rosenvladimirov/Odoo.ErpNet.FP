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
