import os
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
import requests
import ccxt


@dataclass
class AssetRule:
    ticker: str
    lookback: int
    min_rejection_ratio: float
    max_candle_range_pct: float
    sl_buffer_pct: float
    risk_pct: float
    rr_target: float = 3.0
    timeframe_label: str = "4H Range -> 15M Setup"
    precaution_note: str = ""


@dataclass
class MultiCommodityDisciplineConfig:
    capital_inr: float = 100000.0
    leverage: int = 8
    max_total_daily_trades: int = 3
    max_total_daily_losses: int = 2
    cooldown_minutes: int = 60
    sensitive_ist_hours: tuple[int, ...] = (18, 19, 20)
    block_in_sensitive_hours: bool = False
    state_file: str = "commodity_sweep_bot/commodity_sweep_state.json"
    send_startup_test_alert: bool = False
    rules: dict[str, AssetRule] = field(default_factory=lambda: {
        "GOLD": AssetRule(
            ticker="PAXG/USDT",
            lookback=15,
            min_rejection_ratio=0.45,
            max_candle_range_pct=1.0,
            sl_buffer_pct=0.0012,
            risk_pct=1.5,
            rr_target=3.0,
            timeframe_label="4H Range -> 15M Setup",
            precaution_note="डेटा/सत्र के समय अचानक बाउंस से बचें, SL को पीछे न खिसकाएँ।",
        ),
        "SILVER": AssetRule(
            ticker="XAG/USDT",
            lookback=18,
            min_rejection_ratio=0.50,
            max_candle_range_pct=2.5,
            sl_buffer_pct=0.0020,
            risk_pct=1.0,
            rr_target=2.5,
            timeframe_label="4H Range -> 15M Setup",
            precaution_note="सिल्वर में फेक ब्रेक और दोनों तरफ के झटके आम हैं; कम जोखिम व सख्त SL रखें।",
        ),
        "OIL": AssetRule(
            ticker="USOIL/USDT",
            lookback=16,
            min_rejection_ratio=0.40,
            max_candle_range_pct=2.2,
            sl_buffer_pct=0.0018,
            risk_pct=1.25,
            rr_target=3.0,
            timeframe_label="4H Range -> 15M Setup",
            precaution_note="इन्वेंट्री/डेटा और शाम के सत्र की अस्थिरता में बिना कन्फ़र्मेशन न कूदें।",
        ),
    })


