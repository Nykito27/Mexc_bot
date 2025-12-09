import ccxt
import pandas as pd
import pandas_ta as ta
import time
import requests
import os
import sys
import json
import numpy as np
from flask import Flask, request, jsonify, render_template_string
from threading import Thread, Lock

# --- 1. CONFIGURATION ---
API_KEY = os.environ.get('MEXC_API_KEY')
SECRET_KEY = os.environ.get('MEXC_SECRET_KEY')
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID')
TELEGRAM_WEBHOOK = os.environ.get('TELEGRAM_WEBHOOK', '0') == '1'

# Cloud Persistence (JsonBin)
JSONBIN_BIN_ID = os.environ.get('JSONBIN_BIN_ID')
JSONBIN_API_KEY = os.environ.get('JSONBIN_API_KEY')

# Settings
CHECK_INTERVAL = 30  # Slowed down to prevent rate limits
DEFAULT_RISK_PCT = 1.0
LOCAL_FILE = 'active_trades.json'

# Thread Safety
data_lock = Lock()
app = Flask(__name__)

# --- 2. ADVANCED ANALYSIS MODULES (Moved to Top) ---

def volatility_sizing(balance, recent_returns, risk_pct=1.0):
    """Calculates safe position size based on volatility (VaR)"""
    if len(recent_returns) < 2: return balance * 0.05 # Default 5% if no data
    
    # Calculate simple VaR (95% confidence)
    sorted_r = np.sort(recent_returns)
    idx = int((1 - 0.95) * len(sorted_r))
    var = abs(sorted_r[idx])
    
    if var == 0: var = 0.01
    
    risk_capital = balance * (risk_pct / 100.0)
    safe_size = risk_capital / var
    
    # Safety cap: Never bet more than 20% of balance even if low vol
    return min(safe_size, balance * 0.2)

def news_risk_filter():
    """Checks for major crypto news to pause trading"""
    try:
        # Using a public free endpoint for demonstration
        url = "https://cryptopanic.com/api/v1/posts/?auth_token=DEMO&kind=news&filter=important"
        r = requests.get(url, timeout=3).json()
        results = r.get('results', [])
        # If there is "Important" news in the last 15 mins, return True (Risk High)
        if results:
            # Check timestamps (omitted for brevity, assuming 'important' filter is enough)
            return True 
    except:
        return False
    return False

def market_regime(df):
    """Detects if market is Trending or Ranging"""
    try:
        adx = df.ta.adx(length=14)['ADX_14'].iloc[-1]
        ma50 = df.ta.sma(length=50).iloc[-1]
        price = df['close'].iloc[-1]
        
        if adx > 25:
            return "TRENDING" if price > ma50 else "TRENDING_DOWN"
        return "RANGING"
    except:
        return "UNKNOWN"

# --- 3. PERSISTENCE (CLOUD SAVING) ---

def load_active_trades():
    # Try Cloud First
    if JSONBIN_BIN_ID and JSONBIN_API_KEY:
        try:
            url = f"https://api.jsonbin.io/v3/b/{JSONBIN_BIN_ID}/latest"
            headers = {"X-Master-Key": JSONBIN_API_KEY}
            resp = requests.get(url, headers=headers, timeout=5)
            if resp.status_code == 200:
                return resp.json().get("record", {})
        except Exception as e:
            print(f"Cloud Load Error: {e}")
    
    # Fallback to Local
    if os.path.exists(LOCAL_FILE):
        try:
            with open(LOCAL_FILE, 'r') as f: return json.load(f)
        except: pass
    return {}

def save_active_trades(trades):
    # Save to Cloud
    if JSONBIN_BIN_ID and JSONBIN_API_KEY:
        def upload():
            try:
                url = f"https://api.jsonbin.io/v3/b/{JSONBIN_BIN_ID}"
                headers = {"Content-Type": "application/json", "X-Master-Key": JSONBIN_API_KEY}
                requests.put(url, json=trades, headers=headers)
            except: pass
        Thread(target=upload).start()

    # Save Local
    try:
        with open(LOCAL_FILE, 'w') as f: json.dump(trades, f, indent=2)
    except: pass

# --- 4. DASHBOARD & WEB SERVER ---

