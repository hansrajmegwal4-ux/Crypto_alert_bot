import os
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
import requests


@dataclass
class AssetRule:
    lookback: int
    min_rejection_ratio: float
    max_candle_range_pct: float
    sl_buffer_pct: float
    risk_pct: float
    rr_target: float = 3.0
    timeframe_label: str = "4H Range → 15M Setup"
    precaution_note: str = ""


@dataclass
class MultiCommodityDisciplineConfig:
    capital_inr: float = 100000.0
    leverage: int = 8
    max_total_daily_trades: int = 3
    max_total_daily_losses: int = 2
    cooldown_minutes: int = 60
    sensitive_ist_hours: tuple[int, ...] = (18, 19, 20)
    block_in_sensitive_hours: bool = True
    state_file: str = "commodity_sweep_bot/commodity_sweep_state.json"
    rules: dict[str, AssetRule] = field(default_factory=lambda: {
        "GOLD": AssetRule(
            lookback=15,
            min_rejection_ratio=0.45,
            max_candle_range_pct=1.8,
            sl_buffer_pct=0.0012,
            risk_pct=1.5,
            rr_target=3.0,
            timeframe_label="4H Range → 15M Setup",
            precaution_note="डेटा/सत्र के समय अचानक स्पाइक से बचें; SL को पीछे न खिसकाएं।",
        ),
        "SILVER": AssetRule(
            lookback=18,
            min_rejection_ratio=0.58,
            max_candle_range_pct=2.5,
            sl_buffer_pct=0.0020,
            risk_pct=1.0,
            rr_target=2.5,
            timeframe_label="4H Range → 15M Setup",
            precaution_note="सिल्वर में फ़ेक स्वीप और दोनों तरफ़ के झटके आम हैं; कम जोखिम व सख़्त SL रखें।",
        ),
        "OIL": AssetRule(
            lookback=15,
            min_rejection_ratio=0.48,
            max_candle_range_pct=2.2,
            sl_buffer_pct=0.0018,
            risk_pct=1.25,
            rr_target=3.0,
            timeframe_label="4H Range → 15M Setup",
            precaution_note="इन्वेंटरी/डेटा और शाम के सत्र की अस्थिरता में बिना कन्फर्मेशन न कूदें।",
        ),
    })


def is_sensitive_market_window_ist(cfg: MultiCommodityDisciplineConfig) -> tuple[bool, str]:
    ist_now = datetime.now(timezone(timedelta(hours=5, minutes=30)))
    if ist_now.hour in cfg.sensitive_ist_hours:
        return True, f"IST {ist_now.strftime('%H:%M')} (संवेदनशील डेटा/सत्र विंडो)"
    return False, f"IST {ist_now.strftime('%H:%M')} (सामान्य विंडो)"


def evaluate_commodity_sweep(symbol: str, candles: list[dict], rule: AssetRule) -> tuple[dict | None, str | None]:
    if len(candles) < rule.lookback + 5:
        return None, None

    last = candles[-1]
    window = candles[-(rule.lookback + 1):-1]

    swing_high = max(c["high"] for c in window)
    swing_low = min(c["low"] for c in window)

    candle_range = last["high"] - last["low"]
    base_price = last["open"] if last["open"] > 0 else last["close"]
    if candle_range <= 0 or base_price <= 0:
        return None, None

    range_pct = (candle_range / base_price) * 100.0
    if range_pct > rule.max_candle_range_pct:
        return (
            None,
            f"असामान्य कैंडल विस्तार ({range_pct:.2f}% > {rule.max_candle_range_pct}%) — स्पाइक/न्यूज़ जोखिम के कारण छोड़ा गया।",
        )

    upper_wick = last["high"] - max(last["open"], last["close"])
    lower_wick = min(last["open"], last["close"]) - last["low"]

    # Bearish Sweep + Rejection
    if last["high"] > swing_high and last["close"] < swing_high:
        rej_ratio = upper_wick / candle_range
        if rej_ratio >= rule.min_rejection_ratio and last["close"] < last["open"]:
            entry = last["close"]
            sl = last["high"] * (1.0 + rule.sl_buffer_pct)
            risk_per_unit = sl - entry
            if risk_per_unit <= 0:
                return None, None
            target_1 = entry - (risk_per_unit * 2.0)
            target_2 = entry - (risk_per_unit * rule.rr_target)
            return {
                "side": "SHORT",
                "range_low": round(swing_low, 4),
                "range_high": round(swing_high, 4),
                "entry": round(entry, 4),
                "sl": round(sl, 4),
                "target_1": round(target_1, 4),
                "target_2": round(target_2, 4),
                "risk_per_unit": risk_per_unit,
                "risk_pct": rule.risk_pct,
                "rr_target": rule.rr_target,
                "rejection_pct": round(rej_ratio * 100, 1),
                "range_pct": round(range_pct, 2),
                "timeframe_label": rule.timeframe_label,
                "precaution_note": rule.precaution_note,
            }, None

    # Bullish Sweep + Rejection
    if last["low"] < swing_low and last["close"] > swing_low:
        rej_ratio = lower_wick / candle_range
        if rej_ratio >= rule.min_rejection_ratio and last["close"] > last["open"]:
            entry = last["close"]
            sl = last["low"] * (1.0 - rule.sl_buffer_pct)
            risk_per_unit = entry - sl
            if risk_per_unit <= 0:
                return None, None
            target_1 = entry + (risk_per_unit * 2.0)
            target_2 = entry + (risk_per_unit * rule.rr_target)
            return {
                "side": "LONG",
                "range_low": round(swing_low, 4),
                "range_high": round(swing_high, 4),
                "entry": round(entry, 4),
                "sl": round(sl, 4),
                "target_1": round(target_1, 4),
                "target_2": round(target_2, 4),
                "risk_per_unit": risk_per_unit,
                "risk_pct": rule.risk_pct,
                "rr_target": rule.rr_target,
                "rejection_pct": round(rej_ratio * 100, 1),
                "range_pct": round(range_pct, 2),
                "timeframe_label": rule.timeframe_label,
                "precaution_note": rule.precaution_note,
            }, None

    return None, None


