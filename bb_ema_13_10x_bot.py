import os
import json
import requests
import pandas as pd
import numpy as np
import ccxt


# ==========================================
# 1. कॉन्फ़िगरेशन (1h टाइमफ्रेम + 13 पेयर्स)
# ==========================================
TIMEFRAME = "1h"
EMA_FAST = 9
EMA_SLOW = 20
BB_PERIOD = 20
BB_STD = 2.0

# टारगेट अनुपात (Target 1 और Target 2)
RR_T1 = 1.0
RR_T2 = 3.0

# 13 पेयर्स (आवश्यकतानुसार एक्सचेंज/फ़ीड के अनुसार समायोजित करें)
SYMBOLS = [
    "NEAR/USDT",
    "DOT/USDT",
    "PAXG/USDT",
    "CFG/USDT",
    "TRX/USDT",
    "NATGAS/USDT",
    "CL/USDT",
    "ENA/USDT",
    "XPL/USDT",
    "XLM/USDT",
    "DASH/USDT",
    "PORTAL/USDT",
    "TLM/USDT",
]

# Workflow के commit चरण से मेल खाता हुआ स्टेट फ़ाइल नाम
STATE_FILE = "alerts_13_10x_state.json"

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "YOUR_TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "YOUR_TELEGRAM_CHAT_ID")


# ==========================================
# 2. इंडिकेटर गणना (9 EMA, 20 EMA, BB 20,2)
# ==========================================
def calculate_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["ema_9"] = df["close"].ewm(span=EMA_FAST, adjust=False).mean()
    df["ema_20"] = df["close"].ewm(span=EMA_SLOW, adjust=False).mean()

    df["bb_mid"] = df["close"].rolling(window=BB_PERIOD).mean()
    df["bb_std"] = df["close"].rolling(window=BB_PERIOD).std()
    df["bb_upper"] = df["bb_mid"] + (BB_STD * df["bb_std"])
    df["bb_lower"] = df["bb_mid"] - (BB_STD * df["bb_std"])

    # Bollinger Band की चौड़ाई (Squeeze पहचानने के लिए)
    df["bb_width"] = (df["bb_upper"] - df["bb_lower"]) / df["bb_mid"].replace(0, np.nan)
    df["bb_width_min_prev"] = df["bb_width"].shift(1).rolling(window=10).min()
    df["bb_width_pct_rank"] = df["bb_width"].shift(1).rolling(window=20).rank(pct=True)

    return df


# ==========================================
# 3. विक/क्लस्टर फ़िल्टर और सिग्नल लॉजिक
# ==========================================
def is_clean_candle(candle: pd.Series) -> bool:
    """बहुत लंबी विक (spike) वाली कैंडल को फ़िल्टर करता है ताकि बॉडी स्पष्ट हो।"""
    total_range = candle["high"] - candle["low"]
    body = abs(candle["close"] - candle["open"])
    if total_range <= 0:
        return False
    return (body / total_range) >= 0.45


def evaluate_signal(df: pd.DataFrame) -> dict | None:
    """अंतिम पूरी तरह बंद हुई 1h कैंडल (iloc[-2]) पर सिग्नल का मूल्यांकन करता है।"""
    if len(df) < 30:
        return None

    c = df.iloc[-2]
    prev = df.iloc[-3]

    # यदि दोनों EMA आपस में बहुत करीब उलझे हों (Sideways cluster), तो ट्रेड न लें
    ema_gap_ratio = abs(c["ema_9"] - c["ema_20"]) / max(c["close"], 1e-9)
    if (
        ema_gap_ratio < 0.0015
        and abs(c["close"] - c["bb_mid"]) / max(c["close"], 1e-9) < 0.002
    ):
        return None

    if not is_clean_candle(c):
        return None

    was_squeezed = (
        pd.notna(c["bb_width_pct_rank"]) and c["bb_width_pct_rank"] <= 0.40
    ) or (
        pd.notna(c["bb_width_min_prev"])
        and prev["bb_width"] <= c["bb_width_min_prev"] * 1.15
    )

    # LONG सेटअप
    bullish_breakout = (
        c["close"] > c["open"]
        and c["close"] > c["ema_9"]
        and c["close"] > c["ema_20"]
        and c["ema_9"] >= c["ema_20"] * 0.998
        and (was_squeezed or prev["close"] <= prev["ema_20"])
    )

    # SHORT सेटअप
    bearish_breakdown = (
        c["close"] < c["open"]
        and c["close"] < c["ema_9"]
        and c["close"] < c["ema_20"]
        and c["ema_9"] <= c["ema_20"] * 1.002
        and (was_squeezed or prev["close"] >= prev["ema_20"])
    )

    if bullish_breakout:
        entry = float(c["close"])
        sl = float(min(c["low"], c["ema_20"]) * 0.998)
        risk = entry - sl
        if risk <= 0:
            return None

        return {
            "side": "LONG",
            "mode": "BB Squeeze / EMA Breakout",
            "candle_time": str(c["timestamp"]),
            "entry": entry,
            "sl": sl,
            "t1": entry + (risk * RR_T1),
            "t2": entry + (risk * RR_T2),
            "ema_9": float(c["ema_9"]),
            "ema_20": float(c["ema_20"]),
        }

    if bearish_breakdown:
        entry = float(c["close"])
        sl = float(max(c["high"], c["ema_20"]) * 1.002)
        risk = sl - entry
        if risk <= 0:
            return None

        return {
            "side": "SHORT",
            "mode": "BB Squeeze / EMA Breakdown",
            "candle_time": str(c["timestamp"]),
            "entry": entry,
            "sl": sl,
            "t1": entry - (risk * RR_T1),
            "t2": entry - (risk * RR_T2),
            "ema_9": float(c["ema_9"]),
            "ema_20": float(c["ema_20"]),
        }

    return None


