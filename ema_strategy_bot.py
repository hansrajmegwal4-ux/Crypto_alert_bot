import os
import json
import requests
import ccxt
import pandas as pd

# ================== कॉन्फ़िगरेशन ==================
TELEGRAM_TOKEN = os.getenv(
    "TELEGRAM_TOKEN",
    "8463951584:AAHDD47KSQJoQAURgsBiHRDNbWMfuhdyrxM",
)  #[span_1](start_span)[span_1](end_span)
TELEGRAM_CHAT_ID = os.getenv(
    "TELEGRAM_CHAT_ID",
    "6292109019",
)  #[span_2](start_span)[span_2](end_span)

COINS = [
    "BTC/USDT",
    "ETH/USDT",
    "SOL/USDT",
    "BNB/USDT",
    "XRP/USDT",
    "ADA/USDT",
    "AVAX/USDT",
    "LINK/USDT",
    "DOT/USDT",
    "SUI/USDT",
    "LTC/USDT",
    "TRX/USDT",
]

TIMEFRAMES = ["15m", "1h"]
STATE_FILE = "last_alerts.json"

# सुरक्षा / कन्फर्मेशन पैरामीटर
SL_BUFFER_PCT = 0.005        # 0.5% अतिरिक्त स्टॉप-लॉस बफ़र
MAX_EXTENSION_PCT = 4.5      # असामान्य रूप से चौड़े मूव/स्पाइक से बचाव
UPPER_WICK_RATIO = 0.35      # कम से कम 35% ऊपरी रिजेक्शन विक


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
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram credentials missing. Set TELEGRAM_TOKEN and TELEGRAM_CHAT_ID.")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
    }
    try:
        response = requests.post(url, json=payload, timeout=10)
        response.raise_for_status()
    except Exception as e:
        print(f"Telegram Error: {e}")


def fetch_data(exchange, symbol, timeframe, limit=100):
    ohlcv = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
    df = pd.DataFrame(
        ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"]
    )
    return df


