# mexc_master_bot_v3.1_pro.py
"""
MASTER VERSION v3.1 + PRO MARKET SCANNER + RISK MODULE
- All features A-K included (MTF, Breakouts, Volatility filters, Funding/OI, dynamic SL/TP, sizing, correlation avoidance)
- Scans USDT-perps every 10 minutes by default.
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
import traceback
from flask import Flask, request, jsonify, render_template_string
from threading import Thread, Lock
from functools import wraps
from typing import Callable, Any, Optional, Dict

# -------------------- CONFIGURATION --------------------
API_KEY = os.environ.get('MEXC_API_KEY')
SECRET_KEY = os.environ.get('MEXC_SECRET_KEY')
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID')
TELEGRAM_WEBHOOK = os.environ.get('TELEGRAM_WEBHOOK', '0') == '1'
JSONBIN_BIN_ID = os.environ.get('JSONBIN_BIN_ID')
JSONBIN_API_KEY = os.environ.get('JSONBIN_API_KEY')

CHECK_INTERVAL = int(os.environ.get('CHECK_INTERVAL', 30))  # trade loop sleep
# UPDATED: Safer defaults as requested
FULL_SCAN_INTERVAL = int(os.environ.get('FULL_SCAN_INTERVAL', 600))  # 600s = 10 minutes
DEFAULT_RISK_PCT = float(os.environ.get('DEFAULT_RISK_PCT', 1.0))
LOCAL_FILE = os.environ.get('LOCAL_FILE', 'bot_data.json')
MAX_BACKOFF = 32

# PRO FILTERS / HYPERPARAMS (tune these)
MIN_VOLUME_USDT_1M = float(os.environ.get('MIN_VOLUME_USDT_1M', 200000.0))  # min $ volume per 1m bar
STRONG_SCORE_THRESHOLD = float(os.environ.get('STRONG_SCORE_THRESHOLD', 40.0))
CORRELATION_AVOID_THRESHOLD = float(os.environ.get('CORRELATION_AVOID_THRESHOLD', 0.85))
FUNDING_BIAS_THRESHOLD = float(os.environ.get('FUNDING_BIAS_THRESHOLD', 0.001))  # 0.1% -> 0.001
MIN_OI = float(os.environ.get('MIN_OI', 500000.0))  # minimum open interest $ to consider
# UPDATED: Safer default as requested
MAX_SYMBOLS_PER_SCAN = int(os.environ.get('MAX_SYMBOLS_PER_SCAN', 100))  # safety cap
ATR_TRAIL_MULT = float(os.environ.get('ATR_TRAIL_MULT', 1.5))  # ATR multiplier for trailing SL
PNL_ALERT_PCT = float(os.environ.get('PNL_ALERT_PCT', 80.0))  # alert when 80% to TP

# -------------------- LOGGING --------------------
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger('mexc_master_pro')

# -------------------- GLOBALS --------------------
data_lock = Lock()
bot_memory = {
    "trades": {},
    "known_symbols": []
}
app = Flask(__name__)

# -------------------- UTIL: RETRY LOGIC --------------------
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
                        logger.error(f"Max retries reached for {fn.__name__}")
                        raise
                    logger.warning(f"{fn.__name__} failed ({attempt}/{max_attempts}): {e}. Retrying in {delay}s...")
                    time.sleep(delay)
                    delay = min(delay * 2, max_backoff)
        return wrapper
    return decorator

# -------------------- PERSISTENCE --------------------
@retry(exceptions=(requests.RequestException,))
def load_data_from_jsonbin() -> dict:
    if not (JSONBIN_BIN_ID and JSONBIN_API_KEY): return {}
    url = f"https://api.jsonbin.io/v3/b/{JSONBIN_BIN_ID}/latest"
    headers = {"X-Master-Key": JSONBIN_API_KEY}
    resp = requests.get(url, headers=headers, timeout=8)
    if resp.status_code == 200:
        return resp.json().get('record', {})
    return {}

def load_bot_data():
    global bot_memory
    data = {}
    # 1. Try Cloud
    try:
        cloud_data = load_data_from_jsonbin()
        if isinstance(cloud_data, dict): data = cloud_data
    except Exception as e:
        logger.warning(f"Cloud load warning: {e}")

    # 2. Try Local Fallback
    if not data and os.path.exists(LOCAL_FILE):
        try:
            with open(LOCAL_FILE, 'r') as f:
                local_data = json.load(f)
                if isinstance(local_data, dict): data = local_data
        except Exception as e:
            logger.error(f"Local load error: {e}")

    # 3. Initialize if empty or partial
    if not isinstance(data, dict): data = {}
    if "trades" not in data: data["trades"] = {}
    if "known_symbols" not in data: data["known_symbols"] = []
    
    bot_memory = data
    return bot_memory

def _upload_jsonbin(snapshot: dict):
    if not (JSONBIN_BIN_ID and JSONBIN_API_KEY): return
    try:
        url = f"https://api.jsonbin.io/v3/b/{JSONBIN_BIN_ID}"
        headers = {"Content-Type": "application/json", "X-Master-Key": JSONBIN_API_KEY}
        requests.put(url, json=snapshot, headers=headers, timeout=10)
    except Exception:
        logger.debug("jsonbin upload failed", exc_info=True)

def save_bot_data():
    snapshot = bot_memory.copy()
    # Local Save
    try:
        with open(LOCAL_FILE, 'w') as f: json.dump(snapshot, f, indent=2)
    except Exception:
        logger.debug("local save failed", exc_info=True)
    
    # Cloud Save (Threaded)
    if JSONBIN_BIN_ID and JSONBIN_API_KEY:
        Thread(target=_upload_jsonbin, args=(snapshot,), daemon=True).start()

# -------------------- PRO STRATEGY MODULES --------------------

def volatility_sizing(balance, recent_returns, risk_pct=DEFAULT_RISK_PCT):
    """
    Enhanced sizing using volatility + a Sharpe-like adjustment.
    recent_returns: list/array of recent percentage returns (decimal form, e.g., 0.01)
    """
    try:
        if len(recent_returns) < 5:
            return balance * 0.02  # default conservative sizing
        returns = np.asarray(recent_returns)
        mean = returns.mean()
        std = returns.std(ddof=1)
        # Sharpe-like estimator (over short sample)
        sharpe = (mean / std) if std > 0 else 0.0
        volatility = std * 100.0  # percent
        base_size = (balance * (risk_pct / 100.0)) / (volatility + 0.01)
        # Adjust by sharpe: better sharpe -> scale up, poor sharpe -> scale down
        adj = 1.0 + np.tanh(sharpe) * 0.5
        size = min(base_size * adj, balance * 0.2)
        return float(size)
    except Exception:
        return balance * 0.02

def news_risk_filter():
    """
    Basic news filter: returns True if high-impact news found.
    """
    try:
        url = "https://cryptopanic.com/api/v1/posts/?auth_token=DEMO&kind=news&filter=important"
        r = requests.get(url, timeout=5).json()
        return True if r.get('results') else False
    except Exception:
        return False

def market_regime(df):
    try:
        adx = df.ta.adx(length=14)['ADX_14'].iloc[-1]
        ma50 = df.ta.sma(length=50).iloc[-1]
        price = df['close'].iloc[-1]
        return "TRENDING" if (adx > 25 and price > ma50) else "RANGING"
    except Exception:
        return "UNKNOWN"

# -------------------- EXCHANGE WRAPPER --------------------

class ExchangeWrapper:
    def __init__(self, api_key, secret):
        self.exchange = ccxt.mexc({
            'apiKey': api_key,
            'secret': secret,
            'enableRateLimit': True,
            'options': {'defaultType': 'swap'}
        })
        self._cached_markets = set()
        self._markets_info = {}

    @retry(exceptions=(ccxt.NetworkError, ccxt.ExchangeError), max_attempts=3)
    def load_markets(self, reload=False):
        m = self.exchange.load_markets(reload=reload)
        self._cached_markets = set(m.keys())
        self._markets_info = m
        return m

    @retry(exceptions=(ccxt.NetworkError, ccxt.ExchangeError), max_attempts=3)
    def fetch_ohlcv(self, symbol, timeframe='1m', limit=200):
        bars = self.exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        df = pd.DataFrame(bars, columns=['time', 'open', 'high', 'low', 'close', 'vol'])
        if not df.empty:
            try:
                df['rsi'] = ta.rsi(df['close'], length=14)
            except Exception:
                df['rsi'] = pd.Series([np.nan]*len(df))
            try:
                df['vwap'] = ta.vwap(df['high'], df['low'], df['close'], df['vol'])
            except Exception:
                df['vwap'] = df['close'].rolling(14).mean()
            try:
                adx = df.ta.adx(length=14)
                df['adx'] = adx['ADX_14']
            except Exception:
                df['adx'] = pd.Series([np.nan]*len(df))
            try:
                df['atr'] = ta.atr(df['high'], df['low'], df['close'], length=14)
            except Exception:
                df['atr'] = pd.Series([np.nan]*len(df))
        return df

    def fetch_ticker_price(self, symbol):
        try:
            return float(self.exchange.fetch_ticker(symbol)['last'])
        except Exception:
            return None

    def fetch_funding_rate(self, symbol):
        try:
            if hasattr(self.exchange, 'fetch_funding_rate'):
                fr = self.exchange.fetch_funding_rate(symbol)
                if isinstance(fr, dict):
                    return float(fr.get('fundingRate') or fr.get('rate') or fr.get('funding_rate') or 0.0)
            t = self.exchange.fetch_ticker(symbol)
            for k in ['fundingRate', 'funding_rate', 'funding']:
                if k in t and t[k] is not None:
                    return float(t[k])
        except Exception:
            return None
        return None

    def fetch_open_interest(self, symbol):
        try:
            if hasattr(self.exchange, 'fetch_open_interest'):
                oi = self.exchange.fetch_open_interest(symbol)
                if isinstance(oi, dict):
                    return float(oi.get('openInterest') or oi.get('open_interest') or 0.0)
                elif isinstance(oi, (int, float)):
                    return float(oi)
        except Exception:
            return None
        return None

# -------------------- TELEGRAM & COMMANDS --------------------
session = requests.Session()

def send_telegram(message, buttons=None):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID: 
        logger.info("Telegram disabled or missing credentials. Would send:\n%s", message)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
    if buttons: payload['reply_markup'] = {"inline_keyboard": buttons}
    try: session.post(url, json=payload, timeout=7)
    except Exception:
        logger.exception("Failed to send telegram")

def process_message(text, chat_id):
    if not text: return
    parts = text.strip().split()
    cmd = parts[0].lower()
    global bot_memory

    # --- RESET COMMAND ---
    if cmd == '/reset':
        with data_lock:
            bot_memory["trades"] = {}
            save_bot_data()
        send_telegram("🗑️ **Trade Memory Wiped.** (Known coins kept safe)")
        return

    # --- TRACK COMMAND ---
    if cmd == '/track':
        if len(parts) < 7:
            send_telegram("⚠️ **Usage:**\n`/track SYMBOL SIDE ENTRY TP SL AMT LEV`")
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
                'symbol': symbol, 'side': side, 'entry_price': entry,
                'tp': tp, 'sl': sl, 'amount': amt, 'leverage': lev,
                'contracts': contracts, 'last_pnl': 0.0, 'status': 'RUNNING',
                'created': time.time()
            }
            
            with data_lock:
                bot_memory["trades"][trade_id] = new_trade
                save_bot_data()
            
            btns = [[{"text": "❌ Close", "callback_data": f"CLOSE:{trade_id}"}]]
            send_telegram(f"✅ **Tracking** `{trade_id}`: {symbol}", buttons=btns)
        except Exception as e:
            send_telegram(f"❌ Error: {str(e)}")

    # --- LIST COMMAND ---
    elif cmd == '/list':
        with data_lock:
            trades = bot_memory.get("trades", {})
            if not trades:
                send_telegram("📭 No active trades.")
                return
            msg = "📋 **Active Trades:**\n"
            for tid, t in trades.items():
                sym = t.get('symbol', '??')
                side = t.get('side', '?')
                pnl = t.get('last_pnl', 0)
                msg += f"`{tid}`: {sym} {side} ({pnl:.2f}%)\n"
        send_telegram(msg)

    elif cmd == '/stats':
        with data_lock:
            t_cnt = len(bot_memory.get("trades", {}))
            s_cnt = len(bot_memory.get("known_symbols", []))
        send_telegram(f"📊 **System Status**\nActive Trades: {t_cnt}\nKnown Coins: {s_cnt}\nOnline 🟢")

    elif cmd == '/remove' and len(parts) >= 2:
        tid = parts[1]
        with data_lock:
            if tid in bot_memory["trades"]:
                del bot_memory["trades"][tid]
                save_bot_data()
                send_telegram(f"🗑 Removed `{tid}`")
            else: send_telegram("❌ ID not found.")

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
<h1>🤖 MEXC Master Pro Dashboard</h1>
<div class="card">
    <h3>Active Trades ({{ trades|length }})</h3>
    <table>
        <tr><th>Symbol</th><th>Side</th><th>Entry</th><th>P&L</th></tr>
        {% for tid, t in trades.items() %}
        <tr>
            <td>{{ t.get('symbol', '?') }}</td>
            <td class="{{ 'long' if t.get('side') == 'LONG' else 'short' }}">{{ t.get('side', '?') }}</td>
            <td>{{ t.get('entry_price', 0) }}</td>
            <td>{{ '{:.2f}'.format(t.get('last_pnl', 0)) }}%</td>
        </tr>
        {% endfor %}
    </table>
    <p>Memory: {{ known_count }} known coins</p>
</div>
</body>
</html>
"""

