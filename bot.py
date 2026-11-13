import os
import ccxt
import requests

TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID')

def send_telegram_message(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram tokens missing!")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown"
    }
    try:
        response = requests.post(url, json=payload)
        print("Telegram Response:", response.text)
    except Exception as e:
        print("Error sending telegram message:", e)

def calculate_rsi(closes, period=14):
    if len(closes) < period + 1:
        return 50.0
    gains = 0
    losses = 0
    for i in range(1, period + 1):
        change = closes[-i] - closes[-i-1]
        if change > 0:
            gains += change
        else:
            losses -= change
    avg_gain = gains / period
    avg_loss = losses / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    return rsi

def check_crypto():
    exchange = ccxt.binance()
    symbols = ['BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'XRP/USDT', 'ADA/USDT', 'DOGE/USDT', 'AVAX/USDT', 'LINK/USDT', 'NEAR/USDT', 'PEPE/USDT']
    
    report = "🤖 *Crypto Alert Bot Status*\n\n"
    
    for symbol in symbols:
        try:
            ohlcv = exchange.fetch_ohlcv(symbol, timeframe='15m', limit=30)
            closes = [x[4] for x in ohlcv]
            current_price = closes[-1]
            rsi = calculate_rsi(closes)
            
            signal = "NEUTRAL ⚪"
            if rsi < 30:
                signal = "OVERSOLD (BUY) 🟢"
            elif rsi > 70:
                signal = "OVERBOUGHT (SELL) 🔴"
                
            report += f"*{symbol}*\nPrice: `{current_price}`\nRSI: `{rsi:.2f}`\nSignal: {signal}\n\n"
            print(f"Checked {symbol}: Price={current_price}, RSI={rsi:.2f}")
        except Exception as e:
            print(f"Error fetching {symbol}: {e}")
            
    send_telegram_message(report)

if __name__ == "__main__":
    check_crypto()
