import json
import os
import pandas as pd
import requests
import yfinance as yf

BOT_NAME = "Sweep Bot V2 (Lean 1h/4h)"
STATE_FILE = "state_v2.json"

WATCHLIST = {
    "GOLD": "GC=F",
    "CRUDE OIL": "CL=F",
}

TIMEFRAME_CONFIG = {
    "1h": {"period": "15d", "lookback": 20, "ema": 50, "buffer": 0.0008},
    "4h": {"period": "60d", "lookback": 18, "ema": 50, "buffer": 0.0010},
}

RR_RATIO = 2.5

TELEGRAM_TOKEN = os.getenv(
    "TELEGRAM_BOT_TOKEN", "8963851584:AAHDD47K5Q20qAURgsBIoMQhBwMfUhdyrxM"
)
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "6292189019")


def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_state(state: dict):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        print(f"State save error: {e}")


def send_telegram_alert(message: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print(message)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        requests.post(
            url,
            json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"},
            timeout=10,
        )
    except Exception as e:
        print(f"Telegram alert error: {e}")


def fetch_data(ticker: str, interval: str, period: str, ema_period: int) -> pd.DataFrame:
    df = yf.download(tickers=ticker, interval=interval, period=period, progress=False)
    if df is None or df.empty:
        return pd.DataFrame()

    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    df = df.rename(
        columns={
            "Open": "open",
            "High": "high",
            "Low": "low",
            "Close": "close",
            "Volume": "volume",
        }
    ).reset_index()

    time_col = "Datetime" if "Datetime" in df.columns else "Date"
    df = df.rename(columns={time_col: "timestamp"})
    df["ema"] = df["close"].ewm(span=ema_period, adjust=False).mean()
    return df[["timestamp", "open", "high", "low", "close", "volume", "ema"]].dropna()


def check_sweep_setup(df: pd.DataFrame, lookback: int, ema_period: int, buffer_pct: float):
    if len(df) < max(lookback + 3, ema_period + 3):
        return None

    history = df.iloc[-(lookback + 2):-2]
    swing_low = float(history["low"].min())
    swing_high = float(history["high"].max())

    last_c = df.iloc[-2]
    prev_c = df.iloc[-3]

    entry = float(last_c["close"])
    ema_val = float(last_c["ema"])
    last_open = float(last_c["open"])
    last_high = float(last_c["high"])
    last_low = float(last_c["low"])
    prev_high = float(prev_c["high"])
    prev_low = float(prev_c["low"])

    # Bullish Sweep + Bullish Reclaim
    swept_down = (last_low < swing_low) or (prev_low < swing_low)
    if swept_down and (entry > swing_low) and (entry > last_open) and (entry >= ema_val * 0.995):
        sl = min(last_low, prev_low) * (1 - buffer_pct)
        risk = entry - sl
        if risk > 0:
            return {
                "side": "LONG",
                "swept": round(swing_low, 4),
                "entry": round(entry, 4),
                "sl": round(sl, 4),
                "target": round(entry + (risk * RR_RATIO), 4),
                "time": str(last_c["timestamp"]),
            }

    # Bearish Sweep + Bearish Reclaim
    swept_up = (last_high > swing_high) or (prev_high > swing_high)
    if swept_up and (entry < swing_high) and (entry < last_open) and (entry <= ema_val * 1.005):
        sl = max(last_high, prev_high) * (1 + buffer_pct)
        risk = sl - entry
        if risk > 0:
            return {
                "side": "SHORT",
                "swept": round(swing_high, 4),
                "entry": round(entry, 4),
                "sl": round(sl, 4),
                "target": round(entry - (risk * RR_RATIO), 4),
                "time": str(last_c["timestamp"]),
            }

    return None


def main():
    state = load_state()
    changed = False

    for tf, cfg in TIMEFRAME_CONFIG.items():
        for name, ticker in WATCHLIST.items():
            key = f"{ticker}_{tf}"
            try:
                df = fetch_data(ticker, tf, cfg["period"], cfg["ema"])
                sig = check_sweep_setup(df, cfg["lookback"], cfg["ema"], cfg["buffer"])
                if sig:
                    sig_id = f"{sig['time']}_{sig['side']}"
                    if state.get(key) == sig_id:
                        continue

                    msg = (
                        f"<b>[{BOT_NAME}]</b> ({tf})\n"
                        f"Asset: <b>{name}</b> (<code>{ticker}</code>)\n"
                        f"Side: <b>{sig['side']}</b>\n"
                        f"Swept: <code>{sig['swept']}</code>\n"
                        f"Entry: <code>{sig['entry']}</code>\n"
                        f"SL: <code>{sig['sl']}</code>\n"
                        f"Target (1:{RR_RATIO}): <code>{sig['target']}</code>\n"
                        f"Time: {sig['time']}"
                    )
                    send_telegram_alert(msg)
                    state[key] = sig_id
                    changed = True
                else:
                    print(f"[{BOT_NAME} | {name} | {tf}] No sweep setup.")
            except Exception as e:
                print(f"[{BOT_NAME} | {name} | {tf}] Error: {e}")

    if changed:
        save_state(state)


if __name__ == "__main__":
    main()
