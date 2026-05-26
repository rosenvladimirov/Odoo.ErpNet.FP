"""ZenLocalRunner + GraphStore — proxy-side ZEN evaluator + graph cache.

GraphStore persists graphs to /app/data/zen_graphs/<code>.json with
version metadata. ZenLocalRunner lazy-loads zen-engine (optional dep)
and evaluates locally. On any failure → fail-secure deny.

Heartbeat protocol extension:
- Outbound (proxy → Odoo): payload includes zen_graphs: [{code, version,
  sha256}, ...] reporting loaded versions
- Inbound (Odoo → proxy): response может да включи pending_graphs:
  [{code, version, graph_blob, hmac}, ...]; proxy applies + persists
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Optional

_logger = logging.getLogger(__name__)

try:
    import zen  # type: ignore[import-not-found]
    _ZEN_AVAILABLE = True
except ImportError:
    _ZEN_AVAILABLE = False
    _logger.info(
        "zen-engine not installed — proxy-side offline access decisions "
        "disabled. Install with: pip install zen-engine (or "
        "extras [zen] of odoo-erpnet-fp).")


class GraphStore:
    """Local cache на ZEN graphs синхронизирани от Odoo. Filesystem-
    persisted в /app/data/zen_graphs/."""

    def __init__(self, root: str = "/app/data/zen_graphs"):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, code: str) -> Path:
        # Sanitize code за filesystem (alphanumeric + _-.)
        safe = "".join(c for c in code if c.isalnum() or c in "_-.")
        return self.root / f"{safe}.json"

    def _meta_path(self, code: str) -> Path:
        safe = "".join(c for c in code if c.isalnum() or c in "_-.")
        return self.root / f"{safe}.meta.json"

    def save(self, code: str, version: int, graph: dict) -> str:
        """Persist graph + meta. Returns sha256 на canonical JSON."""
        canonical = json.dumps(graph, sort_keys=True, separators=(",", ":"))
        sha = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        self._path(code).write_text(canonical, encoding="utf-8")
        meta = {"code": code, "version": version, "sha256": sha}
        self._meta_path(code).write_text(
            json.dumps(meta, indent=2), encoding="utf-8")
        return sha

    def load(self, code: str) -> Optional[dict]:
        p = self._path(code)
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            _logger.warning("GraphStore load %s failed: %s", code, e)
            return None

    def load_meta(self, code: str) -> Optional[dict]:
        p = self._meta_path(code)
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return None

    def list_loaded(self) -> list[dict]:
        """Return [{code, version, sha256}, ...] for всички cached graphs.
        Used в heartbeat outbound."""
        result = []
        for meta_file in self.root.glob("*.meta.json"):
            try:
                meta = json.loads(meta_file.read_text(encoding="utf-8"))
                result.append(meta)
            except Exception:  # noqa: BLE001
                continue
        return result


class ZenLocalRunner:
    """Stateless ZEN evaluator. Lazy engine init. Fail-secure on any
    error (return None → caller treats as deny)."""

    _engine = None

    @classmethod
    def is_available(cls) -> bool:
        return _ZEN_AVAILABLE

    @classmethod
    def _get_engine(cls):
        if not _ZEN_AVAILABLE:
            return None
        if cls._engine is None:
            cls._engine = zen.ZenEngine()
        return cls._engine

    @classmethod
    def evaluate(cls, graph: dict, context: dict) -> Optional[dict]:
        """Returns result dict, or None on failure. Caller MUST treat
        None as fail-secure deny."""
        if not graph:
            return None
        engine = cls._get_engine()
        if engine is None:
            _logger.warning(
                "ZenLocalRunner.evaluate: zen-engine unavailable — "
                "fail-secure deny.")
            return None
        try:
            content = json.dumps(graph) if isinstance(graph, dict) else graph
            decision = engine.create_decision(content)
            result = decision.evaluate(context)
            return result.get("result", result)
        except Exception as e:  # noqa: BLE001
            _logger.warning(
                "ZenLocalRunner.evaluate failed: %s — fail-secure deny.", e)
            return None
