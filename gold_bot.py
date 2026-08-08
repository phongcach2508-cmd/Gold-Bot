import ccxt.async_support as ccxt
import asyncio
import os
import sys
import json
import time
import math
import logging
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
import threading
import urllib.request
import urllib.error

# Khắc phục hiển thị tiếng Việt trên Windows Console
try:
    sys.stdout.reconfigure(encoding='utf-8')
except AttributeError:
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("okx-gold-paper")

# Tải cấu hình từ tệp .env nếu có (chạy cục bộ)
if os.path.exists(".env"):
    try:
        with open(".env", "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ[k.strip()] = v.strip().strip('"').strip("'")
    except Exception as e:
        logger.warning(f"⚠️ Không thể đọc file .env: {e}")

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
    logger.error("🔴 Lỗi: Chưa cấu hình TELEGRAM_BOT_TOKEN hoặc TELEGRAM_CHAT_ID trong tệp .env hoặc biến môi trường!")
    sys.exit(1)

SYMBOL = "XAU/USDT:USDT"                     # Mã sản phẩm Vàng trên OKX
SYMBOL_ID = "XAU-USDT-SWAP"                 # ID giao dịch thực tế
INTERVAL = "15m"                            # Khung thời gian quét chính
PORTFOLIO_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "okx_paper_gold_portfolio.json")

# Tham số chiến thuật & Quản trị rủi ro (ĐÃ NÂNG CẤP V2 - DIỆT TẢO PHÍ SÀN)
RISK_PERCENT = 2.0                          # Rủi ro 2% tài khoản mỗi lệnh
INITIAL_BALANCE = 100.0                     # Vốn khởi tạo
SL_PCT = 0.006                              # Dừng lỗ 0.6% (Mở rộng để hạ số lượng HĐ & diệt tảo phí)
TP_PCT = 0.012                              # Chốt lời 1.2% (R:R = 2.0R - Lãi gấp đôi lỗ)
TAKER_FEE_RATE = 0.0005                     # Phí sàn OKX Taker 0.05% mỗi chiều

portfolio = {}
exchange = ccxt.okx({'enableRateLimit': True})

def save_portfolio():
    try:
        os.makedirs(os.path.dirname(PORTFOLIO_FILE), exist_ok=True)
        with open(PORTFOLIO_FILE, "w", encoding="utf-8") as f:
            json.dump(portfolio, f, indent=4, ensure_ascii=False)
        logger.info("💾 Đã lưu trạng thái danh mục giả lập Vàng (Có phí sàn).")
    except Exception as e:
        logger.error(f"🔴 Lỗi ghi file portfolio Vàng: {e}")

def load_portfolio():
    global portfolio
    if os.path.exists(PORTFOLIO_FILE):
        try:
            with open(PORTFOLIO_FILE, "r", encoding="utf-8") as f:
                portfolio = json.load(f)
            if "total_fees_paid" not in portfolio:
                portfolio["total_fees_paid"] = 0.0
            logger.info(f"💾 Đã nạp danh mục Vàng. Số dư: {portfolio.get('balance', INITIAL_BALANCE):.2f} USDT")
        except Exception as e:
            logger.error(f"🔴 Lỗi đọc file portfolio Vàng: {e}")
            init_new_portfolio()
    else:
        init_new_portfolio()

def init_new_portfolio():
    global portfolio
    portfolio = {
        "balance": INITIAL_BALANCE,
        "total_fees_paid": 0.0,
        "position": None,
        "trades_history": [],
        "last_signal_time": 0
    }
    save_portfolio()
    logger.info("🆕 Đã khởi tạo danh mục giả lập Vàng mới với vốn 100.0 USDT.")

def send_telegram_message(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML"
    }
    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode('utf-8'),
            headers={'Content-Type': 'application/json'}
        )
        with urllib.request.urlopen(req, timeout=5) as response:
            pass
    except Exception as e:
        logger.error(f"🔴 Lỗi gửi tin nhắn Telegram: {e}")

# ==========================================================
# CÁC CHỈ BÁO KỸ THUẬT
# ==========================================================
def calculate_ema(prices, length):
    ema = [0.0] * len(prices)
    if len(prices) < length: return ema
    sma = sum(prices[:length]) / length
    ema[length - 1] = sma
    alpha = 2.0 / (length + 1)
    for i in range(length, len(prices)):
        ema[i] = prices[i] * alpha + ema[i - 1] * (1 - alpha)
    return ema

