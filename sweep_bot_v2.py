import os
import requests
import pandas as pd
import ccxt


# ==========================================
# कॉन्फ़िगरेशन
# ==========================================
TELEGRAM_BOT_TOKEN = os.getenv(
    "TELEGRAM_BOT_TOKEN",
    "8963851584:AAHDD47K5Q20qAURgsBIoMQhBwMfUhdyrxM",
)
TELEGRAM_CHAT_ID = os.getenv(
    "TELEGRAM_CHAT_ID",
    "6292189019",
)

SYMBOLS = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT", "BNB/USDT"]
HTF_TIMEFRAME = "4h"      # रेंज के लिए बड़ा टाइमफ्रेम
LTF_TIMEFRAME = "15m"     # स्वीप और रिएक्शन के लिए छोटा टाइमफ्रेम
REWARD_RATIO = 2.0        # फ़ॉलबैक रिस्क-रिवॉर्ड अनुपात


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


def fetch_ohlcv_df(exchange: ccxt.Exchange, symbol: str, timeframe: str, limit: int = 100) -> pd.DataFrame:
    raw = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
    df = pd.DataFrame(raw, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    return df


def analyze_crt_setup(htf_df: pd.DataFrame, ltf_df: pd.DataFrame) -> dict | None:
    """
    CRT (कैंडल रेंज थ्योरी) लॉजिक:
    1. HTF की पिछली बंद कैंडल (iloc[-2]) के High और Low से रेंज तय करता है।
    2. LTF की हालिया बंद कैंडल्स में स्वीप (Sweep) और रेंज में वापसी देखता है।
    3. रेंज में रिएक्शन मिलने पर Long या Short सेटअप बनाता है।
    """
    if len(htf_df) < 3 or len(ltf_df) < 15:
        return None

    ref_candle = htf_df.iloc[-2]
    range_high = float(ref_candle["high"])
    range_low = float(ref_candle["low"])
    range_mid = (range_high + range_low) / 2.0

    recent_ltf = ltf_df.iloc[-10:-1].copy()
    last_closed = ltf_df.iloc[-2]
    prev_closed = ltf_df.iloc[-3]
    current_price = float(last_closed["close"])

    # नीचे की लिक्विडिटी स्वीप -> रेंज में वापसी -> बुलिश रिएक्शन (LONG)
    swept_low = bool((recent_ltf["low"] < range_low).any())
    back_in_range_from_low = (range_low <= current_price <= range_high)
    bullish_reaction = (
        float(last_closed["close"]) > float(last_closed["open"])
        and float(last_closed["close"]) > float(prev_closed["high"])
    )

    if swept_low and back_in_range_from_low and bullish_reaction:
        sweep_low_extreme = float(recent_ltf["low"].min())
        entry = current_price
        stop_loss = sweep_low_extreme
        risk = entry - stop_loss
        if risk <= 0:
            return None

        target_1 = range_mid if range_mid > entry else entry + risk * REWARD_RATIO
        target_2 = range_high if range_high > target_1 else entry + risk * (REWARD_RATIO + 1)
        rr_ratio = (target_2 - entry) / risk if risk > 0 else REWARD_RATIO

        return {
            "side": "LONG (Bullish CRT Reversal)",
            "direction_badge": "🟢 LONG (Bullish CRT Reversal)",
            "sl_note": "Sweep Low",
            "t1_label": "Target 1 (Mid Range)",
            "t2_label": "Target 2 (Range High)",
            "ltf_candle_time": str(last_closed["timestamp"]),
            "range_high": range_high,
            "range_low": range_low,
            "entry": entry,
            "stop_loss": stop_loss,
            "target_1": target_1,
            "target_2": target_2,
            "rr_ratio": rr_ratio,
        }

    # ऊपर की लिक्विडिटी स्वीप -> रेंज में वापसी -> बेयरिश रिएक्शन (SHORT)
    swept_high = bool((recent_ltf["high"] > range_high).any())
    back_in_range_from_high = (range_low <= current_price <= range_high)
    bearish_reaction = (
        float(last_closed["close"]) < float(last_closed["open"])
        and float(last_closed["close"]) < float(prev_closed["low"])
    )

    if swept_high and back_in_range_from_high and bearish_reaction:
        sweep_high_extreme = float(recent_ltf["high"].max())
        entry = current_price
        stop_loss = sweep_high_extreme
        risk = stop_loss - entry
        if risk <= 0:
            return None

        target_1 = range_mid if range_mid < entry else entry - risk * REWARD_RATIO
        target_2 = range_low if range_low < target_1 else entry - risk * (REWARD_RATIO + 1)
        rr_ratio = (entry - target_2) / risk if risk > 0 else REWARD_RATIO

        return {
            "side": "SHORT (Bearish CRT Reversal)",
            "direction_badge": "🔴 SHORT (Bearish CRT Reversal)",
            "sl_note": "Sweep High",
            "t1_label": "Target 1 (Mid Range)",
            "t2_label": "Target 2 (Range Low)",
            "ltf_candle_time": str(last_closed["timestamp"]),
            "range_high": range_high,
            "range_low": range_low,
            "entry": entry,
            "stop_loss": stop_loss,
            "target_1": target_1,
            "target_2": target_2,
            "rr_ratio": rr_ratio,
        }

    return None


def format_alert(symbol: str, setup: dict) -> str:
    return (
        f"🔱🏛️ <b>हंसराज मेघवाल</b> 🏛️🔱\n"
        f"🚨 <b>CRT TRADING ALERT</b> 🚨\n\n"
        f"🪙 <b>Coin Pair:</b> {symbol}\n"
        f"⏱️ <b>Timeframe:</b> {HTF_TIMEFRAME.upper()} Range ➔ {LTF_TIMEFRAME.upper()} Setup\n"
        f"🎯 <b>Direction:</b> {setup['direction_badge']}\n"
        f"📅 <b>LTF Candle:</b> <code>{setup['ltf_candle_time']}</code>\n\n"
        f"📏 <b>HTF Range:</b>\n"
        f" • Low: <code>{setup['range_low']:.2f}</code>\n"
        f" • High: <code>{setup['range_high']:.2f}</code>\n\n"
        f"⚡ <b>Entry Price:</b> <code>{setup['entry']:.2f}</code>\n"
        f"🛑 <b>Stop Loss:</b> <code>{setup['stop_loss']:.2f}</code> ({setup['sl_note']})\n\n"
        f"💰 <b>Take Profit Targets:</b>\n"
        f" 🎯 {setup['t1_label']}: <code>{setup['target_1']:.2f}</code>\n"
        f" 🚀 {setup['t2_label']}: <code>{setup['target_2']:.2f}</code>\n\n"
        f"🔒 <b>Risk/Reward:</b> 1:{setup['rr_ratio']:.1f}"
    )


def run_once() -> None:
    exchange = ccxt.coindcx({"enableRateLimit": True})
    for symbol in SYMBOLS:
        try:
            htf_df = fetch_ohlcv_df(exchange, symbol, HTF_TIMEFRAME, limit=50)
            ltf_df = fetch_ohlcv_df(exchange, symbol, LTF_TIMEFRAME, limit=50)
            setup = analyze_crt_setup(htf_df, ltf_df)
            if setup:
                send_telegram_alert(format_alert(symbol, setup))
            else:
                print(f"[{symbol}] No CRT setup.")
        except Exception as e:
            print(f"[{symbol}] Error: {e}")


if __name__ == "__main__":
    run_once()
