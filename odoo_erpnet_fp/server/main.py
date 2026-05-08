"""
FastAPI application factory + CLI launcher for Odoo.ErpNet.FP.

TLS:
  Uvicorn natively accepts `ssl_certfile` and `ssl_keyfile` paths; we
  pass them through from the config. Any PEM-encoded cert / key works —
  Cloudflare Origin Certificate, Let's Encrypt, Step-CA, self-signed,
  etc. — the server doesn't care about provenance, only that the files
  are readable.

Usage:
  $ odoo-erpnet-fp --config /etc/odoo-erpnet-fp/config.yaml
  $ odoo-erpnet-fp --config /etc/odoo-erpnet-fp/configuration.json
"""

from __future__ import annotations

import argparse
import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse

from ..config.loader import AppConfig, load_config
from .service import (
    DisplayRegistry,
    PinpadRegistry,
    PrinterRegistry,
    ReaderRegistry,
    ScaleRegistry,
)

_logger = logging.getLogger(__name__)
_access_logger = logging.getLogger("odoo_erpnet_fp.access")
_STATIC_DIR = Path(__file__).resolve().parent / "static"


def _normalise_path(raw: str) -> str:
    """Collapse path parameters into bounded labels for Prometheus.

    /printers/dp150/status     → /printers/:id/status
    /readers/bt1/last          → /readers/:id/last
    /scales/cas1/weight        → /scales/:id/weight

    This keeps the label cardinality of `http_requests_total` finite —
    metric explosion is the most common Prometheus mistake.
    """
    parts = raw.split("/")
    normalised: list[str] = []
    # Known top-level resource collections that take an :id segment
    id_after = {"printers", "readers", "scales", "displays", "pinpads",
                "iot_drivers", "hw_drivers"}
    skip_next = False
    for i, part in enumerate(parts):
        if skip_next:
            normalised.append(":id")
            skip_next = False
            continue
        normalised.append(part)
        if part in id_after and i + 1 < len(parts) and parts[i + 1]:
            skip_next = True
    return "/".join(normalised)


def _read_version() -> str:
    """Resolve installed package version, with a fallback for editable
    in-tree development."""
    try:
        from importlib.metadata import version
        return version("odoo-erpnet-fp")
    except Exception:
        return "dev"