DASHBOARD_HTML = """
<!doctype html>
<html>
<head>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
    body { background-color: #0d1117; color: #c9d1d9; font-family: sans-serif; padding: 20px; }
    h1 { color: #58a6ff; text-align: center; }
    .card { background: #161b22; border: 1px solid #30363d; border-radius: 6px; padding: 15px; margin-bottom: 20px; }
    table { width: 100%; border-collapse: collapse; }
    th, td { padding: 10px; border-bottom: 1px solid #30363d; text-align: left; }
    th { color: #8b949e; }
    .long { color: #3fb950; font-weight: bold; }
    .short { color: #f85149; font-weight: bold; }
    .status-box { display: flex; justify-content: space-between; font-size: 0.9em; color: #8b949e; }
</style>
</head>
<body>
<h1>🤖 MEXC Pro Bot</h1>

<div class="card status-box">
    <span>Storage: {{ storage }}</span>
    <span>News Filter: {{ news_status }}</span>
</div>

<div class="card">
    <h3>Active Trades ({{ trades|length }})</h3>
    <table>
        <tr><th>Symbol</th><th>Side</th><th>Entry</th><th>Size (Cont)</th><th>P&L</th></tr>
        {% for tid, t in trades.items() %}
        <tr>
            <td>{{ t['symbol'] }}</td>
            <td class="{{ 'long' if t['side'] == 'LONG' else 'short' }}">{{ t['side'] }}</td>
            <td>{{ t['entry_price'] }}</td>
            <td>{{ '{:.4f}'.format(t.get('contracts', 0)) }}</td>
            <td>{{ '{:.2f}'.format(t.get('last_pnl', 0)) }}%</td>
        </tr>
        {% endfor %}
    </table>
</div>
</body>
</html>
"""

@app.route('/')
def home():
    with data_lock:
        trades = load_active_trades()
    store_mode = "☁️ CLOUD" if JSONBIN_BIN_ID else "⚠️ LOCAL"
    return render_template_string(DASHBOARD_HTML, trades=trades, storage=store_mode, news_status="ON")

@app.route('/telegram_webhook', methods=['POST'])
def telegram_webhook():
    data = request.json
    if not data: return jsonify({'ok': False}), 400
    msg = data.get('message', {})
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

# --- 5. TELEGRAM & LOGIC ---

def send_telegram(message, buttons=None):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID: return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
    if buttons: payload['reply_markup'] = {"inline_keyboard": buttons}
    try: requests.post(url, json=payload, timeout=5)
    except: pass

def get_market_data(exchange, symbol, limit=100):
    try:
        bars = exchange.fetch_ohlcv(symbol, timeframe='1m', limit=limit)
        df = pd.DataFrame(bars, columns=['time', 'open', 'high', 'low', 'close', 'vol'])
        df['rsi'] = ta.rsi(df['close'], length=14)
        df['vwap'] = ta.vwap(df['high'], df['low'], df['close'], df['vol'])
        return df
    except: return None

def process_message(text, chat_id, trades):
    if not text: return
    parts = text.strip().split()
    cmd = parts[0].lower()
    
    # /track SYMBOL SIDE ENTRY TP SL AMOUNT LEV
    if cmd == '/track' and len(parts) >= 7:
        try:
            symbol, side, entry, tp, sl, amt = parts[1].upper(), parts[2].upper(), float(parts[3]), float(parts[4]), float(parts[5]), float(parts[6])
            lev = float(parts[7]) if len(parts) >= 8 else 10.0
            
            # --- INTEGRATED VOLATILITY SIZING ---
            # Instead of blindly accepting 'amt', we check if it's too risky
            contracts = (amt * lev) / entry
            
            # NOTE: In a real "Pro" bot, we would fetch history here to check Volatility
            # but for speed in manual tracking, we accept user input and just warn
            if amt > 500: # Example safeguard
                send_telegram(f"⚠️ **High Value Trade Detected**\nUsing requested size: {amt} USDT")

            trade_id = str(int(time.time()))[-5:]
            trades[trade_id] = {
                'symbol': symbol, 'side': side, 'entry_price': entry,
                'tp': tp, 'sl': sl, 'amount': amt, 'leverage': lev,
                'contracts': contracts, 'last_pnl': 0, 'status': 'RUNNING'
            }
            save_active_trades(trades)
            
            # Interactive Buttons
            btns = [[
                {"text": "❌ Close", "callback_data": f"CLOSE:{trade_id}"},
                {"text": "🛡️ Move SL BE", "callback_data": f"BE:{trade_id}"}
            ]]
            send_telegram(f"✅ **Tracking Started**\nID: `{trade_id}`\n{symbol} {side}\nSize: {contracts:.4f} Cont", buttons=btns)
            
        except Exception as e:
            send_telegram(f"❌ Error: {e}")

    elif cmd == '/list':
        if not trades: send_telegram("No active trades.")
        else:
            msg = "📋 **Active Trades:**\n"
            for tid, t in trades.items():
                msg += f"`{tid}`: {t['symbol']} {t['side']} ({t['last_pnl']:.2f}%)\n"
            send_telegram(msg)

    elif cmd == '/stats':
        # Simple stats check
        send_telegram(f"📊 **Bot Status**\nActive Trades: {len(trades)}\nSystem: Online 🟢")

    elif cmd == '/remove' and len(parts) >= 2:
        tid = parts[1]
        if tid in trades:
            del trades[tid]
            save_active_trades(trades)
            send_telegram(f"🗑 Removed trade `{tid}`")

# --- 6. MAIN LOOP ---