def analyze_strategy(df):
    if len(df) < 30:
        return None

    # 1. 9 EMA और 20 EMA की गणना
    df["ema9"] = df["close"].ewm(span=9, adjust=False).mean()
    df["ema20"] = df["close"].ewm(span=20, adjust=False).mean()

    # आखिरी पूरी तरह बंद हुई कैंडल (-2) और पिछली कैंडल्स
    curr = df.iloc[-2]
    prev = df.iloc[-3]
    prev2 = df.iloc[-4]
    recent_swing_candles = df.iloc[-10:-2]  # पिछले 8 बंद कैंडल्स का स्विंग दायरा

    # 2. जिग-जैग साइडवेज़ फ़िल्टर (पिछली 6 बंद कैंडल्स में >= 2 क्रॉस हों तो स्किप करें)
    crosses = 0
    for i in range(-6, -1):
        c0 = df.iloc[i]
        c1 = df.iloc[i - 1]
        if (c0["ema9"] > c0["ema20"] and c1["ema9"] <= c1["ema20"]) or (
            c0["ema9"] < c0["ema20"] and c1["ema9"] >= c1["ema20"]
        ):
            crosses += 1

    if crosses >= 2:
        return None

    candle_ts = int(curr["timestamp"])
    candle_range = max(curr["high"] - curr["low"], 1e-9)

    # ================== BULLISH (BUY) सेटअप ==================
    is_uptrend = (
        (curr["ema9"] > curr["ema20"])
        and (prev["ema9"] > prev["ema20"])
        and (prev2["ema9"] >= prev2["ema20"])
    )

    if is_uptrend:
        pullback_zone = max(curr["ema9"], curr["ema20"]) * 1.002
        pullback_hit = (curr["low"] <= pullback_zone) or (prev["low"] <= pullback_zone)

        prev_was_strong_red = (
            (prev["close"] < prev["open"])
            and ((prev["open"] - prev["close"]) > (candle_range * 0.6))
        )

        lower_wick = min(curr["open"], curr["close"]) - curr["low"]
        strong_lower_rejection = (lower_wick / candle_range) >= UPPER_WICK_RATIO

        bull_confirm = (
            (curr["close"] > curr["open"])
            and (curr["close"] > curr["ema9"])
            and (curr["close"] > curr["ema20"])
            and strong_lower_rejection
            and (not prev_was_strong_red)
        )

        if pullback_hit and bull_confirm:
            entry = float(curr["close"])
            swing_low = float(recent_swing_candles["low"].min())
            raw_sl = min(swing_low, float(curr["ema20"])) * (1.0 - SL_BUFFER_PCT)
            stop_loss = min(raw_sl, entry * 0.992)

            risk = entry - stop_loss
            risk_pct = (risk / entry) * 100 if entry > 0 else 0
            if risk <= 0 or risk_pct > MAX_EXTENSION_PCT:
                return None

            target_1 = entry + (risk * 1.5)
            target_2 = entry + (risk * 2.0)
            target_3 = entry + (risk * 3.0)

            return {
                "id": f"{candle_ts}_BUY",
                "type": "BUY / LONG 🟢",
                "trend_note": "Uptrend Pullback + Strong Lower Rejection",
                "entry": entry,
                "stop_loss": stop_loss,
                "target_1": target_1,
                "target_2": target_2,
                "target_3": target_3,
                "ema9": float(curr["ema9"]),
                "ema20": float(curr["ema20"]),
                "exit_rule": "यदि कैंडल 20 EMA के नीचे क्लोज़ हो तो ट्रेड से निकलें/ट्रेल करें।",
            }

    # ================== BEARISH (SELL) सेटअप ==================
    is_downtrend = (
        (curr["ema9"] < curr["ema20"])
        and (prev["ema9"] < prev["ema20"])
        and (prev2["ema9"] <= prev2["ema20"])
    )

    if is_downtrend:
        pullback_zone = min(curr["ema9"], curr["ema20"]) * 0.998
        pullback_hit = (curr["high"] >= pullback_zone) or (prev["high"] >= pullback_zone)

        prev_was_strong_green = (
            (prev["close"] > prev["open"])
            and ((prev["close"] - prev["open"]) > (candle_range * 0.6))
        )

        upper_wick = curr["high"] - max(curr["open"], curr["close"])
        strong_upper_rejection = (upper_wick / candle_range) >= UPPER_WICK_RATIO

        bear_confirm = (
            (curr["close"] < curr["open"])
            and (curr["close"] < curr["ema9"])
            and (curr["close"] < curr["ema20"])
            and strong_upper_rejection
            and (not prev_was_strong_green)
        )

        if pullback_hit and bear_confirm:
            entry = float(curr["close"])
            swing_high = float(recent_swing_candles["high"].max())
            raw_sl = max(swing_high, float(curr["ema20"])) * (1.0 + SL_BUFFER_PCT)
            stop_loss = max(raw_sl, entry * 1.008)

            risk = stop_loss - entry
            risk_pct = (risk / entry) * 100 if entry > 0 else 0
            if risk <= 0 or risk_pct > MAX_EXTENSION_PCT:
                return None

            target_1 = entry - (risk * 1.5)
            target_2 = entry - (risk * 2.0)
            target_3 = entry - (risk * 3.0)

            return {
                "id": f"{candle_ts}_SELL",
                "type": "SELL / SHORT 🔴",
                "trend_note": "Downtrend Pullback + Strong Upper Rejection",
                "entry": entry,
                "stop_loss": stop_loss,
                "target_1": target_1,
                "target_2": target_2,
                "target_3": target_3,
                "ema9": float(curr["ema9"]),
                "ema20": float(curr["ema20"]),
                "exit_rule": "यदि कैंडल 20 EMA के ऊपर क्लोज़ हो तो ट्रेड से निकलें/ट्रेल करें।",
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
                        f"⏱️ <b>Timeframe:</b> {tf}\n"
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

            except Exception as e:
                print(f"Error processing {symbol} ({tf}): {e}")

    if state_changed:
        save_state(state)


if __name__ == "__main__":
    main()
