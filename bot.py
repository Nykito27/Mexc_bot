import ccxt
import pandas as pd
import pandas_ta as ta
import time
import requests
import os
import sys
import json
from flask import Flask, request, jsonify, render_template_string
from threading import Thread, Event, Lock

# --- 1. CONFIG & GLOBALS ---
API_KEY = os.environ.get('MEXC_API_KEY')
SECRET_KEY = os.environ.get('MEXC_SECRET_KEY')
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID')
TELEGRAM_WEBHOOK = os.environ.get('TELEGRAM_WEBHOOK', '0') == '1'

# --- CLOUD KEYS ---
JSONBIN_BIN_ID = os.environ.get('JSONBIN_BIN_ID')
JSONBIN_API_KEY = os.environ.get('JSONBIN_API_KEY')

# Thread Safety Lock
data_lock = Lock()

# Settings
LOCAL_FILE = 'active_trades.json'
CHECK_INTERVAL = 10 

# --- 2. WEB SERVER & DASHBOARD ---
app = Flask(__name__)

DASHBOARD_HTML = """
<!doctype html>
<html>
<head>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
    body { background-color: #121212; color: #e0e0e0; font-family: monospace; padding: 10px; }
    h1 { color: #00ff88; text-align: center; }
    table { width: 100%; border-collapse: collapse; margin-top: 20px; }
    th, td { border: 1px solid #333; padding: 8px; text-align: left; font-size: 12px; }
    th { background-color: #1e1e1e; color: #03dac6; }
    tr:nth-child(even) { background-color: #1a1a1a; }
    .long { color: #00ff88; }
    .short { color: #ff5555; }
    .cloud { color: #ffcc00; font-weight: bold; }
</style>
</title>
<body>
<h1>🤖 MEXC Bot Dashboard</h1>
<p>Storage: <span class="cloud">{{ storage_mode }}</span></p>
<table>
  <tr><th>Symbol</th><th>Side</th><th>Entry</th><th>P&L</th><th>Status</th></tr>
  {% if trades %}
      {% for tid, t in trades.items() %}
      <tr>
        <td>{{ t['symbol'] }}</td>
        <td class="{{ 'long' if t['side'] == 'LONG' else 'short' }}">{{ t['side'] }}</td>
        <td>{{ t['entry_price'] }}</td>
        <td>{{ t.get('last_pnl', '0') }}%</td>
        <td>Running</td>
      </tr>
      {% endfor %}
  {% else %}
      <tr><td colspan="5" style="text-align:center">No active trades</td></tr>
  {% endif %}
</table>
</body>
</html>
"""

@app.route('/')
def home():
    with data_lock:
        trades = load_active_trades()
    mode = "CLOUD ☁️" if JSONBIN_BIN_ID else "LOCAL ⚠️ (Risk)"
    return render_template_string(DASHBOARD_HTML, trades=trades, storage_mode=mode)

@app.route('/telegram_webhook', methods=['POST'])
def telegram_webhook():
    data = request.json
    if not data: return jsonify({'ok': False}), 400
    msg = data.get('message') or data.get('edited_message')
    if not msg: return jsonify({'ok': True})
    text = msg.get('text', '')
    chat_id = msg.get('chat', {}).get('id')
    
    if TELEGRAM_CHAT_ID and str(chat_id) != str(TELEGRAM_CHAT_ID):
        return jsonify({'ok': True})
        
    with data_lock:
        trades = load_active_trades()
        process_message(text, chat_id, trades)
    return jsonify({'ok': True})

def run_http():
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

def keep_alive():
    t = Thread(target=run_http)
    t.daemon = True
    t.start()

# --- 3. TELEGRAM FUNCTIONS ---
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

def send_telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID: return
    url = f"{TELEGRAM_API}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
    try: requests.post(url, json=payload, timeout=5)
    except: pass