@app.route('/')
def home():
    with data_lock:
        snapshot = bot_memory.get("trades", {}).copy()
        k_count = len(bot_memory.get("known_symbols", []))
    return render_template_string(DASHBOARD_HTML, trades=snapshot, known_count=k_count)

@app.route('/telegram_webhook', methods=['POST'])
def telegram_webhook():
    data = request.json
    if not data: return jsonify({'ok': False}), 400
    msg = data.get('message', {})
    text = msg.get('text', '')
    chat_id = msg.get('chat', {}).get('id')
    if TELEGRAM_CHAT_ID and str(chat_id) != str(TELEGRAM_CHAT_ID): return jsonify({'ok': True})
    process_message(text, chat_id)
    return jsonify({'ok': True})

def run_http():
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

# -------------------- SCORING + SIGNALS (PRO) --------------------
def compute_vol_zscore(df, window=30):
    try:
        vols = df['vol'].pct_change().abs().dropna()
        if len(vols) < window: return 0.0
        recent = vols.tail(window)
        z = (recent.iloc[-1] - recent.mean()) / (recent.std(ddof=1) + 1e-9)
        return float(z)
    except Exception:
        return 0.0

def compute_correlation_with_btc(ex, symbol, timeframe='1m', lookback=120):
    try:
        if symbol == 'BTC/USDT': return 1.0
        df_sym = ex.fetch_ohlcv(symbol, timeframe=timeframe, limit=lookback)
        df_btc = ex.fetch_ohlcv('BTC/USDT', timeframe=timeframe, limit=lookback)
        if df_sym is None or df_btc is None: return 0.0
        ret_sym = df_sym['close'].pct_change().dropna()
        ret_btc = df_btc['close'].pct_change().dropna()
        n = min(len(ret_sym), len(ret_btc))
        if n < 10: return 0.0
        corr = np.corrcoef(ret_sym.tail(n), ret_btc.tail(n))[0,1]
        if np.isnan(corr): return 0.0
        return float(corr)
    except Exception:
        return 0.0

