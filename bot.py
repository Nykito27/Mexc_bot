import ccxt
import pandas as pd
import pandas_ta as ta
import time
import requests
import os
import sys
from flask import Flask
from threading import Thread

# --- 1. FAKE WEB SERVER (Keeps Render Awake) ---
app = Flask('')

@app.route('/')
def home():
    return "Bot is running!"

def run_http():
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

def keep_alive():
    t = Thread(target=run_http)
    t.start()

# --- 2. SECURE CONFIGURATION ---
# The bot reads these from Render Settings, not this file
API_KEY = os.environ.get('MEXC_API_KEY')
SECRET_KEY = os.environ.get('MEXC_SECRET_KEY')
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID')

# Settings
RSI_ENTRY = 70
known_symbols = set()

# --- 3. BOT FUNCTIONS ---
def send_telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram keys missing!")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
    try:
        requests.post(url, json=payload)
    except:
        pass

def get_market_data(exchange, symbol):
    try:
        bars = exchange.fetch_ohlcv(symbol, timeframe='1m', limit=50)
        df = pd.DataFrame(bars, columns=['time', 'open', 'high', 'low', 'close', 'vol'])
        df['rsi'] = ta.rsi(df['close'], length=14)
        df['vwap'] = ta.vwap(df['high'], df['low'], df['close'], df['vol'])
        return df
    except:
        return None

def main_loop():
    # Connect to MEXC
    try:
        exchange = ccxt.mexc({
            'apiKey': API_KEY,
            'secret': SECRET_KEY,
            'options': {'defaultType': 'swap'}, 
            'enableRateLimit': True
        })
        print("Connected to MEXC")
    except Exception as e:
        print(f"Connection Failed: {e}")
        return

    # Load initial list
    try:
        markets = exchange.load_markets()
        for symbol in markets:
            known_symbols.add(symbol)
        send_telegram("🤖 **Bot Active on Render**\nScanning for new listings...")
    except Exception as e:
        print(f"Startup Error: {e}")

    while True:
        try:
            markets = exchange.load_markets(reload=True)
            current_symbols = set(markets.keys())
            new_coins = current_symbols - known_symbols
            
            if new_coins:
                for coin in new_coins:
                    known_symbols.add(coin)
                    
                    # ALERT 1: Preparation
                    send_telegram(f"🆕 **NEW LISTING DETECTED: {coin}**\n\n1. Open App\n2. Search {coin}\n3. Wait for signal...")
                    
                    # Watch for 30 mins
                    start_watch = time.time()
                    while (time.time() - start_watch) < (30 * 60):
                        df = get_market_data(exchange, coin)
                        if df is not None:
                            rsi = df['rsi'].iloc[-1]
                            price = df['close'].iloc[-1]
                            vwap = df['vwap'].iloc[-1]
                            
                            # ALERT 2: Entry Signal
                            if rsi > RSI_ENTRY and price < vwap:
                                send_telegram(f"🚨 **ENTER SHORT NOW!**\nSymbol: {coin}\nPrice broke below VWAP.")
                                break 
                        time.sleep(10)
            
            time.sleep(60) 
        except Exception as e:
            print(f"Error: {e}")
            time.sleep(10)

if __name__ == '__main__':
    keep_alive()
    main_loop()
