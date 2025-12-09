# mexc_pro_bot_enhanced.py
"""
Enhanced MEXC Pro Bot
- Added logging
- Improved thread-safety (single in-memory `trades` dict guarded by Lock)
- Explicit error handling and reporting
- Exponential backoff and retry wrappers for network/API calls
- Clear separation of modules: config, persistence, exchange API wrappers, strategy, execution, webserver
- More deterministic saving (no background uploads that can race), but still use thread for JSONBin to avoid blocking on long network calls
- Rate-limit aware fetch wrapper
- Better Telegram command parsing and validation
"""

import ccxt
import pandas as pd
import pandas_ta as ta
import time
import requests
import os
import sys
import json
import numpy as np
import logging
from flask import Flask, request, jsonify, render_template_string
from threading import Thread, Lock
from functools import wraps
from typing import Callable, Any, Optional, Dict

# -------------------- CONFIG --------------------
API_KEY = os.environ.get('MEXC_API_KEY')
SECRET_KEY = os.environ.get('MEXC_SECRET_KEY')
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID')
TELEGRAM_WEBHOOK = os.environ.get('TELEGRAM_WEBHOOK', '0') == '1'
JSONBIN_BIN_ID = os.environ.get('JSONBIN_BIN_ID')
JSONBIN_API_KEY = os.environ.get('JSONBIN_API_KEY')

CHECK_INTERVAL = int(os.environ.get('CHECK_INTERVAL', 30))
DEFAULT_RISK_PCT = float(os.environ.get('DEFAULT_RISK_PCT', 1.0))
LOCAL_FILE = os.environ.get('LOCAL_FILE', 'active_trades.json')
MAX_BACKOFF = 32

# -------------------- LOGGING --------------------
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger('mexc_pro_bot')

# -------------------- GLOBALS --------------------
data_lock = Lock()
trades: Dict[str, dict] = {}  # single in-memory authoritative store
app = Flask(__name__)

# -------------------- UTIL: RETRIES & BACKOFF --------------------
def retry(backoff_base=1, max_backoff=MAX_BACKOFF, exceptions=(Exception,), max_attempts=5):
    def decorator(fn: Callable):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            attempt = 0
            delay = backoff_base
            while True:
                try:
                    return fn(*args, **kwargs)
                except exceptions as e:
                    attempt += 1
                    if attempt >= max_attempts:
                        logger.exception("Max retry attempts reached for %s", fn.__name__)
                        raise
                    logger.warning("%s failed (attempt %s/%s): %s — retrying in %ss", fn.__name__, attempt, max_attempts, e, delay)
                    time.sleep(delay)
                    delay = min(delay * 2, max_backoff)
        return wrapper
    return decorator

# -------------------- PERSISTENCE --------------------
@retry(exceptions=(requests.RequestException,))
def load_active_trades_from_jsonbin() -> dict:
    if not (JSONBIN_BIN_ID and JSONBIN_API_KEY):
        return {}
    url = f"https://api.jsonbin.io/v3/b/{JSONBIN_BIN_ID}/latest"
    headers = {"X-Master-Key": JSONBIN_API_KEY}
    resp = requests.get(url, headers=headers, timeout=8)
    resp.raise_for_status()
    return resp.json().get('record', {})


def load_active_trades() -> dict:
    global trades
    # Try JSONBin first (best-effort)
    try:
        record = load_active_trades_from_jsonbin()
        if isinstance(record, dict) and record:
            logger.info("Loaded %d trades from JSONBin", len(record))
            return record
    except Exception as e:
        logger.debug("JSONBin load failed: %s", e)

    # Fallback to local file
    if os.path.exists(LOCAL_FILE):
        try:
            with open(LOCAL_FILE, 'r') as f:
                data = json.load(f)
                logger.info("Loaded %d trades from local file", len(data))
                return data
        except Exception as e:
            logger.exception("Failed to load local trades: %s", e)
    logger.info("No persisted trades found. Starting fresh.")
    return {}