# --- 4. PERSISTENCE (CLOUD FIX) ---
def load_active_trades():
    raw_data = {}
    # Try Cloud First
    if JSONBIN_BIN_ID and JSONBIN_API_KEY:
        try:
            url = f"https://api.jsonbin.io/v3/b/{JSONBIN_BIN_ID}/latest"
            headers = {"X-Master-Key": JSONBIN_API_KEY}
            resp = requests.get(url, headers=headers, timeout=5)
            if resp.status_code == 200:
                raw_data = resp.json().get("record", {})
        except Exception as e:
            print(f"Cloud Load Error: {e}")
    else:
        # Fallback to Local
        try:
            if os.path.exists(LOCAL_FILE):
                with open(LOCAL_FILE, 'r') as f:
                    raw_data = json.load(f)
        except: pass

    # CLEANUP: Remove dummy data (like "status": "active")
    clean_trades = {}
    if isinstance(raw_data, dict):
        for k, v in raw_data.items():
            if isinstance(v, dict) and 'symbol' in v:
                clean_trades[k] = v
    return clean_trades

def save_active_trades(trades):
    # Save to Cloud
    if JSONBIN_BIN_ID and JSONBIN_API_KEY:
        try:
            url = f"https://api.jsonbin.io/v3/b/{JSONBIN_BIN_ID}"
            headers = {
                "Content-Type": "application/json",
                "X-Master-Key": JSONBIN_API_KEY
            }
            # Run in thread to not block bot
            Thread(target=requests.put, args=(url,), kwargs={'json': trades, 'headers': headers}).start()
        except Exception as e:
            print(f"Cloud Save Error: {e}")

    # Always save local backup
    try:
        with open(LOCAL_FILE, 'w') as f:
            json.dump(trades, f, indent=2)
    except: pass

# --- 5. LOGIC & CALCULATORS ---
def suggest_position_size(entry_price, notional_quote, leverage=10):
    try:
        entry_price = float(entry_price)
        notional = float(notional_quote)
        contracts = (notional * leverage) / entry_price
        return contracts
    except: return 0

def compute_contract_pnl(entry, current, contracts, side):
    entry, current, contracts = float(entry), float(current), float(contracts)
    diff = (current - entry) if side == 'LONG' else (entry - current)
    pnl_value = diff * contracts
    pct = (diff / entry) * 100 * float(10) 
    return pnl_value, pct

def process_message(text, chat_id, trades):
    if not text: return
    parts = text.strip().split()
    cmd = parts[0].lower()
    
    if cmd == '/track' and len(parts) >= 7:
        try:
            symbol, side, entry, tp, sl, amt = parts[1].upper(), parts[2].upper(), float(parts[3]), float(parts[4]), float(parts[5]), float(parts[6])
            lev = float(parts[7]) if len(parts) >= 8 else 10.0
            
            contracts = suggest_position_size(entry, amt, lev)
            trade_id = str(int(time.time()))[-5:]
            
            trades[trade_id] = {
                'symbol': symbol, 'side': side, 'entry_price': entry,
                'tp': tp, 'sl': sl, 'amount': amt, 'leverage': lev,
                'contracts': contracts, 'last_pnl': 0
            }
            save_active_trades(trades) # Saves to Cloud
            send_telegram(f"✅ **Tracking Started**\nID: `{trade_id}`\n{symbol} {side}\nSize: {contracts:.4f} Cont")
        except Exception as e:
            send_telegram(f"❌ Error: {e}")

    elif cmd == '/list':
        if not trades: send_telegram("No active trades.")
        else:
            msg = "📋 **Active Trades:**\n"
            for tid, t in trades.items():
                msg += f"`{tid}`: {t['symbol']} {t['side']} ({t['last_pnl']:.2f}%)\n"
            send_telegram(msg)

    elif cmd == '/remove' and len(parts) >= 2:
        tid = parts[1]
        if tid in trades:
            del trades[tid]
            save_active_trades(trades) # Updates Cloud
            send_telegram(f"🗑 Removed trade `{tid}`")

