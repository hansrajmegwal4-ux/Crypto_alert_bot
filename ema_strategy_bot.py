import json
import os
from datetime import datetime
import requests
import pandas as pd
import ccxt


# ==========================================
# कॉन्फ़िगरेशन (Telegram और Strategy)
# ==========================================
TELEGRAM_BOT_TOKEN = os.getenv(
    "TELEGRAM_BOT_TOKEN",
   os.getenv("TELEGRAM_BOT_TOKEN")
)
TELEGRAM_CHAT_ID = os.getenv(
    "TELEGRAM_CHAT_ID",
    os.getenv("TELEGRAM_CHAT_ID")
)

EXCHANGE_ID = "binance"
SYMBOLS = [
    "BTC/USDT",
    "ETH/USDT",
    "SOL/USDT",
    "BNB/USDT",
    "XRP/USDT",
]
TIMEFRAME = "15m"        # विकल्प: "15m", "30m", "1h", "4h"
FAST_EMA = 9             # शॉर्ट-टर्म EMA
SLOW_EMA = 20            # मीड-टर्म EMA
REWARD_RATIO = 3.0       # टारगेट रिस्क-रेशियो (1:3)
SWING_LOOKBACK = 6       # स्विंग हाई/लो रेंज के लिए पिछली कैंडल

STATE_FILE = "sent_alerts.json"
MAX_STATE_AGE_SECONDS = 60 * 60 * 24 * 3  # 3 दिन पुराने रिकॉर्ड अपने आप साफ हों


def load_sent_alerts() -> dict:
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception as e:
        print(f"State load warning: {e}")
        return {}


def save_sent_alerts(data: dict) -> None:
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"State save warning: {e}")


def is_already_sent(symbol: str, side: str, timeframe: str, candle_time: str) -> bool:
    sent = load_sent_alerts()
    now_ts = datetime.now().timestamp()

    # पुराने रिकॉर्ड हटाकर फ़ाइल साफ़ रखें
    cleaned = {
        k: v for k, v in sent.items()
        if isinstance(v, (int, float)) and (now_ts - v) < MAX_STATE_AGE_SECONDS
    }

    alert_key = f"{symbol}|{side}|{timeframe}|{candle_time}"
    already_exists = alert_key in cleaned

    if not already_exists:
        cleaned[alert_key] = now_ts

    save_sent_alerts(cleaned)
    return already_exists


def send_telegram_alert(message: str) -> bool:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
    }
    try:
        resp = requests.post(url, json=payload, timeout=10)
        resp.raise_for_status()
        print("Telegram alert sent successfully.")
        return True
    except Exception as e:
        print(f"Telegram error: {e}")
        return False


def create_exchange() -> ccxt.Exchange:
    exchange_cls = getattr(ccxt, EXCHANGE_ID)
    return exchange_cls({"enableRateLimit": True})


