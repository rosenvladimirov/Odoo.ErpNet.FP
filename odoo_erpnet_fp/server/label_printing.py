# -*- coding: utf-8 -*-
# Part of Odoo.ErpNet.FP. License: LGPL-3.
"""Печат на етикети през glabels-qt към принтерите на ОС.

Договорът (Станцията ↔ проксито, Росен 24.10.2026):

* списъкът — принтерите, които ОС вижда (CUPS `lpstat` на Linux/macOS;
  спулерът на Windows — `win32print`, ако е наличен, иначе PowerShell),
  подразбиращият се и версията на glabels-qt;
* печатът — шаблонът `.glabels` + записите → CSV (UTF-8, първи ред —
  полетата) → `glabels-batch-qt` в пакетен режим към PDF → PDF-ът към
  принтера през ОС.

🔑 Сливането идва от ШАБЛОНА, не от командния ред: `-i` подменя само
източника, видът (`<Merge type=…>`) е в XML-а. Шаблон без `<Merge>`
се печата като прост етикет, а CSV-то се ПРЕНЕБРЕГВА мълчаливо (проба
24.10.2026: 2 записа → 1 празен етикет). Затова проксито пренаписва
`<Merge>` на `Text/Comma/Line1Keys` — точно формата на CSV-то, което
пише само.

🔑 glabels-batch-qt връща 0 и когато не е отпечатал нищо (липсващ или
нечетим шаблон — само ред в stderr). Успехът се мери по PDF-а, не по
кода на изхода.
"""

from __future__ import annotations

import base64
import binascii
import glob
import gzip
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import zlib
from pathlib import Path
from typing import Any, Optional

_logger = logging.getLogger(__name__)

MERGE_TYPE = "Text/Comma/Line1Keys"
CSV_NAME = "records.csv"
TEMPLATE_NAME = "template.glabels"
PDF_NAME = "labels.pdf"

_MERGE_RE = re.compile(r"<Merge\b[^>]*?(?:/>|>.*?</Merge\s*>)", re.S)
_DOC_END_RE = re.compile(r"</Glabels-document\s*>")
# „Printing 2 items on 2 pages.“ / „Printing 1 item on 1 page.“
_PAGES_RE = re.compile(r"\bon\s+(\d+)\s+pages?\b")
_PDF_PAGE_RE = re.compile(rb"/Type\s*/Page(?![A-Za-z])")
_LP_JOB_RE = re.compile(r"request id is\s+(\S+)")
_LPSTAT_DEFAULT_RE = re.compile(r"system default destination:\s*(\S+)")


class LabelRequestError(Exception):
    """Невалидна заявка — маршрутът я връща като 4xx."""

    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def _is_windows() -> bool:
    return sys.platform.startswith("win")


def _c_env(**extra) -> dict:
    # CUPS превежда изхода си по LANG — „system default destination“ би
    # излязло на български и разборът би пропуснал подразбиращия се.
    env = dict(os.environ)
    env["LC_ALL"] = "C"
    env["LANG"] = "C"
    env.update(extra)
    return env


def _tail(text: Any, limit: int = 400) -> str:
    if isinstance(text, bytes):
        text = text.decode("utf-8", "replace")
    text = (text or "").strip()
    return text[-limit:]


def _csv_cell(value: Any) -> str:
    """Една клетка за CSV парсъра на glabels (`backends/merge/Text.cpp`).

    Парсърът НЕ е RFC 4180: обратната наклонена черта е escape (`\\n` е
    нов ред, `\\t` — табулация, `\\x` — буквално x), а `\\r` след
    затваряща кавичка го изкарва от полето. Затова всяка клетка е в
    кавички, `"` се удвоява, `\\` се удвоява, а новите редове и
    табулациите стават escape последователности.
    """
    if value is None:
        text = ""
    elif isinstance(value, bool):
        text = "true" if value else "false"
    else:
        text = str(value)
    text = (text.replace("\\", "\\\\")
                .replace('"', '""')
                .replace("\r\n", "\n")
                .replace("\r", "\n")
                .replace("\n", "\\n")
                .replace("\t", "\\t"))
    return '"' + text + '"'


