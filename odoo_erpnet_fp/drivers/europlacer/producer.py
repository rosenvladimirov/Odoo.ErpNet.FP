# -*- coding: utf-8 -*-
# Part of Odoo.ErpNet.FP. License: LGPL-3.
"""Europlacer file producer — write order XML → wait ``.ans`` → retry.

``EuroplacerFileProducer`` is the producer analogue of ``CfxAmqpIngest``:
one instance per ``EuroplacerStationSpec`` (a machine's shared folder),
owned by ``EuroplacerRegistry`` and keyed by ``spec.name``. It exposes the
same duck-typed lifecycle (``start`` / ``stop`` / ``status``) plus a public
``submit(order)`` action the ``/europlacer/{name}/order`` route calls.

Flow per order (runs on a worker thread, NOT the FastAPI event loop — the
``.ans`` wait can reach the machine's answer timeout, param 290):

    1. write the ready XML atomically into ``order_dir`` (temp-in-dir →
       fsync → os.replace, so the machine never reads a half file);
    2. poll ``answer_dir`` for ``<stem>.ans`` up to ``answer_timeout``;
    3. read ``<Error>N</Error>``:
         Error ∈ {0,5}          → success (5 = already-existing, idempotent)
         Error ∈ {4,10,12,13,14} → transient → retry (backoff, ≤ retry_limit)
         Error other            → fatal (no retry)
         no .ans by timeout     → retry (backoff, ≤ retry_limit)
    4. report the terminal outcome to Odoo — live ping via bus_inject +
       (optional) a durable signed POST to ``result_url`` — and keep it for
       ``GET /europlacer/{name}/order/{order_id}`` polling.

The retry WAIT + state machine is new work: Odoo 11 did a single immediate
``os.path.isfile`` check and only warned on mismatch
(``europlacer_trac.py:929-944``). Retrying re-writes a fresh filename each
attempt; a duplicate that the machine already applied returns Error 5
(already-existing) → success, so re-submission is safe (idempotent by the
Europlacer protocol).
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
import threading
import time
import uuid
from collections import OrderedDict, deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Optional

from .common import (
    AnswerMalformed,
    AnswerNotReady,
    EuroplacerOrder,
    EuroplacerOutcome,
    STATE_FATAL,
    STATE_SUCCESS,
    STATE_TIMEOUT,
    STATE_TRANSIENT,
    classify,
    meaning,
    parse_answer,
)

_logger = logging.getLogger(__name__)

# Колко изхода да пазим за /order/{id} poll (по машина). Ринг буфер.
_OUTCOME_CAP = 500


class EuroplacerFileProducer:
    """One Europlacer station: writes order XML, waits for the ``.ans``.

    Start/stop are idempotent. Each ``submit`` runs its write→wait→retry
    cycle on a bounded thread pool so a slow machine (or a long answer
    timeout) never blocks the FastAPI event loop and independent orders
    proceed in parallel.
    """

    def __init__(self, config, app=None):
        self.config = config          # EuroplacerStationSpec (single station)
        self.name = getattr(config, "name", "default")
        self._app = app               # FastAPI app — for BusInjectClient.from_app
        self._executor: Optional[ThreadPoolExecutor] = None
        self._started = False
        self._lock = threading.Lock()
        # Set on stop() so an in-flight .ans wait / backoff aborts promptly
        # (worker threads are non-daemon; without this a running order would
        # block process exit for up to answer_timeout × retries — bad for a
        # k8s SIGTERM grace window).
        self._stop_evt = threading.Event()

        # Telemetry
        self.orders_received = 0
        self.orders_success = 0
        self.orders_failed_transient = 0
        self.orders_failed_fatal = 0
        self.orders_timeout = 0
        self.retries_total = 0
        self.last_order_time: Optional[float] = None

        # In-flight order ids + last outcomes (за /order/{id} poll).
        self._inflight: set[str] = set()
        self._outcomes: "OrderedDict[str, dict]" = OrderedDict()
        # Ring buffer с последните изходи — за дашборда.
        self.recent = deque(maxlen=80)

    # ─── lifecycle ──────────────────────────────────────────────

    def start(self) -> bool:
        """Create the worker pool. True if newly started."""
        if not self.config or not self.config.enabled:
            return False
        if self._started:
            return False
        workers = max(1, int(getattr(self.config, "max_concurrent", 4) or 4))
        self._stop_evt.clear()
        self._executor = ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix=f"epl-{self.name}")
        self._started = True
        order_dir = getattr(self.config, "order_dir", "") or ""
        # NB: НЕ пипаме файловата система тук. os.path.isdir() на hung CIFS/NFS
        # mount би блокирал event loop-а — start() се вика синхронно от
        # lifespan-а И от /order route-а (start_one). Папката се създава лениво
        # в _write_order на worker thread-а.
        _logger.info("Europlacer producer[%s] started (order_dir=%s "
                     "answer_timeout=%ss retry_limit=%d workers=%d)",
                     self.name, order_dir,
                     getattr(self.config, "answer_timeout", "?"),
                     int(getattr(self.config, "retry_limit", 0) or 0), workers)
        return True

    def stop(self) -> bool:
        """Shut the pool down. In-flight writes already on disk are left
        for the machine; only queued (not-yet-started) orders are dropped."""
        if not self._started:
            return False
        self._stop_evt.set()          # abort any in-flight .ans wait / backoff
        ex = self._executor
        self._executor = None
        self._started = False
        if ex is not None:
            # Не чакаме (wait=False): една заявка може да е в 290s .ans
            # изчакване и би блокирала lifespan shutdown-а. Файлът вече е
            # на диска; изходът просто няма да се докладва след рестарт.
            try:
                ex.shutdown(wait=False, cancel_futures=True)
            except TypeError:
                # cancel_futures е от py3.9 — fallback за по-стари.
                ex.shutdown(wait=False)
        _logger.info("Europlacer producer[%s] stopped", self.name)
        return True

    def status(self) -> dict:
        cfg = self.config
        with self._lock:
            inflight = len(self._inflight)
            recent = list(self.recent)
        return {
            "name": self.name,
            "enabled": bool(cfg and cfg.enabled),
            "running": bool(self._started),
            "machine_kind": getattr(cfg, "machine_kind", None) if cfg else None,
            "order_dir": getattr(cfg, "order_dir", None) if cfg else None,
            "answer_dir": (getattr(cfg, "answer_dir", None) or
                           getattr(cfg, "order_dir", None)) if cfg else None,
            "inflight": inflight,
            "orders_received": self.orders_received,
            "orders_success": self.orders_success,
            "orders_failed_transient": self.orders_failed_transient,
            "orders_failed_fatal": self.orders_failed_fatal,
            "orders_timeout": self.orders_timeout,
            "retries_total": self.retries_total,
            "last_order_time": self.last_order_time,
            "recent": recent,
        }

    # ─── public action (called by the route) ────────────────────

    def submit(self, order: EuroplacerOrder) -> None:
        """Enqueue one order onto the worker pool. Non-blocking.

        Raises RuntimeError if the producer is not started (the route
        materialises + starts the instance first)."""
        ex = self._executor
        if not self._started or ex is None:
            raise RuntimeError(
                f"Europlacer producer {self.name!r} is not running")
        with self._lock:
            self.orders_received += 1
            self.last_order_time = time.time()
            self._inflight.add(order.order_id)
        ex.submit(self._process_order, order)

    def get_outcome(self, order_id: str) -> Optional[dict]:
        """The stored terminal outcome, ``{"state": "pending"}`` while
        in-flight, or None if the id is unknown."""
        with self._lock:
            if order_id in self._outcomes:
                return self._outcomes[order_id]
            if order_id in self._inflight:
                return {"order_id": order_id, "state": "pending", "ok": False}
        return None

    # ─── the write → wait-.ans → retry state machine ────────────

    def _process_order(self, order: EuroplacerOrder) -> None:
        """Runs on a worker thread. Never raises into the pool."""
        cfg = self.config
        retry_limit = max(0, int(getattr(cfg, "retry_limit", 3) or 0))
        max_attempts = 1 + retry_limit
        order_dir = getattr(cfg, "order_dir", "") or "."
        # Уникален, filesystem-safe base stem за ЦЕЛИЯ order (един за всички
        # опити): гарантира, че две различни поръчки никога не се сблъскват на
        # диска и всяка .ans е еднозначно нейна. order_id влиза само като
        # sanitized hint — никога не докосва пътя.
        base = self._order_basename(order)
        last_filename = ""
        outcome: Optional[EuroplacerOutcome] = None
        try:
            for attempt in range(1, max_attempts + 1):
                if self._stop_evt.is_set():
                    outcome = EuroplacerOutcome(
                        order_id=order.order_id, state=STATE_TRANSIENT,
                        attempts=max(0, attempt - 1),
                        filename=os.path.basename(last_filename),
                        detail="aborted: producer stopping")
                    break
                suffix = f"_r{attempt}" if attempt > 1 else ""
                filename = os.path.join(order_dir, f"{base}{suffix}.xml")
                last_filename = filename
                try:
                    self._write_order(filename, order.xml)
                except OSError as exc:
                    # Транзиентен IO на mount-а (EROFS/ENETUNREACH/ETIMEDOUT/
                    # ENOENT при mount flap): третираме като липсваща .ans —
                    # retry, НЕ фатал. Записът е идемпотентен (нов filename,
                    # atomic replace), консистентно с _wait_answer OSError
                    # толерантността.
                    _logger.warning(
                        "Europlacer[%s] order %s write failed (attempt %d): %s",
                        self.name, order.order_id, attempt, exc)
                    if attempt < max_attempts:
                        self._backoff(attempt)
                        continue
                    outcome = EuroplacerOutcome(
                        order_id=order.order_id, state=STATE_TRANSIENT,
                        attempts=attempt, filename=os.path.basename(filename),
                        detail=f"write_failed: {exc.__class__.__name__}: {exc}")
                    break
                except Exception as exc:  # noqa: BLE001
                    # Не-OSError (напр. програмна грешка) = наистина фатал.
                    _logger.exception("Europlacer[%s] order %s write crashed: %s",
                                      self.name, order.order_id, exc)
                    outcome = EuroplacerOutcome(
                        order_id=order.order_id, state=STATE_FATAL,
                        attempts=attempt, filename=os.path.basename(filename),
                        detail=f"write_crashed: {exc.__class__.__name__}: {exc}")
                    break

                status, code = self._wait_answer(filename)

                if status == "aborted":
                    outcome = EuroplacerOutcome(
                        order_id=order.order_id, state=STATE_TRANSIENT,
                        attempts=attempt, filename=os.path.basename(filename),
                        detail="aborted: producer stopping (order file written; "
                               "machine may still process it — resubmit is "
                               "idempotent via Error 5)")
                    break

                if status == "answered":
                    kind = classify(code)
                    if kind == "success":
                        outcome = self._mk_outcome(
                            order, STATE_SUCCESS, code, attempt, filename)
                        break
                    if kind == "fatal":
                        outcome = self._mk_outcome(
                            order, STATE_FATAL, code, attempt, filename)
                        break
                    # transient
                    if attempt < max_attempts:
                        self._backoff(attempt)
                        continue
                    outcome = self._mk_outcome(
                        order, STATE_TRANSIENT, code, attempt, filename)
                    break

                if status == "malformed":
                    # Добре оформен, но без валиден <Error> → фатален протокол.
                    outcome = EuroplacerOutcome(
                        order_id=order.order_id, state=STATE_FATAL,
                        attempts=attempt, filename=os.path.basename(filename),
                        detail="malformed .ans (no <Error>)")
                    break

                # status == "timeout" — нямаше .ans до answer_timeout.
                if attempt < max_attempts:
                    self._backoff(attempt)
                    continue
                outcome = EuroplacerOutcome(
                    order_id=order.order_id, state=STATE_TIMEOUT,
                    attempts=attempt, filename=os.path.basename(filename),
                    detail=f"no .ans within {self._answer_timeout()}s "
                           f"after {attempt} attempt(s)")
                break
        except Exception as exc:  # noqa: BLE001
            _logger.exception("Europlacer[%s] order %s crashed: %s",
                              self.name, order.order_id, exc)
            outcome = EuroplacerOutcome(
                order_id=order.order_id, state=STATE_FATAL,
                filename=os.path.basename(last_filename),
                detail=f"crash: {exc.__class__.__name__}: {exc}")

        if outcome is None:  # defensive — не бива да се случи
            outcome = EuroplacerOutcome(
                order_id=order.order_id, state=STATE_FATAL,
                detail="no outcome produced")
        self._finish(order, outcome)

    def _mk_outcome(self, order, state, code, attempt, filename):
        return EuroplacerOutcome(
            order_id=order.order_id, state=state, error_code=code,
            error_meaning=meaning(code), attempts=attempt,
            filename=os.path.basename(filename))

    # ─── steps ──────────────────────────────────────────────────

    def _order_basename(self, order: EuroplacerOrder) -> str:
        """Stable, filesystem-safe, collision-proof base stem for one order.

        Uniqueness mirrors Odoo 11's ``EUROPLACER_<ts>_<counter>.xml``
        (``europlacer_trac.py:852-855`` used a monotonic ir.sequence) — here a
        uuid token guarantees CROSS-order uniqueness, so two distinct orders
        (even with short/shared correlation ids submitted in the same second)
        never map to the same file and each ``.ans`` is unambiguously its own.
        The order_id is a WHITELISTED ``[A-Za-z0-9]`` human hint only: it never
        contributes path separators or ``..`` — a ref like ``WH/OUT/00012`` can
        neither escape ``order_dir`` (traversal) nor be dropped into a nested
        subtree the machine never scans. The caller appends ``_rN`` per retry.
        """
        cfg = self.config
        prefix = getattr(cfg, "filename_prefix", "") or "EUROPLACER"
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        hint = re.sub(r"[^A-Za-z0-9]", "", order.order_id or "")[:16]
        token = uuid.uuid4().hex[:12]
        return (f"{prefix}_{ts}_{hint}_{token}" if hint
                else f"{prefix}_{ts}_{token}")

    def _write_order(self, path: str, data: bytes) -> None:
        """Atomic write (temp-in-dir → fsync → os.replace) so the machine,
        which tails the folder for ``*.xml``, never reads a partial file.

        The temp uses a dot-prefix + ``.tmp`` suffix so a ``*.xml`` watcher
        won't pick it up. The order folder may not pre-exist (fresh mount)
        → create it. (Idiom from ``server/cors_writer.py:_atomic_write`` +
        ``server/service.py:_write_fragment_atomic``.)
        """
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = tempfile.NamedTemporaryFile(
            "wb", delete=False, dir=str(target.parent),
            prefix=f".{target.stem}.", suffix=".tmp")
        try:
            tmp.write(data)
            tmp.flush()
            os.fsync(tmp.fileno())
            tmp.close()
            os.replace(tmp.name, str(target))
        except Exception:
            try:
                os.unlink(tmp.name)
            except OSError:
                pass
            raise

    def _answer_path(self, order_filename: str) -> str:
        """``.ans`` за даден order файл: същият stem, разширение ``answer_ext``,
        в ``answer_dir`` (или в order папката, ако answer_dir е празен).

        Odoo 11: ``process_file.replace("xml","ans")`` в същата папка
        (``europlacer_trac.py:931``) — тук е конфигурируемо (input/output
        folders модел от документацията).
        """
        cfg = self.config
        answer_ext = getattr(cfg, "answer_ext", "") or ".ans"
        order_path = Path(order_filename)
        stem = order_path.stem
        answer_dir = (getattr(cfg, "answer_dir", "") or "").strip()
        base = Path(answer_dir) if answer_dir else order_path.parent
        return str(base / f"{stem}{answer_ext}")

    def _wait_answer(self, order_filename: str) -> tuple[str, Optional[int]]:
        """Poll for the ``.ans`` up to ``answer_timeout``.

        Returns ``(status, code)``:
          * ``("answered", N)`` — a valid ``<Error>N</Error>`` was read;
          * ``("malformed", None)`` — a well-formed ``.ans`` had no ``<Error>``;
          * ``("timeout", None)`` — no readable ``.ans`` within the window.

        A partially-written ``.ans`` (not-yet-well-formed XML) is tolerated:
        we keep re-reading until it parses or the window expires.
        """
        answer_path = self._answer_path(order_filename)
        timeout = self._answer_timeout()
        poll = max(0.05, float(getattr(self.config, "poll_interval", 0.5) or 0.5))
        deadline = time.monotonic() + timeout
        while True:
            if self._stop_evt.is_set():
                return "aborted", None
            if os.path.isfile(answer_path):
                try:
                    with open(answer_path, "rb") as f:
                        raw = f.read()
                    code = parse_answer(raw)
                    return "answered", code
                except AnswerNotReady:
                    # Машината още пише файла — изчакай следващия poll.
                    pass
                except AnswerMalformed:
                    return "malformed", None
                except OSError:
                    # Транзиентен IO на mount-а — пробвай пак.
                    pass
            if time.monotonic() >= deadline:
                return "timeout", None
            # Abort-aware sleep (не преминава deadline-а): връща True, ако
            # stop() е бил извикан по време на чакането.
            if self._stop_evt.wait(min(poll, max(0.0, deadline - time.monotonic()))):
                return "aborted", None

    def _answer_timeout(self) -> float:
        # Europlacer param 290 "Answer time out" — конфигурируем tuning knob
        # (стойността в секунди, НЕ номерът на параметъра).
        return max(1.0, float(getattr(self.config, "answer_timeout", 60.0) or 60.0))

    def _backoff(self, attempt: int) -> None:
        """Exponential backoff между опитите (capped)."""
        with self._lock:
            self.retries_total += 1
        base = max(0.0, float(getattr(self.config, "retry_backoff", 2.0) or 0.0))
        cap = max(base, float(getattr(self.config, "retry_backoff_max", 30.0) or 30.0))
        delay = min(cap, base * (2 ** (attempt - 1)))
        if delay > 0:
            # Abort-aware: stop() прекъсва backoff-а веднага (loop-ът горе
            # после произвежда "aborted" изхода).
            self._stop_evt.wait(delay)

    # ─── finish: bookkeeping + report to Odoo ───────────────────

    def _finish(self, order: EuroplacerOrder, outcome: EuroplacerOutcome) -> None:
        with self._lock:
            self._inflight.discard(order.order_id)
            if outcome.state == STATE_SUCCESS:
                self.orders_success += 1
            elif outcome.state == STATE_TRANSIENT:
                self.orders_failed_transient += 1
            elif outcome.state == STATE_TIMEOUT:
                self.orders_timeout += 1
            else:
                self.orders_failed_fatal += 1
            d = outcome.to_dict()
            self._outcomes[order.order_id] = d
            while len(self._outcomes) > _OUTCOME_CAP:
                self._outcomes.popitem(last=False)
            self.recent.append(d)

        level = logging.INFO if outcome.ok else logging.WARNING
        _logger.log(level,
                    "Europlacer[%s] order %s → %s (Error=%s %s) "
                    "attempts=%d file=%s",
                    self.name, order.order_id, outcome.state,
                    outcome.error_code, outcome.error_meaning or "-",
                    outcome.attempts, outcome.filename)

        self._archive(order, outcome)
        self._report_to_odoo(outcome)

    def _archive(self, order: EuroplacerOrder, outcome: EuroplacerOutcome) -> None:
        """Move the processed ``.xml`` (+ its ``.ans``) into ``archive_dir``
        if configured — folder hygiene so it doesn't grow unbounded. Best
        effort; a failure never affects the outcome."""
        archive_dir = (getattr(self.config, "archive_dir", "") or "").strip()
        if not archive_dir or not outcome.filename:
            return
        try:
            os.makedirs(archive_dir, exist_ok=True)
            order_dir = getattr(self.config, "order_dir", "") or "."
            xml_src = os.path.join(order_dir, outcome.filename)
            for src in (xml_src, self._answer_path(xml_src)):
                if os.path.isfile(src):
                    try:
                        os.replace(src, os.path.join(
                            archive_dir, os.path.basename(src)))
                    except OSError:
                        pass
        except Exception:  # noqa: BLE001
            _logger.debug("Europlacer[%s] archive failed", self.name,
                          exc_info=True)

    def _report_to_odoo(self, outcome: EuroplacerOutcome) -> None:
        """Deliver the terminal outcome to Odoo — best effort.

        (a) LIVE ping via bus_inject (``europlacer.order.result``) — reaches
            the operator UI instantly on channel ``erpnet_fp_proxy_events``;
        (b) DURABLE signed POST to ``result_url`` if configured — the
            authoritative callback (bus_inject is non-fatal/silent-drop, so
            it must not be the sole carrier). Odoo can also just poll
            ``GET /europlacer/{name}/order/{id}``.
        """
        app = self._app
        if app is None:
            return
        from ...clients.bus_inject import BusInjectClient
        client = BusInjectClient.from_app(app)
        try:
            data = outcome.to_dict()
            event_type = ("europlacer.order.done" if outcome.ok
                          else "europlacer.order.failed")
            if client is not None:
                try:
                    client.emit(event_type, device=self.name,
                                device_kind="machine", data=data)
                except Exception:  # noqa: BLE001
                    _logger.debug("Europlacer[%s] live emit failed",
                                  self.name, exc_info=True)
                self._post_result(client, data)
        finally:
            if client is not None:
                client.close()

    def _post_result(self, client, data: dict) -> None:
        """Signed sync POST of the outcome to ``result_url`` (if set).

        Reuses the registry HMAC secret the bus_inject client already
        resolved + the house ``X-Bus-Inject-Signature`` scheme (same as CFX
        audit ``amqp_listener._forward_audit``), so a future Odoo europlacer
        controller verifies it the identical way it verifies cfx ingest."""
        result_url = (getattr(self.config, "result_url", "") or "").strip()
        if not result_url:
            return
        secret = getattr(client, "secret", "")
        if not secret:
            return
        try:
            import httpx

            from ...server.odoo_forwarder import canonicalise, sign_body
            body = {"source": {"proxy": getattr(client, "proxy_name", ""),
                               "device": self.name, "device_kind": "machine"},
                    "type": "europlacer.order.result", "data": data}
            raw = canonicalise(body)
            sig = sign_body(raw, secret)
            with httpx.Client(timeout=15.0) as hc:
                r = hc.post(result_url, content=raw, headers={
                    "Content-Type": "application/json",
                    "X-Bus-Inject-Signature": sig,
                    "X-Bus-Inject-Proxy": getattr(client, "proxy_name", ""),
                    "User-Agent": "Odoo.ErpNet.FP/1.0 (europlacer result)",
                    "Accept": "application/json",
                })
            if r.status_code != 200:
                _logger.warning("Europlacer[%s] result POST → HTTP %d: %s",
                                self.name, r.status_code, r.text[:200])
        except Exception:  # noqa: BLE001
            _logger.debug("Europlacer[%s] result POST failed", self.name,
                          exc_info=True)