# ==========================================
# 4. डुप्लिकेट अलर्ट रोकना और Telegram अलर्ट
# ==========================================
def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def format_price(val: float) -> str:
    if val >= 100:
        return f"{val:.4f}"
    if val >= 1:
        return f"{val:.4f}"
    if val >= 0.01:
        return f"{val:.5f}"
    return f"{val:.6f}"


def send_telegram_message(text: str) -> None:
    if "YOUR_TELEGRAM" in TELEGRAM_BOT_TOKEN or "YOUR_TELEGRAM" in TELEGRAM_CHAT_ID:
        print("[Telegram Preview]\n" + text + "\n" + "-" * 40)
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
    }
    try:
        resp = requests.post(url, json=payload, timeout=15)
        resp.raise_for_status()
    except Exception as exc:
        print(f"Telegram भेजने में त्रुटि: {exc}")


def format_alert(symbol: str, sig: dict) -> str:
    if sig["side"] == "LONG":
        direction_text = f"🟢 LONG ({sig['mode']})"
        trail_text = f"Close below 20 EMA ({format_price(sig['ema_20'])})"
    else:
        direction_text = f"🔴 SHORT ({sig['mode']})"
        trail_text = f"Close beyond 20 EMA ({format_price(sig['ema_20'])})"

    return (
        f"🔱🏛️ हंसराज मेघवाल 🏛️🔱\n"
        f"📊 BB SQUEEZE + 9/20 EMA TRADING ALERT 📊\n\n"
        f"🪙 Coin Pair: {symbol}\n"
        f"⏱️ Timeframe: 1H\n"
        f"📈 Direction: {direction_text}\n"
        f"🕯️ Candle: {sig['candle_time']}\n"
        f"📉 EMA Levels:\n"
        f"• 9 EMA: {format_price(sig['ema_9'])}\n"
        f"• 20 EMA: {format_price(sig['ema_20'])}\n"
        f"🎯 Entry Price: {format_price(sig['entry'])}\n"
        f"🛑 Stop Loss: {format_price(sig['sl'])}\n"
        f"💰 Take Profit Targets:\n"
        f"• Target 1 (1:1): {format_price(sig['t1'])}\n"
        f"• Target 2 (1:3): {format_price(sig['t2'])}\n"
        f"🔄 Trail/Exit Rule: {trail_text}"
    )


# ==========================================
# 5. मुख्य रन लूप
# ==========================================
def run_bot_once() -> None:
    exchange = ccxt.binance({"enableRateLimit": True})
    state = load_state()

    for symbol in SYMBOLS:
        try:
            ohlcv = exchange.fetch_ohlcv(symbol, timeframe=TIMEFRAME, limit=100)
            df = pd.DataFrame(
                ohlcv, columns=["ts", "open", "high", "low", "close", "volume"]
            )
            df["timestamp"] = pd.to_datetime(df["ts"], unit="ms", utc=True).dt.strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            df = calculate_indicators(df)
            sig = evaluate_signal(df)

            if sig:
                signal_key = f"{symbol}_{sig['side']}_{sig['candle_time']}"
                if state.get(symbol) != signal_key:
                    msg = format_alert(symbol, sig)
                    send_telegram_message(msg)
                    state[symbol] = signal_key
                    save_state(state)
                else:
                    print(f"[Skip Duplicate] {signal_key}")
            else:
                print(f"[No Signal] {symbol} ({TIMEFRAME})")

        except ccxt.BadSymbol:
            print(f"[Symbol Not Found] {symbol} — अपने डेटा फ़ीड/एक्सचेंज के अनुसार सिंबल नाम जांचें।")
        except Exception as exc:
            print(f"[Error] {symbol}: {exc}")


if __name__ == "__main__":
    print("1h BB Squeeze + 9/20 EMA (13 Coins) बॉट शुरू हो रहा है...")
    run_bot_once()