def calculate_vol_ma(volumes, length=20):
    ma = [0.0] * len(volumes)
    if len(volumes) < length: return ma
    for i in range(length - 1, len(volumes)):
        ma[i] = sum(volumes[i - length + 1 : i + 1]) / length
    return ma

def calculate_adx(candles, length=14):
    n = len(candles)
    adx = [0.0] * n
    if n <= length * 2: return adx
    dm_plus, dm_minus, tr_list = [0.0]*n, [0.0]*n, [0.0]*n
    for i in range(1, n):
        high_diff = candles[i]["high"] - candles[i-1]["high"]
        low_diff = candles[i-1]["low"] - candles[i]["low"]
        dm_plus[i] = high_diff if high_diff > low_diff and high_diff > 0 else 0.0
        dm_minus[i] = low_diff if low_diff > high_diff and low_diff > 0 else 0.0
        tr_list[i] = max(candles[i]["high"] - candles[i-1]["high"], abs(candles[i]["high"] - candles[i-1]["close"]), abs(candles[i]["low"] - candles[i-1]["close"]))
    sm_tr = sum(tr_list[1:length+1])
    sm_dm_plus = sum(dm_plus[1:length+1])
    sm_dm_minus = sum(dm_minus[1:length+1])
    di_plus, di_minus, dx = [0.0]*n, [0.0]*n, [0.0]*n
    for i in range(length, n):
        if i > length:
            sm_tr = sm_tr - sm_tr / length + tr_list[i]
            sm_dm_plus = sm_dm_plus - sm_dm_plus / length + dm_plus[i]
            sm_dm_minus = sm_dm_minus - sm_dm_minus / length + dm_minus[i]
        if sm_tr > 0:
            di_plus[i] = 100.0 * sm_dm_plus / sm_tr
            di_minus[i] = 100.0 * sm_dm_minus / sm_tr
            dsum = di_plus[i] + di_minus[i]
            if dsum > 0: dx[i] = 100.0 * abs(di_plus[i] - di_minus[i]) / dsum
    start = length * 2
    adx[start] = sum(dx[length:start]) / length
    for i in range(start + 1, n):
        adx[i] = (adx[i-1] * (length - 1) + dx[i]) / length
    return adx

def is_weekend():
    vn_time = datetime.now(timezone(timedelta(hours=7)))
    weekday = vn_time.weekday()
    hour = vn_time.hour
    if weekday == 4 and hour >= 22: return True
    if weekday == 5: return True
    if weekday == 6: return True
    if weekday == 0 and hour < 6: return True
    return False

# ==========================================================
# QUÉT VÀ QUẢN LÝ LỆNH VÀNG (CÓ TRỪ PHÍ SÀN THẬT)
# ==========================================================
async def check_active_position(current_price, high_price, low_price):
    global portfolio
    pos = portfolio.get("position")
    if not pos: return
        
    entry_price = pos["entry_price"]
    sl = pos["sl"]
    tp = pos["tp"]
    pos_type = pos["type"]
    contracts = pos["contracts"]
    contract_size = 0.001 # 1 OKX Gold contract = 0.001 oz
    
    closed = False
    exit_price = 0.0
    gross_pnl = 0.0
    outcome = ""
    
    if pos_type == "LONG":
        if low_price <= sl:
            closed = True
            exit_price = sl
            gross_pnl = (sl - entry_price) * contracts * contract_size
            outcome = "DỪNG LỖ (SL)"
        elif high_price >= tp:
            closed = True
            exit_price = tp
            gross_pnl = (tp - entry_price) * contracts * contract_size
            outcome = "CHỐT LỜI (TP)"
    else: # SHORT
        if high_price >= sl:
            closed = True
            exit_price = sl
            gross_pnl = (entry_price - sl) * contracts * contract_size
            outcome = "DỪNG LỖ (SL)"
        elif low_price <= tp:
            closed = True
            exit_price = tp
            gross_pnl = (entry_price - tp) * contracts * contract_size
            outcome = "CHỐT LỜI (TP)"
            
    if closed:
        exit_val = exit_price * contracts * contract_size
        exit_fee = exit_val * TAKER_FEE_RATE
        net_pnl = gross_pnl - exit_fee
        
        portfolio["balance"] += net_pnl
        portfolio["total_fees_paid"] += exit_fee
        
        trade_log = {
            "symbol": SYMBOL_ID,
            "type": pos_type,
            "entry_price": entry_price,
            "exit_price": exit_price,
            "contracts": contracts,
            "gross_pnl": round(gross_pnl, 4),
            "exit_fee": round(exit_fee, 4),
            "net_pnl": round(net_pnl, 4),
            "outcome": outcome,
            "time": int(time.time())
        }
        portfolio["trades_history"].append(trade_log)
        portfolio["position"] = None
        save_portfolio()
        
        emoji = "🟢" if net_pnl > 0 else "🔴"
        msg = (
            f"{emoji} <b>[MÔ PHỎNG VÀNG - ĐÓNG LỆNH] {SYMBOL_ID} ({outcome})</b>\n\n"
            f"🎟️ <b>Loại vị thế:</b> {pos_type} ({contracts} contracts)\n"
            f"💵 <b>Giá vào:</b> {entry_price:.1f} | <b>Giá đóng:</b> {exit_price:.1f}\n"
            f"💵 <b>Lợi nhuận gộp:</b> {gross_pnl:+.4f} USDT\n"
            f"💸 <b>Phí đóng lệnh OKX (0.05%):</b> -{exit_fee:.4f} USDT\n"
            f"💰 <b>LỢI NHUẬN RÒNG THỰC TẾ:</b> <b>{net_pnl:+.4f} USDT</b>\n\n"
            f"📊 <b>Số dư tài khoản:</b> <b>{portfolio['balance']:.2f} USDT</b>\n"
            f"💸 <b>Tổng phí sàn tích lũy:</b> {portfolio['total_fees_paid']:.4f} USDT"
        )
        send_telegram_message(msg)
        logger.info(f"✅ Đã đóng vị thế giả lập XAUUSD: {outcome} | LN Ròng: {net_pnl:+.4f} USDT")

