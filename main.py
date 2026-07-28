import logging
import os
import re
import requests
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from telegram import Update
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
    server = HTTPServer(('0.0.0.0', port), HealthCheckHandler)
    server.serve_forever()

# 로깅 설정
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

# 봇 토큰 (환경 변수 또는 직접 입력)
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "REVOKED-TOKEN-REDACTED")

def get_usdt_price():
    """업비트(Upbit) API를 이용한 원화(KRW) 기준 USDT 실시간 시세 조회"""
    try:
        url = "https://api.upbit.com/v1/ticker?markets=KRW-USDT"
        response = requests.get(url, timeout=3)
        response.raise_for_status()
        data = response.json()
        
        krw_price = data[0]['trade_price']
        change_rate = data[0]['signed_change_rate'] * 100
        high_price = data[0]['high_price']
        low_price = data[0]['low_price']

        return {
            "krw": krw_price,
            "change_rate": change_rate,
            "high": high_price,
            "low": low_price
        }
    except Exception as e:
        logging.error(f"Error fetching USDT price: {e}")
        return None

def get_ton_price():
    """CoinGecko API를 이용한 TON(Toncoin) 실시간 시세 조회"""
    try:
        url = "https://api.coingecko.com/api/v3/simple/price?ids=the-open-network&vs_currencies=krw,usd&include_24hr_change=true"
        headers = {'User-Agent': 'Mozilla/5.0'}
        response = requests.get(url, headers=headers, timeout=5)
        response.raise_for_status()
        data = response.json().get('the-open-network', {})
        
        krw_price = data.get('krw', 0)
        usd_price = data.get('usd', 0)
        change_rate = data.get('krw_24h_change', 0)

        return {
            "krw": krw_price,
            "usd": usd_price,
            "change_rate": change_rate
        }
    except Exception as e:
        logging.error(f"Error fetching TON price: {e}")
        return None

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
            
            message = (
                f"💵 **USDT 계산 결과**\n\n"
                f"• **수량:** {amount_str} USDT\n"
                f"• **현재가 (1 USDT):** {krw_price:,} KRW\n"
                f"• **총 원화 금액:** **{total_krw_str} KRW**\n\n"
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
            
            message = (
                f"💎 **TON (Toncoin) 계산 결과**\n\n"
                f"• **수량:** {amount_str} TON\n"
                f"• **현재가 (1 TON):** {krw_price:,.0f} KRW (${usd_price:,.2f})\n"
                f"• **총 원화 금액:** **{total_krw_str} KRW** (${total_usd_str})\n\n"
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

def main():
    if TOKEN == "YOUR_TELEGRAM_BOT_TOKEN_HERE":
        print("⚠️ 경고: main.py 파일 내 TOKEN 값에 BotFather에게 받은 실제 텔레그램 봇 토큰을 입력해 주세요.")
        
    app = Application.builder().token(TOKEN).build()

    # USDT 명령어 & 메시지 핸들러
    app.add_handler(CommandHandler("usdt", usdt_command))
    app.add_handler(MessageHandler(filters.Regex(re.compile(r"^/?시세(\s+[0-9,.]+)?$", re.IGNORECASE)), usdt_command))
    app.add_handler(MessageHandler(filters.Regex(re.compile(r"^/?([0-9,]+(?:\.[0-9]+)?)\s*(?:테더|usdt)$", re.IGNORECASE)), usdt_command))

    # TON (Toncoin) 명령어 & 메시지 핸들러
    app.add_handler(CommandHandler("ton", ton_command))
    app.add_handler(MessageHandler(filters.Regex(re.compile(r"^/?톤(\s+[0-9,.]+)?$", re.IGNORECASE)), ton_command))
    app.add_handler(MessageHandler(filters.Regex(re.compile(r"^/?([0-9,]+(?:\.[0-9]+)?)\s*(?:톤|ton)$", re.IGNORECASE)), ton_command))

    # Render 웹 서비스 헬스체크용 웹서버 데몬 스레드 실행
    threading.Thread(target=start_health_check_server, daemon=True).start()

    print("텔레그램 코인 시세/계산 봇이 실행 중입니다... (종료하려면 Ctrl+C)")
    app.run_polling()

if __name__ == "__main__":
    main()