def score_coin_pro(ex: ExchangeWrapper, symbol: str) -> dict:
    try:
        df1 = ex.fetch_ohlcv(symbol, timeframe='1m', limit=200)
        df5 = ex.fetch_ohlcv(symbol, timeframe='5m', limit=200)
        df15 = ex.fetch_ohlcv(symbol, timeframe='15m', limit=200)
        if df1 is None or df1.empty: return {'score': 0.0, 'reason': 'no_1m'}
        if len(df1) < 30: return {'score': 0.0, 'reason': 'short_1m'}

        price = float(df1['close'].iloc[-1])
        rsi1 = float(df1['rsi'].iloc[-1]) if 'rsi' in df1.columns else None
        rsi5 = float(df5['rsi'].iloc[-1]) if df5 is not None and 'rsi' in df5.columns else None
        rsi15 = float(df15['rsi'].iloc[-1]) if df15 is not None and 'rsi' in df15.columns else None

        vwap1 = float(df1['vwap'].iloc[-1]) if 'vwap' in df1.columns else price
        adx15 = float(df15['adx'].iloc[-1]) if df15 is not None and 'adx' in df15.columns else None
        
        vwap_dist_pct = ((price - vwap1) / (vwap1 if vwap1 != 0 else price)) * 100.0
        vol_z = compute_vol_zscore(df1, window=30)
        funding = ex.fetch_funding_rate(symbol)
        oi = ex.fetch_open_interest(symbol)
        
        last_vol = float(df1['vol'].iloc[-1])
        est_usd_vol_1m = last_vol * price
        
        try:
            vol_pct = df1['close'].pct_change().dropna().tail(30).std() * 100.0
        except Exception:
            vol_pct = 0.0

        score = 0.0
        reasons = []

        # 1) MTF confirmation
        mtf_long_votes = 0
        mtf_short_votes = 0
        if rsi1 is not None:
            if rsi1 < 34: mtf_long_votes += 1
            if rsi1 > 66: mtf_short_votes += 1
        if rsi5 is not None:
            if rsi5 < 40: mtf_long_votes += 1
            if rsi5 > 60: mtf_short_votes += 1
        if rsi15 is not None:
            if rsi15 < 48: mtf_long_votes += 1
            if rsi15 > 52: mtf_short_votes += 1
        score += (mtf_long_votes - mtf_short_votes) * 8.0

        # 2) VWAP alignment
        if vwap_dist_pct < -2.5: score += 18
        elif vwap_dist_pct < -1.0: score += 8
        elif vwap_dist_pct > 2.0: score -= 8

        # 3) ADX
        if adx15 is not None:
            if adx15 > 30: score += 12
            elif adx15 < 12: score -= 6

        # 4) Vol Z
        if vol_z > 1.5: score += 14
        elif vol_z > 0.8: score += 6

        # 5) ATR vol
        if 0 < vol_pct < 3.5: score += 6
        elif 3.5 <= vol_pct < 8: score += 10
        elif vol_pct >= 8: score -= 8

        # 6) Funding bias
        if funding is not None:
            if funding > FUNDING_BIAS_THRESHOLD: score -= 8
            elif funding < -FUNDING_BIAS_THRESHOLD: score += 8

        # 7) OI signals
        if oi is not None:
            if oi > MIN_OI and vol_z > 1.0: score += 10
            elif oi is not None and oi < MIN_OI: score -= 6

        # 8) Breakout detection
        try:
            last20_high = df1['high'].tail(20).max()
            last20_low = df1['low'].tail(20).min()
            if price > last20_high and vol_z > 0.8: score += 18
            if price < last20_low and vol_z > 0.8: score -= 18
        except: pass

        # 9) Liquidity check
        if est_usd_vol_1m < MIN_VOLUME_USDT_1M: score -= 50

        # 10) Correlation avoidance
        corr_with_btc = compute_correlation_with_btc(ex, symbol, timeframe='1m', lookback=120)
        if corr_with_btc >= CORRELATION_AVOID_THRESHOLD: score -= 6

        score = max(min(score, 100.0), -100.0)
        return {
            'score': float(score), 'price': price,
            'rsi1': rsi1, 'rsi5': rsi5, 'rsi15': rsi15,
            'vwap_dist_pct': float(vwap_dist_pct),
            'adx15': adx15, 'vol_z': float(vol_z),
            'vol_pct': float(vol_pct), 'funding': funding,
            'oi': oi, 'est_usd_vol_1m': float(est_usd_vol_1m),
            'corr_with_btc': float(corr_with_btc),
            'reasons': reasons
        }
    except Exception:
        return {'score': 0.0, 'reason': 'error'}