async def force_close_weekend(current_price):
    global portfolio
    pos = portfolio.get("position")
    if not pos: return
        
    pos_type = pos["type"]
    entry_price = pos["entry_price"]
    contracts = pos["contracts"]
    contract_size = 0.001
    
    if pos_type == "LONG":
        gross_pnl = (current_price - entry_price) * contracts * contract_size
    else:
        gross_pnl = (entry_price - current_price) * contracts * contract_size
        
    exit_val = current_price * contracts * contract_size
    exit_fee = exit_val * TAKER_FEE_RATE
    net_pnl = gross_pnl - exit_fee
    
    portfolio["balance"] += net_pnl
    portfolio["total_fees_paid"] += exit_fee
    
    trade_log = {
        "symbol": SYMBOL_ID,
        "type": pos_type,
        "entry_price": entry_price,
        "exit_price": current_price,
        "outcome": "ĐÓNG CUỐI TUẦN (FORCE CLOSE)",
        "gross_pnl": round(gross_pnl, 4),
        "exit_fee": round(exit_fee, 4),
        "net_pnl": round(net_pnl, 4),
        "time": int(time.time())
    }
    portfolio["trades_history"].append(trade_log)
    portfolio["position"] = None
    save_portfolio()
    
    msg = (
        f"⏳ <b>[MÔ PHỎNG VÀNG - ĐÓNG LỆNH CUỐI TUẦN] {SYMBOL_ID}</b>\n\n"
        f"🎟️ <b>Loại vị thế:</b> {pos_type}\n"
        f"💵 <b>Giá vào:</b> {entry_price:.1f} | <b>Giá đóng:</b> {current_price:.1f}\n"
        f"💸 <b>Phí đóng lệnh:</b> -{exit_fee:.4f} USDT\n"
        f"💰 <b>Lợi nhuận ròng:</b> {net_pnl:+.4f} USDT\n"
        f"📊 <b>Số dư tài khoản:</b> {portfolio['balance']:.2f} USDT"
    )
    send_telegram_message(msg)
    logger.info(f"⚠️ Đóng vị thế cuối tuần bắt buộc cho XAUUSD ở giá {current_price:.1f}")

