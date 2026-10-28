"""Scrapes the commercial USD/EUR/RUB rate from TrustBank's Corporate Internet Banking (CIB)."""
import asyncio
import logging
import random
import re
import socket
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path

from playwright.async_api import async_playwright, TimeoutError as PWTimeoutError

logger = logging.getLogger(__name__)

DEBUG_DIR = Path(__file__).parent / "debug_screenshots"
DEBUG_KEEP_FILES = 60  # the dir used to grow without bound (198 files before this cap)

# TrustBank's CIB checks the client's MAC address through a local helper app
# (iABS7-Client) over ws://localhost:8088. Chrome's "Local Network Access"
# checks block a remote page from reaching localhost unless disabled here.
CHROMIUM_ARGS = [
    "--disable-features=LocalNetworkAccessChecks,PrivateNetworkAccessChecks,"
    "PrivateNetworkAccessRespectPreflightResults,PrivateNetworkAccessSendPreflights,"
    "PrivateNetworkAccessForNavigations",
]

IABS_CLIENT_DIR = Path(r"C:\Program Files (x86)\iABS7-Client")
IABS_CLIENT_PORT = 8088

# How far back to walk the panel's "Дата" field when the bank has opened a new
# operational day but has not published the USD commercial rate for it yet.
MAX_DATE_FALLBACK_DAYS = 6


class ScrapeError(RuntimeError):
    """A fetch failed. Assumed transient — the retry loop will try again."""


class PermanentScrapeError(ScrapeError):
    """A fetch failed for a reason retrying cannot fix (bad password, blocked account)."""


def _port_open(port: int, host: str = "127.0.0.1", timeout: float = 0.5) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        try:
            return s.connect_ex((host, port)) == 0
        except OSError:
            return False


def ensure_iabs_client_running(wait_seconds: float = 8.0) -> None:
    """Starts the iABS7-Client local helper (required for CIB device/MAC check) if not already running."""
    if _port_open(IABS_CLIENT_PORT):
        return

    javaw = IABS_CLIENT_DIR / "jre" / "bin" / "javaw.exe"
    jar = IABS_CLIENT_DIR / "iabs-client-ws.jar"
    if not javaw.exists() or not jar.exists():
        raise PermanentScrapeError(
            f"iABS7-Client не найден в {IABS_CLIENT_DIR}. Установите клиент вручную "
            "(он нужен банку для проверки MAC-адреса устройства)."
        )

    logger.info("iABS7-Client is not running, starting it...")
    subprocess.Popen(
        [str(javaw), "-jar", str(jar)],
        cwd=str(IABS_CLIENT_DIR),
        creationflags=subprocess.CREATE_NO_WINDOW,
    )

    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        if _port_open(IABS_CLIENT_PORT):
            logger.info("iABS7-Client is now running")
            return
        time.sleep(0.5)
    raise ScrapeError("iABS7-Client не запустился вовремя (порт 8088 не открылся).")


# --- parsing ---------------------------------------------------------------

# The panel mixes regular spaces, NBSP and thin spaces as thousands separators,
# and the bank switched the decimals from 2 ("11 856.00") to 4 ("11 856.0000")
# in late July 2026 — which is what broke the old fixed `\.\d{2}` patterns.
# Accept any number of decimals and either decimal separator.
_SPACES = "\u00a0\u2009\u202f\u2007\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2008\u200a"
_NUM = r"\d[\d" + _SPACES + r"\s']*(?:[.,]\d+)?"

CURRENCIES = {
    "usd": ("USD", "Доллар США"),
    "eur": ("EUR", "Евро"),
    "rub": ("RUB", "Российский рубль"),
    "cny": ("CNY", "Юань ренминби"),
}


def _to_float(raw: str) -> float:
    cleaned = raw
    for ch in _SPACES:
        cleaned = cleaned.replace(ch, "")
    cleaned = re.sub(r"[\s']", "", cleaned).replace(",", ".")
    return float(cleaned)


def _row_numbers(tail: str) -> list:
    """Numbers in a rate row, with the parenthesised (delta) columns removed."""
    tail = re.sub(r"\([^)]*\)", " ", tail)
    out = []
    for raw in re.findall(_NUM, tail):
        try:
            out.append(_to_float(raw))
        except ValueError:
            continue
    return out


