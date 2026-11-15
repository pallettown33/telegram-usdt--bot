import asyncio
from concurrent.futures import ThreadPoolExecutor, as_completed
import hmac
import logging
import os
import re
import requests
import threading
import hashlib
import sqlite3
import time
from datetime import datetime, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from ecdsa import SECP256k1, VerifyingKey
from telegram import (
    BotCommand,
    BotCommandScopeAllGroupChats,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.error import InvalidToken, TelegramError

load_dotenv()

from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

class HealthCheckHandler(BaseHTTPRequestHandler):
    """Render Web Service 헬스체크용 더미 HTTP 바인딩 레스폰더"""
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")
    def log_message(self, format, *args):
        return

def start_health_check_server():
    port = int(os.getenv("PORT", 8080))
    try:
        server = HTTPServer(('0.0.0.0', port), HealthCheckHandler)
        server.serve_forever()
    except Exception as e:
        logging.warning(f"Health check server port {port} binding failed (already in use or unavailable): {e}")

# 로깅 설정
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)

# 봇 토큰 (환경 변수 전용 - 보안을 위해 하드코딩 금지)
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
PRICE_CACHE = {}
CHAT_STATS_DB = os.getenv("CHAT_STATS_DB_PATH", "chat_stats.sqlite3")
KST = ZoneInfo("Asia/Seoul")
CHAT_ADMIN_IDS = {33067423, 67724357, 7795437617, 8754993619}
REPEAT_MIN_INTERVAL = 10
REPEAT_MAX_INTERVAL = 86400
REPEAT_TASKS = {}