def _upload_jsonbin(trades_snapshot: dict):
    # Run in separate thread to avoid blocking main loop
    if not (JSONBIN_BIN_ID and JSONBIN_API_KEY):
        return
    try:
        url = f"https://api.jsonbin.io/v3/b/{JSONBIN_BIN_ID}"
        headers = {"Content-Type": "application/json", "X-Master-Key": JSONBIN_API_KEY}
        resp = requests.put(url, json=trades_snapshot, headers=headers, timeout=8)
        resp.raise_for_status()
        logger.info("Uploaded trades snapshot to JSONBin")
    except Exception:
        logger.exception("Failed to upload trades to JSONBin")


def save_active_trades(trades_to_save: dict):
    try:
        with open(LOCAL_FILE, 'w') as f:
            json.dump(trades_to_save, f, indent=2)
        logger.debug("Saved %d trades locally", len(trades_to_save))
    except Exception:
        logger.exception("Failed to save trades locally")
    # Fire-and-forget upload to JSONBin
    if JSONBIN_BIN_ID and JSONBIN_API_KEY:
        Thread(target=_upload_jsonbin, args=(trades_to_save.copy(),), daemon=True).start()

# -------------------- NETWORK HELPERS --------------------
session = requests.Session()
adapter = requests.adapters.HTTPAdapter(max_retries=3)
session.mount('https://', adapter)
session.mount('http://', adapter)

@retry(exceptions=(requests.RequestException,), max_attempts=4)
def safe_requests_get(url, **kwargs):
    return session.get(url, timeout=kwargs.pop('timeout', 5), **kwargs)

# -------------------- STRATEGY / RISK --------------------

def volatility_sizing(balance, recent_returns, risk_pct=DEFAULT_RISK_PCT):
    try:
        if len(recent_returns) < 2:
            return balance * 0.05
        sorted_r = np.sort(np.asarray(recent_returns))
        idx = int((1 - 0.95) * len(sorted_r))
        idx = max(0, min(idx, len(sorted_r) - 1))
        var = abs(sorted_r[idx])
        if var == 0: var = 0.01
        size = min((balance * (risk_pct / 100.0)) / var, balance * 0.2)
        return float(size)
    except Exception:
        logger.exception("Error in volatility_sizing")
        return balance * 0.05


def news_risk_filter():
    try:
        url = "https://cryptopanic.com/api/v1/posts/?auth_token=DEMO&kind=news&filter=important"
        r = safe_requests_get(url).json()
        return True if r.get('results') else False
    except Exception:
        logger.debug("News risk filter unavailable")
        return False


def market_regime(df: pd.DataFrame) -> str:
    try:
        adx = df.ta.adx(length=14)['ADX_14'].iloc[-1]
        ma50 = df.ta.sma(length=50).iloc[-1]
        price = df['close'].iloc[-1]
        return "TRENDING" if (adx > 25 and price > ma50) else "RANGING"
    except Exception:
        return "UNKNOWN"

# -------------------- EXCHANGE WRAPPERS --------------------

class ExchangeWrapper:
    def __init__(self, api_key, secret):
        self.exchange = ccxt.mexc({
            'apiKey': api_key,
            'secret': secret,
            'enableRateLimit': True,
            'options': {'defaultType': 'swap'}
        })
        self._load_markets_cached = set()

    @retry(exceptions=(ccxt.NetworkError, ccxt.ExchangeError), max_attempts=5)
    def load_markets(self, reload=False):
        if reload:
            m = self.exchange.load_markets(reload=True)
        else:
            m = self.exchange.load_markets()
        self._load_markets_cached = set(m.keys())
        return m

    @retry(exceptions=(ccxt.NetworkError, ccxt.ExchangeError), max_attempts=4)
    def fetch_ohlcv(self, symbol, timeframe='1m', limit=200):
        bars = self.exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        df = pd.DataFrame(bars, columns=['time', 'open', 'high', 'low', 'close', 'vol'])
        if not df.empty:
            df['rsi'] = ta.rsi(df['close'], length=14)
            df['vwap'] = ta.vwap(df['high'], df['low'], df['close'], df['vol'])
        return df

    @retry(exceptions=(ccxt.NetworkError, ccxt.ExchangeError), max_attempts=4)
    def fetch_ticker_last(self, symbol):
        tk = self.exchange.fetch_ticker(symbol)
        return float(tk['last'])

# -------------------- TELEGRAM --------------------