def _parse_section(section: str) -> dict:
    """Pulls {code: {"buy": float, "sell": float}} out of the rate panel's text."""
    result = {}
    for key, (code, name) in CURRENCIES.items():
        patterns = [
            # "Доллар США  1  USD  11 724.0000 (-14.0000)  11 856.0000 (0.0000)"
            r"^[^\n]*?\b1\s*" + code + r"\b([^\n]*)$",
            # Fallback if the ISO code column ever disappears; the negative
            # lookahead drops the cross-rate rows, which repeat the same names.
            r"^(?![^\n]*\u2192)[^\n]*?" + re.escape(name) + r"\b([^\n]*)$",
        ]
        for pattern in patterns:
            for m in re.finditer(pattern, section, re.MULTILINE | re.UNICODE):
                nums = _row_numbers(m.group(1))
                if len(nums) < 2 or nums[0] <= 0 or nums[1] <= 0:
                    continue
                result[key] = {"buy": nums[0], "sell": nums[1]}
                break
            if key in result:
                break
    return result


def _slice_section(full_text: str) -> str:
    """The 'Коммерческий курс' block, excluding the 'Кросс курс' table below it."""
    cross = full_text.find("Кросс курс")
    head = full_text.rfind("Коммерческий курс", 0, cross if cross != -1 else len(full_text))
    if head == -1:
        return full_text
    return full_text[head:cross] if cross != -1 else full_text[head:]


def _extract_rates(full_text: str) -> dict:
    rates = _parse_section(_slice_section(full_text))
    if "usd" not in rates:
        # Section slicing can fail if the headings change; retry on everything.
        rates = _parse_section(full_text) or rates
    return rates


# --- debug artefacts -------------------------------------------------------

def _prune_debug_dir():
    try:
        files = sorted(DEBUG_DIR.glob("*.*"), key=lambda p: p.stat().st_mtime, reverse=True)
        for stale in files[DEBUG_KEEP_FILES:]:
            stale.unlink(missing_ok=True)
    except Exception:
        logger.debug("Could not prune %s", DEBUG_DIR, exc_info=True)


