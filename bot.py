"""Telegram bot: posts TrustBank CIB commercial USD/EUR/RUB rate daily at 09:00 Asia/Tashkent,
and on demand via the "Курс на сегодня" button."""
import asyncio
import json
import logging
import re
import time
from datetime import date, datetime, time as dt_time, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path
from zoneinfo import ZoneInfo

from telegram import ReplyKeyboardMarkup, Update
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter, TimedOut
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

import bank_scraper
import rate_store

BASE_DIR = Path(__file__).parent
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        # Rotating, so a long-running bot cannot fill the disk with one huge log.
        RotatingFileHandler(BASE_DIR / "bot.log", maxBytes=2_000_000, backupCount=5, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("usd_rate_bot")

CONFIG = json.loads((BASE_DIR / "config.json").read_text(encoding="utf-8"))
CREDS = json.loads((BASE_DIR / "credentials.json").read_text(encoding="utf-8"))

BANK_URL = CONFIG.get("bank_url", "https://ibank.trustbank.uz:224/cib/index_09030.jsp")
TIMEZONE = ZoneInfo(CONFIG.get("timezone", "Asia/Tashkent"))
GROUP_CHAT_ID = CONFIG["group_chat_id"]
ALLOWED_USER_IDS = set(CONFIG.get("allowed_user_ids", []))
RATE_COOLDOWN_SECONDS = CONFIG.get("rate_cooldown_seconds", 120)
DAILY_HH, DAILY_MM = (int(x) for x in CONFIG.get("daily_time", "09:00").split(":"))

# If the bank is unreachable at the scheduled time, keep trying instead of
# giving up on the day: the group must still get the rate.
DAILY_RETRY_DELAY = CONFIG.get("daily_retry_delay_seconds", 600)
DAILY_MAX_ATTEMPTS = CONFIG.get("daily_max_attempts", 8)

BUTTON_TEXT = "📊 Курс на сегодня"
KEYBOARD = ReplyKeyboardMarkup([[BUTTON_TEXT]], resize_keyboard=True)

STATE_DIR = BASE_DIR / "state"
LAST_GOOD_JSON = STATE_DIR / "last_good.json"
LAST_GOOD_PNG = STATE_DIR / "last_good.png"
LAST_POST_JSON = STATE_DIR / "last_post.json"

_cache_lock = asyncio.Lock()
_cache = {"rates": None, "screenshot": None, "ts": None}


def _fmt(v: float) -> str:
    return f"{int(v):,}".replace(",", " ") if v == int(v) else f"{v:,.2f}".replace(",", " ")


# --- date requests ---------------------------------------------------------

# "10.09.2026", "10.09", "10/9/26", "10-09-2026", "2026-09-10"
DATE_INPUT_RE = re.compile(r"^\s*(\d{1,4})[.\-/](\d{1,2})(?:[.\-/](\d{1,4}))?\s*$")


def parse_user_date(text: str) -> date | None:
    """A date the user typed, in any of the usual shapes. None if it isn't one."""
    m = DATE_INPUT_RE.match(text or "")
    if not m:
        return None
    first, second, third = m.group(1), m.group(2), m.group(3)
    try:
        if len(first) == 4 and third is not None:
            year, month, day = int(first), int(second), int(third)  # 2026-09-10
        else:
            day, month = int(first), int(second)
            if third is None:
                year = datetime.now(TIMEZONE).year  # bare "10.09" means this year
            else:
                year = int(third)
                if year < 100:
                    year += 2000
        return date(year, month, day)
    except ValueError:
        return None  # 31.02 and friends


# --- last known good rate --------------------------------------------------

def _save_last_good(rates: dict, screenshot: bytes | None):
    """Keeps the newest successful fetch on disk so an outage never leaves the
    group with nothing to post."""
    try:
        STATE_DIR.mkdir(exist_ok=True)
        LAST_GOOD_JSON.write_text(json.dumps(rates, ensure_ascii=False, indent=2), encoding="utf-8")
        if screenshot:
            LAST_GOOD_PNG.write_bytes(screenshot)
        else:
            # An older screenshot would show numbers that no longer match these rates.
            LAST_GOOD_PNG.unlink(missing_ok=True)
    except Exception:
        logger.exception("Could not save the last known good rate")


def _load_last_good() -> tuple | None:
    try:
        rates = json.loads(LAST_GOOD_JSON.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(rates, dict) or "usd" not in rates:
        return None
    screenshot = None
    try:
        if LAST_GOOD_PNG.exists():
            screenshot = LAST_GOOD_PNG.read_bytes()
    except Exception:
        logger.exception("Could not read the saved rate screenshot")
    return rates, screenshot


def _posted_today() -> bool:
    try:
        data = json.loads(LAST_POST_JSON.read_text(encoding="utf-8"))
    except Exception:
        return False
    return data.get("date") == datetime.now(TIMEZONE).strftime("%Y-%m-%d")


def _mark_posted():
    try:
        STATE_DIR.mkdir(exist_ok=True)
        now = datetime.now(TIMEZONE)
        LAST_POST_JSON.write_text(
            json.dumps({"date": now.strftime("%Y-%m-%d"), "at": now.isoformat()}, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception:
        logger.exception("Could not record the daily post")


# --- fetching --------------------------------------------------------------

def _is_past(date_str: str) -> bool:
    try:
        return datetime.strptime(date_str, "%d.%m.%Y").date() < datetime.now(TIMEZONE).date()
    except ValueError:
        return False


async def get_snapshot_for_date(target_date: str) -> dict:
    """Rate for one specific day (dd.mm.yyyy).

    A day that has already passed cannot change, so the stored copy is
    authoritative and answers instantly instead of spending ~6s on a CIB login.
    Today and tomorrow always go to the bank — those numbers still move.

    Never falls back to the last-known-good file: that holds *today's* rate, and
    serving it for another day would answer the wrong question.
    """
    if _is_past(target_date):
        row = rate_store.get(target_date, with_screenshot=True)
        if row and row.get("usd"):
            logger.info("Rate for %s served from the cache", target_date)
            rates = {code: row[code] for code in rate_store.CURRENCIES if code in row}
            rates["rate_date"] = row["date"]
            rates["fetched_at"] = row.get("fetched_at")
            return {
                "rates": rates,
                "screenshot": row.get("screenshot"),
                "stale": False,
                "error": None,
                "requested_date": target_date,
                "cached": True,
            }

    async with _cache_lock:
        rates, screenshot_bytes = await bank_scraper.fetch_rates_with_retry(
            BANK_URL, CREDS["login"], CREDS["password"], target_date=target_date
        )
    rate_store.save(rates, screenshot_bytes)
    return {
        "rates": rates,
        "screenshot": screenshot_bytes,
        "stale": False,
        "error": None,
        "requested_date": target_date,
        "cached": False,
    }


async def get_snapshot(force: bool = False) -> dict:
    """{"rates", "screenshot", "stale"} — live if possible, last known good otherwise."""
    async with _cache_lock:
        now = time.monotonic()
        if not force and _cache["rates"] and now - _cache["ts"] < RATE_COOLDOWN_SECONDS:
            return {"rates": _cache["rates"], "screenshot": _cache["screenshot"], "stale": False}
        try:
            rates, screenshot_bytes = await bank_scraper.fetch_rates_with_retry(
                BANK_URL, CREDS["login"], CREDS["password"]
            )
        except Exception as e:
            logger.exception("Live rate fetch failed, falling back to the last known good rate")
            cached = _load_last_good()
            if cached is None:
                raise
            rates, screenshot_bytes = cached
            # Carry the reason along: without it a stale post says only "bank
            # unavailable", and the actual cause stays buried in bot.log.
            return {
                "rates": rates,
                "screenshot": screenshot_bytes,
                "stale": True,
                "error": f"{type(e).__name__}: {e}",
            }

        _cache["rates"] = rates
        _cache["screenshot"] = screenshot_bytes
        _cache["ts"] = now
        _save_last_good(rates, screenshot_bytes)
        rate_store.save(rates, screenshot_bytes)  # feeds the HTTP API in api.py
        return {"rates": rates, "screenshot": screenshot_bytes, "stale": False, "error": None}


def build_caption(snapshot: dict) -> str:
    rates = snapshot["rates"]
    date_str = rates.get("rate_date") or datetime.now(TIMEZONE).strftime("%d.%m.%Y")
    usd = rates.get("usd", {})
    caption = (
        "Ассалому алейкум! 👋\n\n"
        f"Курс доллара на {date_str}:\n"
        f"USD — Продажа: {_fmt(usd.get('sell', 0))} сум"
    )
    requested = snapshot.get("requested_date")
    if requested and requested != date_str:
        # Weekend or holiday: say so instead of quietly answering a different day.
        caption += f"\n\nℹ️ На {requested} курс не публиковался, показан ближайший предыдущий."
    if snapshot.get("stale"):
        fetched = rates.get("fetched_at", "")
        try:
            fetched = datetime.fromisoformat(fetched).strftime("%d.%m.%Y %H:%M")
        except ValueError:
            pass
        caption += (
            "\n\n⚠️ Банк сейчас недоступен, это последние сохранённые данные"
            + (f" (от {fetched})" if fetched else "")
            + "."
        )
    return caption


# --- sending ---------------------------------------------------------------

async def deliver(bot, chat_id: int, snapshot: dict, max_attempts: int = 6, retry_delay: float = 8.0):
    caption = build_caption(snapshot)
    screenshot = snapshot.get("screenshot")
    for attempt in range(1, max_attempts + 1):
        try:
            if screenshot:
                await bot.send_photo(chat_id=chat_id, photo=screenshot, filename="rate.png", caption=caption)
            else:
                await bot.send_message(chat_id=chat_id, text=caption)
            return
        except RetryAfter as e:
            # Flood limit — Telegram tells us exactly how long to wait.
            if attempt == max_attempts:
                raise
            wait = float(e.retry_after) + 1
            logger.warning("Rate limited by Telegram, waiting %.0fs before retrying", wait)
            await asyncio.sleep(wait)
        except Forbidden:
            raise  # kicked out of the chat — retrying cannot help
        except BadRequest as e:
            # BadRequest subclasses NetworkError in python-telegram-bot but is a
            # permanent error, so it must be handled before the transient branch.
            # If it was the photo Telegram objected to, still deliver the numbers.
            if screenshot:
                logger.warning("Telegram rejected the photo (%s), falling back to a text message", e)
                screenshot = None
                continue
            raise
        except (NetworkError, TimedOut) as e:
            if attempt == max_attempts:
                raise
            logger.warning(
                "deliver attempt %d/%d failed (%s), retrying in %.0fs...",
                attempt, max_attempts, e, retry_delay,
            )
            await asyncio.sleep(retry_delay)


async def send_rate(bot, chat_id: int, force: bool = False):
    snapshot = await get_snapshot(force=force)
    await deliver(bot, chat_id, snapshot)
    return snapshot


async def _notify_admins(bot, text: str):
    for admin_id in ALLOWED_USER_IDS:
        try:
            await bot.send_message(chat_id=admin_id, text=text)
        except Exception:
            logger.warning("Could not notify admin %s", admin_id, exc_info=True)


# --- jobs ------------------------------------------------------------------

async def daily_job(context: ContextTypes.DEFAULT_TYPE):
    attempt = (context.job.data or {}).get("attempt", 1)
    is_last_attempt = attempt >= DAILY_MAX_ATTEMPTS

    snapshot = None
    try:
        snapshot = await get_snapshot(force=True)
    except Exception:
        logger.exception("Daily rate fetch failed (attempt %d/%d)", attempt, DAILY_MAX_ATTEMPTS)

    # Prefer waiting for live data over posting a stale rate — but on the final
    # attempt post whatever we have, so the group is never left with nothing.
    if snapshot is not None and (not snapshot["stale"] or is_last_attempt):
        try:
            await deliver(context.bot, GROUP_CHAT_ID, snapshot)
            _mark_posted()
            logger.info(
                "Daily rate posted to group %s (attempt %d, %s)",
                GROUP_CHAT_ID, attempt, "stale" if snapshot["stale"] else "live",
            )
            if snapshot["stale"]:
                await _notify_admins(
                    context.bot,
                    "⚠️ Курс дня опубликован по сохранённым данным — свежий курс получить не удалось.\n\n"
                    f"Причина: {snapshot.get('error') or 'неизвестна'}",
                )
            return
        except Exception:
            logger.exception("Daily rate send failed (attempt %d/%d)", attempt, DAILY_MAX_ATTEMPTS)

    if not is_last_attempt:
        context.job_queue.run_once(
            daily_job,
            when=DAILY_RETRY_DELAY,
            data={"attempt": attempt + 1},
            name=f"daily_rate_retry_{attempt + 1}",
        )
        logger.warning(
            "Daily rate not posted yet, retrying in %d min (attempt %d/%d)",
            DAILY_RETRY_DELAY // 60, attempt + 1, DAILY_MAX_ATTEMPTS,
        )
    else:
        logger.error("Daily rate could not be posted after %d attempts", DAILY_MAX_ATTEMPTS)
        await _notify_admins(context.bot, "⚠️ Не удалось опубликовать курс дня. Смотрите bot.log")


# --- handlers --------------------------------------------------------------

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Бот курса валют TrustBank запущен.\n\n"
        "• Нажмите кнопку — курс на сегодня.\n"
        "• Напишите дату (например 10.09.2026) — курс на эту дату.",
        reply_markup=KEYBOARD,
    )


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id if update.effective_user else None
    if chat_id != GROUP_CHAT_ID and user_id not in ALLOWED_USER_IDS:
        logger.info("Ignoring button press from unauthorized chat_id=%s user_id=%s", chat_id, user_id)
        return  # no access — stay silent, don't reply here and don't touch the group

    status_msg = await update.message.reply_text("⏳ Получаю курс из CIB...")
    try:
        snapshot = await send_rate(context.bot, chat_id)
        if snapshot.get("stale"):
            # Tell the admins privately why it was not live, instead of putting
            # a technical error into the group's message.
            await _notify_admins(
                context.bot,
                "⚠️ Свежий курс получить не удалось, отправлены сохранённые данные.\n\n"
                f"Причина: {snapshot.get('error') or 'неизвестна'}",
            )
    except Exception:
        logger.exception("On-demand rate fetch failed")
        await update.message.reply_text("⚠️ Не удалось получить курс сейчас. Попробуйте ещё раз через пару минут.")
    finally:
        try:
            await status_msg.delete()
        except Exception:
            pass


async def on_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id if update.effective_user else None
    if chat_id != GROUP_CHAT_ID and user_id not in ALLOWED_USER_IDS:
        logger.info("Ignoring date request from unauthorized chat_id=%s user_id=%s", chat_id, user_id)
        return

    requested = parse_user_date(update.message.text)
    if requested is None:
        await update.message.reply_text("Не понял дату. Напишите так: 10.09.2026")
        return
    # The bank opens the next operational day in advance, so tomorrow is allowed.
    if requested > datetime.now(TIMEZONE).date() + timedelta(days=1):
        await update.message.reply_text("Эта дата ещё не наступила — курс на неё банк не публиковал.")
        return

    date_str = requested.strftime("%d.%m.%Y")
    status_msg = await update.message.reply_text(f"⏳ Смотрю курс на {date_str}...")
    try:
        snapshot = await get_snapshot_for_date(date_str)
        await deliver(context.bot, chat_id, snapshot)
    except Exception as e:
        logger.exception("Rate lookup for %s failed", date_str)
        await update.message.reply_text(
            f"⚠️ Не удалось получить курс на {date_str}. "
            "Возможно, банк на эту дату курс не публиковал."
        )
        await _notify_admins(context.bot, f"⚠️ Запрос курса на {date_str} не удался.\n\nПричина: {e}")
    finally:
        try:
            await status_msg.delete()
        except Exception:
            pass


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    """Last line of defence — an unhandled error must never stop the polling loop."""
    logger.error("Unhandled error while processing an update", exc_info=context.error)


async def post_init(app: Application):
    # If the machine was asleep or the bot was down at the scheduled time, post
    # the rate now instead of skipping the day entirely.
    now = datetime.now(TIMEZONE)
    if now.time() >= dt_time(hour=DAILY_HH, minute=DAILY_MM) and not _posted_today():
        app.job_queue.run_once(daily_job, when=15, data={"attempt": 1}, name="daily_rate_catchup")
        logger.info("Today's rate has not been posted yet — scheduling a catch-up post")


def main():
    rate_store.init_db()
    app = (
        Application.builder()
        .token(CONFIG["bot_token"])
        .connect_timeout(30)
        .read_timeout(60)
        # Uploading the screenshot over a slow link needs a generous write timeout.
        .write_timeout(120)
        .pool_timeout(30)
        .get_updates_read_timeout(60)
        .post_init(post_init)
        .build()
    )
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(MessageHandler(filters.Text([BUTTON_TEXT]), on_button))
    app.add_handler(MessageHandler(filters.Regex(DATE_INPUT_RE) & ~filters.COMMAND, on_date))
    app.add_error_handler(on_error)

    app.job_queue.run_daily(
        daily_job,
        time=dt_time(hour=DAILY_HH, minute=DAILY_MM, tzinfo=TIMEZONE),
        data={"attempt": 1},
        name="daily_rate",
    )
    logger.info("Scheduled daily rate post at %02d:%02d %s", DAILY_HH, DAILY_MM, TIMEZONE)

    # Button presses queued while the bot was offline would all fire at once and
    # start a scrape each; drop them and let users press again.
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
