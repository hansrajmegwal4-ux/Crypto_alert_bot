import os
import requests
import pandas as pd
import ccxt


# ==========================================
# कॉन्फ़िगरेशन (Telegram और Strategy)
# ==========================================
TELEGRAM_BOT_TOKEN = os.getenv(
    "TELEGRAM_BOT_TOKEN",
    "8963851584:AAHDD47K5Q20qAURgsBIoMQhBwMfUhdyrxM",
)
TELEGRAM_CHAT_ID = os.getenv(
    "TELEGRAM_CHAT_ID",
    "6292189019",
)

EXCHANGE_ID = "kucoin"
SYMBOLS = [
    "BTC/USDT",
    "ETH/USDT",
    "SOL/USDT",
    "BNB/USDT",
    "XRP/USDT",
]
TIMEFRAME = "15m"         # विकल्प: "15m", "30m", "1h", "4h"
FAST_EMA = 9              # शॉर्ट-टर्म EMA
SLOW_EMA = 20             # मिड-टर्म EMA
REWARD_RATIO = 3.0        # आदर्श रिस्क-रिवॉर्ड (1:3)
SWING_LOOKBACK = 6        # स्विंग हाई/लो स्टॉप लॉस के लिए पिछली कैंडल्स


def send_telegram_alert(message: str) -> None:
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
    except Exception as e:
        print(f"Telegram error: {e}")


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
    तो इसे रेंज-बाउंड / साइडवेज़ मार्केट मानकर स्किप करता है।
    """
    recent = df.iloc[-lookback - 1 : -1]
    above = recent["ema_9"] > recent["ema_20"]
    cross_count = int((above != above.shift(1)).iloc[1:].sum())
    return cross_count >= 3


def analyze_9_20_ema_strategy(df: pd.DataFrame) -> dict | None:
    """
    वेरिफ़ाइड 9/20 EMA स्ट्रैटेजी लॉजिक:
    1. 9 EMA और 20 EMA निकालता है।
    2. रेंज-बाउंड/चॉपी मार्केट से बचता है।
    3. बुलिश ट्रेंड (EMA 9 > EMA 20) में 9 EMA / 9-20 ज़ोन के पास पुलबैक और बुलिश कन्फर्मेशन पर LONG देता है।
    4. बेयरिश ट्रेंड (EMA 9 < EMA 20) में 9 EMA / 9-20 ज़ोन के पास पुलबैक और बेयरिश कन्फर्मेशन पर SHORT देता है।
    """
    if len(df) < SLOW_EMA + SWING_LOOKBACK + 5:
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
    prev_high = float(prev_closed["high"])
    prev_low = float(prev_closed["low"])

    # बुलिश ट्रेंड: 9 EMA ऊपर, 20 EMA नीचे और विस्तार (ema9 > ema20)
    bullish_trend = ema9_curr > ema20_curr and (ema9_curr - ema20_curr) / ema20_curr >= 0.0015
    touched_bull_zone = (low_curr <= ema9_curr * 1.0015) and (close_curr >= ema20_curr)
    bullish_confirmation = (close_curr > open_curr) and (close_curr > prev_high)

    if bullish_trend and touched_bull_zone and bullish_confirmation:
        swing_low = float(swing_slice["low"].min())
        stop_loss = min(swing_low, low_curr)
        entry = close_curr
        risk = entry - stop_loss
        if risk <= 0:
            return None

        target_1 = entry + (risk * 2.0)
        target_2 = entry + (risk * REWARD_RATIO)

        return {
            "side": "LONG (9/20 EMA Pullback)",
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

    # बेयरिश ट्रेंड: 9 EMA नीचे, 20 EMA ऊपर और विस्तार (ema9 < ema20)
    bearish_trend = ema9_curr < ema20_curr and (ema20_curr - ema9_curr) / ema20_curr >= 0.0015
    touched_bear_zone = (high_curr >= ema9_curr * 0.9985) and (close_curr <= ema20_curr)
    bearish_confirmation = (close_curr < open_curr) and (close_curr < prev_low)

    if bearish_trend and touched_bear_zone and bearish_confirmation:
        swing_high = float(swing_slice["high"].max())
        stop_loss = max(swing_high, high_curr)
        entry = close_curr
        risk = stop_loss - entry
        if risk <= 0:
            return None

        target_1 = entry - (risk * 2.0)
        target_2 = entry - (risk * REWARD_RATIO)

        return {
            "side": "SHORT (9/20 EMA Pullback)",
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
        f"🔱🏛️ <b>हंसराज मेघवाल</b> 🏛️🔱\n"
        f"📈 <b>9/20 EMA TRADING ALERT</b> 📈\n\n"
        f"🪙 <b>Coin Pair:</b> {symbol}\n"
        f"⏱️ <b>Timeframe:</b> {TIMEFRAME.upper()}\n"
        f"🎯 <b>Direction:</b> {setup['direction_badge']}\n"
        f"📅 <b>Candle:</b> <code>{setup['candle_time']}</code>\n\n"
        f"📊 <b>EMA Levels:</b>\n"
        f" • 9 EMA: <code>{setup['ema_9']:.2f}</code>\n"
        f" • 20 EMA: <code>{setup['ema_20']:.2f}</code>\n\n"
        f"⚡ <b>Entry Price:</b> <code>{setup['entry']:.2f}</code>\n"
        f"🛑 <b>Stop Loss:</b> <code>{setup['stop_loss']:.2f}</code>\n\n"
        f"💰 <b>Take Profit Targets:</b>\n"
        f" 🎯 Target 1 (1:2): <code>{setup['target_1']:.2f}</code>\n"
        f" 🚀 Target 2 (1:{setup['rr_ratio']:.0f}): <code>{setup['target_2']:.2f}</code>\n\n"
        f"🛡️ <b>Trail/Exit Rule:</b> Close beyond 20 EMA (<code>{setup['trail_exit']:.2f}</code>)"
    )


def run_once() -> None:
    exchange = create_exchange()
    for symbol in SYMBOLS:
        try:
            df = fetch_ohlcv_df(exchange, symbol, TIMEFRAME, limit=120)
            setup = analyze_9_20_ema_strategy(df)
            if setup:
                send_telegram_alert(format_alert(symbol, setup))
            else:
                print(f"[{symbol}] No 9/20 EMA setup.")
        except Exception as e:
            print(f"[{symbol}] Error: {e}")


if __name__ == "__main__":
    run_once()