def fetch_ohlcv_df(exchange: ccxt.Exchange, symbol: str, timeframe: str, limit: int = 120) -> pd.DataFrame:
    raw = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
    df = pd.DataFrame(raw, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    return df


def is_choppy_market(df: pd.DataFrame, lookback: int = 8) -> bool:
    """
    यदि 9 EMA और 20 EMA हाल की कैंडल्स में बार-बार ऊपर-नीचे क्रॉस कर रहे हैं,
    तो इसे साइडवेज़ / चॉपी मार्केट मानकर फ़िल्टर करता है।
    """
    if len(df) < lookback + 2:
        return False
    recent = df.iloc[-lookback - 1 : -1]
    above = recent["ema_9"] > recent["ema_20"]
    cross_count = int((above != above.shift(1)).iloc[1:].sum())
    return cross_count >= 3


def analyze_9_20_ema_strategy(df: pd.DataFrame) -> dict | None:
    """
    बेसिक 9/20 EMA पुलबैक लॉजिक:
    1. 9 EMA और 20 EMA कैलकुलेट करता है।
    2. साइडवेज़/चॉपी मार्केट से बचता है।
    3. बुलिश ट्रेंड (EMA 9 > EMA 20) में 9 EMA / 20 EMA ज़ोन के पास पुलबैक और बुलिश कन्फ़र्मेशन।
    4. बेयरिश ट्रेंड (EMA 9 < EMA 20) में 9 EMA / 20 EMA ज़ोन के पास पुलबैक और बेयरिश कन्फ़र्मेशन।
    """
    if len(df) < SLOW_EMA + SWING_LOOKBACK + 3:
        return None

    df = df.copy()
    df["ema_9"] = df["close"].ewm(span=FAST_EMA, adjust=False).mean()
    df["ema_20"] = df["close"].ewm(span=SLOW_EMA, adjust=False).mean()

    if is_choppy_market(df):
        return None

    last_closed = df.iloc[-2]
    prev_closed = df.iloc[-3]
    swing_slice = df.iloc[-SWING_LOOKBACK - 2 : -2]

    ema9_curr = float(last_closed["ema_9"])
    ema20_curr = float(last_closed["ema_20"])
    open_curr = float(last_closed["open"])
    high_curr = float(last_closed["high"])
    low_curr = float(last_closed["low"])
    close_curr = float(last_closed["close"])
    prev_close = float(prev_closed["close"])

    # बुलिश ट्रेंड पुलबैक (9 EMA > 20 EMA)
    bullish_trend = ema9_curr > ema20_curr
    touched_bull_zone = (low_curr <= ema9_curr * 1.0015) and (close_curr >= ema20_curr * 0.9985)
    bullish_confirmation = (close_curr > open_curr) and (close_curr >= prev_close)

    if bullish_trend and touched_bull_zone and bullish_confirmation:
        swing_low = float(swing_slice["low"].min())
        stop_loss = min(swing_low, low_curr)
        entry = close_curr
        risk = entry - stop_loss
        if risk <= 0:
            return None

        target_1 = entry + risk
        target_2 = entry + (risk * REWARD_RATIO)

        return {
            "side": "LONG",
            "direction_badge": "🟢 LONG (9/20 EMA Pullback)",
            "candle_time": str(last_closed["timestamp"]),
            "ema_9": ema9_curr,
            "ema_20": ema20_curr,
            "entry": entry,
            "stop_loss": stop_loss,
            "target_1": target_1,
            "target_2": target_2,
            "trail_exit": ema20_curr,
            "rr_ratio": REWARD_RATIO,
        }

    # बेयरिश ट्रेंड पुलबैक (9 EMA < 20 EMA)
    bearish_trend = ema9_curr < ema20_curr
    touched_bear_zone = (high_curr >= ema9_curr * 0.9985) and (close_curr <= ema20_curr * 1.0015)
    bearish_confirmation = (close_curr < open_curr) and (close_curr <= prev_close)

    if bearish_trend and touched_bear_zone and bearish_confirmation:
        swing_high = float(swing_slice["high"].max())
        stop_loss = max(swing_high, high_curr)
        entry = close_curr
        risk = stop_loss - entry
        if risk <= 0:
            return None

        target_1 = entry - risk
        target_2 = entry - (risk * REWARD_RATIO)

        return {
            "side": "SHORT",
            "direction_badge": "🔴 SHORT (9/20 EMA Pullback)",
            "candle_time": str(last_closed["timestamp"]),
            "ema_9": ema9_curr,
            "ema_20": ema20_curr,
            "entry": entry,
            "stop_loss": stop_loss,
            "target_1": target_1,
            "target_2": target_2,
            "trail_exit": ema20_curr,
            "rr_ratio": REWARD_RATIO,
        }

    return None


def format_alert(symbol: str, setup: dict) -> str:
    return (
        f"🚨 <b>सफल सिग्नल</b> 🚨\n"
        f"📊 <b>9/20 EMA TRADING ALERT</b> 📊\n\n"
        f"🪙 <b>Coin Pair:</b> <code>{symbol}</code>\n"
        f"⏱️ <b>Timeframe:</b> <code>{TIMEFRAME.upper()}</code>\n"
        f"📈 <b>Direction:</b> {setup['direction_badge']}\n"
        f"🕯️ <b>Candle:</b> <code>{setup['candle_time']}</code>\n"
        f"📉 <b>EMA Levels:</b>\n"
        f"• 9 EMA: <code>{setup['ema_9']:.4f}</code>\n"
        f"• 20 EMA: <code>{setup['ema_20']:.4f}</code>\n"
        f"🎯 <b>Entry Price:</b> <code>{setup['entry']:.4f}</code>\n"
        f"🛑 <b>Stop Loss:</b> <code>{setup['stop_loss']:.4f}</code>\n"
        f"💰 <b>Take Profit Targets:</b>\n"
        f"• Target 1 (1:1): <code>{setup['target_1']:.4f}</code>\n"
        f"• Target 2 (1:{setup['rr_ratio']:.0f}): <code>{setup['target_2']:.4f}</code>\n"
        f"🔄 <b>Trail/Exit Rule:</b> Close beyond 20 EMA (<code>{setup['trail_exit']:.4f}</code>)"
    )


def run_once() -> None:
    exchange = create_exchange()
    for symbol in SYMBOLS:
        try:
            df = fetch_ohlcv_df(exchange, symbol, TIMEFRAME, limit=120)
            setup = analyze_9_20_ema_strategy(df)
            if setup:
                candle_time = setup["candle_time"]
                side = setup["side"]
                if is_already_sent(symbol, side, TIMEFRAME, candle_time):
                    print(f"[{symbol}] Duplicate skipped for {side} ({candle_time}).")
                    continue

                sent = send_telegram_alert(format_alert(symbol, setup))
                if not sent:
                    print(f"[{symbol}] Telegram send failed; state not locked.")
            else:
                print(f"[{symbol}] No 9/20 EMA setup.")
        except Exception as e:
            print(f"[{symbol}] Error: {e}")


if __name__ == "__main__":
    run_once()