async def scan_market():
    global portfolio
    
    if is_weekend():
        pos = portfolio.get("position")
        if pos:
            try:
                ticker = await exchange.fetch_ticker(SYMBOL)
                await force_close_weekend(ticker["last"])
            except Exception as e:
                logger.error(f"🔴 Lỗi lấy giá đóng lệnh cuối tuần: {e}")
        return
        
    try:
        raw_candles = await exchange.fetch_ohlcv(SYMBOL, INTERVAL, limit=250)
        if len(raw_candles) < 220: return
            
        c = [{"time": int(o[0]), "open": float(o[1]), "high": float(o[2]), "low": float(o[3]), "close": float(o[4]), "volume": float(o[5])} for o in raw_candles]
        
        idx = len(c) - 2
        candle_time = int(c[idx]["time"])
        
        if candle_time <= portfolio.get("last_signal_time", 0):
            return
            
        close = c[idx]["close"]
        volume = c[idx]["volume"]
        
        current_price = c[-1]["close"]
        current_high = c[-1]["high"]
        current_low = c[-1]["low"]
        
        if portfolio.get("position"):
            await check_active_position(current_price, current_high, current_low)
            return
            
        closes_all = [x["close"] for x in c]
        vols_all = [x["volume"] for x in c]
        
        vol_ma = calculate_vol_ma(vols_all, 20)
        adx = calculate_adx(c, 14)
        adx_val = adx[idx]
        
        window = c[idx-20:idx]
        res_level = max(x["high"] for x in window)
        sup_level = min(x["low"] for x in window)
        
        is_long = close > res_level and volume > 1.5 * vol_ma[idx] and adx_val >= 25.0
        is_short = close < sup_level and volume > 1.5 * vol_ma[idx] and adx_val >= 25.0
        
        contract_size = 0.001 # 1 contract = 0.001 oz
        
        if is_long:
            risk_amount = portfolio["balance"] * (RISK_PERCENT / 100.0)
            sl_price = close * (1.0 - SL_PCT)
            tp_price = close * (1.0 + TP_PCT)
            
            ounces = risk_amount / (close * SL_PCT)
            contracts = int(round(ounces / contract_size))
            if contracts < 1: contracts = 1
            
            entry_val = close * contracts * contract_size
            entry_fee = entry_val * TAKER_FEE_RATE
            
            # Trừ phí mở lệnh ngay vào tài khoản
            portfolio["balance"] -= entry_fee
            portfolio["total_fees_paid"] += entry_fee
            
            portfolio["position"] = {
                "type": "LONG",
                "entry_price": close,
                "sl": sl_price,
                "tp": tp_price,
                "contracts": contracts,
                "risk_amount": risk_amount,
                "entry_val": entry_val,
                "entry_fee": entry_fee,
                "entry_time": int(time.time())
            }
            portfolio["last_signal_time"] = candle_time
            save_portfolio()
            
            msg = (
                f"🟢 <b>[MÔ PHỎNG VÀNG - MỞ LỆNH] LONG {SYMBOL_ID} (15m)</b>\n\n"
                f"🎟️ <b>Khối lượng:</b> {contracts} Contracts ({contracts * 0.001:.3f} oz)\n"
                f"👉 <b>Giá vào lệnh:</b> {close:.1f}\n"
                f"⚡ <b>Chỉ số ADX:</b> {adx_val:.2f} (>= 20.0)\n"
                f"🛡️ <b>Stop Loss ({SL_PCT*100:.2f}%):</b> {sl_price:.1f} (Rủi ro: -{risk_amount:.2f} USDT)\n"
                f"🎯 <b>Take Profit ({TP_PCT*100:.2f}%):</b> {tp_price:.1f} (Mục tiêu gộp: +{risk_amount * (TP_PCT / SL_PCT):.2f} USDT)\n"
                f"💸 <b>Phí mở lệnh OKX (0.05% Taker):</b> -{entry_fee:.4f} USDT\n\n"
                f"📊 <b>Số dư sau trừ phí mở:</b> {portfolio['balance']:.2f} USDT"
            )
            send_telegram_message(msg)
            logger.info(f"🟢 Mở vị thế giả lập LONG XAUUSD: Entry {close:.1f} | SL {sl_price:.1f} | TP {tp_price:.1f} | Phí: -{entry_fee:.4f} USDT")
            
        elif is_short:
            risk_amount = portfolio["balance"] * (RISK_PERCENT / 100.0)
            sl_price = close * (1.0 + SL_PCT)
            tp_price = close * (1.0 - TP_PCT)
            
            ounces = risk_amount / (close * SL_PCT)
            contracts = int(round(ounces / contract_size))
            if contracts < 1: contracts = 1
            
            entry_val = close * contracts * contract_size
            entry_fee = entry_val * TAKER_FEE_RATE
            
            portfolio["balance"] -= entry_fee
            portfolio["total_fees_paid"] += entry_fee
            
            portfolio["position"] = {
                "type": "SHORT",
                "entry_price": close,
                "sl": sl_price,
                "tp": tp_price,
                "contracts": contracts,
                "risk_amount": risk_amount,
                "entry_val": entry_val,
                "entry_fee": entry_fee,
                "entry_time": int(time.time())
            }
            portfolio["last_signal_time"] = candle_time
            save_portfolio()
            
            msg = (
                f"🔴 <b>[MÔ PHỎNG VÀNG - MỞ LỆNH] SHORT {SYMBOL_ID} (15m)</b>\n\n"
                f"🎟️ <b>Khối lượng:</b> {contracts} Contracts ({contracts * 0.001:.3f} oz)\n"
                f"👉 <b>Giá vào lệnh:</b> {close:.1f}\n"
                f"⚡ <b>Chỉ số ADX:</b> {adx_val:.2f} (>= 20.0)\n"
                f"🛡️ <b>Stop Loss ({SL_PCT*100:.2f}%):</b> {sl_price:.1f} (Rủi ro: -{risk_amount:.2f} USDT)\n"
                f"🎯 <b>Take Profit ({TP_PCT*100:.2f}%):</b> {tp_price:.1f} (Mục tiêu gộp: +{risk_amount * (TP_PCT / SL_PCT):.2f} USDT)\n"
                f"💸 <b>Phí mở lệnh OKX (0.05% Taker):</b> -{entry_fee:.4f} USDT\n\n"
                f"📊 <b>Số dư sau trừ phí mở:</b> {portfolio['balance']:.2f} USDT"
            )
            send_telegram_message(msg)
            logger.info(f"🔴 Mở vị thế giả lập SHORT XAUUSD: Entry {close:.1f} | SL {sl_price:.1f} | TP {tp_price:.1f} | Phí: -{entry_fee:.4f} USDT")
            
    except Exception as e:
        logger.error(f"🔴 Lỗi trong chu kỳ quét thị trường Vàng: {e}")