async def _save_debug_screenshot(page, name: str, page_text: str = ""):
    try:
        DEBUG_DIR.mkdir(exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = DEBUG_DIR / f"{ts}_{name}.png"
        await page.screenshot(path=str(path), full_page=True)
        logger.info("Saved debug screenshot: %s", path)
        if page_text:
            # The screenshot alone never showed why a parse failed; keep the text too.
            (DEBUG_DIR / f"{ts}_{name}.txt").write_text(page_text, encoding="utf-8")
        _prune_debug_dir()
    except Exception:
        logger.exception("Could not save debug screenshot")


# --- the "Дата" field ------------------------------------------------------

async def _find_date_input(info_frame):
    """The panel's date box, located by its dd.mm.yyyy value rather than a guessed id."""
    inputs = info_frame.locator('input[type="text"], input:not([type])')
    try:
        count = await inputs.count()
    except Exception:
        return None, None
    for i in range(min(count, 20)):
        el = inputs.nth(i)
        try:
            value = (await el.input_value(timeout=1000) or "").strip()
        except Exception:
            continue
        if re.fullmatch(r"\d{2}\.\d{2}\.\d{4}", value):
            return el, value
    return None, None


PANEL_SETTLE_MS = 600


async def _reload_panel_for_date(page, info_frame, date_input, value: str):
    legend = info_frame.locator("legend", has_text="Коммерческий курс").first
    await date_input.fill(value)
    await date_input.press("Enter")

    # The panel hides itself while it reloads. Waiting for that transition is
    # both faster and safer than a fixed sleep: it is positive proof the bank
    # accepted the new date. Without it we can read the PREVIOUS date's numbers
    # under the new date's label — measured, not hypothetical.
    try:
        await legend.wait_for(state="hidden", timeout=15000)
    except PWTimeoutError:
        try:
            await date_input.dispatch_event("change")  # some builds only bind onchange
            await legend.wait_for(state="hidden", timeout=15000)
        except Exception:
            raise ScrapeError(
                f"Панель курса не перезагрузилась после смены даты на {value}; "
                "цифры могли остаться от предыдущей даты."
            )

    # Changing the date resets the rate type to "Курс ЦБ" (#Currence_curs back to
    # 1), and the commercial fieldset never reappears on its own — waiting just
    # times out on a hidden <legend> — so pick the commercial type again.
    selector = info_frame.locator("#Currence_curs")
    await selector.wait_for(timeout=30000)
    if await selector.input_value() != "2":
        await selector.select_option("2")
    else:
        await selector.dispatch_event("change")

    await legend.wait_for(timeout=120000)
    await page.wait_for_timeout(PANEL_SETTLE_MS)


async def _rates_with_date_fallback(page, info_frame) -> tuple:
    """Rates for the panel's default date, walking back a few days if USD is not published yet."""
    full_text = await info_frame.locator("body").inner_text()
    rates = _extract_rates(full_text)
    date_input, shown_date = await _find_date_input(info_frame)

    if "usd" in rates or date_input is None or shown_date is None:
        return rates, shown_date, full_text

    # Overnight the bank opens the next operational day with EUR/RUB filled in but
    # no USD row yet; the previous day's rate is what everyone actually quotes.
    logger.warning("No USD row for %s, walking the date back...", shown_date)
    try:
        cursor = datetime.strptime(shown_date, "%d.%m.%Y")
    except ValueError:
        return rates, shown_date, full_text

    for _ in range(MAX_DATE_FALLBACK_DAYS):
        cursor -= timedelta(days=1)
        candidate = cursor.strftime("%d.%m.%Y")
        try:
            await _reload_panel_for_date(page, info_frame, date_input, candidate)
        except Exception:
            logger.warning("Could not load the panel for %s", candidate, exc_info=True)
            break
        full_text = await info_frame.locator("body").inner_text()
        rates = _extract_rates(full_text)
        if "usd" in rates:
            logger.info("Found the USD rate on the fallback date %s", candidate)
            return rates, candidate, full_text

    return rates, shown_date, full_text


# --- screenshots -----------------------------------------------------------

async def _capture_panel(page, info_frame):
    """Screenshot of the rate panel, degrading to the frame and then the whole page.

    The bot's whole point is posting a picture of the rate, so never give up after
    only the first (narrowest) locator fails.
    """
    attempts = [
        (
            "panel",
            lambda: info_frame.locator("fieldset")
            .filter(has=info_frame.locator("legend", has_text="Коммерческий курс"))
            .first.screenshot(timeout=8000),
        ),
        ("frame", lambda: info_frame.locator("body").screenshot(timeout=8000)),
        ("page", lambda: page.screenshot(timeout=8000)),
    ]
    for label, take in attempts:
        try:
            shot = await take()
            if shot:
                logger.info("Captured the rate screenshot (%s, %d bytes)", label, len(shot))
                return shot
        except Exception:
            logger.warning("Screenshot via %s failed, trying a wider shot", label, exc_info=True)
    logger.error("Every screenshot strategy failed, the message will be text-only")
    return None


# --- login / fetch ---------------------------------------------------------

_PERMANENT_DIALOG_MARKERS = (
    "неверный логин",
    "неверный пароль",
    "неправильный пароль",
    "заблокирован",
    "блокирован",
)

MAC_NOT_READY_MARKER = "MAC-адрес не определён"


def _classify_login_failure(dialog_messages: list) -> ScrapeError:
    extra = f" Сообщение со страницы: {dialog_messages[-1]!r}" if dialog_messages else ""
    haystack = " ".join(dialog_messages).lower()
    if any(marker in haystack for marker in _PERMANENT_DIALOG_MARKERS):
        return PermanentScrapeError(
            "CIB отказал во входе — проверьте логин/пароль в credentials.json "
            "или разблокируйте учётную запись в банке." + extra
        )
    return ScrapeError(
        "Не удалось войти в CIB (не появился пункт меню 'Выход')."
        + extra
        + " Проверьте логин/пароль или измените селекторы в bank_scraper.py."
    )


async def _logout(page, dialog_state: dict):
    """Best-effort logout. Must always run: a leaked session makes CIB serve the
    next login a blank page, which is what turned single failures into whole
    runs of failures (5 attempts, then an empty-page 'unexpected_error')."""
    try:
        # "Выход" raises a confirm(); the default dialog handler dismisses every
        # dialog, which answers "Cancel" and silently leaves the session open.
        dialog_state["accept"] = True
        await page.keyboard.press("Escape")
        await page.click("text=Выход", timeout=5000)
        # Back at the login form means the session was really closed.
        await page.wait_for_selector('input[name="login"]', timeout=15000)
        logger.info("Logged out of CIB")
    except Exception:
        logger.warning("Logout did not confirm — the CIB session may still be open", exc_info=True)
    finally:
        dialog_state["accept"] = False


async def fetch_rates(
    bank_url: str,
    login: str,
    password: str,
    headless: bool = True,
    target_date: str | None = None,
) -> tuple:
    """Logs into CIB, opens Инфо -> Коммерческий курс, and returns parsed rates.

    target_date ("dd.mm.yyyy") asks the panel for a specific day instead of the
    one it opens on. If the bank published nothing for that day (weekend,
    holiday), the walk-back finds the nearest earlier day and "rate_date" says
    which day the numbers actually belong to.

    Returns: (rates, screenshot_png_bytes_or_None)
    rates = {"usd": {"buy": float, "sell": float}, "eur": {...}, "rub": {...},
             "rate_date": "dd.mm.yyyy", "fetched_at": ...}
    """
    ensure_iabs_client_running()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=headless, args=CHROMIUM_ARGS)
        context = await browser.new_context(viewport={"width": 1600, "height": 1000}, ignore_https_errors=True)
        page = await context.new_page()

        dialog_messages = []
        logged_in = False
        dialog_state = {"accept": False}

        async def _handle_dialog(dialog):
            dialog_messages.append(dialog.message)
            if dialog_state["accept"]:
                logger.info("Confirming dialog: %s", dialog.message)
                await dialog.accept()
            else:
                logger.warning("Browser dialog appeared: %s", dialog.message)
                await dialog.dismiss()

        page.on("dialog", lambda d: asyncio.ensure_future(_handle_dialog(d)))

        try:
            for attempt in range(3):
                try:
                    await page.goto(bank_url, wait_until="domcontentloaded", timeout=60000)
                    await page.wait_for_selector('input[name="login"]', timeout=40000)
                    break
                except PWTimeoutError:
                    if attempt == 2:
                        # A blank login page means CIB is refusing this client for
                        # now (usually leaked sessions); that is transient, so
                        # report it as a ScrapeError instead of an opaque crash.
                        await _save_debug_screenshot(page, "login_page_unavailable")
                        raise ScrapeError(
                            "Страница входа CIB не загрузилась (форма логина не появилась). "
                            "Обычно это временно — банк отказывает после серии входов."
                        )
                    logger.warning("Page load timed out (attempt %d/3), retrying...", attempt + 1)

            # Despite the page being titled "Корпоративный Интернет-Банкинг", this
            # account's login is registered under the "Обычный" (ch_sys=1) option in
            # the dropdown, not "Корпоративный" (ch_sys=2) — confirmed by testing.
            await page.select_option('select[name="ch_sys"]', "1")
            await page.fill('input[name="login"]', login)
            await page.fill('input[name="password"]', password)
            await page.click("#sButton")

            try:
                await page.wait_for_selector("text=Выход", timeout=40000)
                logged_in = True
            except PWTimeoutError:
                await _save_debug_screenshot(page, "login_failed")
                raise _classify_login_failure(dialog_messages)

            # A "Сообщения" popup often opens right after login and blocks the menu.
            try:
                await page.click(".modal-close", timeout=1500)
            except PWTimeoutError:
                pass

            # The Инфо panel's rate table loads over AJAX and reliably takes ~15-20s to
            # populate. Open it once and wait it out — reopening it restarts the load
            # and never lets it finish, which is why a short retry-by-reopening loop
            # would time out every single attempt.
            try:
                await page.click('text="Инфо"', timeout=10000)
            except PWTimeoutError:
                await page.click("text=Инфо", timeout=10000)

            # The Инфо content (including the rate-type dropdown) is rendered inside a
            # child <iframe src=".../user/info.jsp">, not the main document — every
            # page-level selector for it was silently searching the wrong frame.
            info_frame = page.frame_locator('iframe[src*="info.jsp"]')

            # The <select> itself appears quickly — switch to "Коммерческий курс"
            # (value=2) right away instead of waiting for the default "Курс ЦБ" data
            # to finish loading first, which would waste one whole slow AJAX round trip.
            await info_frame.locator("#Currence_curs").wait_for(timeout=40000)
            await info_frame.locator("#Currence_curs").select_option("2")

            # (A plain text locator would also match the still-in-DOM <option>, which
            # is never "visible" — target the actual <legend> heading instead.)
            await info_frame.locator("legend", has_text="Коммерческий курс").first.wait_for(timeout=180000)
            await page.wait_for_timeout(500)  # let numbers finish rendering

            if target_date:
                date_input, _ = await _find_date_input(info_frame)
                if date_input is None:
                    raise ScrapeError("Не удалось найти поле 'Дата' на панели курса.")
                logger.info("Requesting the rate panel for %s", target_date)
                await _reload_panel_for_date(page, info_frame, date_input, target_date)

            rates, rate_date, full_text = await _rates_with_date_fallback(page, info_frame)

            if "usd" not in rates:
                await _save_debug_screenshot(page, "parse_failed", full_text)
                for_date = f" на {target_date}" if target_date else ""
                raise ScrapeError(
                    f"Банк не опубликовал коммерческий курс доллара{for_date} "
                    f"(на странице только: {', '.join(sorted(rates)) or 'ничего'}). "
                    "См. debug_screenshots/."
                )

            rates["rate_date"] = rate_date
            rates["fetched_at"] = datetime.now().isoformat()
            logger.info(
                "Parsed rates for %s: %s",
                rate_date,
                {k: v for k, v in rates.items() if isinstance(v, dict)},
            )

            screenshot_bytes = await _capture_panel(page, info_frame)
            return rates, screenshot_bytes
        except ScrapeError:
            raise
        except Exception as e:
            await _save_debug_screenshot(page, "unexpected_error")
            raise ScrapeError(f"Неожиданная ошибка при получении курса: {e}") from e
        finally:
            # Logging out used to sit after the `return`, so it was skipped on every
            # failure and each failed attempt leaked a live CIB session.
            if logged_in:
                await _logout(page, dialog_state)
            try:
                await context.close()
            finally:
                await browser.close()


