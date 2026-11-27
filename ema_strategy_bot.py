import json
import os
import time
from datetime import datetime
import numpy as np
import pandas as pd
import requests

# ================= CONFIGURATION =================
TELEGRAM_BOT_TOKEN = (
    os.getenv("TELEGRAM_BOT_TOKEN")
    or os.getenv("BOT_TOKEN")
    or "8963851584:AAHDD47K5Q20qAURgsBIoMQhBwMfUhdyrxM"
)
TELEGRAM_CHAT_ID = (
    os.getenv("TELEGRAM_CHAT_ID")
    or os.getenv("CHAT_ID")
    or "6292189019"
)
HEADER_NAME = "हंसराज मेघवाल"

# 50 USDT कॉइन्स की Watchlist
SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT",
    "DOGEUSDT", "ADAUSDT", "AVAXUSDT", "LINKUSDT", "LTCUSDT",
    "SUIUSDT", "TRXUSDT", "DOTUSDT", "POLUSDT", "NEARUSDT",
    "UNIUSDT", "APTUSDT", "ARBUSDT", "OPUSDT", "INJUSDT",
    "ATOMUSDT", "FILUSDT", "ETCUSDT", "PEPEUSDT", "SHIBUSDT",
    "WIFUSDT", "SEIUSDT", "TIAUSDT", "AAVEUSDT", "RNDRUSDT",
    "ENAUSDT", "ONDOUSDT", "BCHUSDT", "XLMUSDT", "HBARUSDT",
    "VETUSDT", "ALGOUSDT", "ICPUSDT", "SANDUSDT", "MANAUSDT",
    "GALAUSDT", "CRVUSDT", "LDOUSDT", "RUNEUSDT", "JUPUSDT",
    "ORDIUSDT", "BONKUSDT", "FLOKIUSDT", "RENDERUSDT", "FETUSDT",
]
TIMEFRAME = "1h"

FAST_EMA = 9
SLOW_EMA = 20
REWARD_RATIO = 3.0
SWING_LOOKBACK = 6

# रिस्क और लेवरेज सेटिंग्स (10% कैपिटल SL और 30% कैपिटल TP)
LEVERAGE = 15
CAPITAL_SL_PCT = 10.0
CAPITAL_TP_PCT = 30.0

STATE_FILE = "sent_alerts.json"
MAX_STATE_AGE_SECONDS = 60 * 60 * 24 * 7  # 7 दिन पुराने रिकॉर्ड साफ़ करने के लिए


# ================= STATE / DEDUPLICATION =================
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

    cleaned = {
        k: v for k, v in sent.items()
        if isinstance(v, (int, float)) and (now_ts - v) <= MAX_STATE_AGE_SECONDS
    }

    alert_key = f"{symbol}|{side}|{timeframe}|{candle_time}"
    already_exists = alert_key in cleaned

    if not already_exists:
        cleaned[alert_key] = now_ts

    save_sent_alerts(cleaned)
    return already_exists


# ================= TELEGRAM NOTIFICATION =================
def send_telegram_alert(message: str) -> bool:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
    }
    try:
        resp = requests.post(url, json=payload, timeout=15)
        resp.raise_for_status()
        print("Telegram alert sent successfully.")
        return True
    except Exception as e:
        print(f"Telegram error: {e}")
        return False


# ================= DATA FETCH (WITH 451 FALLBACKS) =================
def _normalize_symbol(symbol: str) -> str:
    clean = symbol.replace("/", "").replace("-", "").replace(":USDT", "").upper()
    if clean == "RNDRUSDT":
        return "RENDERUSDT"
    return clean


def _fetch_binance_vision(clean_symbol: str, timeframe: str, limit: int) -> pd.DataFrame:
    url = "https://data-api.binance.vision/api/v3/klines"
    params = {"symbol": clean_symbol, "interval": timeframe, "limit": limit}
    resp = requests.get(url, params=params, timeout=15)
    resp.raise_for_status()
    raw = resp.json()

    df = pd.DataFrame(
        raw,
        columns=[
            "open_time", "open", "high", "low", "close", "volume",
            "close_time", "quote_asset_volume", "trades",
            "taker_buy_base", "taker_buy_quote", "ignore"
        ],
    )
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype(float)
    df["timestamp"] = pd.to_datetime(df["open_time"], unit="ms").dt.strftime("%Y-%m-%d %H:%M:%S")
    return df[["open_time", "open", "high", "low", "close", "volume", "timestamp"]]


