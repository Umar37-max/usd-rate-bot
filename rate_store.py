"""SQLite cache of commercial rates, keyed by the day they belong to.

A rate for a day that has already passed never changes, so once a day is in
here it can be served forever without touching the bank — which is the whole
point: a CIB login costs ~6 seconds, a lookup here costs well under a
millisecond.
"""
import logging
import sqlite3
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

# One connection per thread, opened once. Reconnecting and re-running the schema
# on every lookup cost ~15ms per call — most of the budget for a cache that is
# supposed to answer in a fraction of a millisecond.
_local = threading.local()
_schema_ready = False

DB_PATH = Path(__file__).parent / "state" / "rates.db"
CURRENCIES = ("usd", "eur", "rub", "cny")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS rates (
    date       TEXT PRIMARY KEY,   -- dd.mm.yyyy, the day the rate belongs to
    usd_buy REAL, usd_sell REAL,
    eur_buy REAL, eur_sell REAL,
    rub_buy REAL, rub_sell REAL,
    cny_buy REAL, cny_sell REAL,
    fetched_at TEXT,
    screenshot BLOB
);
"""


def _connect() -> sqlite3.Connection:
    global _schema_ready
    conn = getattr(_local, "conn", None)
    if conn is not None:
        return conn
    DB_PATH.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=5, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")  # reader (API) never blocks the writer (bot)
    if not _schema_ready:
        conn.executescript(_SCHEMA)
        _schema_ready = True
    _local.conn = conn
    return conn


def init_db():
    _connect()


def save(rates: dict, screenshot: bytes | None = None) -> bool:
    """Stores one day's rates. Keeps an existing screenshot if none is given."""
    date = rates.get("rate_date")
    if not date or "usd" not in rates:
        return False
    cols = {"date": date, "fetched_at": rates.get("fetched_at")}
    for code in CURRENCIES:
        row = rates.get(code) or {}
        cols[f"{code}_buy"] = row.get("buy")
        cols[f"{code}_sell"] = row.get("sell")
    try:
        with _connect() as conn:
            names = ", ".join(cols)
            holes = ", ".join("?" for _ in cols)
            conn.execute(f"INSERT OR REPLACE INTO rates ({names}) VALUES ({holes})", list(cols.values()))
            if screenshot:
                conn.execute("UPDATE rates SET screenshot = ? WHERE date = ?", (screenshot, date))
        return True
    except sqlite3.Error:
        logger.exception("Could not store the rate for %s", date)
        return False


def _row_to_dict(row: sqlite3.Row, with_screenshot: bool = False) -> dict:
    out = {"date": row["date"], "fetched_at": row["fetched_at"]}
    for code in CURRENCIES:
        buy, sell = row[f"{code}_buy"], row[f"{code}_sell"]
        if buy is not None or sell is not None:
            out[code] = {"buy": buy, "sell": sell}
    if with_screenshot:
        out["screenshot"] = row["screenshot"]
    return out


def get(date: str | None = None, with_screenshot: bool = False) -> dict | None:
    """One day's rates, or the most recent day when date is None."""
    try:
        with _connect() as conn:
            if date:
                row = conn.execute("SELECT * FROM rates WHERE date = ?", (date,)).fetchone()
            else:
                # dd.mm.yyyy does not sort chronologically, so order by the parts.
                row = conn.execute(
                    "SELECT * FROM rates ORDER BY substr(date,7,4) DESC, "
                    "substr(date,4,2) DESC, substr(date,1,2) DESC LIMIT 1"
                ).fetchone()
        return _row_to_dict(row, with_screenshot) if row else None
    except sqlite3.Error:
        logger.exception("Could not read the rate for %s", date or "latest")
        return None


def all_rows() -> list:
    """Every cached day, newest first, without the screenshot blobs."""
    try:
        with _connect() as conn:
            rows = conn.execute(
                "SELECT * FROM rates ORDER BY substr(date,7,4) DESC, "
                "substr(date,4,2) DESC, substr(date,1,2) DESC"
            ).fetchall()
        return [_row_to_dict(r) for r in rows]
    except sqlite3.Error:
        logger.exception("Could not read the cache")
        return []


def dates() -> list:
    try:
        with _connect() as conn:
            rows = conn.execute(
                "SELECT date FROM rates ORDER BY substr(date,7,4) DESC, "
                "substr(date,4,2) DESC, substr(date,1,2) DESC"
            ).fetchall()
        return [r["date"] for r in rows]
    except sqlite3.Error:
        logger.exception("Could not list cached dates")
        return []


if __name__ == "__main__":
    init_db()
    cached = dates()
    print(f"{DB_PATH}: {len(cached)} дат")
    for d in cached[:10]:
        row = get(d)
        print(f"  {d}: USD {row['usd']['buy']} / {row['usd']['sell']}")
