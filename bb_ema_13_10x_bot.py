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

# 13 पेयर्स
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
# 2. एक्सचेंज कनेक्शन (Bybit + KuCoin फ़ॉलबैक)
# ==========================================
def init_exchanges() -> list[tuple[str, ccxt.Exchange]]:
    exchanges = []
    configs = [
        ("Bybit-Linear", ccxt.bybit({"enableRateLimit": True, "options": {"defaultType": "linear"}})),
        ("Bybit-Spot", ccxt.bybit({"enableRateLimit": True, "options": {"defaultType": "spot"}})),
        ("KuCoin-Spot", ccxt.kucoin({"enableRateLimit": True})),
    ]
    for label, ex in configs:
        try:
            ex.load_markets()
            exchanges.append((label, ex))
            print(f"[Connected] {label} ({len(ex.markets)} markets)")
        except Exception as exc:
            print(f"[Connection Warning] {label}: {exc}")
    return exchanges


def fetch_symbol_ohlcv(symbol: str, exchanges: list[tuple[str, ccxt.Exchange]]):
    """Bybit या KuCoin में से जहाँ पेयर उपलब्ध हो, वहाँ से 1h OHLCV डेटा लाता है।"""
    for label, ex in exchanges:
        candidates = [symbol, f"{symbol}:USDT"]
        for candidate in candidates:
            if candidate in ex.markets:
                try:
                    ohlcv = ex.fetch_ohlcv(candidate, timeframe=TIMEFRAME, limit=100)
                    if ohlcv and len(ohlcv) >= 30:
                        return ohlcv, label
                except Exception as exc:
                    print(f"[Fetch Retry] {candidate} on {label}: {exc}")
    return None, None


# ==========================================
# 3. इंडिकेटर गणना (9 EMA, 20 EMA, BB 20,2)
# ==========================================
def calculate_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["ema_9"] = df["close"].ewm(span=EMA_FAST, adjust=False).mean()
    df["ema_20"] = df["close"].ewm(span=EMA_SLOW, adjust=False).mean()

    df["bb_mid"] = df["close"].rolling(window=BB_PERIOD).mean()
    df["bb_std"] = df["close"].rolling(window=BB_PERIOD).std()
    df["bb_upper"] = df["bb_mid"] + (BB_STD * df["bb_std"])
    df["bb_lower"] = df["bb_mid"] - (BB_STD * df["bb_std"])

    # Bollinger Band की चौड़ाई और शुरुआती संकुचन/विस्तार
    df["bb_width"] = (df["bb_upper"] - df["bb_lower"]) / df["bb_mid"].replace(0, np.nan)
    df["bb_width_min_prev"] = df["bb_width"].shift(1).rolling(window=10).min()
    df["bb_width_pct_rank"] = df["bb_width"].shift(1).rolling(window=20).rank(pct=True)

    return df


# ==========================================
# 4. पॉइंट-टू-पॉइंट कैंडल व ट्रेड फ़िल्टर
# ==========================================
def is_clean_candle(candle: pd.Series) -> bool:
    """मजबूत बॉडी वाली कैंडल चुनता है ताकि शुरुआती चाल में विक का शोर कम रहे।"""
    total_range = candle["high"] - candle["low"]
    body = abs(candle["close"] - candle["open"])
    if total_range <= 0:
        return False
    return (body / total_range) >= 0.50