# -------------------- TRADE MANAGEMENT --------------------
def adjust_trailing_stop(ex: ExchangeWrapper, tid: str, trade: dict):
    try:
        df1 = ex.fetch_ohlcv(trade['symbol'], timeframe='1m', limit=100)
        if df1 is None or df1.empty or 'atr' not in df1.columns: return
        atr = float(df1['atr'].iloc[-1])
        entry = float(trade['entry_price'])
        price = ex.fetch_ticker_price(trade['symbol'])
        if price is None: return

        pnl = (price - entry) / entry * 100.0 if trade['side'] == 'LONG' else (entry - price) / entry * 100.0
        new_sl = None
        if pnl >= 10: new_sl = entry
        if pnl >= 20:
            buffer = atr * ATR_TRAIL_MULT
            if trade['side'] == 'LONG': new_sl = max(new_sl or trade['sl'], entry + buffer)
            else: new_sl = min(new_sl or trade['sl'], entry - buffer)

        if new_sl is not None:
            with data_lock:
                old_sl = bot_memory["trades"][tid]['sl']
                tighten = (trade['side'] == 'LONG' and new_sl > old_sl) or (trade['side'] == 'SHORT' and new_sl < old_sl)
                if tighten:
                    bot_memory["trades"][tid]['sl'] = float(new_sl)
                    save_bot_data()
                    send_telegram(f"🔒 `{tid}` SL moved to {new_sl:.6f} (PnL {pnl:.2f}%)")
    except: pass

