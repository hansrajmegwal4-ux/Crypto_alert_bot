import os
import json
import requests
import ccxt
import pandas as pd

# ================= कॉन्फ़िगरेशन =================
TELEGRAM_TOKEN = os.getenv(
    "TELEGRAM_TOKEN",
    "8963851584:AAHDD47K5Q20qAURgsBIoMQhBwMfUhdyrxM"
)
TELEGRAM_CHAT_ID = os.getenv(
    "TELEGRAM_CHAT_ID",
    "6292189019"
)

COINS = [
    "BTC/USDT",
    "ETH/USDT",
    "SOL/USDT",
    "BNB/USDT",
    "XRP/USDT",
    "ADA/USDT"
]

TIMEFRAMES = ["15m", "1h"]
STATE_FILE = "last_alerts.json"
# ===============================================

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_state(state):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f)
    except Exception as e:
        print(f"State Save Error: {e}")

def send_telegram_alert(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML"
    }
    try:
        response = requests.post(url, json=payload, timeout=10)
        response.raise_for_status()
    except Exception as e:
        print(f"Telegram Error: {e}")

def fetch_data(exchange, symbol, timeframe, limit=100):
    ohlcv = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
    df = pd.DataFrame(ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"])
    return df

def analyze_strategy(df):
    # 1. 9 EMA और 20 EMA की गणना
    df["ema9"] = df["close"].ewm(span=9, adjust=False).mean()
    df["ema20"] = df["close"].ewm(span=20, adjust=False).mean()

    # नवीनतम पूरी तरह बंद हुई कैंडल (-2) और पिछली कैंडल्स
    curr = df.iloc[-2]
    prev = df.iloc[-3]
    prev2 = df.iloc[-4]
    recent_candles = df.iloc[-7:-2]

    # 2. रेंज-बाउंड मार्केट फ़िल्टर
    crosses = 0
    for i in range(-6, -1):
        c1 = df.iloc[i]
        c0 = df.iloc[i - 1]
        if (c0["ema9"] > c0["ema20"] and c1["ema9"] < c1["ema20"]) or \
           (c0["ema9"] < c0["ema20"] and c1["ema9"] > c1["ema20"]):
            crosses += 1

    if crosses >= 2:
        return None

    # 3. EMA एक्सपेंशन चेक
    ema_spread_pct = abs(curr["ema9"] - curr["ema20"]) / curr["close"] * 100
    if ema_spread_pct < 0.15:
        return None

    candle_ts = int(curr["timestamp"])

    # ================= BULLISH (BUY) सेटअप =================
    is_uptrend = (curr["ema9"] > curr["ema20"]) and (prev["ema9"] > prev["ema20"])
    fresh_bull_cross = (prev2["ema9"] <= prev2["ema20"]) and (prev["ema9"] > prev["ema20"])

    if is_uptrend:
        pullback_zone = max(curr["ema9"], curr["ema20"]) * 1.002
        pullback_hit = (curr["low"] <= pullback_zone) or (prev["low"] <= pullback_zone)

        bull_confirm = (
            curr["close"] > curr["open"]
            and curr["close"] > curr["ema9"]
            and curr["close"] > curr["ema20"]
        )

        if pullback_hit and bull_confirm:
            entry = curr["close"]
            swing_low = recent_candles["low"].min()
            stop_loss = min(swing_low, curr["ema20"]) * 0.998
            risk = entry - stop_loss

            if risk > 0:
                target_1 = entry + (risk * 1.5)   # Target 1 (1:1.5 R:R)
                target_2 = entry + (risk * 2.0)   # Target 2 (1:2 R:R)
                target_3 = entry + (risk * 3.0)   # Target 3 (1:3 R:R)

                return {
                    "id": f"{candle_ts}_BUY",
                    "type": "BUY / LONG 🟢",
                    "trend_note": "Fresh 9/20 Bullish Cross + Pullback" if fresh_bull_cross else "Uptrend Pullback + Rejection",
                    "entry": entry,
                    "stop_loss": stop_loss,
                    "target_1": target_1,
                    "target_2": target_2,
                    "target_3": target_3,
                    "ema9": curr["ema9"],
                    "ema20": curr["ema20"],
                    "exit_rule": "यदि कैंडल 20 EMA के नीचे क्लोज़ हो तो ट्रेड से निकलें/ट्रेल करें।"
                }

    # ================= BEARISH (SELL) सेटअप =================
    is_downtrend = (curr["ema9"] < curr["ema20"]) and (prev["ema9"] < prev["ema20"])
    fresh_bear_cross = (prev2["ema9"] >= prev2["ema20"]) and (prev["ema9"] < prev["ema20"])

    if is_downtrend:
        pullback_zone = min(curr["ema9"], curr["ema20"]) * 0.998
        pullback_hit = (curr["high"] >= pullback_zone) or (prev["high"] >= pullback_zone)

        bear_confirm = (
            curr["close"] < curr["open"]
            and curr["close"] < curr["ema9"]
            and curr["close"] < curr["ema20"]
        )

        if pullback_hit and bear_confirm:
            entry = curr["close"]
            swing_high = recent_candles["high"].max()
            stop_loss = max(swing_high, curr["ema20"]) * 1.002
            risk = stop_loss - entry

            if risk > 0:
                target_1 = entry - (risk * 1.5)   # Target 1 (1:1.5 R:R)
                target_2 = entry - (risk * 2.0)   # Target 2 (1:2 R:R)
                target_3 = entry - (risk * 3.0)   # Target 3 (1:3 R:R)

                return {
                    "id": f"{candle_ts}_SELL",
                    "type": "SELL / SHORT 🔴",
                    "trend_note": "Fresh 9/20 Bearish Cross + Pullback" if fresh_bear_cross else "Downtrend Pullback + Rejection",
                    "entry": entry,
                    "stop_loss": stop_loss,
                    "target_1": target_1,
                    "target_2": target_2,
                    "target_3": target_3,
                    "ema9": curr["ema9"],
                    "ema20": curr["ema20"],
                    "exit_rule": "यदि कैंडल 20 EMA के ऊपर क्लोज़ हो तो ट्रेड से निकलें/ट्रेल करें।"
                }

    return None

def main():
    exchange = ccxt.binance({"enableRateLimit": True})
    state = load_state()
    state_changed = False

    for symbol in COINS:
        for tf in TIMEFRAMES:
            try:
                df = fetch_data(exchange, symbol, tf)
                signal = analyze_strategy(df)

                if signal:
                    key = f"{symbol}_{tf}"
                    if state.get(key) == signal["id"]:
                        print(f"Duplicate skipped for {key} ({signal['id']})")
                        continue

                    msg = (
                        f"🚨 <b>9/20 EMA STRATEGY ALERT</b> 🚨\n\n"
                        f"🪙 <b>Coin:</b> {symbol}\n"
                        f"⏱ <b>Timeframe:</b> {tf}\n"
                        f"📊 <b>Signal:</b> {signal['type']}\n"
                        f"📌 <b>Setup:</b> {signal['trend_note']}\n\n"
                        f"🎯 <b>Entry:</b> {signal['entry']:.4f}\n"
                        f"🛑 <b>Stop Loss:</b> {signal['stop_loss']:.4f}\n"
                        f"✅ <b>Target 1 (1:1.5):</b> {signal['target_1']:.4f}\n"
                        f"✅ <b>Target 2 (1:2):</b> {signal['target_2']:.4f}\n"
                        f"🏆 <b>Target 3 (1:3):</b> {signal['target_3']:.4f}\n\n"
                        f"📈 <b>9 EMA:</b> {signal['ema9']:.4f} | <b>20 EMA:</b> {signal['ema20']:.4f}\n"
                        f"⚠️ <i>{signal['exit_rule']}</i>"
                    )
                    send_telegram_alert(msg)
                    state[key] = signal["id"]
                    state_changed = True
                    print(f"Alert sent for {key}")
                else:
                    print(f"No valid setup for {symbol} ({tf})")
            except Exception as e:
                print(f"Error checking {symbol} ({tf}): {e}")

    if state_changed:
        save_state(state)

if __name__ == "__main__":
    main()