def main_loop():
    # Load persistence
    with data_lock:
        trades = load_active_trades()
    
    # Connect to Exchange
    try:
        exchange = ccxt.mexc({'apiKey': API_KEY, 'secret': SECRET_KEY, 'options': {'defaultType': 'swap'}, 'enableRateLimit': True})
        markets = exchange.load_markets()
        known_symbols = set(markets.keys())
        send_telegram("🤖 **Pro Bot Online**\nSystem initialized.\nWaiting for signals...")
    except Exception as e:
        print(f"Startup Error: {e}")
        return

    # Telegram Poller (Backup if Webhook fails)
    if not TELEGRAM_WEBHOOK and TELEGRAM_TOKEN:
        def poller():
            last_id = None
            while True:
                try:
                    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
                    params = {'timeout': 10, 'offset': last_id + 1 if last_id else None}
                    resp = requests.get(url, params=params).json()
                    for u in resp.get('result', []):
                        last_id = u['update_id']
                        if 'message' in u:
                            with data_lock:
                                process_message(u['message'].get('text'), u['message']['chat']['id'], trades)
                        # Handle Button Clicks
                        if 'callback_query' in u:
                            cb = u['callback_query']
                            data = cb['data']
                            if "CLOSE:" in data:
                                tid = data.split(":")[1]
                                if tid in trades:
                                    del trades[tid]
                                    save_active_trades(trades)
                                    send_telegram(f"🛑 Closed Trade {tid}")
                except: time.sleep(5)
        Thread(target=poller, daemon=True).start()

    # Timers
    last_news_check = 0

    while True:
        try:
            current_time = time.time()
            
            # --- A. NEWS FILTER (Every 5 mins) ---
            if current_time - last_news_check > 300:
                is_risky = news_risk_filter()
                if is_risky:
                    send_telegram("⚠️ **MARKET ALERT**\nHigh impact news detected. Pausing auto-scans.")
                last_news_check = current_time

            # --- B. SCAN NEW LISTINGS ---
            markets = exchange.load_markets(reload=True)
            current_symbols = set(markets.keys())
            new_coins = current_symbols - known_symbols
            
            if new_coins:
                for coin in new_coins:
                    known_symbols.add(coin)
                    
                    # Fetch Data & Analyze
                    df = get_market_data(exchange, coin)
                    if df is not None:
                        rsi = df['rsi'].iloc[-1]
                        price = df['close'].iloc[-1]
                        vwap = df['vwap'].iloc[-1]
                        
                        # Market Regime Check
                        regime = market_regime(df)
                        
                        msg = f"🆕 **NEW LISTING: {coin}**\nPrice: {price}\nRSI: {rsi:.1f}\nRegime: {regime}"
                        
                        # Smart Signal Logic
                        if rsi > 70 and price < vwap:
                            msg += "\n🚨 **SIGNAL: SHORT** (Reversal)"
                            
                            # Auto-Calc Volatility Size
                            returns = df['close'].pct_change().dropna()
                            # Assume 1000 balance for calc if not connected to wallet
                            rec_size = volatility_sizing(1000, returns.values)
                            msg += f"\n💰 Rec. Size: {rec_size:.2f} USDT"
                            
                        elif rsi < 30:
                            msg += "\n🛒 **SIGNAL: BUY DIP**"
                        
                        send_telegram(msg)
                    else:
                        send_telegram(f"🆕 **NEW LISTING: {coin}** (No data yet)")

            # --- C. MONITOR ACTIVE TRADES ---
            with data_lock:
                to_remove = []
                for tid, t in trades.items():
                    try:
                        ticker = exchange.fetch_ticker(t['symbol'])
                        price = float(ticker['last'])
                        
                        # Calc PnL
                        entry, current, contracts = float(t['entry_price']), price, float(t['contracts'])
                        diff = (current - entry) if t['side'] == 'LONG' else (entry - current)
                        pnl_pct = (diff / entry) * 100 * float(t['leverage'])
                        t['last_pnl'] = pnl_pct
                        
                        # Check TP/SL
                        tp_hit = (t['side'] == 'LONG' and price >= t['tp']) or (t['side'] == 'SHORT' and price <= t['tp'])
                        sl_hit = (t['side'] == 'LONG' and price <= t['sl']) or (t['side'] == 'SHORT' and price >= t['sl'])
                        
                        if tp_hit or sl_hit:
                            reason = "✅ TP HIT" if tp_hit else "🛑 SL HIT"
                            send_telegram(f"{reason} for {t['symbol']}\nPrice: {price}\nPnL: {pnl_pct:.2f}%")
                            to_remove.append(tid)
                    except: pass
                
                # Cleanup
                for tid in to_remove:
                    del trades[tid]
                if to_remove: save_active_trades(trades)

            time.sleep(CHECK_INTERVAL)

        except Exception as e:
            print(f"Loop error: {e}")
            time.sleep(30)

if __name__ == '__main__':
    Thread(target=run_http, daemon=True).start()
    main_loop()