def check_pnl_distance_alert(tid: str, trade: dict, price: float):
    try:
        entry, tp, sl = float(trade['entry_price']), float(trade['tp']), float(trade['sl'])
        side = trade['side']
        if side == 'LONG':
            progress = (price - entry) / (tp - entry) * 100.0 if tp > entry else 0
        else:
            progress = (entry - price) / (entry - tp) * 100.0 if tp < entry else 0
        if progress >= PNL_ALERT_PCT and progress < 100.0:
            send_telegram(f"⚡ `{tid}` {progress:.0f}% to TP ({trade['symbol']})")
    except: pass

# -------------------- FULL MARKET SCAN (PRO) --------------------
def scan_all_markets_pro(ex: ExchangeWrapper, top_n: int = 3, strong_threshold: float = STRONG_SCORE_THRESHOLD):
    logger.info("Starting PRO full market scan...")
    results = []
    symbols = sorted(list(ex._cached_markets))
    symbols = [s for s in symbols if s.endswith('/USDT')]
    if MAX_SYMBOLS_PER_SCAN and len(symbols) > MAX_SYMBOLS_PER_SCAN:
        symbols = symbols[:MAX_SYMBOLS_PER_SCAN]

    for symbol in symbols:
        try:
            df1 = ex.fetch_ohlcv(symbol, timeframe='1m', limit=32)
            if df1 is None or df1.empty: continue
            price = float(df1['close'].iloc[-1])
            est_usd_vol_1m = float(df1['vol'].iloc[-1]) * price
            if est_usd_vol_1m < MIN_VOLUME_USDT_1M: continue

            diag = score_coin_pro(ex, symbol)
            if diag:
                diag['symbol'] = symbol
                results.append(diag)
        except: continue

    if not results: return

    strongs = [r for r in results if abs(r.get('score', 0.0)) >= strong_threshold]
    if strongs:
        best = sorted(strongs, key=lambda x: abs(x['score']), reverse=True)[0]
        s = best['score']
        side = "LONG" if s > 0 else "SHORT"
        send_telegram(f"🔥 *PRO STRONG SIGNAL* — {best['symbol']}\nScore: {s:.0f} ({side})\nPrice: {best.get('price')}\nCorrBTC: {best.get('corr_with_btc'):.2f}")
        return

    # Top 3 positives
    top_list = sorted([r for r in results if r['score'] > 0], key=lambda x: x['score'], reverse=True)[:top_n]
    if top_list:
        msg_lines = ["⚠️ *No ideal trade found.*", "📊 *Top coins (PRO score):*"]
        for idx, item in enumerate(top_list, start=1):
            msg_lines.append(f"{idx}. `{item['symbol']}` — Score: {item['score']:.0f}")
        send_telegram("\n".join(msg_lines))