def fetch_yahoo_candles(ticker: str, interval: str = "15m", range_span: str = "5d") -> list[dict]:
    try:
        exchange = ccxt.binance({"enableRateLimit": True})
        ohlcv = exchange.fetch_ohlcv(ticker, timeframe=interval, limit=200)
        candles: list[dict] = []
        for row in ohlcv:
            if not row or len(row) < 6:
                continue
            ts_ms, o, h, l, c, v = row[:6]
            if None in (ts_ms, o, h, l, c):
                continue
            candles.append({
                "ts": int(ts_ms // 1000),
                "open": float(o),
                "high": float(h),
                "low": float(l),
                "close": float(c),
                "volume": float(v or 0),
            })
        return candles
    except Exception as exc:
        print(f"[ERROR] {ticker} कैंडल डेटा लाने में समस्या: {exc}", flush=True)
        return []


def is_sensitive_market_window_ist(cfg: MultiCommodityDisciplineConfig) -> tuple[bool, str]:
    ist_now = datetime.now(timezone(timedelta(hours=5, minutes=30)))
    if ist_now.hour in cfg.sensitive_ist_hours:
        return True, f"IST {ist_now.strftime('%H:%M')} (संवेदनशील डेटा/सत्र विंडो)"
    return False, f"IST {ist_now.strftime('%H:%M')} (सामान्य विंडो)"


def evaluate_commodity_sweep(candles: list[dict], rule: AssetRule) -> tuple[dict | None, str | None]:
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
            f"असामान्य कैंडल विस्तार ({range_pct:.2f}% > {rule.max_candle_range_pct}%) - अत्यधिक/न्यूज़ जोखिम",
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
            return (
                {
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
                },
                None,
            )

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
            return (
                {
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
                },
                None,
            )

    return None, None


def load_state(path: str) -> dict:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    default = {
        "date": today,
        "trades_today": 0,
        "losses_today": 0,
        "last_alert_ts": {},
        "last_candle_ts": {},
        "stand_down_sent": False,
        "last_caution_note": "",
        "startup_test_sent": False,
    }
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or data.get("date") != today:
            return default
        data.setdefault("trades_today", 0)
        data.setdefault("losses_today", 0)
        data.setdefault("last_alert_ts", {})
        data.setdefault("last_candle_ts", {})
        data.setdefault("stand_down_sent", False)
        data.setdefault("last_caution_note", "")
        data.setdefault("startup_test_sent", False)
        return data
    except Exception:
        return default


def save_state(path: str, state: dict) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def send_telegram(text: str) -> None:
    token = (
        os.getenv("TELEGRAM_BOT_TOKEN")
        or os.getenv("TELEGRAM_TOKEN")
        or ""
    ).strip()
    chat_id = (
        os.getenv("TELEGRAM_CHAT_ID")
        or ""
    ).strip()

    if not token or not chat_id:
        print("[WARN] Telegram token या chat_id नहीं मिला", flush=True)
        print(text, flush=True)
        return

    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            timeout=15,
        )
        r.raise_for_status()
        print("[INFO] Telegram मैसेज सफलतापूर्वक भेजा गया", flush=True)
    except Exception as exc:
        print(f"[ERROR] Telegram alert failed: {exc}", flush=True)


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
        f"<b>हंसराज मेघवाल</b>\n"
        f"<b>🚨 LIQUIDITY SWEEP ALERT 🚨</b>\n"
        f"<b>🪙 Asset:</b> {symbol} ({cfg.rules[symbol].ticker})\n"
        f"<b>🕒 Timeframe:</b> {setup['timeframe_label']}\n"
        f"<b>🧭 Direction:</b> {direction_text}\n"
        f"<b>⏱️ LTF Candle:</b> {candle_time}\n"
        f"<b>📊 Range:</b>\n"
        f"• Low: <code>{setup['range_low']}</code>\n"
        f"• High: <code>{setup['range_high']}</code>\n"
        f"<b>🎯 Entry Price:</b> <code>{setup['entry']}</code>\n"
        f"<b>🛑 Stop Loss:</b> <code>{setup['sl']}</code> ({sl_label})\n"
        f"<b>🏁 Take Profit Targets:</b>\n"
        f"• Target 1 (1:2): <code>{setup['target_1']}</code>\n"
        f"• Target 2 (1:{setup['rr_target']}): <code>{setup['target_2']}</code>\n"
        f"<b>⚖️ Risk/Reward:</b> 1:{setup['rr_target']}\n"
        f"<b>🛡️ Risk &amp; Discipline:</b> ₹{sizing['max_loss_inr']} ({setup['risk_pct']}%) | Margin: ₹{sizing['margin_inr']}\n"
        f"<b>📌 Daily Quota:</b> Trades {state['trades_today']}/{cfg.max_total_daily_trades} | Losses {state['losses_today']}/{cfg.max_total_daily_losses}\n"
        f"<b>⚠️ Caution:</b> {setup['precaution_note']}"
    )