class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "application/json; charset=utf-8")
        self.end_headers()
        res = {
            "status": "alive",
            "bot": "okx-gold-paper-strategy3",
            "symbol": SYMBOL_ID,
            "balance": portfolio.get("balance", INITIAL_BALANCE),
            "total_fees_paid": portfolio.get("total_fees_paid", 0.0),
            "position": portfolio.get("position"),
            "vietnamese": "Hệ thống mô phỏng Vàng (Chiến thuật 3 + Trừ phí sàn) hoạt động bình thường."
        }
        self.wfile.write(json.dumps(res, ensure_ascii=False).encode('utf-8'))
        
    def log_message(self, format, *args):
        return

def start_health_server():
    port = int(os.environ.get("PORT", 10003))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    logger.info(f"🌐 Health Check Server cho Gold Bot chạy tại cổng {port}")
    server.serve_forever()

async def main_loop():
    logger.info("🚀 KHỞI CHẠY BOT MÔ PHỎNG VÀNG OKX (TẬP TRUNG CHIẾN THUẬT 3 + TRỪ PHÍ SÀN THẬT)...")
    init_new_portfolio()
    
    send_telegram_message(
        f"🚀 <b>BOT MÔ PHỎNG VÀNG OKX (CHIẾN THUẬT 3 + TRỪ PHÍ SÀN THẬT) KHỞI CHẠY!</b>\n\n"
        f"📊 <b>Sản phẩm quét:</b> {SYMBOL_ID} (15m)\n"
        f"📈 <b>Cấu hình chiến thuật 3:</b> S/R Breakout 20 + Vol 1.5x (SL 0.3%, TP 0.45%, Risk 2%)\n"
        f"💸 <b>TỰ ĐỘNG TRỪ PHÍ SÀN OKX THẬT:</b> 0.05% Taker mỗi chiều mở/đóng\n"
        f"💵 <b>Vốn khởi tạo mới:</b> {portfolio.get('balance', INITIAL_BALANCE):.2f} USDT"
    )
    
    t = threading.Thread(target=start_health_server, daemon=True)
    t.start()
    
    while True:
        try:
            await scan_market()
        except Exception as e:
            logger.error(f"🔴 Lỗi vòng lặp chính của Gold Bot: {e}")
            await asyncio.sleep(30)

if __name__ == '__main__':
    try:
        asyncio.run(main_loop())
    except KeyboardInterrupt:
        logger.info("👋 Đã dừng Bot Vàng.")
        sys.exit(0)