def create_app(config: AppConfig, config_path: Path | None = None) -> FastAPI:
    """Build a FastAPI app bound to the given config.

    Importing `routes.printers` is deferred until here so that simply
    importing this module (e.g. by the CLI dispatcher) doesn't pull in
    the full route tree before the registry is configured.

    `config_path` is the on-disk YAML path; the fleet registry client
    needs it to write back the long-lived secret after pairing. If
    omitted, pairing still works in-memory but the secret is lost on
    restart.
    """
    import asyncio  # noqa: F401  — needed for fleet task management below
    @asynccontextmanager
    async def _lifespan(app: FastAPI):
        # Bind reader threads to the live event loop so push events reach
        # WebSocket / SSE / webhook subscribers. Failures are logged
        # per-reader; one bad device doesn't block server startup.
        await reader_registry.start_all()
        await display_registry.start_all()
        # Fleet registry — pair once if needed, then heartbeat in
        # background. No-op when server.registry.enabled is false.
        from .registry import fleet_loop
        fleet_task: asyncio.Task | None = None
        if app.state.config.server.registry.enabled:
            fleet_task = asyncio.create_task(
                fleet_loop(app, config_path or Path("config.yaml")),
                name="fleet-heartbeat",
            )
        try:
            yield
        finally:
            if fleet_task is not None:
                fleet_task.cancel()
                try:
                    await fleet_task
                except (asyncio.CancelledError, Exception):
                    pass
            await reader_registry.stop_all()
            await display_registry.stop_all()

    app = FastAPI(
        title="Odoo.ErpNet.FP",
        version="0.1.0",
        description=(
            "Python drop-in replacement for ErpNet.FP HTTP fiscal-printer "
            "server (BG market)."
        ),
        docs_url="/docs",
        redoc_url="/redoc",
        lifespan=_lifespan,
    )

    registry = PrinterRegistry.from_config(config)
    pinpad_registry = PinpadRegistry.from_config(config)
    scale_registry = ScaleRegistry.from_config(config)
    reader_registry = ReaderRegistry.from_config(config)
    display_registry = DisplayRegistry.from_config(config)
    app.state.registry = registry
    app.state.pinpad_registry = pinpad_registry
    app.state.scale_registry = scale_registry
    app.state.reader_registry = reader_registry
    app.state.display_registry = display_registry
    app.state.config = config

    from . import metrics
    metrics.set_proxy_info(_read_version())

    @app.middleware("http")
    async def log_and_meter_requests(request: Request, call_next):
        import time
        start = time.monotonic()
        client = request.client.host if request.client else "?"
        try:
            response = await call_next(request)
        except Exception:
            _access_logger.exception(
                "%s %s %s — exception",
                client,
                request.method,
                request.url.path,
            )
            raise
        elapsed_s = time.monotonic() - start
        elapsed_ms = int(elapsed_s * 1000)
        _access_logger.info(
            "%s %s %s → %s (%dms)",
            client,
            request.method,
            request.url.path,
            response.status_code,
            elapsed_ms,
        )
        # Record metrics. Use `request.url.path` directly — it has
        # placeholders like /printers/<id>/status, which would
        # explode label cardinality. Replace numeric / hex segments
        # with `:id` so we get a bounded set of paths.
        try:
            metric_path = _normalise_path(request.url.path)
            metrics.http_requests_total.labels(
                method=request.method,
                path=metric_path,
                status_code=str(response.status_code),
            ).inc()
            metrics.http_request_duration_seconds.labels(
                method=request.method, path=metric_path,
            ).observe(elapsed_s)
        except Exception:
            pass
        return response

    from .routes.admin import router as admin_router
    from .routes.displays import router as displays_router
    from .routes.iot_compat import (
        legacy_router as iot_compat_legacy_router,
        v18_router as iot_compat_v18_router,
        v19_router as iot_compat_v19_router,
    )
    from .routes.pinpads import router as pinpads_router
    from .routes.printers import router as printers_router
    from .routes.readers import router as readers_router
    from .routes.scales import router as scales_router
    app.include_router(printers_router)
    app.include_router(pinpads_router)
    app.include_router(scales_router)
    app.include_router(readers_router)
    app.include_router(displays_router)
    app.include_router(admin_router)
    # Native Odoo IoT Box compatibility — same handlers, two prefixes
    # so a single ErpNet.FP instance answers both Odoo 18 (/hw_drivers)
    # and Odoo 19+ (/iot_drivers) clients.
    app.include_router(iot_compat_v18_router)
    app.include_router(iot_compat_v19_router)
    app.include_router(iot_compat_legacy_router)

    @app.get("/metrics", include_in_schema=False)
    def prometheus_metrics():
        """Prometheus text-exposition endpoint.

        Returns 501 if `prometheus_client` is not installed (optional
        dep). Scrape with the standard Prometheus / Grafana stack:

            scrape_configs:
              - job_name: erpnet-fp
                static_configs:
                  - targets: ['erpnet.example.com']

        Reader subscriber gauge is updated lazily here on each scrape,
        so the value is always fresh without a polling loop.
        """
        if not metrics.is_available():
            from fastapi.responses import PlainTextResponse
            return PlainTextResponse(
                "prometheus_client not installed; metrics disabled",
                status_code=501,
            )
        # Lazy gauges — refresh per-reader subscriber counts on scrape
        for rid, entry in reader_registry.readers.items():
            try:
                metrics.reader_subscribers.labels(reader_id=rid).set(
                    entry.bus.subscriber_count
                )
            except Exception:
                pass
        from fastapi.responses import Response
        body, ctype = metrics.render()
        return Response(content=body, media_type=ctype)

    @app.get("/healthz")
    def healthz():
        return {
            "ok": True,
            "version": _read_version(),
            "printers": list(registry.printers.keys()),
            "pinpads": list(pinpad_registry.pinpads.keys()),
            "scales": list(scale_registry.scales.keys()),
            "readers": list(reader_registry.readers.keys()),
            "displays": list(display_registry.displays.keys()),
        }

    # Single-page dashboard at root. The HTML uses fetch() against the
    # same-origin /printers/* endpoints, so it works behind any proxy
    # (Cloudflare, Traefik, Nginx) without configuration.
    @app.get("/", include_in_schema=False)
    def dashboard():
        return FileResponse(_STATIC_DIR / "index.html")

    return app


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="odoo-erpnet-fp",
        description=(
            "ErpNet.FP-compatible HTTP fiscal printer server (Python)."
        ),
    )
    p.add_argument(
        "-c",
        "--config",
        type=Path,
        required=True,
        help="Path to config.yaml or configuration.json",
    )
    p.add_argument(
        "--host",
        default=None,
        help="Override the host from config (default: from config or 0.0.0.0)",
    )
    p.add_argument(
        "--port",
        type=int,
        default=None,
        help="Override the port from config (default: from config or 8001)",
    )
    return p


