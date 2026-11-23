import json
import os
from datetime import datetime, timezone
import pandas as pd
import requests
import ccxt

TELEGRAM_BOT_TOKEN = (
    os.getenv("TELEGRAM_BOT_TOKEN")
    or os.getenv("TELEGRAM_BOT_TOKEN_SWEEP")
    or "8365031504:AAEQO47K5Q2bq4iHgdbrvdSPNWTuheyrxsI"
)
TELEGRAM_CHAT_ID = (
    os.getenv("TELEGRAM_CHAT_ID")
    or os.getenv("TELEGRAM_CHAT_ID_SWEEP")
    or "6292189819"
)

EXCHANGE_ID = "kucoin"
SYMBOLS = [
    "BTC/USDT",
    "ETH/USDT",
    "SOL/USDT",
    "BNB/USDT",
    "XRP/USDT",
]
HTF_TIMEFRAME = "4h"
LTF_TIMEFRAME = "15m"
REWARD_RATIO = 3.0
MAX_CANDLE_AGE_MINUTES = 30

# SL हिट होने से बचाने वाले सुधार पैरामीटर्स
SL_BUFFER_PCT = 0.0015  # 0.15% बफ़र ताकि हल्के विक/स्वीप में तुरंत SL न कटे
MAX_RISK_PCT = 0.025    # एंट्री से SL की अधिकतम दूरी (2.5%) ताकि बहुत दूर का SL न बने
MAX_TP2_PCT = 0.04      # Target 2 की अधिकतम दूरी (4.0%) ताकि अव्यावहारिक दूर का टारगेट न बने

STATE_FILE = "sent_alerts.json"
MAX_STATE_AGE_SECONDS = 60 * 60 * 24 * 3


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


def _cleanup_old_state(sent: dict, now_ts: float) -> dict:
    return {
        k: v
        for k, v in sent.items()
        if isinstance(v, (int, float)) and (now_ts - v) <= MAX_STATE_AGE_SECONDS
    }


def is_already_sent(bot_tag: str, symbol: str, side: str, timeframe: str, candle_time: str) -> bool:
    sent = _cleanup_old_state(load_sent_alerts(), datetime.now(timezone.utc).timestamp())
    alert_key = f"{bot_tag}|{symbol}|{side}|{timeframe}|{candle_time}"
    return alert_key in sent


def mark_as_sent(bot_tag: str, symbol: str, side: str, timeframe: str, candle_time: str) -> None:
    now_ts = datetime.now(timezone.utc).timestamp()
    sent = _cleanup_old_state(load_sent_alerts(), now_ts)
    alert_key = f"{bot_tag}|{symbol}|{side}|{timeframe}|{candle_time}"
    sent[alert_key] = now_ts
    save_sent_alerts(sent)


