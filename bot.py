import ccxt
import pandas as pd
import pandas_ta as ta
import requests

# आपकी चैट आईडी और बॉट टोकन
TELEGRAM_TOKEN = "8963851584:AAHDD47K5Q20qAURgsBIoMQhBwMfUhdyrxM"
TELEGRAM_CHAT_ID = "6292189019"

def send_telegram_message(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown"
    }
    try:
        response = requests.post(url, json=payload)
        print('Telegram Response:', response.text)
    except Exception as e:
        print(f"Error sending telegram message: {e}")

def check_crypto():
    try:
        exchange = ccxt.binance({
            'enableRateLimit': True,
            'options': {'defaultType': 'spot'}
        })
        exchange.load_markets()
    except Exception as e:
        print(f"Exchange connection error: {e}")
        return

    # 47 प्रमुख क्रिप्टोकरेंसी की पूरी लिस्ट
    symbols = [
        'BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'XRP/USDT', 'ADA/USDT', 'DOGE/USDT', 
        'AVAX/USDT', 'LINK/USDT', 'NEAR/USDT', 'PEPE/USDT', 'MATIC/USDT', 'DOT/USDT', 
        'UNI/USDT', 'ICP/USDT', 'APT/USDT', 'RENDER/USDT', 'FET/USDT', 'INJ/USDT', 
        'AR/USDT', 'ATOM/USDT', 'LTC/USDT', 'ETC/USDT', 'FTM/USDT', 'STX/USDT', 
        'SUI/USDT', 'HBAR/USDT', 'IMX/USDT', 'GRT/USDT', 'RUNE/USDT', 'OP/USDT', 
        'ARBITRUM/USDT', 'TIA/USDT', 'SEI/USDT', 'FLOKI/USDT', 'SHIB/USDT', 'BONK/USDT', 
        'WIF/USDT', 'JUP/USDT', 'PYTH/USDT', 'MANA/USDT', 'SAND/USDT', 'AXS/USDT', 
        'GALA/USDT', 'CHZ/USDT', 'ENJ/USDT', 'CRV/USDT', 'MKR/USDT'
    ]
    
    print(f"कुल {len(symbols)} कॉइन्स की 5-इंडिकेटर स्कैनिंग (SL और Targets के साथ) शुरू हो रही है...")

    for symbol in symbols:
        try:
            ohlcv = exchange.fetch_ohlcv(symbol, timeframe='15m', limit=100)
            if not ohlcv or len(ohlcv) < 60:
                continue
                
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            
            # --- 5 इंडिकेटर्स की गणना ---
            df['ema9'] = ta.ema(df['close'], length=9)
            df['ema21'] = ta.ema(df['close'], length=21)
            df['rsi'] = ta.rsi(df['close'], length=14)
            
            macd_df = ta.macd(df['close'], fast=12, slow=26, signal=9)
            if macd_df is not None and not macd_df.empty:
                df['macd'] = macd_df['MACD_12_26_9']
                df['macd_signal'] = macd_df['MACDs_12_26_9']
            else:
                continue
                
            bb_df = ta.bbands(df['close'], length=20, std=2)
            if bb_df is not None and not bb_df.empty:
                df['bb_lower'] = bb_df['BBL_20_2.0']
                df['bb_upper'] = bb_df['BBU_20_2.0']
            else:
                continue
                
            df['vol_ma'] = ta.sma(df['volume'], length=20)
            
            curr = df.iloc[-1]
            prev = df.iloc[-2]
            
            current_price = curr['close']
            current_rsi = curr['rsi']
            
            # --- BUY (Bullish) की 5 शर्तें ---
            is_ema_buy = curr['ema9'] > curr['ema21']
            is_rsi_buy = 40 <= current_rsi <= 65
            is_macd_buy = curr['macd'] > curr['macd_signal']
            is_bb_buy = current_price <= curr['bb_lower'] * 1.01 or curr['close'] > prev['close']
            is_vol_buy = curr['volume'] > curr['vol_ma']
            
            if is_ema_buy and is_rsi_buy and is_macd_buy and is_bb_buy and is_vol_buy:
                # Buy के लिए Stop Loss और Targets कैलकुलेशन (Risk-Reward के आधार पर)
                sl = current_price * 0.985       # 1.5% नीचे Stop Loss
                t1 = current_price * 1.012       # Target 1 (+1.2%)
                t2 = current_price * 1.025       # Target 2 (+2.5%)
                t3 = current_price * 1.040       # Target 3 (+4.0%)
                
                msg = (
                    f"🚀 *5/5 INDICATOR BUY SIGNAL* 🟢\n\n"
                    f"🪙 Coin: `{symbol}`\n"
                    f"💰 Entry Price: `{current_price}`\n"
                    f"📈 RSI: `{current_rsi:.2f}`\n\n"
                    f"🛡️ *Stop Loss:* `{sl:.4f}`\n"
                    f"🎯 *Target 1:* `{t1:.4f}`\n"
                    f"🎯 *Target 2:* `{t2:.4f}`\n"
                    f"🎯 *Target 3:* `{t3:.4f}`\n\n"
                    f"📊 Status: All 5 Indicators Confirmed"
                )
                send_telegram_message(msg)
                print(f"BUY Signal sent for {symbol}")

            # --- SELL (Bearish) की 5 शर्तें ---
            is_ema_sell = curr['ema9'] < curr['ema21']
            is_rsi_sell = 35 <= current_rsi <= 60
            is_macd_sell = curr['macd'] < curr['macd_signal']
            is_bb_sell = current_price >= curr['bb_upper'] * 0.99 or curr['close'] < prev['close']
            is_vol_sell = curr['volume'] > curr['vol_ma']
            
            if is_ema_sell and is_rsi_sell and is_macd_sell and is_bb_sell and is_vol_sell:
                # Sell के लिए Stop Loss और Targets कैलकुलेशन
                sl = current_price * 1.015       # 1.5% ऊपर Stop Loss
                t1 = current_price * 0.988       # Target 1 (-1.2%)
                t2 = current_price * 0.975       # Target 2 (-2.5%)
                t3 = current_price * 0.960       # Target 3 (-4.0%)
                
                msg = (
                    f"🔻 *5/5 INDICATOR SELL SIGNAL* 🔴\n\n"
                    f"🪙 Coin: `{symbol}`\n"
                    f"💰 Entry Price: `{current_price}`\n"
                    f"📉 RSI: `{current_rsi:.2f}`\n\n"
                    f"🛡️ *Stop Loss:* `{sl:.4f}`\n"
                    f"🎯 *Target 1:* `{t1:.4f}`\n"
                    f"🎯 *Target 2:* `{t2:.4f}`\n"
                    f"🎯 *Target 3:* `{t3:.4f}`\n\n"
                    f"📊 Status: All 5 Indicators Confirmed"
                )
                send_telegram_message(msg)
                print(f"SELL Signal sent for {symbol}")

        except Exception as e:
            print(f"Error processing {symbol}: {e}")

if __name__ == '__main__':
    check_crypto()