def records_to_csv(records: list[dict]) -> str:
    """Записите → CSV текст: първи ред — полетата (в реда на първата поява)."""
    keys: list[str] = []
    seen: set[str] = set()
    for rec in records:
        for key in rec:
            if key not in seen:
                seen.add(key)
                keys.append(key)
    lines = [",".join(_csv_cell(k) for k in keys)]
    for rec in records:
        lines.append(",".join(_csv_cell(rec.get(k)) for k in keys))
    # Само `\n` — `\r\n` чупи разбора на поле в кавички (виж `_csv_cell`).
    return "\n".join(lines) + "\n"


def prepare_template(xml_text: str) -> str:
    """Насочва сливането на шаблона към CSV-то на проксито."""
    merge = '<Merge type="%s" src="%s"/>' % (MERGE_TYPE, CSV_NAME)
    if _MERGE_RE.search(xml_text):
        return _MERGE_RE.sub(merge, xml_text)
    ends = list(_DOC_END_RE.finditer(xml_text))
    if not ends:
        raise LabelRequestError(400, "template is not a glabels document")
    pos = ends[-1].start()
    return xml_text[:pos] + "  " + merge + "\n" + xml_text[pos:]


def count_pdf_pages(data: bytes) -> int:
    return len(_PDF_PAGE_RE.findall(data))


