import json
import os
from datetime import datetime
import requests
import pandas as pd
import ccxt


TELEGRAM_BOT_TOKEN = (
    os.getenv("TELEGRAM_BOT_TOKEN")
    or os.getenv("TELEGRAM_BOT_TOKEN_SWEEP")
    or "8965031504:AAEQO47K5Q2Oq4UHgdbcvd0PBRWTuheyrx8"
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


def is_already_sent(bot_tag: str, symbol: str, side: str, timeframe: str, candle_time: str) -> bool:
    sent = load_sent_alerts()
    now_ts = datetime.now().timestamp()

    cleaned = {
        k: v for k, v in sent.items()
        if isinstance(v, (int, float)) and (now_ts - v) < MAX_STATE_AGE_SECONDS
    }

    alert_key = f"{bot_tag}|{symbol}|{side}|{timeframe}|{candle_time}"
    already_exists = alert_key in cleaned

    if not already_exists:
        cleaned[alert_key] = now_ts

    save_sent_alerts(cleaned)
    return already_exists


def send_telegram_alert(message: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram error: TELEGRAM_BOT_TOKEN या TELEGRAM_CHAT_ID खाली है।")
        return False

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


def analyze_crt_setup(htf_df: pd.DataFrame, ltf_df: pd.DataFrame) -> dict | None:
    if len(htf_df) < 3 or len(ltf_df) < 6:
        return None

    htf_prev = htf_df.iloc[-2]
    range_high = float(htf_prev["high"])
    range_low = float(htf_prev["low"])
    range_mid = (range_high + range_low) / 2.0

    last_closed = ltf_df.iloc[-2]
    prev_closed = ltf_df.iloc[-3]
    recent_ltf = ltf_df.iloc[-6:-1]
    current_price = float(last_closed["close"])

    # रेंज की लिक्विडिटी स्वीप -> रेंज में वापसी -> बेयरिश रिएक्शन (SHORT)
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

        target_1 = range_mid if range_mid < entry else entry - risk
        target_2 = range_low if range_low < target_1 else entry - (risk * REWARD_RATIO)
        rr_ratio = (entry - target_2) / risk if risk > 0 else REWARD_RATIO

        return {
            "side": "SHORT (Bearish CRT Reversal)",
            "direction_badge": "🔴 SHORT (Bearish CRT Reversal)",
            "sl_meta": "Sweep High",
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
        f"🚨 <b>सफल सिग्नल</b> 🚨\n"
        f"📊 <b>CRT TRADING ALERT</b> 📊\n\n"
        f"🪙 <b>Coin Pair:</b> <code>{symbol}</code>\n"
        f"⏱️ <b>Timeframe:</b> <code>{HTF_TIMEFRAME.upper()} Range → {LTF_TIMEFRAME.upper()}</code>\n"
        f"📈 <b>Direction:</b> {setup['direction_badge']}\n"
        f"🕯️ <b>LTF Candle:</b> <code>{setup['ltf_candle_time']}</code>\n"
        f"📐 <b>HTF Range:</b>\n"
        f"• Low: <code>{setup['range_low']:.2f}</code>\n"
        f"• High: <code>{setup['range_high']:.2f}</code>\n"
        f"🎯 <b>Entry Price:</b> <code>{setup['entry']:.2f}</code>\n"
        f"🛑 <b>Stop Loss:</b> <code>{setup['stop_loss']:.2f}</code> ({setup['sl_meta']})\n"
        f"💰 <b>Take Profit Targets:</b>\n"
        f"• {setup['t1_label']}: <code>{setup['target_1']:.2f}</code>\n"
        f"• {setup['t2_label']}: <code>{setup['target_2']:.2f}</code>\n"
        f"⚖️ <b>Risk/Reward:</b> 1:<code>{setup['rr_ratio']:.1f}</code>"
    )


def run_once() -> None:
    exchange = create_exchange()
    for symbol in SYMBOLS:
        try:
            htf_df = fetch_ohlcv_df(exchange, symbol, HTF_TIMEFRAME, limit=120)
            ltf_df = fetch_ohlcv_df(exchange, symbol, LTF_TIMEFRAME, limit=120)
            setup = analyze_crt_setup(htf_df, ltf_df)
            if setup:
                candle_time = setup["ltf_candle_time"]
                side = setup["side"]
                if is_already_sent("sweep_bot_v2", symbol, side, LTF_TIMEFRAME, candle_time):
                    print(f"[{symbol}] Duplicate skipped for {side} ({candle_time}).")
                    continue

                sent = send_telegram_alert(format_alert(symbol, setup))
                if not sent:
                    print(f"[{symbol}] Telegram send failed; state not locked.")
            else:
                print(f"[{symbol}] No CRT setup.")
        except Exception as e:
            print(f"[{symbol}] Error: {e}")


if __name__ == "__main__":
    run_once()