ATTEMPT_TIMEOUT = 330.0  # a hung browser must not wedge the bot forever


async def fetch_rates_with_retry(
    bank_url: str,
    login: str,
    password: str,
    headless: bool = True,
    max_attempts: int = 5,
    retry_delay: float = 15.0,
    target_date: str | None = None,
) -> tuple:
    """Same as fetch_rates, but retries transient failures with backoff.

    iABS7-Client sometimes reports its port as open before it's actually ready
    to answer the bank's MAC-address check, which makes the very first login
    attempt fail with "MAC-адрес не определён" even though a retry a few
    seconds later succeeds. Retry on any transient ScrapeError; a
    PermanentScrapeError (bad password, blocked account, missing helper) stops
    immediately instead of burning the whole delay budget.
    """
    last_error = None
    for attempt in range(1, max_attempts + 1):
        try:
            return await asyncio.wait_for(
                fetch_rates(bank_url, login, password, headless=headless, target_date=target_date),
                timeout=ATTEMPT_TIMEOUT,
            )
        except PermanentScrapeError:
            raise
        except (ScrapeError, asyncio.TimeoutError) as e:
            if isinstance(e, asyncio.TimeoutError):
                e = ScrapeError(f"Получение курса не завершилось за {ATTEMPT_TIMEOUT:.0f}с.")
            last_error = e
            if attempt == max_attempts:
                break
            if MAC_NOT_READY_MARKER in str(e):
                wait = retry_delay
            else:
                # CIB needs time to release the previous session; hammering it
                # every 3s was what produced the blank-page failures.
                wait = min(10.0 * 2 ** (attempt - 1), 90.0) + random.uniform(0, 3)
            logger.warning(
                "fetch_rates attempt %d/%d failed (%s), retrying in %.0fs...",
                attempt, max_attempts, e, wait,
            )
            await asyncio.sleep(wait)
    raise last_error