def cli(argv: list[str] | None = None) -> int:
    """Console entry point — registered in pyproject.toml [project.scripts]."""
    args = _build_arg_parser().parse_args(argv)

    try:
        config = load_config(args.config)
    except (FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.host:
        config.server.host = args.host
    if args.port:
        config.server.port = args.port

    logging.basicConfig(
        level=getattr(logging, config.server.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # Wire the in-memory ring-buffer handler — feeds /admin/logs +
    # /admin/logs/stream so admins can read recent logs without shell
    # access to the host (e.g. for CF-fronted instances).
    try:
        from .routes.admin import install_log_buffer, bootstrap_admin_token
        install_log_buffer()
        # Auto-bootstrap an admin token on first run if no env var was
        # configured. Prints to logs so the operator can pick it up via
        # `docker logs odoo-erpnet-fp | grep ADMIN_TOKEN_BOOTSTRAP`
        # OR via GET /admin/bootstrap-info from a private-IP client.
        new_token = bootstrap_admin_token()
        if new_token:
            banner = "═" * 70
            logging.warning(
                "\n%s\n"
                "ADMIN_TOKEN_BOOTSTRAP=%s\n"
                "Saved to /app/data/admin_token (mode 600). Use this token "
                "to authenticate /admin/* endpoints (self-update, logs, "
                "vat-rates). Fetch once via GET /admin/bootstrap-info from "
                "any RFC1918 / loopback IP, OR scrape this log banner. "
                "After first claim, /admin/bootstrap-info returns 410.\n"
                "%s",
                banner, new_token, banner,
            )
    except Exception:
        # /admin/logs is optional — proxy must boot even if it fails.
        logging.exception("install_log_buffer / bootstrap_admin_token failed")

    # Lazy import so `odoo-erpnet-fp --help` works without uvicorn installed
    import uvicorn

    app = create_app(config, config_path=args.config)

    uvicorn_kwargs: dict = {
        "host": config.server.host,
        "port": config.server.port,
        "log_level": config.server.log_level,
    }
    if config.server.tls.enabled:
        config.server.tls.validate()
        uvicorn_kwargs.update(
            ssl_certfile=config.server.tls.certfile,
            ssl_keyfile=config.server.tls.keyfile,
            ssl_keyfile_password=config.server.tls.keyfile_password,
            ssl_ca_certs=config.server.tls.ca_certs,
            ssl_cert_reqs=2 if config.server.tls.require_client_cert else 0,
        )
        scheme = "https"
    else:
        scheme = "http"

    printer_list = ", ".join(sorted(app.state.registry.printers.keys())) or "none"
    banner = [
        "=" * 70,
        "  Odoo.ErpNet.FP — starting up",
        "=" * 70,
        f"  Bind:        {scheme}://{config.server.host}:{config.server.port}",
        f"  TLS:         {'enabled' if config.server.tls.enabled else 'disabled'}",
        f"  Printers:    {printer_list}",
        f"  Dashboard:   {scheme}://{config.server.host}:{config.server.port}/",
        f"  API docs:    {scheme}://{config.server.host}:{config.server.port}/docs",
        f"  Healthz:     {scheme}://{config.server.host}:{config.server.port}/healthz",
        "=" * 70,
    ]
    for line in banner:
        _logger.info(line)
    uvicorn.run(app, **uvicorn_kwargs)
    return 0


if __name__ == "__main__":
    raise SystemExit(cli())