def send_telegram_alert(message: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram error: TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID missing.")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        resp = requests.post(url, json=payload, timeout=15)
        resp.raise_for_status()
        print("Telegram alert sent successfully.")
        return True
    except Exception as e:
        print(f"Telegram error: {e}")
        return False


def create_exchange():
    exchange_cls = getattr(ccxt, EXCHANGE_ID)
    return exchange_cls({"enableRateLimit": True})


def fetch_ohlcv_df(exchange, symbol: str, timeframe: str, limit: int = 120) -> pd.DataFrame:
    ohlcv = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
    df = pd.DataFrame(ohlcv, columns=["timestamp_ms", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp_ms"], unit="ms", utc=True)
    df["ema_9"] = df["close"].ewm(span=9, adjust=False).mean()
    df["ema_20"] = df["close"].ewm(span=20, adjust=False).mean()
    return df


def is_closed_candle_fresh(candle_ts_ms: int, max_age_minutes: int = MAX_CANDLE_AGE_MINUTES) -> bool:
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    age_minutes = (now_ms - candle_ts_ms) / 60000.0
    return 0 <= age_minutes <= max_age_minutes


def ema_and_momentum_confirms_short(last_closed: pd.Series, prev_closed: pd.Series) -> bool:
    close_price = float(last_closed["close"])
    ema9 = float(last_closed["ema_9"])
    ema20 = float(last_closed["ema_20"])
    prev_close = float(prev_closed["close"])

    ema_bearish = close_price < ema9 and close_price < ema20 and ema9 <= ema20
    # लगातार गिरते/कमजोर मोमेंटम की जांच ताकि रिवर्सल बाउंस में गलत शॉर्ट न बने
    momentum_bearish = close_price <= prev_close
    return ema_bearish and momentum_bearish


def ema_and_momentum_confirms_long(last_closed: pd.Series, prev_closed: pd.Series) -> bool:
    close_price = float(last_closed["close"])
    ema9 = float(last_closed["ema_9"])
    ema20 = float(last_closed["ema_20"])
    prev_close = float(prev_closed["close"])

    ema_bullish = close_price > ema9 and close_price > ema20 and ema9 >= ema20
    momentum_bullish = close_price >= prev_close
    return ema_bullish and momentum_bullish


def analyze_crt_setup(htf_df: pd.DataFrame, ltf_df: pd.DataFrame):
    if len(htf_df) < 3 or len(ltf_df) < 25:
        return None

    htf_closed = htf_df.iloc[-2]
    range_high = float(htf_closed["high"])
    range_low = float(htf_closed["low"])
    range_mid = (range_high + range_low) / 2.0

    last_closed = ltf_df.iloc[-2]
    prev_closed = ltf_df.iloc[-3]

    candle_ts_ms = int(last_closed["timestamp_ms"])
    if not is_closed_candle_fresh(candle_ts_ms):
        return None

    recent_ltf = ltf_df.iloc[-6:-1]
    current_price = float(last_closed["close"])

    # Bearish CRT Reversal (SHORT) जांच
    swept_high = bool((recent_ltf["high"] > range_high).any())
    back_in_range_from_high = range_low <= current_price <= range_high
    bearish_reaction = (
        float(last_closed["close"]) < float(last_closed["open"])
        and float(last_closed["close"]) < float(prev_closed["low"])
    )

    if (
        swept_high
        and back_in_range_from_high
        and bearish_reaction
        and ema_and_momentum_confirms_short(last_closed, prev_closed)
    ):
        sweep_high_extreme = float(recent_ltf["high"].max())
        entry = current_price
        # बफ़र के साथ SL ताकि हल्के विक/स्वीप में तुरंत न कटे
        stop_loss = sweep_high_extreme * (1.0 + SL_BUFFER_PCT)
        risk = stop_loss - entry
        if risk <= 0 or (risk / entry) > MAX_RISK_PCT:
            return None

        # व्यावहारिक टारगेट (अत्यधिक दूर के टारगेट को सीमित रखते हुए)
        min_tp2 = entry * (1.0 - MAX_TP2_PCT)
        target_1 = range_mid if (min_tp2 < range_mid < entry) else max(entry - risk, min_tp2)
        raw_tp2 = range_low if (range_low < target_1) else entry - (risk * REWARD_RATIO)
        target_2 = max(raw_tp2, min_tp2)

        if target_2 >= entry:
            return None

        rr_ratio = (entry - target_2) / risk if risk > 0 else REWARD_RATIO

        return {
            "side": "SHORT",
            "direction_badge": "🔴 SHORT (Bearish CRT Reversal)",
            "sl_meta": "Sweep High + Buffer",
            "t1_label": "Target 1 (Mid Range)",
            "t2_label": "Target 2 (Range Low)",
            "ltf_candle_time": last_closed["timestamp"].strftime("%Y-%m-%d %H:%M:%S UTC"),
            "range_high": range_high,
            "range_low": range_low,
            "entry": entry,
            "stop_loss": stop_loss,
            "target_1": target_1,
            "target_2": target_2,
            "rr_ratio": rr_ratio,
        }

    # Bullish CRT Reversal (LONG) जांच
    swept_low = bool((recent_ltf["low"] < range_low).any())
    back_in_range_from_low = range_low <= current_price <= range_high
    bullish_reaction = (
        float(last_closed["close"]) > float(last_closed["open"])
        and float(last_closed["close"]) > float(prev_closed["high"])
    )

    if (
        swept_low
        and back_in_range_from_low
        and bullish_reaction
        and ema_and_momentum_confirms_long(last_closed, prev_closed)
    ):
        sweep_low_extreme = float(recent_ltf["low"].min())
        entry = current_price
        stop_loss = sweep_low_extreme * (1.0 - SL_BUFFER_PCT)
        risk = entry - stop_loss
        if risk <= 0 or (risk / entry) > MAX_RISK_PCT:
            return None

        max_tp2 = entry * (1.0 + MAX_TP2_PCT)
        target_1 = range_mid if (entry < range_mid < max_tp2) else min(entry + risk, max_tp2)
        raw_tp2 = range_high if (range_high > target_1) else entry + (risk * REWARD_RATIO)
        target_2 = min(raw_tp2, max_tp2)

        if target_2 <= entry:
            return None

        rr_ratio = (target_2 - entry) / risk if risk > 0 else REWARD_RATIO

        return {
            "side": "LONG",
            "direction_badge": "🟢 LONG (Bullish CRT Reversal)",
            "sl_meta": "Sweep Low + Buffer",
            "t1_label": "Target 1 (Mid Range)",
            "t2_label": "Target 2 (Range High)",
            "ltf_candle_time": last_closed["timestamp"].strftime("%Y-%m-%d %H:%M:%S UTC"),
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
        f"<b>🔱 हंसराज मेघवाल 🔱</b>\n"
        f"<b>🚨 CRT TRADING ALERT 🚨</b>\n\n"
        f"<b>🪙 Coin Pair:</b> <code>{symbol}</code>\n"
        f"<b>⏱ Timeframe:</b> <code>{HTF_TIMEFRAME.upper()} Range → {LTF_TIMEFRAME.upper()} Setup</code>\n"
        f"<b>🎯 Direction:</b> {setup['direction_badge']}\n"
        f"<b>🕯 LTF Candle:</b> <code>{setup['ltf_candle_time']}</code>\n\n"
        f"<b>📐 HTF Range:</b>\n"
        f"• Low: <code>{setup['range_low']:.2f}</code>\n"
        f"• High: <code>{setup['range_high']:.2f}</code>\n\n"
        f"<b>⚡ Entry Price:</b> <code>{setup['entry']:.2f}</code>\n"
        f"<b>🛑 Stop Loss:</b> <code>{setup['stop_loss']:.2f}</code> ({setup['sl_meta']})\n\n"
        f"<b>💰 Take Profit Targets:</b>\n"
        f"🎯 {setup['t1_label']}: <code>{setup['target_1']:.2f}</code>\n"
        f"🚀 {setup['t2_label']}: <code>{setup['target_2']:.2f}</code>\n\n"
        f"<b>⚖️ Risk/Reward:</b> <code>1:{setup['rr_ratio']:.1f}</code>"
    )


def run_once() -> None:
    exchange = create_exchange()
    for symbol in SYMBOLS:
        try:
            htf_df = fetch_ohlcv_df(exchange, symbol, HTF_TIMEFRAME, limit=50)
            ltf_df = fetch_ohlcv_df(exchange, symbol, LTF_TIMEFRAME, limit=120)
            setup = analyze_crt_setup(htf_df, ltf_df)

            if setup:
                candle_time = setup["ltf_candle_time"]
                side = setup["side"]
                if is_already_sent("sweep_bot_v2", symbol, side, LTF_TIMEFRAME, candle_time):
                    print(f"[{symbol}] Duplicate skipped for {side} ({candle_time})")
                    continue

                sent = send_telegram_alert(format_alert(symbol, setup))
                if sent:
                    mark_as_sent("sweep_bot_v2", symbol, side, LTF_TIMEFRAME, candle_time)
                else:
                    print(f"[{symbol}] Telegram send failed, state not saved.")
            else:
                print(f"[{symbol}] No valid CRT + EMA + Momentum setup.")
        except Exception as e:
            print(f"[{symbol}] Error: {e}")


if __name__ == "__main__":
    run_once()