def run_bot() -> None:
    cfg = MultiCommodityDisciplineConfig()
    ist_now_str = datetime.now(timezone(timedelta(hours=5, minutes=30))).strftime("%Y-%m-%d %H:%M:%S")
    state = load_state(cfg.state_file)
    print(f"[START] Commodity bot run at IST {ist_now_str} | state={state}", flush=True)

    if cfg.send_startup_test_alert and not state.get("startup_test_sent"):
        send_telegram("<b>हंसराज मेघवाल</b>: Commodity Liquidity Sweep Bot सक्रिय है।")
        state["startup_test_sent"] = True
        save_state(cfg.state_file, state)

    if (
        state["losses_today"] >= cfg.max_total_daily_losses
        or state["trades_today"] >= cfg.max_total_daily_trades
    ):
        print("[INFO] डेली ट्रेड या लॉस सीमा पूरी हो चुकी है।", flush=True)
        if not state.get("stand_down_sent"):
            send_telegram(
                "<b>🛑 STAND DOWN (GOLD / SILVER / OIL)</b>\n"
                f"• आज की संख्या सीमा पूरी हो गई है (Trades: {state['trades_today']}/{cfg.max_total_daily_trades})\n"
                f"• Losses: {state['losses_today']}/{cfg.max_total_daily_losses}\n"
                "• सावधानी: बढ़ने या बिकवाली की भावना से ट्रेड न लें—शांत रहें।"
            )
            state["stand_down_sent"] = True
            save_state(cfg.state_file, state)
        return

    in_sensitive_window, window_label = is_sensitive_market_window_ist(cfg)
    print(f"[WINDOW] {window_label} | block_in_sensitive_hours={cfg.block_in_sensitive_hours}", flush=True)
    if in_sensitive_window and cfg.block_in_sensitive_hours:
        caution_msg = (
            f"<b>⚠️ सावधानी गई मंज़िल ({window_label})</b>\n"
            "• संवेदनशील डेटा/सत्र विंडो के दौरान अचानक वोलेटिलिटी और फेक स्वीप का जोखिम अधिक रहता है।\n"
            "• इसलिए इस दौरान स्वचालित एंट्री अलर्ट रोके गए हैं—धैर्य रखें और स्पष्ट स्थिति की प्रतीक्षा करें।"
        )
        if state.get("last_caution_note") != window_label:
            send_telegram(caution_msg)
            state["last_caution_note"] = window_label
            save_state(cfg.state_file, state)
        return

    now_ts = int(time.time())
    cooldown_sec = cfg.cooldown_minutes * 60

    for symbol in ("GOLD", "SILVER", "OIL"):
        rule = cfg.rules.get(symbol)
        if not rule:
            continue

        if now_ts - state["last_alert_ts"].get(symbol, 0) < cooldown_sec:
            print(f"[SKIP - {symbol}] कूलडाउन सक्रिय है।", flush=True)
            continue

        candles = fetch_yahoo_candles(rule.ticker, interval="15m", range_span="5d")
        if not candles:
            print(f"[SKIP - {symbol} ({rule.ticker})] कोई कैंडल डेटा नहीं मिला।", flush=True)
            continue

        latest_candle_ts = candles[-1]["ts"]
        if state["last_candle_ts"].get(symbol) == latest_candle_ts:
            print(f"[SKIP - {symbol}] कैंडल टाइमस्टैम्प ({latest_candle_ts}) पहले ही जांचा जा चुका है।", flush=True)
            continue

        setup, skip_reason = evaluate_commodity_sweep(candles, rule)
        if skip_reason:
            print(f"[CAUTION - {symbol}] {skip_reason}", flush=True)
            state["last_candle_ts"][symbol] = latest_candle_ts
            continue

        if not setup:
            print(
                f"[NO_SETUP - {symbol} ({rule.ticker})] latest_ts={latest_candle_ts}, close={candles[-1]['close']}",
                flush=True,
            )
            continue

        max_loss_inr = cfg.capital_inr * (setup["risk_pct"] / 100.0)
        qty = max_loss_inr / setup["risk_per_unit"]
        margin_inr = (qty * setup["entry"]) / cfg.leverage

        state["trades_today"] += 1
        state["last_alert_ts"][symbol] = now_ts
        state["last_candle_ts"][symbol] = latest_candle_ts

        sizing = {
            "max_loss_inr": round(max_loss_inr, 2),
            "qty": round(qty, 4),
            "margin_inr": round(margin_inr, 2),
        }
        msg = build_alert_message(symbol, setup, sizing, cfg, state)
        send_telegram(msg)

        if state["trades_today"] >= cfg.max_total_daily_trades:
            break

    save_state(cfg.state_file, state)
    print("[DONE] रन पूर्ण हुआ", flush=True)


if __name__ == "__main__":
    run_bot()