if __name__ == "__main__":
    import json
    import sys

    logging.basicConfig(level=logging.INFO)
    creds = json.loads((Path(__file__).parent / "credentials.json").read_text(encoding="utf-8"))
    config = json.loads((Path(__file__).parent / "config.json").read_text(encoding="utf-8"))
    bank_url = config.get("bank_url", "https://ibank.trustbank.uz:224/cib/index_09030.jsp")
    headless = "--headed" not in sys.argv
    # Optional dd.mm.yyyy argument: python bank_scraper.py 10.09.2026
    target_date = next((a for a in sys.argv[1:] if re.fullmatch(r"\d{2}\.\d{2}\.\d{4}", a)), None)

    t0 = time.monotonic()
    rates, screenshot_bytes = asyncio.run(
        fetch_rates_with_retry(
            bank_url, creds["login"], creds["password"], headless=headless, target_date=target_date
        )
    )
    print(f"Elapsed: {time.monotonic() - t0:.1f}s")
    print(json.dumps(rates, indent=2, ensure_ascii=False))
    if screenshot_bytes:
        out = Path(__file__).parent / "panel_preview.png"
        out.write_bytes(screenshot_bytes)
        print(f"Panel screenshot saved to {out} ({len(screenshot_bytes)} bytes)")
    else:
        print("No panel screenshot captured.")