# --- 6. MARKET ANALYSIS ---
def get_market_data(exchange, symbol):
    try:
        bars = exchange.fetch_ohlcv(symbol, timeframe='1m', limit=60)
        df = pd.DataFrame(bars, columns=['time', 'open', 'high', 'low', 'close', 'vol'])
        df['rsi'] = ta.rsi(df['close'], length=14)
        df['vwap'] = ta.vwap(df['high'], df['low'], df['close'], df['vol'])
        return df
    except: return None

# --- 7. MAIN LOOP ---
def main_loop():
    with data_lock:
        trades = load_active_trades()
    known_symbols = set()
    
    try:
        exchange = ccxt.mexc({'apiKey': API_KEY, 'secret': SECRET_KEY, 'options': {'defaultType': 'swap'}, 'enableRateLimit': True})
        markets = exchange.load_markets()
        known_symbols = set(markets.keys())
        
        # Notify Storage Status
        store_status = "☁️ CLOUD STORAGE ACTIVE" if JSONBIN_BIN_ID else "⚠️ LOCAL STORAGE (Volatile)"
        send_telegram(f"🤖 **Bot Rebooted**\n{store_status}\nRestored {len(trades)} active trades.")
        
    except Exception as e:
        print(f"Startup Error: {e}")
        return

    # Telegram Poller
    if not TELEGRAM_WEBHOOK and TELEGRAM_TOKEN:
        def poller():
            last_id = None
            while True:
                try:
                    url = f"{TELEGRAM_API}/getUpdates"
                    params = {'timeout': 10, 'offset': last_id + 1 if last_id else None}
                    resp = requests.get(url, params=params).json()
                    for u in resp.get('result', []):
                        last_id = u['update_id']
                        if 'message' in u:
                            with data_lock:
                                process_message(u['message'].get('text'), u['message']['chat']['id'], trades)
                except: time.sleep(5)
        Thread(target=poller, daemon=True).start()

    while True:
        try:
            # 1. SCAN NEW LISTINGS
            markets = exchange.load_markets(reload=True)
            current = set(markets.keys())
            new_coins = current - known_symbols
            
            if new_coins:
                for coin in new_coins:
                    known_symbols.add(coin)
                    df = get_market_data(exchange, coin)
                    if df is not None:
                        rsi = df['rsi'].iloc[-1]
                        price = df['close'].iloc[-1]
                        vwap = df['vwap'].iloc[-1]
                        msg = f"🆕 **NEW: {coin}**\nPrice: {price}\nRSI: {rsi:.1f}"
                        if rsi > 70 and price < vwap:
                            msg += "\n🚨 **SIGNAL: SHORT NOW** (Break VWAP)"
                        else:
                            msg += "\n👀 Watch for Pump > 70 RSI"
                        send_telegram(msg)
                    else:
                        send_telegram(f"🆕 **NEW LISTING: {coin}**")

            # 2. MONITOR TRADES
            with data_lock:
                to_remove = []
                for tid, t in trades.items():
                    try:
                        ticker = exchange.fetch_ticker(t['symbol'])
                        price = float(ticker['last'])
                        pnl_val, pnl_pct = compute_contract_pnl(t['entry_price'], price, t['contracts'], t['side'])
                        t['last_pnl'] = pnl_pct
                        
                        tp_hit = (t['side'] == 'LONG' and price >= t['tp']) or (t['side'] == 'SHORT' and price <= t['tp'])
                        sl_hit = (t['side'] == 'LONG' and price <= t['sl']) or (t['side'] == 'SHORT' and price >= t['sl'])
                        
                        if tp_hit or sl_hit:
                            reason = "✅ TP HIT" if tp_hit else "🛑 SL HIT"
                            send_telegram(f"{reason} for {t['symbol']}\nPrice: {price}\nPnL: {pnl_val:.2f} USDT")
                            to_remove.append(tid)
                    except: pass
                
                for tid in to_remove:
                    del trades[tid]
                if to_remove: save_active_trades(trades)
                
            time.sleep(10)
            
        except Exception as e:
            print(f"Loop error: {e}")
            time.sleep(10)

if __name__ == '__main__':
    keep_alive()
    main_loop()
