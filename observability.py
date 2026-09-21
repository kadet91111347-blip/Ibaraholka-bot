"""
Observability layer для Ibaraholka.

Включает:
  1. JSON-логгер с файловой ротацией (10 MB × 5 файлов)
  2. Process-wide metrics counters (in-memory, /metrics endpoint)
  3. Error reporter — middleware ловит 5xx, шлёт в Sentry (если есть) + в чат админу
"""
from __future__ import annotations
import json
import os
import time
import logging
import logging.handlers
import threading
import traceback
from collections import deque
from typing import Any, Dict, List, Optional

_LOG_DIR = os.getenv("LOG_DIR", "/tmp").strip()
os.makedirs(_LOG_DIR, exist_ok=True)
_LOG_FILE = os.path.join(_LOG_DIR, "ibaraholka.json.log")

# -------- JSON formatter ----------
class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: Dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)) + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        # extras (logger.info("x", extra={...}) → as fields)
        for k, v in record.__dict__.items():
            if k in ("args","asctime","created","exc_info","exc_text","filename",
                     "funcName","levelname","levelno","lineno","message","module",
                     "msecs","msg","name","pathname","process","processName",
                     "relativeCreated","stack_info","thread","threadName","taskName"):
                continue
            try:
                json.dumps(v)
                payload[k] = v
            except TypeError:
                payload[k] = repr(v)
        if record.exc_info:
            payload["exc"] = "".join(traceback.format_exception(*record.exc_info)).rstrip()
        return json.dumps(payload, ensure_ascii=False)


def setup_logging(level: int = logging.INFO) -> None:
    """Заменить default root logger на JSON-rotating."""
    root = logging.getLogger()
    root.handlers.clear()
    stream = logging.StreamHandler()
    stream.setFormatter(JsonFormatter())
    root.addHandler(stream)
    try:
        rf = logging.handlers.RotatingFileHandler(
            _LOG_FILE, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
        )
        rf.setFormatter(JsonFormatter())
        root.addHandler(rf)
    except Exception as e:
        logging.warning(f"file log disabled: {e}")
    root.setLevel(level)


# -------- Metrics ----------
class Metrics:
    """Thread-safe in-memory metrics for /metrics endpoint.

    Хранит: counter, gauge, histogram (последние N значений)."""
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: Dict[str, int] = {}
        self._gauges: Dict[str, float] = {}
        self._histograms: Dict[str, deque] = {}
        self._recent_latency: deque = deque(maxlen=512)  # ms

    def incr(self, name: str, n: int = 1, tags: Optional[Dict[str, str]] = None) -> None:
        key = self._k(name, tags)
        with self._lock:
            self._counters[key] = self._counters.get(key, 0) + n

    def gauge(self, name: str, val: float, tags: Optional[Dict[str, str]] = None) -> None:
        key = self._k(name, tags)
        with self._lock:
            self._gauges[key] = val

    def histogram(self, name: str, val: float, tags: Optional[Dict[str, str]] = None) -> None:
        key = self._k(name, tags)
        with self._lock:
            dq = self._histograms.setdefault(key, deque(maxlen=512))
            dq.append(val)

    def observe_latency(self, path: str, status: int, ms: float) -> None:
        self._recent_latency.append((time.time(), path, status, ms))
        self.incr("http_requests_total", tags={"path": path, "status": str(status)})
        self.histogram("http_request_duration_ms", ms, tags={"path": path, "status": str(status)})

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            counters = dict(self._counters)
            gauges = dict(self._gauges)
            hists = {k: list(d) for k, d in self._histograms.items()}
        hist_summary: Dict[str, Dict[str, float]] = {}
        for k, vals in hists.items():
            if not vals:
                continue
            s = sorted(vals)
            n = len(s)
            hist_summary[k] = {
                "count": n,
                "p50": s[n // 2],
                "p95": s[max(0, int(n * 0.95) - 1)] if n > 1 else s[0],
                "avg": sum(s) / n,
            }
        return {
            "uptime_sec": round(time.time() - _START_TIME, 1),
            "counters": counters,
            "gauges": gauges,
            "histograms": hist_summary,
            "log_file": _LOG_FILE,
        }

    @staticmethod
    def _k(name: str, tags: Optional[Dict[str, str]]) -> str:
        if not tags:
            return name
        return name + "{" + ",".join(f"{a}={b}" for a, b in sorted(tags.items())) + "}"


_START_TIME = time.time()
METRICS = Metrics()


# -------- Error reporter ----------
class ErrorReporter:
    """Ловит 5xx в FastAPI middleware: Sentry + Telegram-уведомление админу."""
    def __init__(self) -> None:
        self._last_sent: Dict[str, float] = {}  # signature -> ts (rate-limit)
        self._lock = threading.Lock()

    async def report(self, request, exc: Exception) -> None:
        path = request.url.path
        sig = f"{type(exc).__name__}:{path}"
        now = time.time()
        with self._lock:
            if now - self._last_sent.get(sig, 0) < 60:
                return  # throttle: same signature → 1 alert/min
            self._last_sent[sig] = now

        msg = f"🚨 <b>{type(exc).__name__}</b> on <code>{path}</code>\n"
        msg += f"<code>{str(exc)[:300]}</code>"
        logging.exception("5xx on %s", path, exc_info=exc)
        METRICS.incr("errors_total", tags={"path": path, "type": type(exc).__name__})

        # Sentry (если подключён)
        try:
            import sentry_sdk
            with sentry_sdk.push_scope() as scope:
                scope.set_extra("path", path)
                sentry_sdk.capture_exception(exc)
        except ImportError:
            pass
        except Exception:
            pass

        # Telegram-уведомление админу (лучше поздно чем никогда)
        try:
            from main import bot, ADMIN_IDS  # late import — main.py определяет их позже
            if bot and ADMIN_IDS:
                import asyncio
                for admin_id in ADMIN_IDS[:3]:  # max 3 admins
                    try:
                        await bot.send_message(admin_id, msg)
                    except Exception:
                        pass
        except Exception:
            pass


REPORTER = ErrorReporter()
