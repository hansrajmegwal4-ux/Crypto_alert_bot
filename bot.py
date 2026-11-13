import ccxt
import pandas as pd
import pandas_ta as ta
import requests
import time
import os

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

def send_telegram_message(message):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print("Telegram tokens not set!")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": message,
        "parse_mode": "Markdown"
    }
    try:
        response = requests.post(url, json=payload)
        return response.json()
    except Exception as e:
        print(f"Error sending message: {e}")

def check_crypto_signals():
    print("Scanning crypto market...")
    exchange = ccxt.binance()
    symbols = ['BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'XRP/USDT', 'ADA/USDT', 'DOGE/USDT', 'AVAX/USDT', 'LINK/USDT', 'NEAR/USDT', 'PEPE/USDT']
    signals_found = 0
    
    for symbol in symbols:
        try:
            ohlcv = exchange.fetch_ohlcv(symbol, timeframe='15m', limit=100)
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df['EMA_20'] = ta.ema(df['close'], length=20)
            df['RSI'] = ta.rsi(df['close'], length=14)
            macd_df = ta.macd(df['close'])
            if macd_df is not None and not macd_df.empty:
                df = pd.concat([df, macd_df], axis=1)
            
            last_row = df.iloc[-1]
            close_price = last_row['close']
            rsi_val = last_row.get('RSI', 50)
            
            if rsi_val < 30:
                msg = f"🟢 **BUY SIGNAL (Oversold)**\nCoin: `{symbol}`\nPrice: `{close_price}`\nRSI: `{rsi_val:.2f}`"
                send_telegram_message(msg)
                signals_found += 1
            elif rsi_val > 70:
                msg = f"🔴 **SELL SIGNAL (Overbought)**\nCoin: `{symbol}`\nPrice: `{close_price}`\nRSI: `{rsi_val:.2f}`"
                send_telegram_message(msg)
                signals_found += 1
                
        except Exception as e:
            print(f"Error processing {symbol}: {e}")
            
    print(f"Scan complete. Signals sent: {signals_found}")

if __name__ == "__main__":
    check_crypto_signals()