def load_state(path: str) -> dict:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    default = {
        "date": today,
        "trades_today": 0,
        "losses_today": 0,
        "last_alert_ts": {},
        "stand_down_sent": False,
        "last_caution_note": "",
    }
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return default if data.get("date") != today else data
    except Exception:
        return default


def save_state(path: str, state: dict) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def send_telegram(text: str) -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        print(text)
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            timeout=15,
        )
        r.raise_for_status()
    except Exception as exc:
        print(f"[ERROR] Telegram alert failed: {exc}")


def build_alert_message(
    symbol: str,
    setup: dict,
    sizing: dict,
    cfg: MultiCommodityDisciplineConfig,
    state: dict,
) -> str:
    is_short = setup["side"] == "SHORT"
    direction_text = (
        "🔴 SHORT (Bearish Sweep + Rejection)"
        if is_short
        else "🟢 LONG (Bullish Sweep + Rejection)"
    )
    sl_label = "Sweep High" if is_short else "Sweep Low"
    candle_time = datetime.now(timezone(timedelta(hours=5, minutes=30))).strftime(
        "%Y-%m-%d %H:%M:%S"
    )

    return (
        f"<b>🔱 हंसराज मेघवाल 🔱</b>\n"
        f"<b>🚨 LIQUIDITY SWEEP ALERT 🚨</b>\n\n"
        f"<b>🪙 Asset:</b> {symbol}\n"
        f"<b>⏱️ Timeframe:</b> {setup['timeframe_label']}\n"
        f"<b>🎯 Direction:</b> {direction_text}\n"
        f"<b>📅 LTF Candle:</b> {candle_time}\n\n"
        f"<b>📐 Range:</b>\n"
        f"• Low: <code>{setup['range_low']}</code>\n"
        f"• High: <code>{setup['range_high']}</code>\n\n"
        f"<b>⚡ Entry Price:</b> <code>{setup['entry']}</code>\n"
        f"<b>🛑 Stop Loss:</b> <code>{setup['sl']}</code> ({sl_label})\n\n"
        f"<b>💰 Take Profit Targets:</b>\n"
        f"🎯 Target 1 (1:2): <code>{setup['target_1']}</code>\n"
        f"🚀 Target 2 (1:3): <code>{setup['target_2']}</code>\n\n"
        f"<b>🔒 Risk/Reward:</b> 1:{setup['rr_target']}\n"
        f"<b>⚖️ Risk &amp; Discipline:</b> ₹{sizing['max_loss_inr']} ({setup['risk_pct']}%) | Margin (~{cfg.leverage}x): ₹{sizing['margin_inr']}\n"
        f"<b>📊 Daily Quota:</b> Trades {state['trades_today']}/{cfg.max_total_daily_trades} | Losses {state['losses_today']}/{cfg.max_total_daily_losses}\n"
        f"<b>🛡️ Caution:</b> {setup['precaution_note']}"
    )