class LabelPrinter:
    """Списъкът с принтерите и печатът — без състояние освен кеша."""

    def __init__(self, cfg):
        self.cfg = cfg
        self._lock = threading.Lock()
        self._printers_cache: Optional[tuple[float, list[dict]]] = None
        self._version_cache: Optional[tuple[float, Optional[str]]] = None

    # ─── изпълними файлове ────────────────────────────────────────────

    @staticmethod
    def _resolve(configured: str, name: str, candidates: list[str]) -> str:
        if configured:
            return configured
        found = shutil.which(name)
        if found:
            return found
        for cand in candidates:
            for path in sorted(glob.glob(cand)):
                if os.path.isfile(path):
                    return path
        # Голото име: ако го няма, `subprocess.run` вдига FileNotFoundError
        # и отговорът казва ясно „не е инсталиран“.
        return name

    def glabels_exe(self) -> str:
        if _is_windows():
            pf = [os.environ.get(v, "") for v in
                  ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA")]
            cands = [os.path.join(p, "*abels*", "bin", "glabels-batch-qt.exe")
                     for p in pf if p]
            cands += [os.path.join(p, "*abels*", "glabels-batch-qt.exe")
                      for p in pf if p]
            return self._resolve(self.cfg.glabels, "glabels-batch-qt", cands)
        return self._resolve(
            self.cfg.glabels, "glabels-batch-qt",
            [str(Path.home() / ".local" / "bin" / "glabels-batch-qt"),
             "/usr/local/bin/glabels-batch-qt", "/usr/bin/glabels-batch-qt"])

    def _qt_env(self) -> dict:
        env = dict(os.environ)
        # QGuiApplication иска дисплей; услугата обикновено няма такъв.
        if not _is_windows():
            env.setdefault("QT_QPA_PLATFORM", "offscreen")
        return env

    def _run(self, args: list[str], timeout: float, env: Optional[dict] = None):
        # Без shell — аргументите (име на принтер с интервали, пътища на
        # кирилица) отиват на процеса непокътнати.
        return subprocess.run(
            args, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout, env=env, shell=False,
        )

    # ─── версия на glabels ────────────────────────────────────────────

    def glabels_version(self, refresh: bool = False) -> Optional[str]:
        now = time.monotonic()
        with self._lock:
            cached = self._version_cache
        if (not refresh and cached
                and now - cached[0] < self.cfg.cache_seconds):
            return cached[1]
        version: Optional[str] = None
        try:
            proc = self._run([self.glabels_exe(), "--version"],
                             self.cfg.list_timeout_seconds, self._qt_env())
            if proc.returncode == 0:
                out = (proc.stdout or proc.stderr or "").strip().splitlines()
                line = out[0].strip() if out else ""
                parts = line.split(None, 1)
                version = (parts[1] if len(parts) == 2 else line) or None
        except (OSError, subprocess.SubprocessError) as exc:
            _logger.debug("labels: glabels-qt is not available: %s", exc)
        with self._lock:
            self._version_cache = (now, version)
        return version

    # ─── принтерите на ОС ─────────────────────────────────────────────

    def printers(self, refresh: bool = False) -> list[dict]:
        now = time.monotonic()
        with self._lock:
            cached = self._printers_cache
        if (not refresh and cached
                and now - cached[0] < self.cfg.cache_seconds):
            return [dict(p) for p in cached[1]]
        try:
            found = (self._windows_printers() if _is_windows()
                     else self._cups_printers())
        except Exception:  # noqa: BLE001 — списъкът не бива да чупи /healthz
            _logger.exception("labels: listing the OS printers failed")
            found = []
        with self._lock:
            self._printers_cache = (now, found)
        return [dict(p) for p in found]

    def printer_names(self) -> list[str]:
        return [p["name"] for p in self.printers()]

    def _cups_printers(self) -> list[dict]:
        lpstat = self._resolve(self.cfg.lpstat, "lpstat", [])
        timeout = self.cfg.list_timeout_seconds
        try:
            proc = self._run([lpstat, "-e"], timeout, _c_env())
        except (OSError, subprocess.SubprocessError) as exc:
            _logger.debug("labels: lpstat is not available: %s", exc)
            return []
        names = [ln.strip() for ln in (proc.stdout or "").splitlines()
                 if ln.strip()] if proc.returncode == 0 else []
        default = None
        try:
            dproc = self._run([lpstat, "-d"], timeout, _c_env())
            m = _LPSTAT_DEFAULT_RE.search(dproc.stdout or "")
            default = m.group(1) if m else None
        except (OSError, subprocess.SubprocessError):
            pass
        return [{"name": n, "default": n == default} for n in names]

    def _windows_printers(self) -> list[dict]:
        try:
            import win32print  # type: ignore[import-not-found]
        except ImportError:
            win32print = None
        if win32print is not None:
            flags = (win32print.PRINTER_ENUM_LOCAL
                     | win32print.PRINTER_ENUM_CONNECTIONS)
            names = [row[2] for row in win32print.EnumPrinters(flags, None, 1)]
            try:
                default = win32print.GetDefaultPrinter()
            except Exception:  # noqa: BLE001 — няма подразбиращ се
                default = None
            return [{"name": n, "default": n == default} for n in names]
        ps = self._resolve(self.cfg.powershell, "powershell", [])
        script = ("Get-CimInstance Win32_Printer | "
                  "Select-Object Name,Default | ConvertTo-Json -Compress")
        try:
            proc = self._run([ps, "-NoProfile", "-NonInteractive",
                              "-Command", script],
                             self.cfg.list_timeout_seconds)
        except (OSError, subprocess.SubprocessError) as exc:
            _logger.warning("labels: PowerShell is not available: %s", exc)
            return []
        if proc.returncode != 0 or not (proc.stdout or "").strip():
            return []
        rows = json.loads(proc.stdout)
        if isinstance(rows, dict):  # един принтер → обект, не списък
            rows = [rows]
        return [{"name": str(r.get("Name")), "default": bool(r.get("Default"))}
                for r in rows if r.get("Name")]

    # ─── печат ────────────────────────────────────────────────────────

    def print_via(self) -> str:
        mode = (self.cfg.print_via or "auto").lower()
        if mode == "auto":
            return "glabels" if _is_windows() else "lp"
        return mode

    def work_dir(self) -> Path:
        base = Path(self.cfg.work_dir) if self.cfg.work_dir else (
            Path(tempfile.gettempdir()) / "odoo-erpnet-fp-labels")
        base.mkdir(parents=True, exist_ok=True)
        return base

    def _decode_template(self, template: str) -> str:
        if not template or not template.strip():
            raise LabelRequestError(400, "template is empty")
        try:
            raw = base64.b64decode("".join(template.split()), validate=True)
        except (binascii.Error, ValueError):
            raise LabelRequestError(400, "template is not valid base64")
        limit = self.cfg.max_template_bytes
        if not raw:
            raise LabelRequestError(400, "template is empty")
        if len(raw) > limit:
            raise LabelRequestError(413, "template is larger than %d bytes" % limit)
        if raw[:2] == b"\x1f\x8b":
            # Компресиран .glabels (така ги пише и самият glabels-qt) —
            # разпакетирането е с таван, иначе малка бомба пълни паметта.
            try:
                dec = zlib.decompressobj(16 + zlib.MAX_WBITS)
                raw = dec.decompress(raw, limit + 1)
            except (zlib.error, gzip.BadGzipFile):
                raise LabelRequestError(400, "template is not valid gzip")
            if len(raw) > limit:
                raise LabelRequestError(
                    413, "template is larger than %d bytes" % limit)
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise LabelRequestError(400, "template is not UTF-8 XML")
        if "<Glabels-document" not in text:
            raise LabelRequestError(400, "template is not a glabels document")
        return text

    def _check_records(self, records: Any) -> list[dict]:
        if not isinstance(records, list) or not records:
            raise LabelRequestError(400, "records are empty")
        if len(records) > self.cfg.max_records:
            raise LabelRequestError(
                413, "more than %d records" % self.cfg.max_records)
        for i, rec in enumerate(records):
            if not isinstance(rec, dict) or not rec:
                raise LabelRequestError(400, "record %d is not an object" % i)
            for key, value in rec.items():
                if not isinstance(key, str) or not key.strip():
                    raise LabelRequestError(400, "record %d has an empty field name" % i)
                if isinstance(value, (dict, list)):
                    raise LabelRequestError(
                        400, "record %d field %r is not a scalar" % (i, key))
        return records

    def print_labels(self, printer: str, template: str, records: Any,
                     copies: int = 1) -> dict:
        """Печат. Вдига `LabelRequestError` при невалидна заявка; грешка на
        glabels/печата е `{"ok": False, "error": …}`."""
        if not printer or not str(printer).strip():
            raise LabelRequestError(400, "printer is empty")
        if not isinstance(copies, int) or isinstance(copies, bool) or not (
                1 <= copies <= self.cfg.max_copies):
            raise LabelRequestError(
                400, "copies must be between 1 and %d" % self.cfg.max_copies)
        xml_text = prepare_template(self._decode_template(template))
        records = self._check_records(records)
        names = self.printer_names()
        if printer not in names:
            # Принтерът може да е добавен току-що — един свеж опит.
            names = [p["name"] for p in self.printers(refresh=True)]
        if printer not in names:
            raise LabelRequestError(404, "unknown printer: %s" % printer)

        job_dir = Path(tempfile.mkdtemp(prefix="job-", dir=str(self.work_dir())))
        try:
            return self._render_and_print(job_dir, printer, xml_text,
                                          records, copies)
        finally:
            shutil.rmtree(job_dir, ignore_errors=True)

    def _render_and_print(self, job_dir: Path, printer: str, xml_text: str,
                          records: list[dict], copies: int) -> dict:
        tpl = job_dir / TEMPLATE_NAME
        csv_path = job_dir / CSV_NAME
        pdf = job_dir / PDF_NAME
        tpl.write_text(xml_text, encoding="utf-8")
        # Без BOM — той би станал част от името на първото поле.
        csv_path.write_text(records_to_csv(records), encoding="utf-8",
                            newline="")
        exe = self.glabels_exe()
        timeout = self.cfg.timeout_seconds
        try:
            proc = self._run(
                [exe, "-i", str(csv_path), "-o", str(pdf),
                 "-c", str(copies), str(tpl)],
                timeout, self._qt_env())
        except FileNotFoundError:
            return self._fail("glabels-qt is not installed (%s)" % exe)
        except subprocess.TimeoutExpired:
            return self._fail("glabels-qt timed out after %g s" % timeout)
        except OSError as exc:
            return self._fail("glabels-qt failed to start: %s" % exc)
        output = (proc.stdout or "") + "\n" + (proc.stderr or "")
        if proc.returncode != 0:
            return self._fail("glabels-qt exited with %d: %s"
                              % (proc.returncode, _tail(output)))
        if not pdf.is_file() or pdf.stat().st_size == 0:
            return self._fail("glabels-qt produced no PDF: %s" % _tail(output))
        m = _PAGES_RE.search(output)
        pages = int(m.group(1)) if m else count_pdf_pages(pdf.read_bytes())
        if pages <= 0:
            return self._fail("glabels-qt rendered no labels")

        mode = self.print_via()
        try:
            if mode == "lp":
                job_id = self._print_lp(printer, pdf, timeout)
            elif mode == "glabels":
                job_id = self._print_glabels(exe, printer, tpl, csv_path,
                                             copies, timeout)
            elif mode == "sumatra":
                job_id = self._print_sumatra(printer, pdf, timeout)
            else:
                return self._fail("unknown labels.print_via: %s" % mode)
        except _PrintError as exc:
            return self._fail(str(exc))
        _logger.info("labels: printed %d page(s) on %s via %s (job %s)",
                     pages, printer, mode, job_id)
        return {"ok": True, "job_id": job_id, "pages": pages}

    @staticmethod
    def _fail(error: str) -> dict:
        _logger.warning("labels: %s", error)
        return {"ok": False, "error": error}

    def _spool(self, what: str, args: list[str], timeout: float,
               env: Optional[dict] = None):
        try:
            proc = self._run(args, timeout, env)
        except FileNotFoundError:
            raise _PrintError("%s is not installed (%s)" % (what, args[0]))
        except subprocess.TimeoutExpired:
            raise _PrintError("%s timed out after %g s" % (what, timeout))
        except OSError as exc:
            raise _PrintError("%s failed to start: %s" % (what, exc))
        if proc.returncode != 0:
            raise _PrintError("%s exited with %d: %s" % (
                what, proc.returncode,
                _tail((proc.stderr or "") + (proc.stdout or ""))))
        return proc

    def _print_lp(self, printer: str, pdf: Path, timeout: float) -> str:
        lp = self._resolve(self.cfg.lp, "lp", [])
        proc = self._spool("lp", [lp, "-d", printer, "-t", "labels", str(pdf)],
                           timeout, _c_env())
        m = _LP_JOB_RE.search(proc.stdout or "")
        return m.group(1) if m else uuid.uuid4().hex

    def _print_glabels(self, exe: str, printer: str, tpl: Path, csv_path: Path,
                       copies: int, timeout: float) -> str:
        # Windows няма вграден печат на PDF; QPrinter на glabels праща
        # същите етикети направо в спулера на принтера.
        self._spool("glabels-qt", [exe, "-p", printer, "-i", str(csv_path),
                                   "-c", str(copies), str(tpl)], timeout)
        return uuid.uuid4().hex

    def _print_sumatra(self, printer: str, pdf: Path, timeout: float) -> str:
        sumatra = self._resolve(self.cfg.sumatra, "SumatraPDF", [])
        self._spool("SumatraPDF", [sumatra, "-print-to", printer, "-silent",
                                   str(pdf)], timeout)
        return uuid.uuid4().hex


class _PrintError(Exception):
    pass