# -------------------- MAIN LOOP --------------------
def main_loop():
    global bot_memory
    
    # 1. Load Persistence
    with data_lock:
        bot_memory = load_bot_data()
        known_set = set(bot_memory.get("known_symbols", []))

    # 2. Init Exchange
    try:
        ex = ExchangeWrapper(API_KEY, SECRET_KEY)
        ex.load_markets()
        current_markets = set(ex._cached_markets)
        if not known_set:
            known_set = current_markets
            with data_lock:
                bot_memory["known_symbols"] = list(known_set)
                save_bot_data()
        logger.info("Exchange initialized.")
        send_telegram(f"🤖 **Master Bot Pro Online**\nMonitoring {len(known_set)} coins.")
    except Exception as e:
        logger.error(f"Init failed: {e}")
        return

    # 3. Telegram Poller
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
                        if 'message' in u: process_message(u['message'].get('text'), u['message']['chat']['id'])
                        if 'callback_query' in u:
                            data = u['callback_query']['data']
                            if "CLOSE:" in data:
                                tid = data.split(":")[1]
                                with data_lock:
                                    if tid in bot_memory["trades"]:
                                        del bot_memory["trades"][tid]
                                        save_bot_data()
                                        send_telegram(f"🛑 Closed {tid}")
                except Exception: time.sleep(5)
        Thread(target=poller, daemon=True).start()

    last_news_check = 0
    last_full_scan = 0
    while True:
        try:
            now = time.time()
            
            # NEWS FILTER
            if now - last_news_check > 300:
                if news_risk_filter(): send_telegram("⚠️ **High Impact News Detected!**")
                last_news_check = now

            # NEW LISTING SCANNER
            try:
                markets = ex.load_markets(reload=True)
                new_coins = set(markets.keys()) - known_set
                if new_coins:
                    for coin in new_coins:
                        known_set.add(coin)
                        with data_lock:
                            bot_memory["known_symbols"] = list(known_set)
                            save_bot_data()
                        df = ex.fetch_ohlcv(coin)
                        if df is not None and not df.empty:
                            rsi = df['rsi'].iloc[-1] if 'rsi' in df.columns else None
                            price = df['close'].iloc[-1]
                            vwap = df['vwap'].iloc[-1] if 'vwap' in df.columns else None
                            regime = market_regime(df)
                            msg = f"🆕 **NEW: {coin}**\nPrice: {price}\nRSI: {rsi:.1f if rsi else 'NA'}\nRegime: {regime}"
                            if rsi and vwap:
                                if rsi > 70 and price < vwap: msg += "\n🚨 **SIGNAL: SHORT**"
                                elif rsi < 30: msg += "\n🛒 **SIGNAL: BUY DIP**"
                            send_telegram(msg)
            except: pass

            # FULL MARKET SCAN
            if now - last_full_scan > FULL_SCAN_INTERVAL:
                try: scan_all_markets_pro(ex, top_n=3, strong_threshold=STRONG_SCORE_THRESHOLD)
                except: pass
                last_full_scan = now

            # TRADE MONITOR
            to_remove = []
            with data_lock: active_ids = list(bot_memory["trades"].keys())
            for tid in active_ids:
                try:
                    with data_lock: t = bot_memory["trades"].get(tid)
                    if not t: continue
                    price = ex.fetch_ticker_price(t['symbol'])
                    if not price: continue
                    entry = float(t['entry_price'])
                    diff = (price - entry) if t['side'] == 'LONG' else (entry - price)
                    pnl = (diff / entry) * 100 * float(t['leverage'])
                    with data_lock: bot_memory["trades"][tid]['last_pnl'] = pnl
                    
                    tp_hit = (t['side'] == 'LONG' and price >= t['tp']) or (t['side'] == 'SHORT' and price <= t['tp'])
                    sl_hit = (t['side'] == 'LONG' and price <= t['sl']) or (t['side'] == 'SHORT' and price >= t['sl'])
                    if tp_hit or sl_hit:
                        send_telegram(f"{'✅ TP' if tp_hit else '🛑 SL'} for {t['symbol']}\nPrice: {price}\nPnL: {pnl:.2f}%")
                        to_remove.append(tid)
                        continue
                    adjust_trailing_stop(ex, tid, t)
                    check_pnl_distance_alert(tid, t, price)
                except: pass
            
            if to_remove:
                with data_lock:
                    for tid in to_remove:
                        if tid in bot_memory["trades"]: del bot_memory["trades"][tid]
                    save_bot_data()

            time.sleep(CHECK_INTERVAL)
        except Exception: time.sleep(10)

if __name__ == '__main__':
    Thread(target=run_http, daemon=True).start()
    main_loop()