def run_multi_commodity_bot(
    candles_by_symbol: dict[str, list[dict]],
    cfg: MultiCommodityDisciplineConfig | None = None,
) -> None:
    cfg = cfg or MultiCommodityDisciplineConfig()
    state = load_state(cfg.state_file)

    if state["losses_today"] >= cfg.max_total_daily_losses or state["trades_today"] >= cfg.max_total_daily_trades:
        if not state.get("stand_down_sent"):
            send_telegram(
                "<b>🛑 STAND DOWN (GOLD / SILVER / OIL)</b>\n"
                f"आज की साझा सीमा पूरी हो गई है (ट्रेड्स: {state['trades_today']}/{cfg.max_total_daily_trades}, "
                f"लॉस: {state['losses_today']}/{cfg.max_total_daily_losses})।\n"
                "सावधानी: बदले या रिकवरी की भावना से ट्रेड न लें—आज के लिए रुकें।"
            )
            state["stand_down_sent"] = True
            save_state(cfg.state_file, state)
        return

    in_sensitive_window, window_label = is_sensitive_market_window_ist(cfg)
    if in_sensitive_window and cfg.block_in_sensitive_hours:
        caution_msg = (
            f"<b>⚠️ सावधानी गार्ड सक्रिय ({window_label})</b>\n"
            "संवेदनशील डेटा/सत्र विंडो के दौरान अचानक बड़े स्पाइक्स और फ़ेक स्वीप का जोखिम अधिक रहता है। "
            "इसलिए इस दौरान स्वचालित एंट्री अलर्ट रोके गए हैं—धैर्य रखें और शांत स्थिति की प्रतीक्षा करें।"
        )
        if state.get("last_caution_note") != window_label:
            send_telegram(caution_msg)
            state["last_caution_note"] = window_label
            save_state(cfg.state_file, state)
        return

    now_ts = int(time.time())
    cooldown_sec = cfg.cooldown_minutes * 60

    for symbol in ("GOLD", "SILVER", "OIL"):
        if symbol not in candles_by_symbol or symbol not in cfg.rules:
            continue
        if now_ts - state["last_alert_ts"].get(symbol, 0) < cooldown_sec:
            continue

        rule = cfg.rules[symbol]
        setup, skip_reason = evaluate_commodity_sweep(symbol, candles_by_symbol[symbol], rule)
        if skip_reason:
            print(f"[CAUTION - {symbol}] {skip_reason}")
            continue
        if not setup:
            continue

        max_loss_inr = cfg.capital_inr * (setup["risk_pct"] / 100.0)
        qty = max_loss_inr / setup["risk_per_unit"]
        margin_inr = (qty * setup["entry"]) / cfg.leverage

        state["trades_today"] += 1
        state["last_alert_ts"][symbol] = now_ts

        sizing = {
            "max_loss_inr": round(max_loss_inr, 2),
            "qty": round(qty, 4),
            "margin_inr": round(margin_inr, 2),
        }
        msg = build_alert_message(symbol, setup, sizing, cfg, state)
        send_telegram(msg)

        if state["trades_today"] >= cfg.max_total_daily_trades:
            break

    save_state(cfg.state_file, state
    def fetch_yahoo_candles(ticker: str, interval: str = "15m", range_span: str = "5d") -> list[dict]:
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
    resp = requests.get(
        url,
        params={"interval": interval, "range": range_span},
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=15,
    )
    resp.raise_for_status()
    result = resp.json().get("chart", {}).get("result", [])
    if not result:
        return []
    q = result[0]["indicators"]["quote"][0]
    candles = []
    for i, ts in enumerate(result[0]["timestamp"]):
        if None in (q["open"][i], q["high"][i], q["low"][i], q["close"][i]):
            continue
        candles.append({
            "ts": int(ts),
            "open": float(q["open"][i]),
            "high": float(q["high"][i]),
            "low": float(q["low"][i]),
            "close": float(q["close"][i]),
        })
    return candles


def simulate_trade_outcome(candles: list[dict], entry_idx: int, setup: dict, max_forward_candles: int = 40) -> str:
    side, sl, target_2 = setup["side"], setup["sl"], setup["target_2"]
    for idx in range(entry_idx + 1, min(len(candles), entry_idx + max_forward_candles + 1)):
        c = candles[idx]
        if side == "SHORT":
            if c["high"] >= sl:
                return "LOSS"
            if c["low"] <= target_2:
                return "WIN"
        else:
            if c["low"] <= sl:
                return "LOSS"
            if c["high"] >= target_2:
                return "WIN"
    return "OPEN"


def run_backtest_report(cfg: MultiCommodityDisciplineConfig | None = None) -> None:
    cfg = cfg or MultiCommodityDisciplineConfig()
    tickers = {"GOLD": "GC=F", "SILVER": "SI=F", "OIL": "CL=F"}
    print("=== COMMODITY LIQUIDITY SWEEP BACKTEST REPORT ===")
    for symbol in ("GOLD", "SILVER", "OIL"):
        rule = cfg.rules[symbol]
        candles = fetch_yahoo_candles(tickers[symbol])
        wins = losses = open_t = 0
        for i in range(rule.lookback + 5, len(candles) - 1):
            setup, _ = evaluate_commodity_sweep(symbol, candles[: i + 1], rule)
            if not setup:
                continue
            res = simulate_trade_outcome(candles, i, setup)
            if res == "WIN":
                wins += 1
            elif res == "LOSS":
                losses += 1
            else:
                open_t += 1
        closed = wins + losses
        wr = round((wins / closed * 100.0), 2) if closed > 0 else 0.0
        print(f"{symbol}: Closed={closed} (Wins={wins}, Losses={losses}, Open={open_t}) | Win Rate={wr}%")


if __name__ == "__main__":
    tickers = {"GOLD": "GC=F", "SILVER": "SI=F", "OIL": "CL=F"}
    live_data = {sym: fetch_yahoo_candles(tkr) for sym, tkr in tickers.items()}
    run_multi_commodity_bot(live_data)