def evaluate_signal(df: pd.DataFrame) -> dict | None:
    """अंतिम पूरी तरह बंद हुई 1h कैंडल (iloc[-2]) पर शुरुआती/स्पष्ट चाल परखता है।"""
    if len(df) < 30:
        return None

    c = df.iloc[-2]
    prev = df.iloc[-3]

    # साइडवेज़ क्लस्टर फ़िल्टर: EMA और मिड-बैंड के बीच बहुत कम अंतर हो तो छोड़ दें
    ema_gap_ratio = abs(c["ema_9"] - c["ema_20"]) / max(c["close"], 1e-9)
    if (
        ema_gap_ratio < 0.0018
        and abs(c["close"] - c["bb_mid"]) / max(c["close"], 1e-9) < 0.0025
    ):
        return None

    if not is_clean_candle(c):
        return None

    # 11 चार्ट्स में दिखने वाली शुरुआती स्क्वीज़ या EMA से निकलने की अवस्था
    was_squeezed = (
        pd.notna(c["bb_width_pct_rank"]) and c["bb_width_pct_rank"] <= 0.45
    ) or (
        pd.notna(c["bb_width_min_prev"])
        and prev["bb_width"] <= c["bb_width_min_prev"] * 1.18
    )

    # LONG सेटअप: बुलिश कैंडल + 9 व 20 EMA के ऊपर क्लोज़ + शुरुआती स्क्वीज़ या पिछली कैंडल पर EMA के नीचे/पास होना
    bullish_setup = (
        c["close"] > c["open"]
        and c["close"] > c["ema_9"]
        and c["close"] > c["ema_20"]
        and c["ema_9"] >= c["ema_20"] * 0.999
        and (was_squeezed or prev["close"] <= prev["ema_20"] * 1.001)
    )

    # SHORT सेटअप: बेयरिश कैंडल + 9 व 20 EMA के नीचे क्लोज़ + शुरुआती स्क्वीज़ या पिछली कैंडल पर EMA के ऊपर/पास होना
    bearish_setup = (
        c["close"] < c["open"]
        and c["close"] < c["ema_9"]
        and c["close"] < c["ema_20"]
        and c["ema_9"] <= c["ema_20"] * 1.001
        and (was_squeezed or prev["close"] >= prev["ema_20"] * 0.999)
    )

    if bullish_setup:
        entry = float(c["close"])
        sl = float(min(c["low"], c["ema_20"]) * 0.998)
        risk = entry - sl
        risk_pct = (risk / entry) * 100 if entry > 0 else 0
        if risk <= 0 or risk_pct < 0.25:
            return None

        return {
            "side": "LONG",
            "mode": "Early Squeeze / 9-20 EMA Move",
            "candle_time": str(c["timestamp"]),
            "entry": entry,
            "sl": sl,
            "t1": entry + (risk * RR_T1),
            "t2": entry + (risk * RR_T2),
            "ema_9": float(c["ema_9"]),
            "ema_20": float(c["ema_20"]),
        }

    if bearish_setup:
        entry = float(c["close"])
        sl = float(max(c["high"], c["ema_20"]) * 1.002)
        risk = sl - entry
        risk_pct = (risk / entry) * 100 if entry > 0 else 0
        if risk <= 0 or risk_pct < 0.25:
            return None

        return {
            "side": "SHORT",
            "mode": "Early Squeeze / 9-20 EMA Move",
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
# 5. डुप्लिकेट अलर्ट रोकना और Telegram अलर्ट
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


def format_alert(symbol: str, sig: dict, source_label: str) -> str:
    if sig["side"] == "LONG":
        direction_text = f"🟢 LONG ({sig['mode']})"
        trail_text = f"Close below 20 EMA ({format_price(sig['ema_20'])})"
    else:
        direction_text = f"🔴 SHORT ({sig['mode']})"
        trail_text = f"Close beyond 20 EMA ({format_price(sig['ema_20'])})"

    return (
        f"🔱🏛️ हंसराज मेघवाल 🏛️🔱\n"
        f"📊 BB SQUEEZE + 9/20 EMA TRADING ALERT 📊\n\n"
        f"🪙 Coin Pair: {symbol} ({source_label})\n"
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
# 6. मुख्य रन लूप
# ==========================================
def run_bot_once() -> None:
    exchanges = init_exchanges()
    if not exchanges:
        raise RuntimeError("Bybit या KuCoin किसी से भी कनेक्शन नहीं बन पाया।")

    state = load_state()
    fetched_count = 0

    for symbol in SYMBOLS:
        try:
            ohlcv, source_label = fetch_symbol_ohlcv(symbol, exchanges)
            if not ohlcv:
                print(f"[Not Available] {symbol} (Bybit/KuCoin पर उपलब्ध नहीं)")
                continue

            fetched_count += 1
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
                    msg = format_alert(symbol, sig, source_label)
                    send_telegram_message(msg)
                    state[symbol] = signal_key
                    save_state(state)
                    print(f"[Alert Sent] {signal_key} via {source_label}")
                else:
                    print(f"[Skip Duplicate] {signal_key}")
            else:
                print(f"[No Signal] {symbol} ({TIMEFRAME}) [{source_label}]")

        except Exception as exc:
            print(f"[Error] {symbol}: {exc}")

    if fetched_count == 0:
        raise RuntimeError("किसी भी पेयर के लिए OHLCV डेटा प्राप्त नहीं हो सका।")


if __name__ == "__main__":
    print("1h BB Squeeze + 9/20 EMA (13 Coins - Point-to-Point) बॉट शुरू हो रहा है...")
    run_bot_once()
