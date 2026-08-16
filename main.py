import logging
import os
import re
import requests
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from dotenv import load_dotenv
from telegram import Update

load_dotenv()

from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

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

# 봇 토큰 (환경 변수 전용 - 보안을 위해 하드코딩 금지)
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")


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
    cleaned = re.sub(r"(?:원|KRW|krw|btc|비트코인|비트|usdt|테더|ton|톤)", "", raw, flags=re.IGNORECASE).strip()
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
    """원화 금액을 읽기 편한 한글 단위 문자열로 변환 (예: 5,000만 원)"""
    if amount >= 100_000_000:
        eok = int(amount // 100_000_000)
        rest = amount % 100_000_000
        man = int(rest // 10_000)
        if man > 0:
            return f"{eok}억 {man:,}만 원"
        return f"{eok}억 원"
    elif amount >= 10_000:
        man = int(amount // 10_000)
        rest = int(amount % 10_000)
        if rest > 0:
            return f"{man:,}만 {rest:,}원"
        return f"{man:,}만 원"
    else:
        return f"{int(amount):,}원"

def get_usdt_price():
    """업비트(Upbit) API를 이용한 원화(KRW) 기준 USDT 실시간 시세 조회"""
    try:
        url = "https://api.upbit.com/v1/ticker?markets=KRW-USDT"
        response = requests.get(url, timeout=3)
        response.raise_for_status()
        data = response.json()
        
        return {
            "krw": data[0]['trade_price'],
            "change_rate": data[0]['signed_change_rate'] * 100,
            "high": data[0]['high_price'],
            "low": data[0]['low_price']
        }
    except Exception as e:
        logging.error(f"Error fetching USDT price: {e}")
        return None

def get_btc_price():
    """업비트(Upbit) API를 이용한 원화(KRW) 기준 BTC(비트코인) 실시간 시세 조회"""
    try:
        url = "https://api.upbit.com/v1/ticker?markets=KRW-BTC"
        response = requests.get(url, timeout=3)
        response.raise_for_status()
        data = response.json()
        
        return {
            "krw": data[0]['trade_price'],
            "change_rate": data[0]['signed_change_rate'] * 100,
            "high": data[0]['high_price'],
            "low": data[0]['low_price']
        }
    except Exception as e:
        logging.error(f"Error fetching BTC price: {e}")
        return None

def get_ton_price():
    """CoinGecko API를 이용한 TON(Toncoin) 실시간 시세 조회"""
    try:
        url = "https://api.coingecko.com/api/v3/simple/price?ids=the-open-network&vs_currencies=krw,usd&include_24hr_change=true"
        headers = {'User-Agent': 'Mozilla/5.0'}
        response = requests.get(url, headers=headers, timeout=5)
        response.raise_for_status()
        data = response.json().get('the-open-network', {})
        
        return {
            "krw": data.get('krw', 0),
            "usd": data.get('usd', 0),
            "change_rate": data.get('krw_24h_change', 0)
        }
    except Exception as e:
        logging.error(f"Error fetching TON price: {e}")
        return None

async def btc_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /btc, /비트, /비트코인 및 '5000만원 btc', '0.5 btc' 등 BTC 관련 입력 처리.
    원화 금액이 입력된 경우 -> 구매 가능한 BTC 수량 계산
    BTC 수량이 입력된 경우 -> 원화 금액 계산
    """
    input_text = ""
    if context.args and len(context.args) > 0:
        input_text = " ".join(context.args)
    elif update.message and update.message.text:
        text = update.message.text.strip()
        # '/비트 5000' 형태에서 명령어 부분 제거
        input_text = re.sub(r"^/?(?:btc|비트코인|비트)\s*", "", text, flags=re.IGNORECASE).strip()

    price_info = get_btc_price()
    if not price_info:
        if update.message:
            await update.message.reply_text("⚠️ BTC 시세 데이터를 불러오는 중 오류가 발생했습니다. 잠시 후 다시 시도해 주세요.")
        return

    krw_price = price_info['krw']
    
    # 텍스트에 원화 키워드가 명시되거나 파싱된 원화 금액인지 검사
    is_explicit_krw = bool(re.search(r"(?:원|krw|만|억)", input_text, re.IGNORECASE))
    parsed_val = parse_krw(input_text)

    # 파싱된 값이 있으며, 원화로 명시되었거나 10,000원 이상인 경우 원화 -> BTC 계산
    if parsed_val is not None and (is_explicit_krw or parsed_val >= 10000):
        krw_amount = parsed_val
        btc_quantity = krw_amount / krw_price
        readable_krw = format_krw_readable(krw_amount)
        btc_str = f"{btc_quantity:,.8f}".rstrip('0').rstrip('.')

        message = (
            f"🟠 **비트코인 (BTC) 원화 환산 결과**\n\n"
            f"• **입력 원화:** {krw_amount:,.0f} KRW ({readable_krw})\n"
            f"• **현재가 (1 BTC):** {krw_price:,.0f} KRW\n"
            f"• **구매 가능 수량:** **{btc_str} BTC**\n\n"
            f"• **전일 대비:** {price_info['change_rate']:+.2f}%\n"
            f"• **고가:** {price_info['high']:,.0f} KRW | **저가:** {price_info['low']:,.0f} KRW\n\n"
            f"⚡ *업비트(Upbit) 실시간 데이터 기준*"
        )
    elif parsed_val is not None and parsed_val > 0:
        # BTC 수량 -> 원화 계산
        btc_amount = parsed_val
        total_krw = btc_amount * krw_price
        btc_str = f"{btc_amount:g}"
        readable_krw = format_krw_readable(total_krw)

        message = (
            f"🟠 **비트코인 (BTC) 수량 계산 결과**\n\n"
            f"• **수량:** {btc_str} BTC\n"
            f"• **현재가 (1 BTC):** {krw_price:,.0f} KRW\n"
            f"• **총 원화 금액:** **{total_krw:,.0f} KRW** ({readable_krw})\n\n"
            f"• **전일 대비:** {price_info['change_rate']:+.2f}%\n"
            f"• **고가:** {price_info['high']:,.0f} KRW | **저가:** {price_info['low']:,.0f} KRW\n\n"
            f"⚡ *업비트(Upbit) 실시간 데이터 기준*"
        )
    else:
        # 단순 시세 조회
        message = (
            f"🟠 **실시간 비트코인 (BTC) 시세 정보**\n\n"
            f"• **현재가:** {krw_price:,.0f} KRW\n"
            f"• **전일 대비:** {price_info['change_rate']:+.2f}%\n"
            f"• **고가:** {price_info['high']:,.0f} KRW\n"
            f"• **저가:** {price_info['low']:,.0f} KRW\n\n"
            f"💡 **사용 예시:**\n"
            f"• 원화로 BTC 갯수 구하기: `/btc 5000만원` 또는 `50000000원`\n"
            f"• BTC로 원화 금액 구하기: `/btc 0.5` 또는 `0.5비트`\n\n"
            f"⚡ *업비트(Upbit) 실시간 데이터 기준*"
        )

    if update.message:
        await update.message.reply_text(message, parse_mode="Markdown")

async def krw_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /krw, /원화, /환산, /계산 및 '5000만원', '100만원' 등 입력 시
    원화 금액으로 매수 가능한 암호화폐 수량(BTC, USDT, TON)을 일괄 계산하여 응답
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

    if usdt_info and usdt_info['krw'] > 0:
        usdt_qty = krw_amount / usdt_info['krw']
        usdt_str = f"{usdt_qty:,.2f}"
        msg_lines.append(f"💵 **USDT (테더):** **{usdt_str} USDT**")
        msg_lines.append(f"  *(1 USDT = {usdt_info['krw']:,.0f} KRW)*\n")

    if ton_info and ton_info['krw'] > 0:
        ton_qty = krw_amount / ton_info['krw']
        ton_str = f"{ton_qty:,.2f}"
        msg_lines.append(f"💎 **TON (톤코인):** **{ton_str} TON**")
        msg_lines.append(f"  *(1 TON = {ton_info['krw']:,.0f} KRW)*\n")

    msg_lines.append("⚡ *업비트 & CoinGecko 실시간 시세 기준*")

    if update.message:
        await update.message.reply_text("\n".join(msg_lines), parse_mode="Markdown")

async def usdt_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/usdt, /시세, /100테더, 100테더 등 USDT 관련 명령어/텍스트 처리"""
    amount = None

    if context.args and len(context.args) > 0:
        try:
            raw_arg = context.args[0].replace(",", "")
            amount = float(raw_arg)
        except ValueError:
            pass

    if amount is None and update.message and update.message.text:
        text = update.message.text.strip()
        match_amount = re.search(r"/?([0-9,]+(?:\.[0-9]+)?)\s*(?:테더|usdt)?", text, re.IGNORECASE)
        if match_amount and match_amount.group(1):
            try:
                amount = float(match_amount.group(1).replace(",", ""))
            except ValueError:
                pass

        if amount is None:
            match_seise = re.search(r"/?시세\s+([0-9,]+(?:\.[0-9]+)?)", text)
            if match_seise:
                try:
                    amount = float(match_seise.group(1).replace(",", ""))
                except ValueError:
                    pass

    price_info = get_usdt_price()
    
    if price_info:
        krw_price = price_info['krw']
        
        if amount is not None and amount > 0:
            total_krw = krw_price * amount
            amount_str = f"{int(amount):,}" if amount.is_integer() else f"{amount:,.4f}".rstrip('0').rstrip('.')
            total_krw_str = f"{total_krw:,.0f}" if total_krw.is_integer() else f"{total_krw:,.2f}"
            readable_krw = format_krw_readable(total_krw)
            
            message = (
                f"💵 **USDT 계산 결과**\n\n"
                f"• **수량:** {amount_str} USDT\n"
                f"• **현재가 (1 USDT):** {krw_price:,} KRW\n"
                f"• **총 원화 금액:** **{total_krw_str} KRW** ({readable_krw})\n\n"
                f"• **전일 대비:** {price_info['change_rate']:+.2f}%\n"
                f"• **고가:** {price_info['high']:,} KRW | **저가:** {price_info['low']:,} KRW\n\n"
                f"⚡ *업비트(Upbit) 실시간 데이터 기준*"
            )
        else:
            message = (
                f"💵 **실시간 USDT 시세 정보**\n\n"
                f"• **현재가:** {krw_price:,} KRW\n"
                f"• **전일 대비:** {price_info['change_rate']:+.2f}%\n"
                f"• **고가:** {price_info['high']:,} KRW\n"
                f"• **저가:** {price_info['low']:,} KRW\n\n"
                f"⚡ *업비트(Upbit) 실시간 데이터 기준*"
            )
    else:
        message = "⚠️ 시세 데이터를 불러오는 중 오류가 발생했습니다. 잠시 후 다시 시도해주세요."

    if update.message:
        await update.message.reply_text(message, parse_mode="Markdown")

async def ton_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/ton, /톤, /100톤, 100톤 등 TON(Toncoin) 관련 명령어/텍스트 처리"""
    amount = None

    if context.args and len(context.args) > 0:
        try:
            raw_arg = context.args[0].replace(",", "")
            amount = float(raw_arg)
        except ValueError:
            pass

    if amount is None and update.message and update.message.text:
        text = update.message.text.strip()
        match_amount = re.search(r"/?([0-9,]+(?:\.[0-9]+)?)\s*(?:톤|ton)?", text, re.IGNORECASE)
        if match_amount and match_amount.group(1):
            try:
                amount = float(match_amount.group(1).replace(",", ""))
            except ValueError:
                pass

        if amount is None:
            match_ton = re.search(r"/?톤\s+([0-9,]+(?:\.[0-9]+)?)", text)
            if match_ton:
                try:
                    amount = float(match_ton.group(1).replace(",", ""))
                except ValueError:
                    pass

    price_info = get_ton_price()
    
    if price_info:
        krw_price = price_info['krw']
        usd_price = price_info['usd']
        
        if amount is not None and amount > 0:
            total_krw = krw_price * amount
            total_usd = usd_price * amount
            amount_str = f"{int(amount):,}" if amount.is_integer() else f"{amount:,.4f}".rstrip('0').rstrip('.')
            total_krw_str = f"{total_krw:,.0f}" if total_krw.is_integer() else f"{total_krw:,.2f}"
            total_usd_str = f"{total_usd:,.2f}"
            readable_krw = format_krw_readable(total_krw)
            
            message = (
                f"💎 **TON (Toncoin) 계산 결과**\n\n"
                f"• **수량:** {amount_str} TON\n"
                f"• **현재가 (1 TON):** {krw_price:,.0f} KRW (${usd_price:,.2f})\n"
                f"• **총 원화 금액:** **{total_krw_str} KRW** ({readable_krw})\n"
                f"• **USD 금액:** **${total_usd_str}**\n\n"
                f"• **24시간 변동률:** {price_info['change_rate']:+.2f}%\n\n"
                f"⚡ *CoinGecko 실시간 데이터 기준*"
            )
        else:
            message = (
                f"💎 **실시간 TON (Toncoin) 시세 정보**\n\n"
                f"• **현재가:** {krw_price:,.0f} KRW (${usd_price:,.2f})\n"
                f"• **24시간 변동률:** {price_info['change_rate']:+.2f}%\n\n"
                f"⚡ *CoinGecko 실시간 데이터 기준*"
            )
    else:
        message = "⚠️ TON 시세 데이터를 불러오는 중 오류가 발생했습니다. 잠시 후 다시 시도해주세요."

    if update.message:
        await update.message.reply_text(message, parse_mode="Markdown")

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/start 및 /help 안내 메시지"""
    message = (
        "🤖 **암호화폐 시세 및 원화 환산 봇에 오신 것을 환영합니다!**\n\n"
        "원하시는 암호화폐 시세 조회 및 원화 금액 환산을 손쉽게 이용하실 수 있습니다.\n\n"
        "🟠 **비트코인 (BTC)**\n"
        "• `/btc` - 실시간 BTC 시세 조회\n"
        "• `/btc 5000만원` 또는 `50000000원` - 원하는 원화로 비트코인 갯수 계산\n"
        "• `/btc 0.5` 또는 `0.5비트` - 비트코인 수량으로 원화 금액 계산\n\n"
        "💰 **원화 (KRW) 통합 환산**\n"
        "• `/krw 50000000` 또는 `5천만원` - 원하는 원화로 매수 가능한 BTC, USDT, TON 갯수 통합 출력\n\n"
        "💵 **USDT (테더)**\n"
        "• `/usdt` - 실시간 USDT 시세 조회\n"
        "• `/usdt 100` - 100 USDT 원화 계산\n\n"
        "💎 **TON (톤코인)**\n"
        "• `/ton` - 실시간 TON 시세 조회\n"
        "• `/ton 50` - 50 TON 원화 계산\n"
    )
    if update.message:
        await update.message.reply_text(message, parse_mode="Markdown")

def main():
    if not TOKEN:
        logging.error("❌ 오류: TELEGRAM_BOT_TOKEN 환경변수가 설정되지 않았습니다. .env 파일이나 서버 환경변수에 토큰을 설정해 주세요.")
        return
        
    app = Application.builder().token(TOKEN).build()

    # 도움말 핸들러 (영문 전용)
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", start_command))

    # BTC / KRW / USDT / TON 영문 CommandHandler
    app.add_handler(CommandHandler("btc", btc_command))
    app.add_handler(CommandHandler("krw", krw_command))
    app.add_handler(CommandHandler("usdt", usdt_command))
    app.add_handler(CommandHandler("ton", ton_command))

    # 한글 명령어 메시지 핸들러 (텔레그램 CommandHandler는 ASCII 전용이므로 Regex 사용)
    app.add_handler(MessageHandler(filters.Regex(re.compile(r"^/?(비트코인|비트)(\s+.*)?$", re.IGNORECASE)), btc_command))
    app.add_handler(MessageHandler(filters.Regex(re.compile(r"^/?(원화|환산|계산)(\s+.*)?$", re.IGNORECASE)), krw_command))
    app.add_handler(MessageHandler(filters.Regex(re.compile(r"^/?시세(\s+[0-9,.]+)?$", re.IGNORECASE)), usdt_command))
    app.add_handler(MessageHandler(filters.Regex(re.compile(r"^/?톤(\s+[0-9,.]+)?$", re.IGNORECASE)), ton_command))

    # 자연어 계산 메시지 핸들러
    # 1) '5000만원 btc', '0.5 btc' 등
    app.add_handler(MessageHandler(filters.Regex(re.compile(r"^/?([0-9,.]+\s*(?:억|만|천)?\s*원?)\s*(?:btc|비트코인|비트)$", re.IGNORECASE)), btc_command))
    app.add_handler(MessageHandler(filters.Regex(re.compile(r"^/?([0-9,.]+)\s*(?:비트코인|비트|btc)$", re.IGNORECASE)), btc_command))
    
    # 2) '100테더', '100usdt' 등
    app.add_handler(MessageHandler(filters.Regex(re.compile(r"^/?([0-9,]+(?:\.[0-9]+)?)\s*(?:테더|usdt)$", re.IGNORECASE)), usdt_command))
    
    # 3) '100톤', '100ton' 등
    app.add_handler(MessageHandler(filters.Regex(re.compile(r"^/?([0-9,]+(?:\.[0-9]+)?)\s*(?:톤|ton)$", re.IGNORECASE)), ton_command))

    # 4) 원화 금액 단위 메시지 (예: "5000만원", "1억원", "50000000원") -> 통합 환산
    app.add_handler(MessageHandler(filters.Regex(re.compile(r"^/?([0-9,.]+\s*(?:억|만|천)?\s*원)$", re.IGNORECASE)), krw_command))

    # Render 웹 서비스 헬스체크용 웹서버 데몬 스레드 실행
    threading.Thread(target=start_health_check_server, daemon=True).start()

    print("텔레그램 코인 시세/계산 봇이 실행 중입니다... (종료하려면 Ctrl+C)")
    app.run_polling()

if __name__ == "__main__":
    main()