def _fetch_kucoin(clean_symbol: str, timeframe: str, limit: int) -> pd.DataFrame:
    base = clean_symbol[:-4] if clean_symbol.endswith("USDT") else clean_symbol
    kucoin_symbol = f"{base}-USDT"
    tf_map = {"15m": "15min", "30m": "30min", "1h": "1hour", "4h": "4hour"}
    k_type = tf_map.get(timeframe, "1hour")

    url = "https://api.kucoin.com/api/v1/market/candles"
    params = {"symbol": kucoin_symbol, "type": k_type}
    resp = requests.get(url, params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json().get("data") or []
    if not data:
        raise ValueError(f"Empty KuCoin candles for {kucoin_symbol}")

    rows = list(reversed(data[:limit]))
    df = pd.DataFrame(rows, columns=["open_time_s", "open", "close", "high", "low", "volume", "turnover"])
    df["open_time"] = df["open_time_s"].astype(np.int64) * 1000
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype(float)
    df["timestamp"] = pd.to_datetime(df["open_time"], unit="ms").dt.strftime("%Y-%m-%d %H:%M:%S")
    return df[["open_time", "open", "high", "low", "close", "volume", "timestamp"]]


def _fetch_bybit(clean_symbol: str, timeframe: str, limit: int) -> pd.DataFrame:
    interval_map = {"15m": "15", "30m": "30", "1h": "60", "4h": "240"}
    b_interval = interval_map.get(timeframe, "60")

    url = "https://api.bybit.com/v5/market/kline"
    params = {"category": "spot", "symbol": clean_symbol, "interval": b_interval, "limit": limit}
    resp = requests.get(url, params=params, timeout=15)
    resp.raise_for_status()
    result = (resp.json().get("result") or {}).get("list") or []
    if not result:
        raise ValueError(f"Empty Bybit candles for {clean_symbol}")

    rows = list(reversed(result))
    df = pd.DataFrame(rows, columns=["open_time_ms", "open", "high", "low", "close", "volume", "turnover"])
    df["open_time"] = df["open_time_ms"].astype(np.int64)
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype(float)
    df["timestamp"] = pd.to_datetime(df["open_time"], unit="ms").dt.strftime("%Y-%m-%d %H:%M:%S")
    return df[["open_time", "open", "high", "low", "close", "volume", "timestamp"]]


def fetch_ohlcv_df(symbol: str, timeframe: str = "1h", limit: int = 120) -> pd.DataFrame:
    clean_symbol = _normalize_symbol(symbol)
    last_err = None
    for fetcher in (_fetch_binance_vision, _fetch_kucoin, _fetch_bybit):
        try:
            df = fetcher(clean_symbol, timeframe, limit)
            if len(df) >= 35:
                return df
        except Exception as e:
            last_err = e
    raise RuntimeError(f"All OHLCV sources failed for {clean_symbol}: {last_err}")


# ================= INDICATORS =================
def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["ema_9"] = df["close"].ewm(span=FAST_EMA, adjust=False).mean()
    df["ema_20"] = df["close"].ewm(span=SLOW_EMA, adjust=False).mean()
    df["ema_spread_pct"] = ((df["ema_9"] - df["ema_20"]).abs() / df["ema_20"]) * 100

    # RSI (14)
    delta = df["close"].diff()
    gain = np.where(delta > 0, delta, 0.0)
    loss = np.where(delta < 0, -delta, 0.0)
    avg_gain = pd.Series(gain, index=df.index).ewm(alpha=1 / 14, min_periods=14, adjust=False).mean()
    avg_loss = pd.Series(loss, index=df.index).ewm(alpha=1 / 14, min_periods=14, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    df["rsi"] = 100 - (100 / (1 + rs))

    # Bollinger Bands (20, 2) & Extreme Squeeze (12th percentile over last 40 candles)
    df["bb_mid"] = df["close"].rolling(20).mean()
    bb_std = df["close"].rolling(20).std()
    df["bb_upper"] = df["bb_mid"] + 2 * bb_std
    df["bb_lower"] = df["bb_mid"] - 2 * bb_std
    df["bb_width_pct"] = ((df["bb_upper"] - df["bb_lower"]) / df["bb_mid"].replace(0, np.nan)) * 100
    df["bb_extreme_squeeze"] = df["bb_width_pct"] <= df["bb_width_pct"].rolling(40).quantile(0.12)

    # Volume Spike & Candle Body Metrics
    df["vol_ma20"] = df["volume"].rolling(20).mean()
    df["vol_strong_spike"] = df["volume"] >= (1.8 * df["vol_ma20"])
    df["body"] = (df["close"] - df["open"]).abs()
    df["avg_body"] = df["body"].rolling(14).mean()

    return df


def build_trade_levels(entry: float, side: str) -> dict:
    price_sl_pct = (CAPITAL_SL_PCT / LEVERAGE) / 100.0
    price_tp_pct = (CAPITAL_TP_PCT / LEVERAGE) / 100.0

    if side == "SHORT":
        stop_loss = entry * (1 + price_sl_pct)
        target_2 = entry * (1 - price_tp_pct)
        target_1 = entry - ((entry - target_2) / REWARD_RATIO)
    else:  # LONG
        stop_loss = entry * (1 - price_sl_pct)
        target_2 = entry * (1 + price_tp_pct)
        target_1 = entry + ((target_2 - entry) / REWARD_RATIO)

    return {
        "entry": entry,
        "stop_loss": stop_loss,
        "target_1": target_1,
        "target_2": target_2,
    }


# ================= EXTREME SQUEEZE + STRICT UP/DOWN LOGIC =================
def analyze_strategy(df: pd.DataFrame) -> dict | None:
    if len(df) < 45:
        return None

    df = add_indicators(df)

    c = df.iloc[-2]                 # हाल ही में बंद हुई 1h कैंडल
    p = df.iloc[-3]                 # उससे पिछली 1h कैंडल
    squeeze_window = df.iloc[-7:-2] # पिछली 5 कैंडल्स (4-5 घंटे का स्क्वीज़/कंसोलिडेशन ज़ोन)

    ema9_curr = float(c["ema_9"])
    ema20_curr = float(c["ema_20"])
    bb_mid_curr = float(c["bb_mid"])
    bb_upper_curr = float(c["bb_upper"])
    bb_lower_curr = float(c["bb_lower"])

    open_curr = float(c["open"])
    entry = float(c["close"])
    body_curr = float(c["body"])
    avg_body_curr = float(c["avg_body"])
    rsi_curr = float(c["rsi"])
    rsi_prev = float(p["rsi"])

    # 1. बहुत ज़्यादा Bollinger Band Squeeze + EMA कसाव की पुष्टि
    extreme_squeeze_count = int(squeeze_window["bb_extreme_squeeze"].sum())
    ema_tightness = float(squeeze_window["ema_spread_pct"].max())
    has_extreme_squeeze = (extreme_squeeze_count >= 2) and (ema_tightness <= 0.85)

    # वॉल्यूम स्पाइक पुष्टि (कम से कम 1.8x औसत)
    strong_volume = bool(c["vol_strong_spike"])
    # स्पष्ट कैंडल बॉडी पुष्टि (औसत बॉडी से कम से कम 1.2x मज़बूत)
    strong_body = body_curr >= (1.2 * avg_body_curr)

    # 2. UP (LONG) कन्फ़र्मेशन: Squeeze के बाद मज़बूत हरी क्लोज़ + RSI > 55 (या 50 से ऊपर क्रॉस) + वॉल्यूम स्पाइक
    up_close_confirmed = (
        (entry > open_curr)
        and (entry > ema20_curr)
        and (entry > bb_mid_curr)
        and (entry >= bb_upper_curr * 0.996 or float(c["high"]) > bb_upper_curr)
    )
    up_rsi_confirmed = (rsi_curr >= 55.0) or ((rsi_prev < 50.0) and (rsi_curr >= 52.0))

    if has_extreme_squeeze and up_close_confirmed and up_rsi_confirmed and strong_body and strong_volume:
        levels = build_trade_levels(entry, "LONG")
        return {
            "side": "LONG",
            "direction_badge": "🟢 LONG (Extreme Squeeze UP Breakout)",
            "candle_time": str(c["timestamp"]),
            "ema_9": ema9_curr,
            "ema_20": ema20_curr,
            "trail_exit": ema20_curr,
            "rr_ratio": REWARD_RATIO,
            **levels,
        }

    # 3. DOWN (SHORT) कन्फ़र्मेशन: Squeeze के बाद मज़बूत लाल क्लोज़ + RSI < 45 (या 50 से नीचे क्रॉस) + वॉल्यूम स्पाइक
    down_close_confirmed = (
        (entry < open_curr)
        and (entry < ema20_curr)
        and (entry < bb_mid_curr)
        and (entry <= bb_lower_curr * 1.004 or float(c["low"]) < bb_lower_curr)
    )
    down_rsi_confirmed = (rsi_curr <= 45.0) or ((rsi_prev > 50.0) and (rsi_curr <= 48.0))

    if has_extreme_squeeze and down_close_confirmed and down_rsi_confirmed and strong_body and strong_volume:
        levels = build_trade_levels(entry, "SHORT")
        return {
            "side": "SHORT",
            "direction_badge": "🔴 SHORT (Extreme Squeeze DOWN Breakdown)",
            "candle_time": str(c["timestamp"]),
            "ema_9": ema9_curr,
            "ema_20": ema20_curr,
            "trail_exit": ema20_curr,
            "rr_ratio": REWARD_RATIO,
            **levels,
        }

    return None


# ================= FORMATTER & RUNNER =================
def format_pair_name(symbol: str) -> str:
    clean = _normalize_symbol(symbol)
    if clean.endswith("USDT"):
        return f"{clean[:-4]}/USDT"
    return symbol


def format_alert(symbol: str, setup: dict) -> str:
    pair = format_pair_name(symbol)
    return (
        f"🔱<b>{HEADER_NAME}</b>🔱\n"
        f"🚨<b>सफल सिग्नल</b>🚨\n"
        f"📊<b>9/20 EMA TRADING ALERT</b>📊\n\n"
        f"🪙<b>Coin Pair:</b> <code>{pair}</code>\n"
        f"⏱️<b>Timeframe:</b> <code>{TIMEFRAME.upper()}</code>\n"
        f"📈<b>Direction:</b> {setup['direction_badge']}\n"
        f"🕯️<b>Candle:</b> <code>{setup['candle_time']}</code>\n"
        f"📉<b>EMA Levels:</b>\n"
        f"• 9 EMA: <code>{setup['ema_9']:.4f}</code>\n"
        f"• 20 EMA: <code>{setup['ema_20']:.4f}</code>\n"
        f"🎯<b>Entry Price:</b> <code>{setup['entry']:.4f}</code>\n"
        f"🛑<b>Stop Loss:</b> <code>{setup['stop_loss']:.4f}</code>\n"
        f"💰<b>Take Profit Targets:</b>\n"
        f"• Target 1 (1:1): <code>{setup['target_1']:.4f}</code>\n"
        f"• Target 2 (1:{setup['rr_ratio']:.0f}): <code>{setup['target_2']:.4f}</code>\n"
        f"🔄<b>Trail/Exit Rule:</b> Close beyond 20 EMA (<code>{setup['trail_exit']:.4f}</code>)"
    )


def run_once() -> None:
    for symbol in SYMBOLS:
        try:
            df = fetch_ohlcv_df(symbol, TIMEFRAME, limit=120)
            setup = analyze_strategy(df)
            if setup:
                candle_time = setup["candle_time"]
                side = setup["side"]
                if is_already_sent(symbol, side, TIMEFRAME, candle_time):
                    print(f"[{symbol}] Duplicate skipped for {side} ({candle_time})")
                    continue

                sent = send_telegram_alert(format_alert(symbol, setup))
                if not sent:
                    print(f"[{symbol}] Telegram send failed.")
            else:
                print(f"[{symbol}] No setup matched.")
        except Exception as e:
            print(f"[{symbol}] Error: {e}")
        time.sleep(0.20)


if __name__ == "__main__":
    run_once()