def send_telegram(message, buttons=None):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logger.debug("Telegram not configured")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
    if buttons:
        payload['reply_markup'] = {"inline_keyboard": buttons}
    try:
        resp = session.post(url, json=payload, timeout=5)
        resp.raise_for_status()
    except Exception:
        logger.exception("Failed to send Telegram message")

# -------------------- DASHBOARD --------------------
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
</style>
</head>
<body>
<h1>🤖 MEXC Pro Dashboard</h1>
<div class="card">
    <h3>Active Trades ({{ trades|length }})</h3>
    <table>
        <tr><th>Symbol</th><th>Side</th><th>Entry</th><th>P&L</th></tr>
        {% for tid, t in trades.items() %}
        <tr>
            <td>{{ t.get('symbol', 'Unknown') }}</td>
            <td class="{{ 'long' if t.get('side') == 'LONG' else 'short' }}">{{ t.get('side', '?') }}</td>
            <td>{{ t.get('entry_price', 0) }}</td>
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
        snapshot = trades.copy()
    return render_template_string(DASHBOARD_HTML, trades=snapshot)

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
        process_message(text, chat_id)
    return jsonify({'ok': True})

# -------------------- COMMAND PROCESSING --------------------

def process_message(text: str, chat_id: int):
    if not text: return
    parts = text.strip().split()
    cmd = parts[0].lower()

    if cmd == '/track':
        if len(parts) < 7:
            send_telegram("⚠️ **Format:**\n`/track SYMBOL SIDE ENTRY TP SL AMT LEV`")
            return
        try:
            symbol = parts[1].upper()
            side = parts[2].upper()
            entry = float(parts[3])
            tp = float(parts[4])
            sl = float(parts[5])
            amt = float(parts[6])
            lev = float(parts[7]) if len(parts) >= 8 else 10.0
            contracts = (amt * lev) / entry
            trade_id = str(int(time.time()))[-6:]
            new_trade = {
                'symbol': symbol,
                'side': side,
                'entry_price': entry,
                'tp': tp,
                'sl': sl,
                'amount': amt,
                'leverage': lev,
                'contracts': contracts,
                'last_pnl': 0.0,
                'status': 'RUNNING',
                'created_at': time.time()
            }
            with data_lock:
                trades[trade_id] = new_trade
                save_active_trades(trades.copy())
            btns = [[{"text": "❌ Close", "callback_data": f"CLOSE:{trade_id}"}]]
            send_telegram(f"✅ **Tracking** `{trade_id}`: {symbol}", buttons=btns)
            logger.info("Added trade %s: %s", trade_id, new_trade)
        except Exception:
            logger.exception("Failed to add trade via /track")
            send_telegram("❌ Error adding trade. Check format and values.")

    elif cmd == '/list':
        with data_lock:
            if not trades:
                send_telegram("No active trades.")
                return
            msg = "📋 **Active Trades:**\n"
            for tid, t in trades.items():
                msg += f"`{tid}`: {t.get('symbol')} {t.get('side')} ({t.get('last_pnl', 0):.2f}%)\n"
        send_telegram(msg)

    elif cmd == '/stats':
        with data_lock:
            n = len(trades)
        send_telegram(f"📊 Active Trades: {n}\nSystem: Online 🟢")

    elif cmd == '/reset':
        with data_lock:
            trades.clear()
            save_active_trades(trades.copy())
        send_telegram("🗑️ Memory Wiped.")

    elif cmd == '/remove' and len(parts) >= 2:
        tid = parts[1]
        with data_lock:
            if tid in trades:
                del trades[tid]
                save_active_trades(trades.copy())
                send_telegram(f"🗑 Removed trade `{tid}`")
            else:
                send_telegram(f"Trade `{tid}` not found.")

# -------------------- MAIN LOOP & MONITORING --------------------

def run_http():
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)


