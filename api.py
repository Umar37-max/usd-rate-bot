"""Tiny HTTP API over the rate cache. Answers in well under a millisecond.

    GET /usd                      курс доллара на последнюю известную дату
    GET /usd?date=10.09.2026      курс доллара на конкретную дату
    GET /rates                    все валюты (usd, eur, rub, cny)
    GET /rates?date=10.09.2026
    GET /dates                    какие даты лежат в кэше
    GET /screenshot?date=...      PNG панели банка за эту дату
    GET /health

Deliberately never calls the bank: a CIB login takes about six seconds, which
would defeat the purpose. Dates the cache has not seen return 404 — the bot
fills the cache as it fetches.
"""
import json
import logging
import os
import re
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import rate_store

BASE_DIR = Path(__file__).parent
CONFIG = json.loads((BASE_DIR / "config.json").read_text(encoding="utf-8"))
# Localhost by default: the API has no authentication, so it must not be
# reachable from the internet unless that is a deliberate choice.
HOST = CONFIG.get("api_host", "127.0.0.1")
PORT = CONFIG.get("api_port", 8777)
TIMEZONE = ZoneInfo(CONFIG.get("timezone", "Asia/Tashkent"))

DATE_RE = re.compile(r"^\d{2}\.\d{2}\.\d{4}$")
logger = logging.getLogger("usd_rate_api")

# The server runs a thread per connection, so going to SQLite on every request
# meant opening a fresh connection per thread — ~2ms. Keep the rows in memory
# and reload only when the database file actually changes.
_memo = {"key": None, "rows": {}, "dates": []}
_memo_lock = threading.Lock()


def _db_fingerprint():
    """Changes whenever the bot writes. The -wal file matters: in WAL mode the
    main database file's mtime does not move until a checkpoint."""
    parts = []
    for suffix in ("", "-wal"):
        try:
            st = os.stat(str(rate_store.DB_PATH) + suffix)
            parts.append((st.st_mtime_ns, st.st_size))
        except OSError:
            parts.append(None)
    return tuple(parts)


def _cached():
    key = _db_fingerprint()
    with _memo_lock:
        if _memo["key"] != key:
            rows = {row["date"]: row for row in rate_store.all_rows()}
            _memo.update(key=key, rows=rows, dates=list(rows))
            logger.info("Cache reloaded: %d дат", len(rows))
        return _memo["rows"], _memo["dates"]


class Handler(BaseHTTPRequestHandler):
    server_version = "usd_rate_bot"
    protocol_version = "HTTP/1.1"  # keep-alive, so repeat calls skip the TCP handshake

    def _send(self, code: int, payload, content_type="application/json; charset=utf-8"):
        body = payload if isinstance(payload, bytes) else json.dumps(
            payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _date_param(self, query):
        value = (query.get("date") or [None])[0]
        if value is None:
            return None, None
        value = value.strip()
        if not DATE_RE.match(value):
            return None, "Дата должна быть в виде ДД.ММ.ГГГГ, например 10.09.2026"
        return value, None

    def do_GET(self):
        started = time.perf_counter()
        parsed = urlparse(self.path)
        route = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)

        rows, order = _cached()

        if route == "/health":
            return self._send(200, {
                "ok": True,
                "cached_days": len(order),
                "latest": order[0] if order else None,
                "now": datetime.now(TIMEZONE).strftime("%d.%m.%Y %H:%M:%S"),
                "took_ms": round((time.perf_counter() - started) * 1000, 3),
            })

        if route == "/dates":
            return self._send(200, {"dates": order})

        if route in ("/usd", "/rates", "/screenshot"):
            date, error = self._date_param(query)
            if error:
                return self._send(400, {"error": error})

            if route == "/screenshot":
                # Blobs stay out of memory; this route is rare.
                row = rate_store.get(date, with_screenshot=True)
                if row is None:
                    return self._send(404, {"error": f"Курс на {date} в кэше нет." if date else "Кэш пуст."})
                shot = row.get("screenshot")
                if not shot:
                    return self._send(404, {"error": f"Для {row['date']} скриншот не сохранён."})
                return self._send(200, shot, "image/png")

            row = rows.get(date) if date else (rows.get(order[0]) if order else None)
            if row is None:
                return self._send(404, {
                    "error": f"Курс на {date} в кэше нет." if date else "Кэш пуст.",
                    "hint": "Запросите эту дату у бота — он сходит в банк и сохранит её здесь.",
                    "cached_dates": order[:10],
                })

            if route == "/usd":
                usd = row.get("usd") or {}
                payload = {"date": row["date"], "buy": usd.get("buy"), "sell": usd.get("sell")}
            else:
                payload = {k: v for k, v in row.items() if k != "screenshot"}
            payload["fetched_at"] = row.get("fetched_at")
            payload["took_ms"] = round((time.perf_counter() - started) * 1000, 3)
            return self._send(200, payload)

        self._send(404, {"error": "Неизвестный путь", "routes": [
            "/usd", "/rates", "/dates", "/screenshot", "/health"]})

    def log_message(self, fmt, *args):  # keep the console quiet; the bot owns the log
        logger.debug("%s - %s", self.address_string(), fmt % args)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    rate_store.init_db()
    _cached()  # warm the memo before the first request arrives
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    cached = rate_store.dates()
    print(f"API: http://{HOST}:{PORT}  (в кэше дат: {len(cached)}"
          + (f", последняя {cached[0]}" if cached else "") + ")")
    print(f"  http://{HOST}:{PORT}/usd")
    print(f"  http://{HOST}:{PORT}/usd?date=10.09.2026")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nОстановлен.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