def init_chat_stats_db():
    with sqlite3.connect(CHAT_STATS_DB) as db:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS chat_message_stats (
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                display_name TEXT NOT NULL,
                username TEXT NOT NULL DEFAULT '',
                message_date TEXT NOT NULL,
                message_count INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (chat_id, user_id, message_date)
            )
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS chat_settings (
                chat_id INTEGER PRIMARY KEY,
                recording_enabled INTEGER NOT NULL DEFAULT 1
            )
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS chat_messages (
                chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                PRIMARY KEY (chat_id, message_id)
            )
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS repeat_messages (
                repeat_id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                owner_id INTEGER NOT NULL,
                interval_seconds INTEGER NOT NULL,
                message_text TEXT NOT NULL,
                button_text TEXT NOT NULL DEFAULT '',
                button_url TEXT NOT NULL DEFAULT '',
                active INTEGER NOT NULL DEFAULT 1
            )
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS wallet_watches (
                watch_id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                chain TEXT NOT NULL,
                account_key TEXT NOT NULL,
                initialized INTEGER NOT NULL DEFAULT 0,
                active INTEGER NOT NULL DEFAULT 1,
                created_at REAL NOT NULL
            )
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS wallet_addresses (
                watch_id INTEGER NOT NULL,
                branch INTEGER NOT NULL,
                address_index INTEGER NOT NULL,
                address TEXT NOT NULL,
                PRIMARY KEY (watch_id, branch, address_index),
                UNIQUE (watch_id, address)
            )
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS wallet_events (
                watch_id INTEGER NOT NULL,
                event_key TEXT NOT NULL,
                txid TEXT NOT NULL,
                address TEXT NOT NULL,
                direction TEXT NOT NULL,
                amount REAL NOT NULL,
                confirmed INTEGER NOT NULL DEFAULT 1,
                observed_at REAL NOT NULL,
                PRIMARY KEY (watch_id, event_key)
            )
            """
        )


def parse_repeat_command(text):
    match = re.fullmatch(r"\.반복\s+(\d+)\s+(.+)", text or "", re.DOTALL)
    if not match:
        return None
    interval = int(match.group(1))
    fields = [field.strip() for field in match.group(2).split("|")]
    if len(fields) == 1:
        message_text, button_text, button_url = fields[0], "", ""
    elif len(fields) == 3:
        message_text, button_text, button_url = fields
    else:
        return None
    parsed_url = urlparse(button_url) if button_url else None
    if not message_text or len(message_text) > 3500 or interval < REPEAT_MIN_INTERVAL or interval > REPEAT_MAX_INTERVAL:
        return None
    if button_url and (not button_text or len(button_text) > 64 or parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc):
        return None
    return interval, message_text, button_text, button_url


def repeat_help_text():
    return (
        "사용법:\n"
        ".반복 초 문구\n"
        ".반복 초 문구 | 버튼명 | https://주소\n\n"
        f"초는 {REPEAT_MIN_INTERVAL}~{REPEAT_MAX_INTERVAL} 범위입니다.\n"
        ".반복목록 · .반복중지 번호 · .반복전체중지"
    )


def repeat_markup(button_text, button_url):
    if not button_text or not button_url:
        return None
    return InlineKeyboardMarkup([[InlineKeyboardButton(button_text, url=button_url)]])


def get_repeat(repeat_id, chat_id):
    with sqlite3.connect(CHAT_STATS_DB) as db:
        return db.execute(
            "SELECT repeat_id, interval_seconds, message_text, button_text, button_url FROM repeat_messages "
            "WHERE repeat_id = ? AND chat_id = ? AND active = 1",
            (repeat_id, chat_id),
        ).fetchone()


async def repeat_message_loop(app, repeat_id, chat_id):
    while True:
        row = get_repeat(repeat_id, chat_id)
        if not row:
            return
        await asyncio.sleep(row[1])
        row = get_repeat(repeat_id, chat_id)
        if not row:
            return
        try:
            await app.bot.send_message(
                chat_id=chat_id,
                text=row[2],
                reply_markup=repeat_markup(row[3], row[4]),
            )
        except TelegramError as error:
            logging.warning("repeat message %s failed: %s", repeat_id, error)


def start_repeat_task(app, repeat_id, chat_id):
    task = REPEAT_TASKS.get(repeat_id)
    if task and not task.done():
        return
    REPEAT_TASKS[repeat_id] = app.create_task(repeat_message_loop(app, repeat_id, chat_id))


def stop_repeat_task(repeat_id):
    task = REPEAT_TASKS.pop(repeat_id, None)
    if task and not task.done():
        task.cancel()


async def repeat_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    chat = update.effective_chat
    user = update.effective_user
    if not message or not chat or not user:
        return
    if chat.type not in {"group", "supergroup"}:
        await message.reply_text(".반복은 그룹방에서만 사용할 수 있습니다.")
        return
    if user.id not in CHAT_ADMIN_IDS:
        await message.reply_text("이 기능은 지정된 봇 관리자만 사용할 수 있습니다.")
        return
    parsed = parse_repeat_command(message.text)
    if not parsed:
        await message.reply_text(repeat_help_text())
        return
    interval, message_text, button_text, button_url = parsed
    with sqlite3.connect(CHAT_STATS_DB) as db:
        cursor = db.execute(
            "INSERT INTO repeat_messages "
            "(chat_id, owner_id, interval_seconds, message_text, button_text, button_url) VALUES (?, ?, ?, ?, ?, ?)",
            (chat.id, user.id, interval, message_text, button_text, button_url),
        )
        repeat_id = cursor.lastrowid
    start_repeat_task(context.application, repeat_id, chat.id)
    await message.reply_text(f"반복 문구 #{repeat_id}를 등록했습니다. {interval}초마다 전송합니다.")


async def repeat_list_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    chat = update.effective_chat
    user = update.effective_user
    if not message or not chat or not user:
        return
    if user.id not in CHAT_ADMIN_IDS:
        await message.reply_text("이 기능은 지정된 봇 관리자만 사용할 수 있습니다.")
        return
    with sqlite3.connect(CHAT_STATS_DB) as db:
        rows = db.execute(
            "SELECT repeat_id, interval_seconds, message_text, button_url FROM repeat_messages "
            "WHERE chat_id = ? AND active = 1 ORDER BY repeat_id",
            (chat.id,),
        ).fetchall()
    if not rows:
        await message.reply_text("현재 반복 문구가 없습니다.")
        return
    lines = ["🔁 현재 반복 문구"]
    for repeat_id, interval, text, button_url in rows:
        suffix = " · 버튼 포함" if button_url else ""
        lines.append(f"#{repeat_id} · {interval}초 · {text[:60]}{suffix}")
    await message.reply_text("\n".join(lines))


async def repeat_stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    chat = update.effective_chat
    user = update.effective_user
    if not message or not chat or not user:
        return
    if user.id not in CHAT_ADMIN_IDS:
        await message.reply_text("이 기능은 지정된 봇 관리자만 사용할 수 있습니다.")
        return
    match = re.fullmatch(r"\.반복중지\s+(\d+)", message.text or "")
    if not match:
        await message.reply_text("사용법: .반복중지 번호")
        return
    repeat_id = int(match.group(1))
    with sqlite3.connect(CHAT_STATS_DB) as db:
        changed = db.execute(
            "UPDATE repeat_messages SET active = 0 WHERE repeat_id = ? AND chat_id = ? AND active = 1",
            (repeat_id, chat.id),
        ).rowcount
    if changed:
        stop_repeat_task(repeat_id)
        await message.reply_text(f"반복 문구 #{repeat_id}를 중지했습니다.")
    else:
        await message.reply_text("해당 반복 문구를 찾을 수 없습니다.")


async def repeat_stop_all_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    chat = update.effective_chat
    user = update.effective_user
    if not message or not chat or not user:
        return
    if user.id not in CHAT_ADMIN_IDS:
        await message.reply_text("이 기능은 지정된 봇 관리자만 사용할 수 있습니다.")
        return
    with sqlite3.connect(CHAT_STATS_DB) as db:
        rows = db.execute(
            "SELECT repeat_id FROM repeat_messages WHERE chat_id = ? AND active = 1",
            (chat.id,),
        ).fetchall()
        changed = db.execute(
            "UPDATE repeat_messages SET active = 0 WHERE chat_id = ? AND active = 1",
            (chat.id,),
        ).rowcount
    for (repeat_id,) in rows:
        stop_repeat_task(repeat_id)
    await message.reply_text(f"반복 문구 {changed}개를 중지했습니다.")


def chat_recording_enabled(chat_id):
    with sqlite3.connect(CHAT_STATS_DB) as db:
        row = db.execute(
            "SELECT recording_enabled FROM chat_settings WHERE chat_id = ?",
            (chat_id,),
        ).fetchone()
    return row is None or bool(row[0])


def set_chat_recording(chat_id, enabled):
    with sqlite3.connect(CHAT_STATS_DB) as db:
        db.execute(
            """
            INSERT INTO chat_settings (chat_id, recording_enabled) VALUES (?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET recording_enabled = excluded.recording_enabled
            """,
            (chat_id, int(enabled)),
        )


async def record_chat_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    user = update.effective_user
    chat = update.effective_chat
    if not message or not user or not chat:
        return
    if not chat_recording_enabled(chat.id):
        with sqlite3.connect(CHAT_STATS_DB) as db:
            db.execute(
                "INSERT OR IGNORE INTO chat_messages (chat_id, message_id, user_id) VALUES (?, ?, ?)",
                (chat.id, message.message_id, user.id),
            )
        return

    today = datetime.now(KST).date().isoformat()
    with sqlite3.connect(CHAT_STATS_DB) as db:
        db.execute(
            """
            INSERT INTO chat_message_stats
                (chat_id, user_id, display_name, username, message_date, message_count)
            VALUES (?, ?, ?, ?, ?, 1)
            ON CONFLICT(chat_id, user_id, message_date) DO UPDATE SET
                display_name = excluded.display_name,
                username = excluded.username,
                message_count = message_count + 1
            """,
            (chat.id, user.id, user.full_name or "이름 없음", user.username or "", today),
        )
        db.execute(
            "INSERT OR IGNORE INTO chat_messages (chat_id, message_id, user_id) VALUES (?, ?, ?)",
            (chat.id, message.message_id, user.id),
        )


def chat_rank_period(period):
    today = datetime.now(KST).date()
    if period == "day":
        return "일간", today.isoformat()
    if period == "week":
        # Sunday 00:00 through Saturday 23:59 in Korea time.
        days_since_sunday = (today.weekday() + 1) % 7
        return "주간", (today - timedelta(days=days_since_sunday)).isoformat()
    if period == "month":
        return "월간", today.replace(day=1).isoformat()
    return "전체", None


def get_chat_ranking(chat_id, period):
    label, start_date = chat_rank_period(period)
    query = """
        SELECT display_name, username, user_id, SUM(message_count) AS message_count
        FROM chat_message_stats
        WHERE chat_id = ?
    """
    params = [chat_id]
    if start_date:
        query += " AND message_date >= ?"
        params.append(start_date)
    query += " GROUP BY user_id ORDER BY message_count DESC, user_id ASC LIMIT 5"
    with sqlite3.connect(CHAT_STATS_DB) as db:
        rows = db.execute(query, params).fetchall()
    return label, rows


def chat_ranking_markup(period, owner_id):
    labels = {"day": "일간", "week": "주간", "month": "월간", "all": "전체"}
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(("✓ " if period == key else "") + label, callback_data=f"chat_rank:{key}:{owner_id}")
        for key, label in labels.items()
    ]])


def chat_ranking_text(chat, period):
    label, rows = get_chat_ranking(chat.id, period)
    chat_name = chat.title or chat.full_name or "현재 채팅방"
    lines = [f"📊 {chat_name}", f"채팅량 · {label} 채팅 건수 순위", ""]
    if not rows:
        lines.append("아직 집계된 채팅이 없습니다.")
    else:
        for rank, (display_name, username, user_id, count) in enumerate(rows, 1):
            account = username or "사용자명없음"
            lines.append(f"{rank}위 {display_name} {account} {user_id}  {count:,}개")
    return "\n".join(lines)


async def chat_ranking_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message and update.effective_chat and update.effective_user:
        await update.message.reply_text(
            chat_ranking_text(update.effective_chat, "day"),
            reply_markup=chat_ranking_markup("day", update.effective_user.id),
        )


async def chat_ranking_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    chat = update.effective_chat
    if not query or not chat:
        return
    parts = query.data.split(":")
    period, owner_id = parts[1], int(parts[2])
    if not query.from_user or query.from_user.id != owner_id:
        await query.answer("이 .채팅 명령을 실행한 사람만 조작할 수 있습니다.", show_alert=True)
        return
    await query.answer()
    await query.edit_message_text(
        chat_ranking_text(chat, period),
        reply_markup=chat_ranking_markup(period, owner_id),
    )


async def toggle_chat_recording(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    chat = update.effective_chat
    user = update.effective_user
    if not message or not chat or not user:
        return
    if user.id not in CHAT_ADMIN_IDS:
        await message.reply_text("이 기능은 지정된 봇 관리자만 사용할 수 있습니다.")
        return
    enabled = message.text.endswith("on")
    set_chat_recording(chat.id, enabled)
    await message.reply_text(f"채팅 기록을 {'켰습니다' if enabled else '껐습니다'}.")


def request_json(url, timeout=3, headers=None):
    """짧은 재시도로 일시적인 시세 API 오류 완화"""
    last_error = None
    for _ in range(2):
        try:
            response = requests.get(url, headers=headers, timeout=timeout)
            response.raise_for_status()
            return response.json()
        except Exception as error:
            last_error = error
    raise last_error

def save_price(asset, price):
    price["source"] = price.get("source", "실시간")
    PRICE_CACHE[asset] = price
    return price

def cached_price(asset):
    price = PRICE_CACHE.get(asset)
    if price:
        return {**price, "source": "마지막 정상값"}
    return None

def price_details(price):
    if price.get("high") is None or price.get("low") is None:
        return f"• **출처:** {price.get('source', '대체 시세')}"
    return f"• **고가:** {price['high']:,.0f} KRW | **저가:** {price['low']:,.0f} KRW"

def equivalent_lines(krw_amount, source_asset, source_price):
    """입력 코인의 원화 금액을 다른 지원 코인 수량으로 환산"""
    getters = {
        "BTC": get_btc_price,
        "USDT": get_usdt_price,
        "TON": get_ton_price,
        "TRX": get_trx_price,
        "LTC": get_ltc_price,
    }
    decimals = {"BTC": 8, "USDT": 2, "TON": 2, "TRX": 2, "LTC": 4}
    prices = {source_asset: source_price}
    for asset, getter in getters.items():
        if asset != source_asset:
            prices[asset] = getter()

    lines = ["📊 **다른 코인 환산**"]
    for asset in ("BTC", "USDT", "TON", "TRX", "LTC"):
        info = prices.get(asset)
        if not info or not info.get("krw"):
            continue
        quantity = krw_amount / info["krw"]
        quantity_text = f"{quantity:,.{decimals[asset]}f}".rstrip("0").rstrip(".")
        label = "Gram (TON)" if asset == "TON" else asset
        lines.append(f"• **{label}:** {quantity_text} {label}")
    return "\n".join(lines)

def get_coingecko_price(asset, coin_id):
    data = request_json(
        f"https://api.coingecko.com/api/v3/simple/price?ids={coin_id}&vs_currencies=krw,usd&include_24hr_change=true",
        timeout=5,
        headers={"User-Agent": "Mozilla/5.0"},
    ).get(coin_id, {})
    return save_price(asset, {
        "krw": data["krw"],
        "usd": data.get("usd", 0),
        "change_rate": data.get("krw_24h_change", 0),
        "high": None,
        "low": None,
        "source": "CoinGecko 대체 시세",
    })

def get_google_finance_price(asset, symbol):
    """Google Finance USD 시세와 USD/KRW 환율로 원화 시세 계산."""
    headers = {"User-Agent": "Mozilla/5.0"}

    def quote(url):
        response = requests.get(url, headers=headers, timeout=5)
        response.raise_for_status()
        match = re.search(
            r'class="N6SYTe"[^>]*>.*?<span jsname="Pdsbrc"[^>]*>.*?<span[^>]*>([0-9][0-9,]*(?:\.[0-9]+)?)</span>'
            r'|class="YMlKec fxKbKc"[^>]*>([^<]+)',
            response.text,
            re.DOTALL,
        )
        if not match:
            raise ValueError(f"Google Finance price not found: {url}")
        return float(next(group for group in match.groups() if group).replace(",", "").replace("$", "").strip())

    # Google Finance has no USDT quote; USDT is valued at its USD peg.
    usd_price = 1.0 if symbol == "USDT" else quote(f"https://www.google.com/finance/quote/{symbol}-USD")
    usd_krw = quote("https://www.google.com/finance/quote/USD-KRW")
    return save_price(asset, {
        "krw": usd_price * usd_krw,
        "usd": usd_price,
        "change_rate": 0,
        "high": None,
        "low": None,
        "source": "Google Finance 기준",
    })


def parse_krw(text: str) -> float | None:
    """
    한글 금액 단위(억, 만, 천, 원) 및 숫자(쉼표 포함)를 해석하여 float(원)으로 변환.
    예: '1억 5천만' -> 150000000.0, '5000만원' -> 50000000.0, '50000000' -> 50000000.0
    """
    if not text:
        return None
    raw = text.replace(",", "").strip()
    
    # 순수 숫자인 경우
    if re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", raw):
        return float(raw)
    
    # 단원/통화 관련 단어 제거
    cleaned = re.sub(r"(?:원|KRW|krw|btc|비트코인|비트|usdt|테더|ton|톤|trx|트론|ltc|라이트코인|라이트)", "", raw, flags=re.IGNORECASE).strip()
    if not cleaned:
        return None

    total = 0.0
    matched = False

    # 억 (100,000,000)
    match_eok = re.search(r"([0-9.]+)\s*억", cleaned)
    if match_eok:
        total += float(match_eok.group(1)) * 100_000_000
        matched = True
        cleaned = re.sub(r"[0-9.]+\s*억", "", cleaned)

    # 만 (10,000) - '5천만', '5000만', '5백만', '50만' 등 처리
    match_man = re.search(r"([0-9.]+|[0-9.]+\s*천|[0-9.]+\s*백)?\s*만", cleaned)
    if match_man:
        sub_str = match_man.group(1) if match_man.group(1) else "1"
        sub_val = 0.0
        if "천" in sub_str:
            sub_val = float(sub_str.replace("천", "").strip()) * 1000
        elif "백" in sub_str:
            sub_val = float(sub_str.replace("백", "").strip()) * 100
        else:
            sub_val = float(sub_str.strip())
        total += sub_val * 10_000
        matched = True
        cleaned = re.sub(r"([0-9.]+|[0-9.]+\s*천|[0-9.]+\s*백)?\s*만", "", cleaned)

    # 천 (1,000)
    match_cheon = re.search(r"([0-9.]+)\s*천", cleaned)
    if match_cheon:
        total += float(match_cheon.group(1)) * 1_000
        matched = True
        cleaned = re.sub(r"[0-9.]+\s*천", "", cleaned)

    # 잔여 숫자
    cleaned_rest = cleaned.strip()
    if cleaned_rest and re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", cleaned_rest):
        total += float(cleaned_rest)
        matched = True

    return total if matched else None

def format_krw_readable(amount: float) -> str:
    """원화 금액을 조·억·만 단위로 읽기 쉽게 변환."""
    remaining = int(amount)
    parts = []
    for unit, value in (("경", 10**16), ("조", 10**12), ("억", 10**8), ("만", 10**4)):
        count, remaining = divmod(remaining, value)
        if count:
            parts.append(f"{count:,}{unit}")
    if remaining or not parts:
        parts.append(f"{remaining:,}원")
    else:
        parts[-1] += "원"
    return " ".join(parts)

def get_usdt_price():
    """Google Finance 기준 USDT 원화 시세 조회"""
    try:
        return get_google_finance_price("USDT", "USDT")
    except Exception as e:
        logging.error(f"Error fetching USDT price: {e}")
        return cached_price("USDT")

def get_btc_price():
    """Google Finance 기준 BTC 원화 시세 조회"""
    try:
        return get_google_finance_price("BTC", "BTC")
    except Exception as e:
        logging.error(f"Error fetching BTC price: {e}")
        return cached_price("BTC")

def get_ltc_price():
    """Google Finance 기준 LTC 원화 시세 조회"""
    try:
        return get_google_finance_price("LTC", "LTC")
    except Exception as e:
        logging.error(f"Error fetching LTC price: {e}")
        return cached_price("LTC")

def get_ton_price():
    """CoinGecko API를 이용한 TON(Toncoin) 실시간 시세 조회"""
    try:
        return get_google_finance_price("TON", "TON")
    except Exception as e:
        logging.error(f"Error fetching TON price: {e}")
        return cached_price("TON")

def get_trx_price():
    """Google Finance 기준 TRX(Tron) 원화 시세 조회"""
    try:
        return get_google_finance_price("TRX", "TRX")
    except Exception as e:
        logging.error(f"Error fetching TRX price: {e}")
        return cached_price("TRX")

def get_hangang_temperature():
    """서울시 수질 API의 한강 관측소(선유) 수온 조회"""
    try:
        url = "http://openapi.seoul.go.kr:8088/sample/json/WPOSInformationTime/1/5"
        response = requests.get(url, timeout=5)
        response.raise_for_status()
        rows = response.json()["WPOSInformationTime"]["row"]
        row = next((item for item in rows if item.get("MSRSTN_NM") in {"선유", "한강"}), rows[0])

        return {
            "temperature": float(row["WATT"]),
            "location": row.get("MSRSTN_NM", "한강"),
            "observed_at": f"{row.get('YMD', '')} {row.get('HR', '')}".strip()
        }
    except Exception as e:
        logging.error(f"Error fetching Hangang temperature: {e}")
        return None

async def krw_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /krw, /원화, /환산, /계산 및 '5000만원', '100만원' 등 입력 시
    원화 금액으로 매수 가능한 암호화폐 수량(BTC, USDT, TON, TRX)을 일괄 계산하여 응답
    """
    input_text = ""
    if context.args and len(context.args) > 0:
        input_text = " ".join(context.args)
    elif update.message and update.message.text:
        text = update.message.text.strip()
        input_text = re.sub(r"^/?(?:krw|원화|환산|계산)\s*", "", text, flags=re.IGNORECASE).strip()

    krw_amount = parse_krw(input_text)
    if not krw_amount or krw_amount <= 0:
        if update.message:
            await update.message.reply_text(
                "💡 **원화 환산 사용법**\n\n"
                "계산하고자 하는 원화 금액을 입력해 주세요.\n"
                "예시:\n"
                "• `/krw 50000000`\n"
                "• `5000만원` 또는 `1억 5천만원`\n"
                "• `/btc 1000만원`",
                parse_mode="Markdown"
            )
        return

    btc_info = get_btc_price()
    trx_info = get_trx_price()
    usdt_info = get_usdt_price()
    ton_info = get_ton_price()

    readable_krw = format_krw_readable(krw_amount)
    msg_lines = [
        f"💰 **원화 (KRW) 암호화폐 환산 결과**\n",
        f"• **입력 원화:** **{krw_amount:,.0f} KRW** ({readable_krw})\n"
    ]

    if btc_info and btc_info['krw'] > 0:
        btc_qty = krw_amount / btc_info['krw']
        btc_str = f"{btc_qty:,.8f}".rstrip('0').rstrip('.')
        msg_lines.append(f"🟠 **BTC (비트코인):** **{btc_str} BTC**")
        msg_lines.append(f"  *(1 BTC = {btc_info['krw']:,.0f} KRW)*\n")

    if trx_info and trx_info['krw'] > 0:
        trx_qty = krw_amount / trx_info['krw']
        trx_str = f"{trx_qty:,.2f}".rstrip('0').rstrip('.')
        msg_lines.append(f"🔴 **TRX (Tron):** **{trx_str} TRX**")
        msg_lines.append(f"  *(1 TRX = {trx_info['krw']:,.0f} KRW)*\n")

    if usdt_info and usdt_info['krw'] > 0:
        usdt_qty = krw_amount / usdt_info['krw']
        usdt_str = f"{usdt_qty:,.2f}"
        msg_lines.append(f"💵 **USDT (테더):** **{usdt_str} USDT**")
        msg_lines.append(f"  *(1 USDT = {usdt_info['krw']:,.0f} KRW)*\n")

    if ton_info and ton_info['krw'] > 0:
        ton_qty = krw_amount / ton_info['krw']
        ton_str = f"{ton_qty:,.2f}"
        msg_lines.append(f"💎 **Gram (TON):** **{ton_str} Gram (TON)**")
        msg_lines.append(f"  *(1 Gram (TON) = {ton_info['krw']:,.0f} KRW)*\n")

    ltc_info = get_ltc_price()
    if ltc_info and ltc_info['krw'] > 0:
        ltc_qty = krw_amount / ltc_info['krw']
        ltc_str = f"{ltc_qty:,.4f}".rstrip('0').rstrip('.')
        msg_lines.append(f"⚪ **LTC (라이트코인):** **{ltc_str} LTC**")
        msg_lines.append(f"  *(1 LTC = {ltc_info['krw']:,.0f} KRW)*\n")

    msg_lines.append("⚡ *Google Finance 기준 시세*")

    if update.message:
        await update.message.reply_text("\n".join(msg_lines), parse_mode="Markdown")


COIN_INFO = {
    "BTC": {"label": "비트코인", "emoji": "🟠", "decimals": 8, "aliases": ("btc", "비트코인", "비트"), "getter": get_btc_price},
    "USDT": {"label": "테더", "emoji": "💵", "decimals": 2, "aliases": ("usdt", "테더", "시세", "환율"), "getter": get_usdt_price},
    "TON": {"label": "Gram (TON)", "emoji": "💎", "decimals": 2, "aliases": ("ton", "톤", "gram", "그램"), "getter": get_ton_price},
    "TRX": {"label": "Tron", "emoji": "🔴", "decimals": 2, "aliases": ("trx", "트론"), "getter": get_trx_price},
    "LTC": {"label": "라이트코인", "emoji": "⚪", "decimals": 4, "aliases": ("ltc", "라이트코인", "라이트"), "getter": get_ltc_price},
}


def coin_input_text(update, context, aliases):
    if context.args:
        return " ".join(context.args).strip()
    if not update.message or not update.message.text:
        return ""
    text = update.message.text.strip()
    alias_pattern = "(?:" + "|".join(re.escape(alias) for alias in aliases) + ")"
    text = re.sub(rf"^[/.]?\s*{alias_pattern}\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(rf"\s*{alias_pattern}\s*$", "", text, flags=re.IGNORECASE)
    return re.sub(r"^[/.]\s*", "", text).strip()


async def standard_coin_command(update: Update, context: ContextTypes.DEFAULT_TYPE, asset):
    info = COIN_INFO[asset]
    input_text = coin_input_text(update, context, info["aliases"])
    price_info = info["getter"]()
    if not price_info:
        if update.message:
            await update.message.reply_text(f"⚠️ {asset} 시세 데이터를 불러오는 중 오류가 발생했습니다. 잠시 후 다시 시도해 주세요.")
        return

    krw_price = round(price_info["krw"], 2)
    parsed_value = parse_krw(input_text)
    is_krw = bool(re.search(r"(?:원|krw|만|억|천)", input_text, re.IGNORECASE))
    conversion_krw = None
    if parsed_value is not None and (is_krw or parsed_value >= 10000):
        conversion_krw = parsed_value
        quantity = parsed_value / krw_price
        quantity_text = f"{quantity:,.{info['decimals']}f}".rstrip("0").rstrip(".")
        message = (
            f"{info['emoji']} **{info['label']} ({asset}) 원화 환산 결과**\n\n"
            f"• **입력 원화:** {parsed_value:,.0f} KRW ({format_krw_readable(parsed_value)})\n"
            f"• **현재가 (1 {asset}):** {krw_price:,.2f} KRW\n"
            f"• **구매 가능 수량:** **{quantity_text} {asset}**\n\n"
            f"• **전일 대비:** {price_info.get('change_rate', 0):+.2f}%\n"
            f"{price_details(price_info)}\n\n"
            f"⚡ *{price_info.get('source', '실시간 시세')} 기준*"
        )
    elif parsed_value is not None and parsed_value > 0:
        conversion_krw = parsed_value * krw_price
        amount_text = f"{parsed_value:g}"
        message = (
            f"{info['emoji']} **{info['label']} ({asset}) 계산 결과**\n\n"
            f"• **수량:** {amount_text} {asset}\n"
            f"• **현재가 (1 {asset}):** {krw_price:,.2f} KRW\n"
            f"• **총 원화 금액:** **{conversion_krw:,.0f} KRW** ({format_krw_readable(conversion_krw)})\n\n"
            f"• **전일 대비:** {price_info.get('change_rate', 0):+.2f}%\n"
            f"{price_details(price_info)}\n\n"
            f"⚡ *{price_info.get('source', '실시간 시세')} 기준*"
        )
    else:
        message = (
            f"{info['emoji']} **실시간 {info['label']} ({asset}) 시세 정보**\n\n"
            f"• **현재가 (1 {asset}):** {krw_price:,.2f} KRW\n"
            f"• **전일 대비:** {price_info.get('change_rate', 0):+.2f}%\n"
            f"{price_details(price_info)}\n\n"
            f"💡 **사용 예시:** `/{asset.lower()} 10` 또는 `10 {asset}`\n\n"
            f"⚡ *{price_info.get('source', '실시간 시세')} 기준*"
        )

    if conversion_krw:
        message += "\n\n" + equivalent_lines(conversion_krw, asset, price_info)
    if update.message:
        await update.message.reply_text(message, parse_mode="Markdown")


async def btc_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await standard_coin_command(update, context, "BTC")


async def usdt_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await standard_coin_command(update, context, "USDT")


async def ton_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await standard_coin_command(update, context, "TON")


async def trx_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await standard_coin_command(update, context, "TRX")


async def ltc_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await standard_coin_command(update, context, "LTC")

async def hangang_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """'한강물!' 입력 시 실시간 한강 수온 안내"""
    info = get_hangang_temperature()
    if info:
        message = (
            f"🌊 **한강물 온도**\n\n"
            f"• **현재 수온:** **{info['temperature']:.1f}℃**\n"
            f"• **측정 지점:** {info['location']}\n\n"
            f"• **측정 시각:** {info['observed_at']}\n\n"
            f"참고: 실시간수질정보시스템 공개 데이터 기준"
        )
    else:
        message = "🌊 한강물 온도를 가져오지 못했어요. 잠시 후 다시 외쳐주세요!"

    if update.message:
        await update.message.reply_text(message, parse_mode="Markdown")

def market_line(label, emoji, price):
    if not price:
        return f"{emoji} **{label}:** 조회 실패"
    return (
        f"{emoji} **{label}:** **{price['krw']:,.0f} KRW** "
        f"({price.get('change_rate', 0):+.2f}%)\n"
        f"  출처: {price.get('source', '실시간 시세')}"
    )

async def all_prices_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """전체 코인 현재가를 한 번에 표시"""
    prices = [
        ("BTC", "🟠", get_btc_price()),
        ("USDT", "💵", get_usdt_price()),
        ("Gram (TON)", "💎", get_ton_price()),
        ("TRX", "🔴", get_trx_price()),
        ("LTC", "⚪", get_ltc_price()),
    ]
    message = "📊 **전체 시세**\n\n" + "\n\n".join(
        market_line(label, emoji, price) for label, emoji, price in prices
    ) + "\n\n⚡ Google Finance 기준 시세"
    if update.message:
        await update.message.reply_text(message, parse_mode="Markdown")

async def fortune_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """사용자와 날짜에 고정된 오늘의 운세"""
    fortunes = [
        ("대길", "오늘은 타이밍이 좋다. 미루던 연락과 결정에 행운이 따른다.", "행운의 코인: BTC"),
        ("길", "작은 기회가 큰 결과로 이어진다. 단, 충동 매수는 잠시 멈춰라.", "행운의 코인: TON"),
        ("중길", "꾸준함이 이기는 날이다. 숫자를 확인하고 천천히 움직여라.", "행운의 코인: LTC"),
        ("평범", "큰 변화보다 안정이 유리하다. 지갑과 마음을 함께 지켜라.", "행운의 코인: USDT"),
        ("반전", "예상 밖 소식이 들어온다. 손실보다 배움에 집중하면 운이 바뀐다.", "행운의 코인: TON"),
    ]
    user_id = str(update.effective_user.id if update.effective_user else "anonymous")
    seed = int(hashlib.sha256(f"{datetime.now():%Y-%m-%d}:{user_id}".encode()).hexdigest(), 16)
    grade, message, lucky = fortunes[seed % len(fortunes)]
    reply = (
        f"🔮 **오늘의 운세**\n\n"
        f"• **운세:** {grade}\n"
        f"• {message}\n"
        f"• **{lucky}**\n\n"
        f"※ 재미로만 즐겨주세요. 내일은 운세가 바뀝니다."
    )
    if update.message:
        await update.message.reply_text(reply, parse_mode="Markdown")

# ── 온체인 트랜잭션 조회 & 컨펌 추적 ─────────────────────────────
USDT_TRC20_CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
USDT_DECIMALS = 6
UTXO_APIS = {"BTC": "https://mempool.space", "LTC": "https://litecoinspace.org"}
TRONGRID_API = "https://api.trongrid.io"
TX_TRACK_INTERVAL = 30              # 컨펌 추적 폴링 주기(초)
TX_TRACK_MAX_AGE = 48 * 60 * 60     # 추적 최대 시간(초) - 이후 포기
TRACKED_TXS = {}                    # (chain, txid) -> {"chat_ids": set, "last_tip": int, "created": float}
BASE58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
WALLET_SCAN_INTERVAL = 30
WALLET_GAP_LIMIT = 20
WALLET_MAX_ADDRESSES = 100
BTC_XPUB_VERSIONS = {
    bytes.fromhex("0488b21e"): "bc",
    bytes.fromhex("04b24746"): "bc",
    bytes.fromhex("043587cf"): "tb",
    bytes.fromhex("045f1cf6"): "tb",
}


def base58_encode(raw: bytes) -> str:
    number = int.from_bytes(raw, "big")
    encoded = ""
    while number:
        number, remainder = divmod(number, 58)
        encoded = BASE58_ALPHABET[remainder] + encoded
    padding = len(raw) - len(raw.lstrip(b"\0"))
    return "1" * padding + encoded


def base58_decode(value: str) -> bytes:
    number = 0
    for char in value:
        number = number * 58 + BASE58_ALPHABET.index(char)
    raw = number.to_bytes((number.bit_length() + 7) // 8, "big") if number else b""
    return b"\0" * (len(value) - len(value.lstrip("1"))) + raw


def decode_extended_public_key(value: str):
    raw = base58_decode(value)
    if len(raw) != 82 or hashlib.sha256(hashlib.sha256(raw[:-4]).digest()).digest()[:4] != raw[-4:]:
        raise ValueError("확장 공개키 checksum이 올바르지 않습니다.")
    payload = raw[:-4]
    network = BTC_XPUB_VERSIONS.get(payload[:4])
    if not network or payload[45] not in (2, 3):
        raise ValueError("native SegWit용 xpub/zpub만 지원합니다.")
    return network, payload[13:45], payload[45:]


def derive_xpub_address(xpub: str, branch: int, index: int) -> str:
    network, chain_code, public_key = decode_extended_public_key(xpub)
    point = VerifyingKey.from_string(public_key, curve=SECP256k1).pubkey.point
    generator = SECP256k1.generator
    order = SECP256k1.order
    for child_index in (branch, index):
        compressed = bytes([2 + (point.y() & 1)]) + int(point.x()).to_bytes(32, "big")
        digest = hmac.new(chain_code, compressed + child_index.to_bytes(4, "big"), hashlib.sha512).digest()
        tweak = int.from_bytes(digest[:32], "big")
        if tweak >= order:
            raise ValueError("xpub child derivation failed")
        point = point + generator * tweak
        chain_code = digest[32:]
    compressed = bytes([2 + (point.y() & 1)]) + int(point.x()).to_bytes(32, "big")
    program = hashlib.new("ripemd160", hashlib.sha256(compressed).digest()).digest()
    return encode_segwit_address(network, program)


def bech32_polymod(values):
    generators = (0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3)
    checksum = 1
    for value in values:
        top = checksum >> 25
        checksum = ((checksum & 0x1FFFFFF) << 5) ^ value
        for bit, generator in enumerate(generators):
            if (top >> bit) & 1:
                checksum ^= generator
    return checksum


def bech32_hrp_expand(hrp):
    return [ord(char) >> 5 for char in hrp] + [0] + [ord(char) & 31 for char in hrp]


def convertbits(data, from_bits, to_bits):
    accumulator = 0
    bits = 0
    output = []
    max_value = (1 << to_bits) - 1
    for value in data:
        accumulator = (accumulator << from_bits) | value
        bits += from_bits
        while bits >= to_bits:
            bits -= to_bits
            output.append((accumulator >> bits) & max_value)
    if bits:
        output.append((accumulator << (to_bits - bits)) & max_value)
    return output


def encode_segwit_address(network, program):
    hrp = "bc" if network == "bc" else "tb"
    data = [0] + convertbits(program, 8, 5)
    checksum = bech32_polymod(bech32_hrp_expand(hrp) + data + [0] * 6) ^ 1
    charset = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
    checksum_values = [(checksum >> (5 * (5 - i))) & 31 for i in range(6)]
    return hrp + "1" + "".join(charset[value] for value in data + checksum_values)


def tron_hex_to_address(hex_addr: str) -> str:
    """이벤트 result의 0x.. hex 주소를 Tron base58 주소로 변환"""
    h = hex_addr.strip().lower()
    if h.startswith("0x"):
        h = "41" + h[2:]
    try:
        raw = bytes.fromhex(h)
    except ValueError:
        return hex_addr
    checksum = hashlib.sha256(hashlib.sha256(raw).digest()).digest()[:4]
    return base58_encode(raw + checksum)


def short_addr(addr: str) -> str:
    return f"{addr[:6]}...{addr[-4:]}" if len(addr) > 12 else addr


def mempool_get(chain: str, path: str):
    """mempool.space 계열 API GET. 404(미존재)면 None, 그 외 오류는 예외."""
    response = requests.get(f"{UTXO_APIS[chain]}/api{path}", timeout=8)
    if response.status_code == 404:
        return None
    response.raise_for_status()
    return response.json()


def mempool_tip(chain: str) -> int:
    response = requests.get(f"{UTXO_APIS[chain]}/api/blocks/tip/height", timeout=8)
    response.raise_for_status()
    return int(response.text.strip())


def utxo_tx_summary(chain: str, txid: str, tx: dict, tip: int | None) -> str:
    status = tx.get("status", {})
    confirmed = status.get("confirmed")
    confirmations = tip - status["block_height"] + 1 if confirmed and tip else (1 if confirmed else 0)
    total_out = sum(output.get("value", 0) for output in tx.get("vout", [])) / 1e8
    fee = tx.get("fee", 0) / 1e8

    lines = [
        f"🌐 **{chain} 트랜잭션**",
        "",
        f"• **TXID:** `{txid}`",
    ]
    if confirmed:
        when = datetime.fromtimestamp(status["block_time"], KST).strftime("%Y-%m-%d %H:%M:%S")
        lines.append(f"• **상태:** ✅ 컨펌 완료 ({confirmations:,} 컨펌)")
        lines.append(f"• **블록:** #{status['block_height']:,} · {when} KST")
    else:
        lines.append("• **상태:** ⏳ 미컨펌 (멤풀 대기 중)")
    lines.append(f"• **출력 합계:** {total_out:,.8f} {chain}")
    lines.append(f"• **수수료:** {fee:,.8f} {chain}")
    lines.append(f"🔗 {UTXO_APIS[chain]}/tx/{txid}")
    return "\n".join(lines)


def tron_tx_info(txid: str):
    response = requests.post(
        f"{TRONGRID_API}/walletsolidity/gettransactioninfobyid",
        json={"value": txid}, timeout=8,
    )
    if response.status_code != 200:
        return None
    data = response.json()
    return data if data.get("id") else None


def tron_usdt_transfers_in_tx(txid: str) -> list:
    """트랜잭션에 포함된 USDT(TRC20) Transfer 이벤트 목록"""
    try:
        events = request_json(f"{TRONGRID_API}/v1/transactions/{txid}/events", timeout=8).get("data", [])
    except Exception:
        return []
    transfers = []
    for event in events:
        if event.get("contract_address") != USDT_TRC20_CONTRACT or event.get("event_name") != "Transfer":
            continue
        result = event.get("result", {})
        try:
            value = int(result.get("value", "0")) / 10 ** USDT_DECIMALS
        except (TypeError, ValueError):
            continue
        transfers.append({
            "from": tron_hex_to_address(result.get("from", "")),
            "to": tron_hex_to_address(result.get("to", "")),
            "value": value,
        })
    return transfers


def tron_account_summary(address: str):
    """USDT/TRX 잔액 조회. 활성화 안 된 주소면 None."""
    data = request_json(f"{TRONGRID_API}/v1/accounts/{address}", timeout=8).get("data", [])
    if not data:
        return None
    account = data[0]
    usdt_raw = 0
    for token in account.get("trc20") or []:
        if USDT_TRC20_CONTRACT in token:
            try:
                usdt_raw = int(token[USDT_TRC20_CONTRACT])
            except (TypeError, ValueError):
                pass
    return {
        "trx": account.get("balance", 0) / 1e6,
        "usdt": usdt_raw / 10 ** USDT_DECIMALS,
    }


def tron_recent_usdt_transfers(address: str, limit: int = 5) -> list:
    # Approval 등 전송 아닌 이벤트가 섞일 수 있어 넉넉히 가져온 뒤 필터링
    url = (
        f"{TRONGRID_API}/v1/accounts/{address}/transactions/trc20"
        f"?contract_address={USDT_TRC20_CONTRACT}&limit=20"
        f"&only_confirmed=true&order_by=block_timestamp,desc"
    )
    rows = request_json(url, timeout=8).get("data", [])
    transfers = [row for row in rows if row.get("type") == "Transfer"]
    return transfers[:limit]


def usdt_krw_text(usdt_amount: float, krw_rate: float | None) -> str:
    if not krw_rate:
        return ""
    return f" (≈ {usdt_amount * krw_rate:,.0f}원)"


def wallet_get(watch_id):
    with sqlite3.connect(CHAT_STATS_DB) as db:
        row = db.execute(
            "SELECT watch_id, chat_id, user_id, chain, account_key, initialized, active "
            "FROM wallet_watches WHERE watch_id = ?",
            (watch_id,),
        ).fetchone()
    if not row:
        return None
    return dict(zip(("watch_id", "chat_id", "user_id", "chain", "account_key", "initialized", "active"), row))


def wallet_add_btc_addresses(watch_id, xpub, start_index, count):
    with sqlite3.connect(CHAT_STATS_DB) as db:
        for branch in (0, 1):
            for index in range(start_index, min(start_index + count, WALLET_MAX_ADDRESSES)):
                address = derive_xpub_address(xpub, branch, index)
                db.execute(
                    "INSERT OR IGNORE INTO wallet_addresses (watch_id, branch, address_index, address) VALUES (?, ?, ?, ?)",
                    (watch_id, branch, index, address),
                )


def wallet_insert_event(watch_id, event):
    with sqlite3.connect(CHAT_STATS_DB) as db:
        cursor = db.execute(
            "INSERT OR IGNORE INTO wallet_events "
            "(watch_id, event_key, txid, address, direction, amount, confirmed, observed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                watch_id,
                event["event_key"],
                event["txid"],
                event["address"],
                event["direction"],
                event["amount"],
                int(event.get("confirmed", True)),
                time.time(),
            ),
        )
    return cursor.rowcount == 1


def wallet_remove_event(watch_id, event_key):
    with sqlite3.connect(CHAT_STATS_DB) as db:
        db.execute(
            "DELETE FROM wallet_events WHERE watch_id = ? AND event_key = ?",
            (watch_id, event_key),
        )


def wallet_btc_transactions(address):
    return request_json(f"{UTXO_APIS['BTC']}/api/address/{address}/txs", timeout=8)


def scan_btc_wallet(watch):
    with sqlite3.connect(CHAT_STATS_DB) as db:
        addresses = db.execute(
            "SELECT branch, address_index, address FROM wallet_addresses WHERE watch_id = ? ORDER BY branch, address_index",
            (watch["watch_id"],),
        ).fetchall()
    events = []
    used_last = {0: False, 1: False}
    transactions_by_address = {}
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {
            executor.submit(wallet_btc_transactions, address): (branch, index, address)
            for branch, index, address in addresses
        }
        for future in as_completed(futures):
            branch, index, address = futures[future]
            try:
                transactions_by_address[(branch, index, address)] = future.result()
            except Exception as error:
                logging.warning("BTC wallet scan failed for %s: %s", address, error)
    for branch, index, address in addresses:
        transactions = transactions_by_address.get((branch, index, address), [])
        if transactions:
            if index >= WALLET_GAP_LIMIT - 1:
                used_last[branch] = True
            for tx in transactions:
                txid = tx.get("txid")
                if not txid:
                    continue
                received = sum(
                    output.get("value", 0)
                    for output in tx.get("vout", [])
                    if output.get("scriptpubkey_address") == address
                )
                spent = sum(
                    input_item.get("prevout", {}).get("value", 0)
                    for input_item in tx.get("vin", [])
                    if input_item.get("prevout", {}).get("scriptpubkey_address") == address
                )
                net_value = received - spent
                if not net_value:
                    continue
                event = {
                    "event_key": f"{txid}:{address}",
                    "txid": txid,
                    "address": address,
                    "direction": "입금" if net_value > 0 else "출금",
                    "amount": abs(net_value) / 1e8,
                    "confirmed": tx.get("status", {}).get("confirmed", False),
                }
                if wallet_insert_event(watch["watch_id"], event):
                    events.append(event)
    if any(used_last.values()):
        with sqlite3.connect(CHAT_STATS_DB) as db:
            max_index = {
                branch: db.execute(
                    "SELECT COALESCE(MAX(address_index), -1) FROM wallet_addresses WHERE watch_id = ? AND branch = ?",
                    (watch["watch_id"], branch),
                ).fetchone()[0]
                for branch in (0, 1)
            }
        for branch in (0, 1):
            if used_last[branch] and max_index[branch] + 1 < WALLET_MAX_ADDRESSES:
                start = max_index[branch] + 1
                with sqlite3.connect(CHAT_STATS_DB) as db:
                    for index in range(start, min(start + WALLET_GAP_LIMIT, WALLET_MAX_ADDRESSES)):
                        address = derive_xpub_address(watch["account_key"], branch, index)
                        db.execute(
                            "INSERT OR IGNORE INTO wallet_addresses (watch_id, branch, address_index, address) VALUES (?, ?, ?, ?)",
                            (watch["watch_id"], branch, index, address),
                        )
    return events


def scan_tron_wallet(watch):
    with sqlite3.connect(CHAT_STATS_DB) as db:
        rows = db.execute(
            "SELECT address FROM wallet_addresses WHERE watch_id = ?",
            (watch["watch_id"],),
        ).fetchall()
    events = []
    for (address,) in rows:
        try:
            transfers = tron_recent_usdt_transfers(address, limit=20)
        except Exception as error:
            logging.warning("TRON wallet scan failed for %s: %s", address, error)
            continue
        for transfer in transfers:
            txid = transfer.get("transaction_id") or transfer.get("txID")
            if not txid:
                continue
            try:
                amount = int(transfer.get("value", "0")) / 10 ** USDT_DECIMALS
            except (TypeError, ValueError):
                continue
            incoming = transfer.get("to") == address
            event = {
                "event_key": f"{txid}:{transfer.get('from')}:{transfer.get('to')}:{transfer.get('value')}",
                "txid": txid,
                "address": address,
                "direction": "입금" if incoming else "출금",
                "amount": amount,
                "confirmed": True,
            }
            if wallet_insert_event(watch["watch_id"], event):
                events.append(event)
    return events


def scan_wallet(watch_id):
    watch = wallet_get(watch_id)
    if not watch or not watch["active"] or not watch["initialized"]:
        return []
    return scan_btc_wallet(watch) if watch["chain"] == "BTC" else scan_tron_wallet(watch)


def initialize_wallet(watch_id):
    watch = wallet_get(watch_id)
    if not watch:
        return
    if watch["chain"] == "BTC":
        wallet_add_btc_addresses(watch_id, watch["account_key"], 0, WALLET_GAP_LIMIT)
        scan_btc_wallet(watch)
    else:
        with sqlite3.connect(CHAT_STATS_DB) as db:
            db.execute(
                "INSERT OR IGNORE INTO wallet_addresses (watch_id, branch, address_index, address) VALUES (?, 0, 0, ?)",
                (watch_id, watch["account_key"]),
            )
        scan_tron_wallet(watch)
    with sqlite3.connect(CHAT_STATS_DB) as db:
        db.execute("UPDATE wallet_watches SET initialized = 1 WHERE watch_id = ?", (watch_id,))


def wallet_event_text(watch, event):
    if watch["chain"] == "BTC":
        return (
            f"₿ **BTC {event['direction']} 감지**\n\n"
            f"• **수량:** {event['amount']:,.8f} BTC\n"
            f"• **주소:** `{short_addr(event['address'])}`\n"
            f"• **상태:** {'✅ 컨펌' if event['confirmed'] else '⏳ 미컨펌'}\n"
            f"• **TXID:** `{event['txid']}`\n"
            f"🔗 {UTXO_APIS['BTC']}/tx/{event['txid']}"
        )
    return (
        f"💵 **USDT(TRC20) {event['direction']} 감지**\n\n"
        f"• **수량:** {event['amount']:,.2f} USDT\n"
        f"• **지갑:** `{short_addr(event['address'])}`\n"
        f"• **TXID:** `{event['txid']}`\n"
        f"🔗 https://tronscan.org/#/transaction/{event['txid']}"
    )


async def wallet_watch_loop(app):
    while True:
        await asyncio.sleep(WALLET_SCAN_INTERVAL)
        with sqlite3.connect(CHAT_STATS_DB) as db:
            watch_ids = [row[0] for row in db.execute(
                "SELECT watch_id FROM wallet_watches WHERE active = 1 AND initialized = 1"
            )]
        for watch_id in watch_ids:
            try:
                events = await asyncio.to_thread(scan_wallet, watch_id)
                watch = wallet_get(watch_id)
                if not watch:
                    continue
                for event in events:
                    try:
                        await app.bot.send_message(
                            chat_id=watch["chat_id"],
                            text=wallet_event_text(watch, event),
                            parse_mode="Markdown",
                            disable_web_page_preview=True,
                        )
                    except TelegramError:
                        wallet_remove_event(watch_id, event["event_key"])
                        raise
            except Exception as error:
                logging.warning("wallet watcher %s failed: %s", watch_id, error)


async def wallet_register_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    chat = update.effective_chat
    user = update.effective_user
    if not message or not chat or not user:
        return
    if chat.type != "private":
        await message.reply_text("지갑 등록은 개인정보 보호를 위해 봇 개인채팅에서만 가능합니다.")
        return
    parts = message.text.strip().split()
    if parts[0].lstrip("/.") in {"btc지갑등록", "btcwallet"} and len(parts) == 2:
        chain, account_key = "BTC", parts[1]
    elif parts[0].lstrip("/.") in {"트론지갑등록", "trc20지갑등록"} and len(parts) == 2:
        chain, account_key = "TRC20", parts[1]
    elif len(parts) == 3 and parts[0].lstrip("/.") == "지갑등록":
        chain, account_key = parts[1].upper(), parts[2]
    else:
        await message.reply_text("사용법: /지갑등록 BTC xpub... 또는 /지갑등록 TRC20 T주소")
        return

    if chain == "BTC":
        try:
            derive_xpub_address(account_key, 0, 0)
        except Exception:
            await message.reply_text("native SegWit용 xpub/zpub만 등록할 수 있습니다. 시드문구·개인키는 보내지 마세요.")
            return
    elif chain == "TRC20" and not re.fullmatch(r"T[1-9A-HJ-NP-Za-km-z]{33}", account_key):
        await message.reply_text("올바른 TRON 주소가 아닙니다.")
        return
    else:
        await message.reply_text("지원 체인은 BTC 또는 TRC20입니다.")
        return

    with sqlite3.connect(CHAT_STATS_DB) as db:
        existing = db.execute(
            "SELECT watch_id FROM wallet_watches WHERE chat_id = ? AND account_key = ? AND active = 1",
            (chat.id, account_key),
        ).fetchone()
        if existing:
            await message.reply_text(f"이미 등록된 지갑입니다. 번호: {existing[0]}")
            return
        cursor = db.execute(
            "INSERT INTO wallet_watches (chat_id, user_id, chain, account_key, created_at) VALUES (?, ?, ?, ?, ?)",
            (chat.id, user.id, chain, account_key, time.time()),
        )
        watch_id = cursor.lastrowid
    context.application.create_task(asyncio.to_thread(initialize_wallet, watch_id))
    await message.reply_text(
        f"✅ {chain} 지갑 #{watch_id} 등록 요청했습니다. 기존 거래는 제외하고 새 입출금부터 감시합니다."
    )


async def wallet_list_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_chat:
        return
    with sqlite3.connect(CHAT_STATS_DB) as db:
        rows = db.execute(
            "SELECT watch_id, chain, account_key, initialized FROM wallet_watches "
            "WHERE chat_id = ? AND active = 1 ORDER BY watch_id",
            (update.effective_chat.id,),
        ).fetchall()
    if not rows:
        await update.message.reply_text("등록된 지갑이 없습니다.")
        return
    lines = ["👛 등록 지갑"]
    for watch_id, chain, account_key, initialized in rows:
        identifier = short_addr(account_key) if chain == "TRC20" else f"xpub...{account_key[-8:]}"
        state = "감시 중" if initialized else "초기화 중"
        lines.append(f"#{watch_id} · {chain} · {identifier} · {state}")
    await update.message.reply_text("\n".join(lines))


async def wallet_delete_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_chat:
        return
    match = re.fullmatch(r"[/.]지갑삭제\s+(\d+)", update.message.text.strip())
    if not match:
        await update.message.reply_text("사용법: /지갑삭제 번호")
        return
    watch_id = int(match.group(1))
    with sqlite3.connect(CHAT_STATS_DB) as db:
        row = db.execute(
            "SELECT watch_id FROM wallet_watches WHERE watch_id = ? AND chat_id = ? AND active = 1",
            (watch_id, update.effective_chat.id),
        ).fetchone()
        if not row:
            await update.message.reply_text("해당 지갑을 찾을 수 없습니다.")
            return
        db.execute("DELETE FROM wallet_events WHERE watch_id = ?", (watch_id,))
        db.execute("DELETE FROM wallet_addresses WHERE watch_id = ?", (watch_id,))
        db.execute("UPDATE wallet_watches SET active = 0 WHERE watch_id = ?", (watch_id,))
    await update.message.reply_text(f"✅ 지갑 #{watch_id} 감시를 중지했습니다.")


async def txid_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """64자리 hex 입력 시 BTC/LTC/TRON 트랜잭션 조회. 미컨펌이면 1컨펌까지 추적."""
    message = update.message
    if not message or not message.text or not update.effective_chat:
        return
    txid = message.text.strip().lower()

    # BTC → LTC 순으로 조회 (둘 다 64-hex)
    chain, tx = None, None
    for candidate in ("BTC", "LTC"):
        try:
            tx = mempool_get(candidate, f"/tx/{txid}")
        except Exception as e:
            logging.warning(f"{candidate} tx lookup failed for {txid}: {e}")
            tx = None
        if tx:
            chain = candidate
            break

    if chain:
        try:
            tip = mempool_tip(chain)
        except Exception:
            tip = None
        if tx.get("status", {}).get("confirmed"):
            await message.reply_text(
                utxo_tx_summary(chain, txid, tx, tip),
                parse_mode="Markdown", disable_web_page_preview=True,
            )
            return

        entry = TRACKED_TXS.setdefault((chain, txid), {
            "chat_ids": set(), "last_tip": tip or 0, "created": time.time(),
        })
        entry["chat_ids"].add(update.effective_chat.id)
        if tip:
            entry["last_tip"] = max(entry["last_tip"], tip)
        await message.reply_text(
            f"🌐 {chain} 블록체인에서 트랜잭션을 찾았습니다.\n"
            f"🔔 트랜잭션이 1 컨펌에 도달하면 알려드리겠습니다.\n\n"
            f"`{txid}`",
            parse_mode="Markdown",
        )
        return

    # TRON (USDT TRC20 포함)
    try:
        info = tron_tx_info(txid)
    except Exception as e:
        logging.warning(f"TRON tx lookup failed for {txid}: {e}")
        info = None
    if not info:
        await message.reply_text(
            "⛓ 해당 트랜잭션을 BTC / LTC / TRON 체인에서 찾지 못했습니다.\n"
            "트랜잭션 ID를 다시 확인해 주세요."
        )
        return

    result = info.get("receipt", {}).get("result")
    block = info.get("blockNumber")
    timestamp = info.get("blockTimeStamp")
    when = datetime.fromtimestamp(timestamp / 1000, KST).strftime("%Y-%m-%d %H:%M:%S") if timestamp else "-"
    status_text = "✅ 성공" if result == "SUCCESS" else f"⚠️ {result or '확인 불가'}"

    price = get_usdt_price()
    krw_rate = price["krw"] if price else None

    lines = [
        "🌐 **TRON 트랜잭션**",
        "",
        f"• **TXID:** `{txid}`",
        f"• **상태:** {status_text}",
        f"• **블록:** #{block:,} · {when} KST" if block else f"• **시각:** {when} KST",
    ]
    transfers = tron_usdt_transfers_in_tx(txid)
    if transfers:
        lines += ["", "💵 **USDT(TRC20) 전송**"]
        for transfer in transfers:
            lines.append(
                f"• `{short_addr(transfer['from'])}` → `{short_addr(transfer['to'])}` : "
                f"**{transfer['value']:,.2f} USDT**{usdt_krw_text(transfer['value'], krw_rate)}"
            )
    lines.append(f"🔗 https://tronscan.org/#/transaction/{txid}")
    await message.reply_text("\n".join(lines), parse_mode="Markdown", disable_web_page_preview=True)


async def tron_address_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """T로 시작하는 TRON 주소 입력 시 USDT 잔액 + 최근 입출금 내역 안내"""
    message = update.message
    if not message or not message.text:
        return
    address = message.text.strip()

    try:
        account = tron_account_summary(address)
    except Exception as e:
        logging.warning(f"TRON account lookup failed for {address}: {e}")
        account = "error"

    if account == "error":
        await message.reply_text("⚠️ TRON 네트워크 조회 중 오류가 발생했습니다. 잠시 후 다시 시도해 주세요.")
        return
    if account is None:
        await message.reply_text("해당 주소는 아직 체인에서 활성화되지 않았습니다. (잔액/내역 없음)")
        return

    price = get_usdt_price()
    krw_rate = price["krw"] if price else None

    lines = [
        "👛 **TRON 지갑 조회**",
        "",
        f"• **주소:** `{address}`",
        f"• **USDT 잔액:** **{account['usdt']:,.2f} USDT**{usdt_krw_text(account['usdt'], krw_rate)}",
        f"• **TRX 잔액:** {account['trx']:,.4f} TRX",
    ]

    try:
        transfers = tron_recent_usdt_transfers(address, limit=5)
    except Exception as e:
        logging.warning(f"TRON transfers lookup failed for {address}: {e}")
        transfers = []

    if transfers:
        lines += ["", "💵 **최근 USDT 입출금**"]
        for transfer in transfers:
            incoming = transfer.get("to") == address
            direction = "🟢 입금" if incoming else "🔴 출금"
            counterparty = transfer.get("from") if incoming else transfer.get("to")
            try:
                value = int(transfer.get("value", "0")) / 10 ** USDT_DECIMALS
            except (TypeError, ValueError):
                value = 0.0
            timestamp = transfer.get("block_timestamp")
            when = datetime.fromtimestamp(timestamp / 1000, KST).strftime("%m-%d %H:%M") if timestamp else "-"
            lines.append(
                f"{direction} **{value:,.2f} USDT**{usdt_krw_text(value, krw_rate)}\n"
                f"   상대 `{short_addr(counterparty or '')}` · {when}"
            )

    lines.append(f"\n🔗 https://tronscan.org/#/address/{address}")
    await message.reply_text("\n".join(lines), parse_mode="Markdown", disable_web_page_preview=True)


async def notify_tracked(app, entry: dict, text: str):
    """추적 중인 트랜잭션을 조회한 모든 채팅에 알림 전송"""
    for chat_id in list(entry["chat_ids"]):
        try:
            await app.bot.send_message(
                chat_id=chat_id, text=text,
                parse_mode="Markdown", disable_web_page_preview=True,
            )
        except TelegramError:
            entry["chat_ids"].discard(chat_id)


async def tx_monitor_loop(app):
    """미컨펌 BTC/LTC 트랜잭션을 주기적으로 폴링해 새 블록/컨펌 여부를 알림"""
    while True:
        await asyncio.sleep(TX_TRACK_INTERVAL)
        if not TRACKED_TXS:
            continue
        tips = {}
        for (chain, txid), entry in list(TRACKED_TXS.items()):
            try:
                if chain not in tips:
                    tips[chain] = await asyncio.to_thread(mempool_tip, chain)
                tip = tips[chain]
                tx = await asyncio.to_thread(mempool_get, chain, f"/tx/{txid}")

                if tx is None:
                    await notify_tracked(
                        app, entry,
                        f"⚠️ {chain} 트랜잭션이 멤풀에서 사라졌습니다. 드롭되거나 다른 트랜잭션으로 대체됐을 수 있습니다.\n`{txid}`",
                    )
                    TRACKED_TXS.pop((chain, txid), None)
                    continue

                status = tx.get("status", {})
                if status.get("confirmed"):
                    confirmations = tip - status["block_height"] + 1
                    await notify_tracked(
                        app, entry,
                        f"✅ {chain} 트랜잭션이 **1 컨펌**을 달성했습니다!\n"
                        f"• **블록:** #{status['block_height']:,} (누적 {confirmations:,} 컨펌)\n"
                        f"🔗 {UTXO_APIS[chain]}/tx/{txid}",
                    )
                    TRACKED_TXS.pop((chain, txid), None)
                    continue

                if tip > entry["last_tip"]:
                    for height in range(entry["last_tip"] + 1, tip + 1):
                        await notify_tracked(
                            app, entry,
                            f"⛏ 새 블록 **#{height:,}** 이(가) 채굴됐지만 트랜잭션이 아직 포함되지 않았습니다.\n"
                            f"수수료가 낮기 때문일 수 있습니다.\n`{txid}`",
                        )
                    entry["last_tip"] = tip

                if time.time() - entry["created"] > TX_TRACK_MAX_AGE:
                    await notify_tracked(
                        app, entry,
                        f"⌛ {chain} 트랜잭션 추적을 종료합니다. 48시간이 지나도 컨펌되지 않았습니다.\n`{txid}`",
                    )
                    TRACKED_TXS.pop((chain, txid), None)
            except Exception as e:
                logging.warning(f"tx monitor error ({chain} {txid[:12]}...): {e}")


async def post_init_tx_monitor(app):
    try:
        await app.bot.set_my_commands(
            [
                BotCommand("start", "사용방법 및 온체인 조회 안내"),
                BotCommand("help", "사용방법 및 온체인 조회 안내"),
                BotCommand("usdt", "USDT 시세 및 원화 계산"),
                BotCommand("btc", "BTC 시세 및 원화 계산"),
                BotCommand("ton", "Gram (TON) 시세 및 원화 계산"),
                BotCommand("trx", "TRX 시세 및 원화 계산"),
                BotCommand("ltc", "LTC 시세 및 원화 계산"),
                BotCommand("all", "전체 시세 조회"),
            ],
            scope=BotCommandScopeAllGroupChats(),
        )
    except TelegramError as error:
        logging.warning("group bot command menu update failed: %s", error)
    app.create_task(tx_monitor_loop(app))
    with sqlite3.connect(CHAT_STATS_DB) as db:
        rows = db.execute(
            "SELECT repeat_id, chat_id FROM repeat_messages WHERE active = 1"
        ).fetchall()
    for repeat_id, chat_id in rows:
        start_repeat_task(app, repeat_id, chat_id)
    app.create_task(wallet_watch_loop(app))


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/start 및 /help 안내 메시지"""
    message = (
        "[사용방법]\n\n"
        "💵 USDT (테더)\n"
        "• /usdt 또는 /환율 - 실시간 USDT 시세 조회\n"
        "• /usdt 100 - 100 USDT 원화 계산\n"
        "• .usdt 100 - 100 USDT 원화 계산\n"
        "• .100테더 - 100 USDT 원화 계산\n"
        "• /100테더 - 100 USDT 원화 계산\n\n"
        "» 원하는 코인의 심볼을 위와 같이 입력하세요.\n\n"
        "조회가능코인\n"
        "USDT / BTC / Gram (TON) / TRX\n\n"
        "전체시세보기 : /all 또는 .시세\n"
        "\n⛓ 온체인 조회 사용법\n"
        "• BTC/LTC/TRON 트랜잭션: 64자리 트랜잭션 ID를 그대로 전송\n"
        "• USDT(TRC20): TRON의 64자리 트랜잭션 ID를 그대로 전송\n"
        "• TRON 지갑 조회: T로 시작하는 지갑 주소를 그대로 전송\n"
        "\n👛 지갑 입출금 알림\n"
        "• /지갑등록 BTC xpub...\n"
        "• /지갑등록 TRC20 T주소\n"
        "• /지갑목록 · /지갑삭제 번호\n"
        "※ BTC는 native SegWit 계정 xpub/zpub 기준이며 시드문구·개인키는 받지 않습니다.\n"
        "• 온체인 조회는 봇 개인메시지에서 사용하는 것을 권장합니다.\n"
    )
    if update.message:
        await update.message.reply_text(message, parse_mode="Markdown")

def main():
    if not TOKEN:
        logging.error("❌ 오류: TELEGRAM_BOT_TOKEN 환경변수가 설정되지 않았습니다. .env 파일이나 서버 환경변수에 토큰을 설정해 주세요.")
        return

    init_chat_stats_db()
    app = Application.builder().token(TOKEN).post_init(post_init_tx_monitor).build()

    # 도움말 핸들러 (영문 전용)
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", start_command))

    # BTC / KRW / USDT / TON / TRX 영문 CommandHandler
    app.add_handler(CommandHandler("btc", btc_command))
    app.add_handler(CommandHandler("krw", krw_command))
    app.add_handler(CommandHandler("usdt", usdt_command))
    app.add_handler(CommandHandler("ton", ton_command))
    app.add_handler(CommandHandler("trx", trx_command))
    app.add_handler(CommandHandler("ltc", ltc_command))
    app.add_handler(CommandHandler("all", all_prices_command))
    app.add_handler(CommandHandler("prices", all_prices_command))

    # 온체인 조회: 64-hex 트랜잭션 ID(BTC/LTC 자동판별, TRON 포함), TRON 주소
    app.add_handler(MessageHandler(filters.Regex(re.compile(r"^\s*[0-9a-fA-F]{64}\s*$")), txid_command))
    app.add_handler(MessageHandler(filters.Regex(re.compile(r"^\s*T[1-9A-HJ-NP-Za-km-z]{33}\s*$")), tron_address_command))
    app.add_handler(MessageHandler(filters.Regex(re.compile(r"^[/.](?:지갑등록\s+(?:BTC|TRC20)\s+\S+|btc지갑등록\s+\S+|트론지갑등록\s+\S+)$", re.IGNORECASE)), wallet_register_command))
    app.add_handler(MessageHandler(filters.Regex(re.compile(r"^[/.]지갑목록$")), wallet_list_command))
    app.add_handler(MessageHandler(filters.Regex(re.compile(r"^[/.]지갑삭제\s+\d+$")), wallet_delete_command))

    # 한글 명령어 메시지 핸들러 (텔레그램 CommandHandler는 ASCII 전용)
    app.add_handler(MessageHandler(filters.Regex(re.compile(r"^[/.](비트코인|비트)(\s+.*)?$", re.IGNORECASE)), btc_command))
    app.add_handler(MessageHandler(filters.Regex(re.compile(r"^[/.](원화|환산|계산)(\s+.*)?$", re.IGNORECASE)), krw_command))
    app.add_handler(MessageHandler(filters.Regex(re.compile(r"^[/.](?:시세|환율)(\s+[0-9,.]+)?$", re.IGNORECASE)), usdt_command))
    app.add_handler(MessageHandler(filters.Regex(re.compile(r"^\.usdt\s+[0-9,.]+$", re.IGNORECASE)), usdt_command))
    app.add_handler(MessageHandler(filters.Regex(re.compile(r"^[/.](톤|ton|gram|그램)(\s+[0-9,.]+)?$", re.IGNORECASE)), ton_command))
    app.add_handler(MessageHandler(filters.Regex(re.compile(r"^[/.](트론|trx)(\s+[0-9,.]+)?$", re.IGNORECASE)), trx_command))
    app.add_handler(MessageHandler(filters.Regex(re.compile(r"^[/.](라이트코인|라이트)(\s+[0-9,.]+)?$", re.IGNORECASE)), ltc_command))
    app.add_handler(MessageHandler(filters.Regex(re.compile(r"^한강물!$")), hangang_command))
    app.add_handler(MessageHandler(filters.Regex(re.compile(r"^/?(?:전체시세|전체 시세)$")), all_prices_command))
    app.add_handler(MessageHandler(filters.Regex(re.compile(r"^/?(?:오늘의운세|오늘의 운세|운세)$")), fortune_command))
    app.add_handler(MessageHandler(filters.Regex(re.compile(r"^[/\.](?:채팅|순위)$")), chat_ranking_command))
    app.add_handler(MessageHandler(filters.Regex(re.compile(r"^\.기록(?:on|off)$")), toggle_chat_recording))
    app.add_handler(MessageHandler(filters.Regex(re.compile(r"^\.반복목록$")), repeat_list_command))
    app.add_handler(MessageHandler(filters.Regex(re.compile(r"^\.반복중지(?:\s+.*)?$")), repeat_stop_command))
    app.add_handler(MessageHandler(filters.Regex(re.compile(r"^\.반복전체중지$")), repeat_stop_all_command))
    app.add_handler(MessageHandler(filters.Regex(re.compile(r"^\.반복(?:\s+.*)?$", re.DOTALL)), repeat_command))
    app.add_handler(CallbackQueryHandler(chat_ranking_callback, pattern=r"^chat_rank:(?:day|week|month|all):[0-9]+$"))

    # Aggregate counts only. Message text is never stored.
    app.add_handler(MessageHandler(filters.ALL, record_chat_message, block=False), group=1)

    # 자연어 계산 메시지 핸들러
    # 1) '5000만원 btc', '0.5 btc' 등
    app.add_handler(MessageHandler(filters.Regex(re.compile(r"^[/.]([0-9,.]+\s*(?:억|만|천)?\s*원?)\s*(?:btc|비트코인|비트)$", re.IGNORECASE)), btc_command))
    app.add_handler(MessageHandler(filters.Regex(re.compile(r"^[/.]([0-9,.]+)\s*(?:비트코인|비트|btc)$", re.IGNORECASE)), btc_command))
    
    # 2) '100테더', '100usdt' 등
    app.add_handler(MessageHandler(filters.Regex(re.compile(r"^[/.]([0-9,]+(?:\.[0-9]+)?)\s*(?:테더|usdt|환율)$", re.IGNORECASE)), usdt_command))
    
    # 3) '100톤', '100ton' 등
    app.add_handler(MessageHandler(filters.Regex(re.compile(r"^[/.]([0-9,]+(?:\.[0-9]+)?)\s*(?:톤|ton|gram|그램)$", re.IGNORECASE)), ton_command))

    # 3) '3트론', '/3TRX', '.3trx' 등
    app.add_handler(MessageHandler(filters.Regex(re.compile(r"^[/.]([0-9,]+(?:\.[0-9]+)?)\s*(?:트론|trx)$", re.IGNORECASE)), trx_command))

    # 4) '10 LTC', '10라이트코인' 등
    app.add_handler(MessageHandler(filters.Regex(re.compile(r"^[/.]([0-9,]+(?:\.[0-9]+)?)\s*(?:라이트코인|라이트|ltc)$", re.IGNORECASE)), ltc_command))

    # 5) 원화 금액 단위 메시지 (예: "5000만원", "1억원", "50000000원") -> 통합 환산
    app.add_handler(MessageHandler(filters.Regex(re.compile(r"^[/.]([0-9,.]+\s*(?:억|만|천)?\s*원)$", re.IGNORECASE)), krw_command))

    # ponytail: 헬스체크 서버는 Render 같은 웹호스팅에서만 필요.
    # VPS 단독 구동은 기본 비활성으로 두고, 다른 웹서버와 포트 충돌을 피한다.
    enable_healthcheck = os.getenv("ENABLE_HEALTHCHECK", "").strip().lower() in {"1", "true", "yes", "on"}
    if enable_healthcheck:
        threading.Thread(target=start_health_check_server, daemon=True).start()

    print("텔레그램 코인 시세/계산 봇이 실행 중입니다... (종료하려면 Ctrl+C)")
    try:
        app.run_polling()
    except InvalidToken:
        logging.error("❌ 오류: TELEGRAM_BOT_TOKEN이 거부되었습니다. VPS의 .env 값을 확인해 주세요.")

if __name__ == "__main__":
    main()