def main_loop():
    global trades
    # Initialize exchange
    try:
        ex = ExchangeWrapper(API_KEY, SECRET_KEY)
        markets = ex.load_markets()
        known_symbols = set(markets.keys())
        logger.info("Exchange markets loaded: %d markets", len(known_symbols))
        send_telegram("🤖 **Pro Bot Online**")
    except Exception:
        logger.exception("Failed to initialize exchange")
        return

    # Load persisted trades once at startup into in-memory store
    with data_lock:
        persisted = load_active_trades()
        trades = persisted if isinstance(persisted, dict) else {}

    if not TELEGRAM_WEBHOOK and TELEGRAM_TOKEN:
        def poller():
            last_id = None
            while True:
                try:
                    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
                    params = {'timeout': 10, 'offset': last_id + 1 if last_id else None}
                    resp = session.get(url, params=params, timeout=20).json()
                    for u in resp.get('result', []):
                        last_id = u['update_id']
                        if 'message' in u:
                            with data_lock:
                                process_message(u['message'].get('text'), u['message']['chat']['id'])
                        if 'callback_query' in u:
                            cb = u['callback_query']
                            data = cb.get('data', '')
                            if data.startswith('CLOSE:'):
                                tid = data.split(':', 1)[1]
                                with data_lock:
                                    if tid in trades:
                                        del trades[tid]
                                        save_active_trades(trades.copy())
                                        send_telegram(f"🛑 Closed Trade {tid}")
                except Exception:
                    logger.exception("Telegram poller error. Sleeping briefly.")
                    time.sleep(5)
        Thread(target=poller, daemon=True).start()

    last_news_check = 0
    while True:
        try:
            current_time = time.time()
            if current_time - last_news_check > 300:
                news_risk_filter()
                last_news_check = current_time

            # Reload markets and detect new symbols
            try:
                markets = ex.load_markets(reload=True)
                new_coins = set(markets.keys()) - known_symbols
            except Exception:
                logger.debug("Failed to refresh markets")
                new_coins = set()

            if new_coins:
                for coin in new_coins:
                    known_symbols.add(coin)
                    try:
                        df = ex.fetch_ohlcv(coin)
                        if df is not None and not df.empty:
                            rsi = float(df['rsi'].iloc[-1]) if 'rsi' in df.columns else None
                            price = float(df['close'].iloc[-1])
                            vwap = float(df['vwap'].iloc[-1]) if 'vwap' in df.columns else None
                            regime = market_regime(df)
                            msg = f"🆕 **NEW LISTING: {coin}**\nPrice: {price}\nRSI: {rsi:.1f if rsi is not None else 'N/A'}\nRegime: {regime}"
                            if rsi and rsi > 70 and vwap and price < vwap:
                                msg += "\n🚨 **SIGNAL: SHORT**"
                            elif rsi and rsi < 30:
                                msg += "\n🛒 **SIGNAL: BUY DIP**"
                            send_telegram(msg)
                    except Exception:
                        logger.debug("Failed to analyze new coin %s", coin)

            # Update PnL for tracked trades
            to_remove = []
            with data_lock:
                tids = list(trades.keys())
            for tid in tids:
                try:
                    with data_lock:
                        t = trades.get(tid)
                    if not t:
                        continue
                    price = ex.fetch_ticker_last(t['symbol'])
                    entry = float(t['entry_price'])
                    diff = (price - entry) if t['side'] == 'LONG' else (entry - price)
                    pnl = (diff / entry) * 100.0 * float(t.get('leverage', 1.0))
                    with data_lock:
                        t['last_pnl'] = pnl

                    tp_hit = (t['side'] == 'LONG' and price >= float(t['tp'])) or (t['side'] == 'SHORT' and price <= float(t['tp']))
                    sl_hit = (t['side'] == 'LONG' and price <= float(t['sl'])) or (t['side'] == 'SHORT' and price >= float(t['sl']))

                    if tp_hit or sl_hit:
                        send_telegram(f"{'✅ TP' if tp_hit else '🛑 SL'} for {t['symbol']}\nPrice: {price}\nPnL: {pnl:.2f}%")
                        to_remove.append(tid)
                except Exception:
                    logger.exception("Failed to update PnL for trade %s", tid)
            if to_remove:
                with data_lock:
                    for tid in to_remove:
                        if tid in trades:
                            del trades[tid]
                    save_active_trades(trades.copy())

            time.sleep(CHECK_INTERVAL)
        except KeyboardInterrupt:
            logger.info("Shutting down main loop")
            break
        except Exception:
            logger.exception("Unexpected error in main loop; sleeping briefly")
            time.sleep(5)

# -------------------- ENTRYPOINT --------------------
if __name__ == '__main__':
    # Start HTTP server
    Thread(target=run_http, daemon=True).start()

    # Start main loop
    main_loop()
