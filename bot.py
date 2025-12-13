#!/usr/bin/env python3
"""
🤖 MEXC MASTER BOT v3.3 - RENDER EDITION
Optimized for 24/7 hosting on Render.com
Mobile-friendly with SQLite database
"""

import os
import sys
import time
import json
import logging
import traceback
import sqlite3
import requests
import pandas as pd
import pandas_ta as ta
import numpy as np
import ccxt
from datetime import datetime, timedelta
from threading import Thread, Lock, Timer
from functools import wraps
from flask import Flask, request, jsonify, render_template_string
import atexit

# ==================== CONFIGURATION ====================
# Render will set these as environment variables
API_KEY = os.environ.get('MEXC_API_KEY', '').strip()
SECRET_KEY = os.environ.get('MEXC_SECRET_KEY', '').strip()
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN', '').strip()
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', '').strip()
TELEGRAM_WEBHOOK = os.environ.get('TELEGRAM_WEBHOOK', '1') == '1'

# Bot settings
CHECK_INTERVAL = int(os.environ.get('CHECK_INTERVAL', 60))  # 60 seconds
FULL_SCAN_INTERVAL = int(os.environ.get('FULL_SCAN_INTERVAL', 900))  # 15 minutes
DEFAULT_RISK_PCT = float(os.environ.get('DEFAULT_RISK_PCT', 0.5))

# Strategy parameters
MIN_VOLUME_USDT_1M = float(os.environ.get('MIN_VOLUME_USDT_1M', 100000.0))
STRONG_SCORE_THRESHOLD = float(os.environ.get('STRONG_SCORE_THRESHOLD', 35.0))
CORRELATION_AVOID_THRESHOLD = float(os.environ.get('CORRELATION_AVOID_THRESHOLD', 0.85))
FUNDING_BIAS_THRESHOLD = float(os.environ.get('FUNDING_BIAS_THRESHOLD', 0.001))
MIN_OI = float(os.environ.get('MIN_OI', 250000.0))
MAX_SYMBOLS_PER_SCAN = int(os.environ.get('MAX_SYMBOLS_PER_SCAN', 50))
ATR_TRAIL_MULT = float(os.environ.get('ATR_TRAIL_MULT', 1.5))
PNL_ALERT_PCT = float(os.environ.get('PNL_ALERT_PCT', 75.0))

# Database - Use in-memory on Render for persistence issues
IS_RENDER = 'RENDER' in os.environ or 'RENDER_EXTERNAL_URL' in os.environ
DB_FILE = ':memory:' if IS_RENDER else os.environ.get('DB_FILE', 'bot_data.db')

# Web server port
PORT = int(os.environ.get('PORT', 10000))

# ==================== LOGGING ====================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%H:%M:%S',
    handlers=[
        logging.StreamHandler(sys.stdout),
    ]
)
logger = logging.getLogger('mexc_bot_render')

# ==================== FLASK APP ====================
app = Flask(__name__)

# ==================== DATABASE ====================
class BotDatabase:
    """SQLite database for bot data"""
    
    def __init__(self, db_file=DB_FILE):
        self.db_file = db_file
        self.lock = Lock()
        self.init_database()
        atexit.register(self.close)
        
    def get_connection(self):
        """Get thread-safe connection"""
        conn = sqlite3.connect(self.db_file, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn
        
    def init_database(self):
        """Initialize tables"""
        with self.lock:
            conn = self.get_connection()
            try:
                cursor = conn.cursor()
                
                # Trades table
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS trades (
                        id TEXT PRIMARY KEY,
                        symbol TEXT NOT NULL,
                        side TEXT NOT NULL,
                        entry_price REAL NOT NULL,
                        tp REAL NOT NULL,
                        sl REAL NOT NULL,
                        amount REAL NOT NULL,
                        leverage REAL DEFAULT 10.0,
                        contracts REAL DEFAULT 0.0,
                        last_pnl REAL DEFAULT 0.0,
                        status TEXT DEFAULT 'RUNNING',
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                ''')
                
                # Known symbols
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS known_symbols (
                        symbol TEXT PRIMARY KEY,
                        first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        last_checked TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                ''')
                
                # Bot state
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS bot_state (
                        key TEXT PRIMARY KEY,
                        value TEXT,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                ''')
                
                conn.commit()
                logger.info(f"Database initialized: {self.db_file}")
                
            except Exception as e:
                logger.error(f"Database init error: {e}")
                raise
            finally:
                conn.close()
    
    def save_trade(self, trade_id, trade_data):
        """Save or update trade"""
        with self.lock:
            conn = self.get_connection()
            try:
                cursor = conn.cursor()
                cursor.execute('''
                    INSERT OR REPLACE INTO trades 
                    (id, symbol, side, entry_price, tp, sl, amount, leverage, contracts, last_pnl, status)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    trade_id,
                    trade_data['symbol'],
                    trade_data['side'],
                    trade_data['entry_price'],
                    trade_data['tp'],
                    trade_data['sl'],
                    trade_data['amount'],
                    trade_data.get('leverage', 10.0),
                    trade_data.get('contracts', 0.0),
                    trade_data.get('last_pnl', 0.0),
                    trade_data.get('status', 'RUNNING')
                ))
                conn.commit()
                return True
            except Exception as e:
                logger.error(f"Save trade error: {e}")
                return False
            finally:
                conn.close()
    
    def get_trade(self, trade_id):
        """Get single trade"""
        with self.lock:
            conn = self.get_connection()
            try:
                cursor = conn.cursor()
                cursor.execute('SELECT * FROM trades WHERE id = ?', (trade_id,))
                row = cursor.fetchone()
                return dict(row) if row else None
            except Exception as e:
                logger.error(f"Get trade error: {e}")
                return None
            finally:
                conn.close()
    
    def get_all_trades(self, status='RUNNING'):
        """Get all trades"""
        with self.lock:
            conn = self.get_connection()
            try:
                cursor = conn.cursor()
                if status == 'ALL':
                    cursor.execute('SELECT * FROM trades ORDER BY created_at DESC')
                else:
                    cursor.execute('SELECT * FROM trades WHERE status = ? ORDER BY created_at DESC', (status,))
                
                return [dict(row) for row in cursor.fetchall()]
            except Exception as e:
                logger.error(f"Get trades error: {e}")
                return []
            finally:
                conn.close()
    
    def delete_trade(self, trade_id):
        """Delete trade"""
        with self.lock:
            conn = self.get_connection()
            try:
                cursor = conn.cursor()
                cursor.execute('DELETE FROM trades WHERE id = ?', (trade_id,))
                conn.commit()
                return cursor.rowcount > 0
            except Exception as e:
                logger.error(f"Delete trade error: {e}")
                return False
            finally:
                conn.close()
    
    def update_trade_pnl(self, trade_id, pnl):
        """Update trade PnL"""
        with self.lock:
            conn = self.get_connection()
            try:
                cursor = conn.cursor()
                cursor.execute('''
                    UPDATE trades 
                    SET last_pnl = ?, updated_at = CURRENT_TIMESTAMP 
                    WHERE id = ?
                ''', (pnl, trade_id))
                conn.commit()
                return True
            except Exception as e:
                logger.error(f"Update PnL error: {e}")
                return False
            finally:
                conn.close()
    
    def update_trade_sl(self, trade_id, new_sl):
        """Update stop loss"""
        with self.lock:
            conn = self.get_connection()
            try:
                cursor = conn.cursor()
                cursor.execute('''
                    UPDATE trades 
                    SET sl = ?, updated_at = CURRENT_TIMESTAMP 
                    WHERE id = ?
                ''', (new_sl, trade_id))
                conn.commit()
                return True
            except Exception as e:
                logger.error(f"Update SL error: {e}")
                return False
            finally:
                conn.close()
    
    def update_trade_status(self, trade_id, status):
        """Update trade status"""
        with self.lock:
            conn = self.get_connection()
            try:
                cursor = conn.cursor()
                cursor.execute('''
                    UPDATE trades 
                    SET status = ?, updated_at = CURRENT_TIMESTAMP 
                    WHERE id = ?
                ''', (status, trade_id))
                conn.commit()
                return True
            except Exception as e:
                logger.error(f"Update status error: {e}")
                return False
            finally:
                conn.close()
    
    def add_known_symbol(self, symbol):
        """Add known symbol"""
        with self.lock:
            conn = self.get_connection()
            try:
                cursor = conn.cursor()
                cursor.execute('''
                    INSERT OR REPLACE INTO known_symbols (symbol, last_checked)
                    VALUES (?, CURRENT_TIMESTAMP)
                ''', (symbol,))
                conn.commit()
                return True
            except Exception as e:
                logger.error(f"Add symbol error: {e}")
                return False
            finally:
                conn.close()
    
    def get_known_symbols(self):
        """Get all known symbols"""
        with self.lock:
            conn = self.get_connection()
            try:
                cursor = conn.cursor()
                cursor.execute('SELECT symbol FROM known_symbols ORDER BY symbol')
                return [row[0] for row in cursor.fetchall()]
            except Exception as e:
                logger.error(f"Get symbols error: {e}")
                return []
            finally:
                conn.close()
    
    def cleanup_old_data(self, days_old=3):
        """Clean old completed trades"""
        with self.lock:
            conn = self.get_connection()
            try:
                cursor = conn.cursor()
                cursor.execute('''
                    DELETE FROM trades 
                    WHERE status IN ('CLOSED', 'STOPPED') 
                    AND created_at < datetime('now', ?)
                ''', (f'-{days_old} days',))
                deleted = cursor.rowcount
                conn.commit()
                if deleted > 0:
                    logger.info(f"Cleaned {deleted} old trades")
                return deleted
            except Exception as e:
                logger.error(f"Cleanup error: {e}")
                return 0
            finally:
                conn.close()
    
    def close(self):
        """Close database"""
        pass

# Initialize database
db = BotDatabase()

# ==================== TELEGRAM ====================
class TelegramManager:
    """Handle Telegram communications"""
    
    def __init__(self):
        self.token = TELEGRAM_TOKEN
        self.chat_id = TELEGRAM_CHAT_ID
        self.session = requests.Session()
        self.session.timeout = (5, 10)
    
    def is_configured(self):
        return bool(self.token and self.chat_id)
    
    def send_message(self, message, buttons=None):
        if not self.is_configured():
            logger.info(f"Telegram: {message[:100]}...")
            return False
            
        try:
            url = f"https://api.telegram.org/bot{self.token}/sendMessage"
            payload = {
                "chat_id": self.chat_id,
                "text": message,
                "parse_mode": "Markdown",
                "disable_notification": len(message) < 50
            }
            
            if buttons:
                payload['reply_markup'] = {"inline_keyboard": buttons}
                
            response = self.session.post(url, json=payload, timeout=10)
            response.raise_for_status()
            return True
            
        except Exception as e:
            logger.warning(f"Telegram send failed: {e}")
            return False
    
    def get_updates(self, offset=None):
        if not self.token:
            return []
            
        try:
            url = f"https://api.telegram.org/bot{self.token}/getUpdates"
            params = {'timeout': 20, 'offset': offset}
            response = self.session.get(url, params=params, timeout=25)
            response.raise_for_status()
            data = response.json()
            return data.get('result', [])
        except Exception as e:
            logger.warning(f"Telegram updates failed: {e}")
            return []

telegram = TelegramManager()

# ==================== EXCHANGE ====================
class ExchangeWrapper:
    """Wrapper for MEXC exchange"""
    
    def __init__(self, api_key, secret):
        self.api_key = api_key
        self.secret = secret
        self.exchange = None
        self.markets = {}
        self.init_exchange()
    
    def init_exchange(self):
        """Initialize exchange connection"""
        try:
            self.exchange = ccxt.mexc({
                'apiKey': self.api_key,
                'secret': self.secret,
                'enableRateLimit': True,
                'options': {'defaultType': 'swap'},
                'timeout': 10000,
            })
            logger.info("Exchange initialized")
        except Exception as e:
            logger.error(f"Exchange init failed: {e}")
            raise
    
    def load_markets(self, reload=False):
        """Load markets"""
        try:
            self.markets = self.exchange.load_markets(reload=reload)
            return self.markets
        except Exception as e:
            logger.error(f"Load markets failed: {e}")
            return {}
    
    def fetch_ohlcv(self, symbol, timeframe='1m', limit=100):
        """Fetch OHLCV data"""
        try:
            bars = self.exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
            if not bars:
                return None
                
            df = pd.DataFrame(bars, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            
            # Calculate indicators
            try:
                df['rsi'] = ta.rsi(df['close'], length=14)
            except:
                df['rsi'] = 50.0
                
            try:
                df['vwap'] = ta.vwap(df['high'], df['low'], df['close'], df['volume'])
            except:
                df['vwap'] = df['close'].rolling(20).mean()
                
            try:
                df['atr'] = ta.atr(df['high'], df['low'], df['close'], length=14)
            except:
                df['atr'] = (df['high'] - df['low']).rolling(14).mean()
                
            return df
        except Exception as e:
            logger.warning(f"OHLCV fetch failed for {symbol}: {e}")
            return None
    
    def fetch_ticker_price(self, symbol):
        """Get current price"""
        try:
            ticker = self.exchange.fetch_ticker(symbol)
            return float(ticker['last'])
        except Exception as e:
            logger.warning(f"Price fetch failed for {symbol}: {e}")
            return None
    
    def fetch_funding_rate(self, symbol):
        """Get funding rate"""
        try:
            if hasattr(self.exchange, 'fetch_funding_rate'):
                fr = self.exchange.fetch_funding_rate(symbol)
                return float(fr.get('fundingRate', 0))
            return 0.0
        except Exception:
            return 0.0
    
    def fetch_open_interest(self, symbol):
        """Get open interest"""
        try:
            if hasattr(self.exchange, 'fetch_open_interest'):
                oi = self.exchange.fetch_open_interest(symbol)
                if isinstance(oi, dict):
                    return float(oi.get('openInterest', 0))
                return float(oi)
            return 0.0
        except Exception:
            return 0.0

# ==================== TELEGRAM COMMANDS ====================
def process_message(text, chat_id):
    """Process Telegram commands"""
    if not text:
        return
        
    parts = text.strip().split()
    if not parts:
        return
        
    cmd = parts[0].lower()
    
    # Check authorization
    if TELEGRAM_CHAT_ID and str(chat_id) != str(TELEGRAM_CHAT_ID):
        telegram.send_message(f"⚠️ Unauthorized access from chat ID: {chat_id}")
        return
    
    # HELP command
    if cmd == '/help':
        help_text = """
🤖 *MEXC Master Bot Commands:*
/help - Show this message
/track SYM SIDE ENTRY TP SL AMT [LEV] - Track new trade
/list - List active trades
/stats - Show bot statistics
/remove ID - Remove a trade
/reset - Reset all trades (⚠️ Warning)
/start - Start the bot
"""
        telegram.send_message(help_text)
        return
    
    # START command
    if cmd == '/start':
        telegram.send_message("✅ *Bot is running!*\nSend /help for commands.")
        return
    
    # TRACK command
    if cmd == '/track':
        if len(parts) < 7:
            telegram.send_message("⚠️ *Usage:* `/track SYMBOL SIDE ENTRY TP SL AMT [LEV=10]`\nExample: `/track BTC/USDT LONG 50000 52000 48000 100 10`")
            return
            
        try:
            symbol = parts[1].upper()
            side = parts[2].upper()
            
            if side not in ['LONG', 'SHORT']:
                telegram.send_message("❌ Side must be LONG or SHORT")
                return
                
            entry = float(parts[3])
            tp = float(parts[4])
            sl = float(parts[5])
            amount = float(parts[6])
            leverage = float(parts[7]) if len(parts) >= 8 else 10.0
            
            # Validation
            if entry <= 0 or tp <= 0 or sl <= 0 or amount <= 0:
                telegram.send_message("❌ All numbers must be positive")
                return
                
            if leverage <= 0 or leverage > 100:
                telegram.send_message("❌ Leverage must be 1-100")
                return
                
            # TP/SL logic
            if side == 'LONG' and (tp <= entry or sl >= entry):
                telegram.send_message("❌ For LONG: TP > Entry > SL")
                return
            elif side == 'SHORT' and (tp >= entry or sl <= entry):
                telegram.send_message("❌ For SHORT: TP < Entry < SL")
                return
            
            # Calculate contracts
            contracts = (amount * leverage) / entry
            
            # Create trade ID
            trade_id = f"T{int(time.time()) % 1000000:06d}"
            
            # Save trade
            trade_data = {
                'symbol': symbol,
                'side': side,
                'entry_price': entry,
                'tp': tp,
                'sl': sl,
                'amount': amount,
                'leverage': leverage,
                'contracts': contracts,
                'last_pnl': 0.0,
                'status': 'RUNNING'
            }
            
            if db.save_trade(trade_id, trade_data):
                buttons = [[{"text": "❌ Close", "callback_data": f"CLOSE:{trade_id}"}]]
                msg = f"""✅ *Tracking Trade* `{trade_id}`
Symbol: {symbol}
Side: {side}
Entry: ${entry:,.2f}
TP: ${tp:,.2f} | SL: ${sl:,.2f}
Amount: ${amount:,.2f} (x{leverage})"""
                telegram.send_message(msg, buttons=buttons)
            else:
                telegram.send_message("❌ Failed to save trade")
                
        except ValueError:
            telegram.send_message("❌ Invalid number format")
        except Exception as e:
            telegram.send_message(f"❌ Error: {str(e)}")
        return
    
    # LIST command
    elif cmd == '/list':
        trades = db.get_all_trades('RUNNING')
        if not trades:
            telegram.send_message("📭 No active trades.")
            return
            
        msg = "📋 *Active Trades:*\n"
        for trade in trades[:10]:
            pnl = trade.get('last_pnl', 0)
            status = "🟢" if pnl >= 0 else "🔴"
            msg += f"{status} `{trade['id']}`: {trade['symbol']} {trade['side']} ({pnl:.2f}%)\n"
            
        if len(trades) > 10:
            msg += f"\n... and {len(trades) - 10} more trades"
            
        telegram.send_message(msg)
    
    # STATS command
    elif cmd == '/stats':
        trades = db.get_all_trades('RUNNING')
        total_trades = len(trades)
        total_pnl = sum(t.get('last_pnl', 0) for t in trades)
        avg_pnl = total_pnl / max(total_trades, 1)
        known_symbols = db.get_known_symbols()
        
        msg = f"""📊 *Bot Statistics:*
Active Trades: {total_trades}
Total P&L: {total_pnl:.2f}%
Avg P&L: {avg_pnl:.2f}%
Known Symbols: {len(known_symbols)}
Status: 🟢 Online
Host: {'Render' if IS_RENDER else 'Local'}"""
        telegram.send_message(msg)
    
    # REMOVE command
    elif cmd == '/remove' and len(parts) >= 2:
        trade_id = parts[1]
        if db.delete_trade(trade_id):
            telegram.send_message(f"🗑️ Removed trade `{trade_id}`")
        else:
            telegram.send_message(f"❌ Trade `{trade_id}` not found")
    
    # RESET command
    elif cmd == '/reset':
        if len(parts) >= 2 and parts[1] == 'confirm':
            trades = db.get_all_trades('ALL')
            for trade in trades:
                db.delete_trade(trade['id'])
            telegram.send_message("✅ All trades reset")
        else:
            telegram.send_message("⚠️ *WARNING:* This deletes ALL trades!\nSend `/reset confirm` to proceed")

# ==================== DASHBOARD ====================
DASHBOARD_HTML = """
<!DOCTYPE html>
<html>
<head>
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>🤖 MEXC Master Pro</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            background: #0f172a;
            color: #e2e8f0;
            font-family: -apple-system, system-ui, sans-serif;
            padding: 16px;
            max-width: 800px;
            margin: 0 auto;
        }
        .header {
            background: linear-gradient(135deg, #1e40af, #3b82f6);
            padding: 20px;
            border-radius: 16px;
            margin-bottom: 20px;
            text-align: center;
            box-shadow: 0 4px 6px rgba(0,0,0,0.3);
        }
        .card {
            background: #1e293b;
            border: 1px solid #334155;
            border-radius: 12px;
            padding: 20px;
            margin-bottom: 16px;
        }
        .trade-row {
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 12px 0;
            border-bottom: 1px solid #334155;
        }
        .trade-row:last-child { border-bottom: none; }
        .symbol { font-weight: bold; font-size: 16px; }
        .side-long { color: #10b981; }
        .side-short { color: #ef4444; }
        .pnl-positive { color: #10b981; }
        .pnl-negative { color: #ef4444; }
        .stats-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
            gap: 12px;
            margin-top: 12px;
        }
        .stat-box {
            background: #0f172a;
            padding: 12px;
            border-radius: 8px;
            text-align: center;
        }
        .stat-value { font-size: 24px; font-weight: bold; }
        .last-update { text-align: center; margin-top: 20px; color: #94a3b8; font-size: 14px; }
        .status-online { color: #10b981; }
        .status-offline { color: #ef4444; }
    </style>
</head>
<body>
    <div class="header">
        <h1>🤖 MEXC Master Pro</h1>
        <p>24/7 Trading Bot on Render</p>
    </div>
    
    <div class="card">
        <h2>📊 Statistics</h2>
        <div class="stats-grid">
            <div class="stat-box">
                <div>Active Trades</div>
                <div class="stat-value">{{ stats.active_trades }}</div>
            </div>
            <div class="stat-box">
                <div>Total P&L</div>
                <div class="stat-value {{ 'pnl-positive' if stats.total_pnl >= 0 else 'pnl-negative' }}">
                    {{ stats.total_pnl }}%
                </div>
            </div>
            <div class="stat-box">
                <div>Avg P&L</div>
                <div class="stat-value {{ 'pnl-positive' if stats.avg_pnl >= 0 else 'pnl-negative' }}">
                    {{ stats.avg_pnl }}%
                </div>
            </div>
            <div class="stat-box">
                <div>Known Coins</div>
                <div class="stat-value">{{ stats.known_symbols }}</div>
            </div>
        </div>
        <p style="margin-top: 10px;">Status: <span class="status-online">🟢 Online</span> | Host: {{ stats.host }}</p>
    </div>
    
    {% if trades %}
    <div class="card">
        <h2>📈 Active Trades ({{ trades|length }})</h2>
        {% for trade in trades %}
        <div class="trade-row">
            <div>
                <div class="symbol">{{ trade.symbol }}</div>
                <div class="side-{{ trade.side|lower }}">{{ trade.side }}</div>
            </div>
            <div>
                <div>Entry: ${{ "%.2f"|format(trade.entry_price) }}</div>
                <div class="pnl-{{ 'positive' if trade.last_pnl >= 0 else 'negative' }}">
                    P&L: {{ "%.2f"|format(trade.last_pnl) }}%
                </div>
            </div>
        </div>
        {% endfor %}
    </div>
    {% else %}
    <div class="card">
        <p style="text-align: center; color: #94a3b8;">No active trades. Use Telegram to add trades.</p>
    </div>
    {% endif %}
    
    <div class="last-update">
        Last updated: {{ current_time }} UTC<br>
        Bot Uptime: {{ stats.uptime }}
    </div>
    
    <script>
        // Auto-refresh every 30 seconds
        setTimeout(() => location.reload(), 30000);
        
        // Status indicator
        const statusEl = document.createElement('div');
        statusEl.style.cssText = 'position: fixed; top: 10px; right: 10px; width: 10px; height: 10px; background: #10b981; border-radius: 50%; animation: pulse 2s infinite;';
        document.body.appendChild(statusEl);
        
        const style = document.createElement('style');
        style.textContent = `
            @keyframes pulse {
                0% { opacity: 1; }
                50% { opacity: 0.5; }
                100% { opacity: 1; }
            }
        `;
        document.head.appendChild(style);
    </script>
</body>
</html>
"""

@app.route('/')
def dashboard():
    """Dashboard homepage"""
    trades = db.get_all_trades('RUNNING')
    known_symbols = db.get_known_symbols()
    
    # Calculate statistics
    total_pnl = sum(t.get('last_pnl', 0) for t in trades)
    avg_pnl = total_pnl / max(len(trades), 1)
    
    # Calculate uptime
    start_time = getattr(dashboard, '_start_time', datetime.now())
    uptime = str(datetime.now() - start_time).split('.')[0]
    
    stats = {
        'active_trades': len(trades),
        'total_pnl': round(total_pnl, 2),
        'avg_pnl': round(avg_pnl, 2),
        'known_symbols': len(known_symbols),
        'host': 'Render' if IS_RENDER else 'Local',
        'uptime': uptime
    }
    
    current_time = datetime.utcnow().strftime('%H:%M:%S')
    
    return render_template_string(
        DASHBOARD_HTML,
        trades=trades,
        stats=stats,
        current_time=current_time
    )

@app.route('/health')
def health_check():
    """Health check endpoint"""
    return jsonify({
        'status': 'healthy',
        'timestamp': datetime.utcnow().isoformat(),
        'trades': len(db.get_all_trades('RUNNING')),
        'symbols': len(db.get_known_symbols()),
        'host': 'Render' if IS_RENDER else 'Local',
        'version': '3.3'
    })

@app.route('/telegram_webhook', methods=['POST'])
def telegram_webhook():
    """Handle Telegram webhook"""
    if not TELEGRAM_WEBHOOK:
        return jsonify({'ok': True})
        
    try:
        data = request.json
        if not data:
            return jsonify({'ok': False}), 400
            
        # Handle callback queries
        if 'callback_query' in data:
            callback = data['callback_query']
            if callback.get('data', '').startswith('CLOSE:'):
                trade_id = callback['data'].split(':')[1]
                if db.delete_trade(trade_id):
                    telegram.send_message(f"✅ Closed trade {trade_id}")
                return jsonify({'ok': True})
                
        # Handle messages
        elif 'message' in data:
            message = data['message']
            text = message.get('text', '')
            chat_id = message.get('chat', {}).get('id')
            process_message(text, chat_id)
            
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        
    return jsonify({'ok': True})

# ==================== TRADE MANAGEMENT ====================
def adjust_trailing_stop(ex, trade_id, trade):
    """Adjust trailing stop loss"""
    try:
        symbol = trade['symbol']
        price = ex.fetch_ticker_price(symbol)
        if not price:
            return
            
        df = ex.fetch_ohlcv(symbol, timeframe='1m', limit=50)
        if not df or 'atr' not in df.columns:
            return
            
        atr = float(df['atr'].iloc[-1])
        entry = float(trade['entry_price'])
        current_sl = float(trade['sl'])
        side = trade['side']
        
        # Calculate PnL
        if side == 'LONG':
            pnl_pct = (price - entry) / entry * 100.0
        else:
            pnl_pct = (entry - price) / entry * 100.0
        
        # Adjust stop loss
        new_sl = current_sl
        
        if pnl_pct >= 10:
            if side == 'LONG':
                new_sl = max(current_sl, entry)
            else:
                new_sl = min(current_sl, entry)
                
        if pnl_pct >= 20:
            buffer = atr * ATR_TRAIL_MULT
            if side == 'LONG':
                new_sl = max(new_sl, price - buffer)
            else:
                new_sl = min(new_sl, price + buffer)
        
        # Update if improved
        sl_improved = (side == 'LONG' and new_sl > current_sl) or (side == 'SHORT' and new_sl < current_sl)
        
        if sl_improved and abs(new_sl - current_sl) / current_sl > 0.001:
            db.update_trade_sl(trade_id, new_sl)
            telegram.send_message(f"🔒 SL moved for `{trade_id}`: {current_sl:.4f} → {new_sl:.4f}")
            
    except Exception as e:
        logger.debug(f"Trailing stop error: {e}")

def check_trade_exits(ex, trade_id, trade):
    """Check if trade should exit"""
    try:
        symbol = trade['symbol']
        price = ex.fetch_ticker_price(symbol)
        if not price:
            return False
            
        side = trade['side']
        tp = float(trade['tp'])
        sl = float(trade['sl'])
        
        # Check TP/SL
        hit_tp = (side == 'LONG' and price >= tp) or (side == 'SHORT' and price <= tp)
        hit_sl = (side == 'LONG' and price <= sl) or (side == 'SHORT' and price >= sl)
        
        if hit_tp or hit_sl:
            entry = float(trade['entry_price'])
            leverage = float(trade.get('leverage', 1))
            
            if side == 'LONG':
                pnl_pct = (price - entry) / entry * 100.0 * leverage
            else:
                pnl_pct = (entry - price) / entry * 100.0 * leverage
            
            reason = "✅ TP HIT" if hit_tp else "🛑 SL HIT"
            telegram.send_message(f"""{reason} - {trade['symbol']}
Price: ${price:.2f}
PnL: {pnl_pct:.2f}%
Trade ID: `{trade_id}`""")
            
            db.update_trade_status(trade_id, 'CLOSED')
            return True
            
    except Exception as e:
        logger.warning(f"Exit check error: {e}")
        
    return False

# ==================== STRATEGY MODULES ====================
def compute_vol_zscore(df, window=30):
    """Compute volume z-score"""
    try:
        if df is None or 'volume' not in df.columns or len(df) < window:
            return 0.0
            
        vols = df['volume'].pct_change().abs().dropna()
        if len(vols) < window:
            return 0.0
            
        recent = vols.tail(window)
        z = (recent.iloc[-1] - recent.mean()) / (recent.std(ddof=1) + 1e-9)
        return float(z)
        
    except Exception:
        return 0.0

def compute_correlation_with_btc(ex, symbol, timeframe='1m', lookback=120):
    """Compute correlation with BTC"""
    try:
        if symbol == 'BTC/USDT':
            return 1.0
            
        df_sym = ex.fetch_ohlcv(symbol, timeframe=timeframe, limit=lookback)
        df_btc = ex.fetch_ohlcv('BTC/USDT', timeframe=timeframe, limit=lookback)
        
        if df_sym is None or df_btc is None:
            return 0.0
            
        ret_sym = df_sym['close'].pct_change().dropna()
        ret_btc = df_btc['close'].pct_change().dropna()
        
        n = min(len(ret_sym), len(ret_btc))
        if n < 10:
            return 0.0
            
        corr = np.corrcoef(ret_sym.tail(n), ret_btc.tail(n))[0,1]
        return float(corr) if not np.isnan(corr) else 0.0
        
    except Exception:
        return 0.0

def score_coin_pro(ex, symbol):
    """Score a coin for trading"""
    try:
        df1 = ex.fetch_ohlcv(symbol, timeframe='1m', limit=100)
        if df1 is None or df1.empty or len(df1) < 30:
            return {'score': 0.0, 'reason': 'insufficient_data'}
            
        df5 = ex.fetch_ohlcv(symbol, timeframe='5m', limit=100)
        df15 = ex.fetch_ohlcv(symbol, timeframe='15m', limit=100)
        
        price = float(df1['close'].iloc[-1])
        
        # Get indicators
        rsi1 = float(df1['rsi'].iloc[-1]) if 'rsi' in df1.columns else 50.0
        rsi5 = float(df5['rsi'].iloc[-1]) if df5 is not None and 'rsi' in df5.columns else 50.0
        rsi15 = float(df15['rsi'].iloc[-1]) if df15 is not None and 'rsi' in df15.columns else 50.0
        
        vwap1 = float(df1['vwap'].iloc[-1]) if 'vwap' in df1.columns else price
        vwap_dist_pct = ((price - vwap1) / vwap1) * 100.0 if vwap1 != 0 else 0.0
        
        vol_z = compute_vol_zscore(df1, window=30)
        funding = ex.fetch_funding_rate(symbol)
        oi = ex.fetch_open_interest(symbol)
        
        # Calculate USD volume
        last_vol = float(df1['volume'].iloc[-1])
        est_usd_vol_1m = last_vol * price
        
        # Volume volatility
        try:
            vol_pct = df1['close'].pct_change().dropna().tail(30).std() * 100.0
        except:
            vol_pct = 0.0
        
        # Start scoring
        score = 0.0
        
        # 1) MTF RSI confirmation
        mtf_long = 0
        mtf_short = 0
        
        if rsi1 < 34: mtf_long += 1
        if rsi1 > 66: mtf_short += 1
        if rsi5 < 40: mtf_long += 1
        if rsi5 > 60: mtf_short += 1
        if rsi15 < 48: mtf_long += 1
        if rsi15 > 52: mtf_short += 1
        
        score += (mtf_long - mtf_short) * 8.0
        
        # 2) VWAP distance
        if vwap_dist_pct < -2.5: score += 18
        elif vwap_dist_pct < -1.0: score += 8
        elif vwap_dist_pct > 2.0: score -= 8
        
        # 3) Volume spike
        if vol_z > 1.5: score += 14
        elif vol_z > 0.8: score += 6
        
        # 4) Volatility
        if 0 < vol_pct < 3.5: score += 6
        elif 3.5 <= vol_pct < 8: score += 10
        elif vol_pct >= 8: score -= 8
        
        # 5) Funding bias
        if funding is not None:
            if funding > FUNDING_BIAS_THRESHOLD: score -= 8
            elif funding < -FUNDING_BIAS_THRESHOLD: score += 8
        
        # 6) Open interest
        if oi is not None:
            if oi > MIN_OI and vol_z > 1.0: score += 10
            elif oi < MIN_OI: score -= 6
        
        # 7) Breakout detection
        try:
            last20_high = df1['high'].tail(20).max()
            last20_low = df1['low'].tail(20).min()
            if price > last20_high and vol_z > 0.8: score += 18
            if price < last20_low and vol_z > 0.8: score -= 18
        except:
            pass
        
        # 8) Liquidity filter
        if est_usd_vol_1m < MIN_VOLUME_USDT_1M:
            score -= 50
        
        # 9) Correlation avoidance
        corr = compute_correlation_with_btc(ex, symbol)
        if corr >= CORRELATION_AVOID_THRESHOLD:
            score -= 6
        
        # Normalize score
        score = max(min(score, 100.0), -100.0)
        
        return {
            'score': float(score),
            'price': price,
            'symbol': symbol,
            'rsi1': rsi1,
            'rsi5': rsi5,
            'rsi15': rsi15,
            'vwap_dist_pct': vwap_dist_pct,
            'vol_z': vol_z,
            'vol_pct': vol_pct,
            'funding': funding,
            'oi': oi,
            'corr_with_btc': corr,
            'est_usd_vol_1m': est_usd_vol_1m
        }
        
    except Exception as e:
        logger.warning(f"Score coin error for {symbol}: {e}")
        return {'score': 0.0, 'reason': str(e)}

def scan_all_markets_pro(ex, top_n=3, strong_threshold=STRONG_SCORE_THRESHOLD):
    """Scan all markets for opportunities"""
    logger.info("Starting market scan...")
    
    try:
        markets = ex.load_markets()
        symbols = [s for s in markets.keys() if s.endswith('/USDT')]
        
        if MAX_SYMBOLS_PER_SCAN and len(symbols) > MAX_SYMBOLS_PER_SCAN:
            symbols = symbols[:MAX_SYMBOLS_PER_SCAN]
        
        results = []
        
        for symbol in symbols:
            try:
                # Quick volume check
                df1 = ex.fetch_ohlcv(symbol, timeframe='1m', limit=10)
                if df1 is None or df1.empty:
                    continue
                    
                price = float(df1['close'].iloc[-1])
                volume = float(df1['volume'].iloc[-1])
                usd_volume = price * volume
                
                if usd_volume < MIN_VOLUME_USDT_1M:
                    continue
                
                # Score the coin
                diag = score_coin_pro(ex, symbol)
                if diag and abs(diag.get('score', 0)) > 10:
                    diag['symbol'] = symbol
                    results.append(diag)
                    
            except Exception as e:
                logger.debug(f"Scan error for {symbol}: {e}")
                continue
        
        if not results:
            logger.info("No results from scan")
            return
        
        # Find strong signals
        strongs = [r for r in results if abs(r.get('score', 0)) >= strong_threshold]
        
        if strongs:
            best = max(strongs, key=lambda x: abs(x['score']))
            side = "LONG" if best['score'] > 0 else "SHORT"
            
            telegram.send_message(f"""🔥 *STRONG SIGNAL* - {best['symbol']}
Score: {best['score']:.0f} ({side})
Price: ${best['price']:,.2f}
RSI (1m/5m/15m): {best.get('rsi1', 0):.1f}/{best.get('rsi5', 0):.1f}/{best.get('rsi15', 0):.1f}
Volume Z: {best.get('vol_z', 0):.2f}
Corr BTC: {best.get('corr_with_btc', 0):.2f}""")
            return
        
        # Top positive signals
        positives = [r for r in results if r['score'] > 0]
        if positives:
            top = sorted(positives, key=lambda x: x['score'], reverse=True)[:top_n]
            
            msg = ["⚠️ *No strong signals found*", "📊 *Top opportunities:*"]
            for i, item in enumerate(top, 1):
                msg.append(f"{i}. `{item['symbol']}` - Score: {item['score']:.0f}")
            
            telegram.send_message("\n".join(msg))
            
    except Exception as e:
        logger.error(f"Market scan error: {e}")

# ==================== MAIN BOT LOOP ====================
def main_loop():
    """Main bot loop"""
    logger.info("🤖 Starting MEXC Master Bot v3.3")
    
    # Check credentials
    if not API_KEY or not SECRET_KEY:
        logger.error("❌ Missing API credentials")
        telegram.send_message("❌ Bot startup failed: Missing API keys")
        return
    
    # Initialize exchange
    try:
        ex = ExchangeWrapper(API_KEY, SECRET_KEY)
        markets = ex.load_markets()
        logger.info(f"✅ Connected to MEXC: {len(markets)} markets")
        
        # Initialize known symbols
        usdt_pairs = [s for s in markets.keys() if s.endswith('/USDT')]
        for symbol in usdt_pairs[:100]:
            db.add_known_symbol(symbol)
        
        telegram.send_message(f"""✅ *Bot Started Successfully*
Markets: {len(markets)}
USDT Pairs: {len(usdt_pairs)}
Check Interval: {CHECK_INTERVAL}s
Host: {'Render' if IS_RENDER else 'Local'}""")
        
    except Exception as e:
        logger.error(f"❌ Bot startup failed: {e}")
        telegram.send_message(f"❌ Bot startup failed: {str(e)}")
        return
    
    # Telegram polling (if not using webhook)
    if not TELEGRAM_WEBHOOK and telegram.is_configured():
        def poll_telegram():
            last_update_id = None
            while True:
                try:
                    updates = telegram.get_updates(offset=last_update_id)
                    for update in updates:
                        last_update_id = update['update_id'] + 1
                        
                        if 'message' in update:
                            msg = update['message']
                            text = msg.get('text', '')
                            chat_id = msg.get('chat', {}).get('id')
                            process_message(text, chat_id)
                            
                        elif 'callback_query' in update:
                            callback = update['callback_query']
                            data = callback.get('data', '')
                            if data.startswith('CLOSE:'):
                                trade_id = data.split(':')[1]
                                if db.delete_trade(trade_id):
                                    telegram.send_message(f"✅ Closed trade {trade_id}")
                                    
                except Exception as e:
                    logger.warning(f"Telegram poll error: {e}")
                    
                time.sleep(2)
                
        Thread(target=poll_telegram, daemon=True).start()
    
    # Keep-alive for Render
    def keep_alive_ping():
        """Ping health endpoint to keep Render awake"""
        try:
            if IS_RENDER:
                service_url = os.environ.get('RENDER_EXTERNAL_URL', '')
                if service_url:
                    requests.get(f"{service_url}/health", timeout=5)
        except:
            pass
        Timer(300, keep_alive_ping).start()  # Every 5 minutes
    
    if IS_RENDER:
        keep_alive_ping()
    
    # Main monitoring loop
    last_scan = 0
    last_cleanup = time.time()
    scan_count = 0
    
    logger.info("🚀 Bot is now running")
    
    while True:
        try:
            current_time = time.time()
            
            # 1. Monitor active trades
            active_trades = db.get_all_trades('RUNNING')
            
            for trade in active_trades:
                trade_id = trade['id']
                
                # Check for exit conditions
                if check_trade_exits(ex, trade_id, trade):
                    continue
                    
                # Update PnL
                try:
                    symbol = trade['symbol']
                    price = ex.fetch_ticker_price(symbol)
                    if price:
                        entry = float(trade['entry_price'])
                        side = trade['side']
                        leverage = float(trade.get('leverage', 1))
                        
                        if side == 'LONG':
                            pnl = ((price - entry) / entry) * 100 * leverage
                        else:
                            pnl = ((entry - price) / entry) * 100 * leverage
                            
                        db.update_trade_pnl(trade_id, pnl)
                        
                        # Adjust trailing stop
                        adjust_trailing_stop(ex, trade_id, trade)
                except Exception as e:
                    logger.debug(f"Trade update error {trade_id}: {e}")
            
            # 2. Periodic market scan
            if current_time - last_scan > FULL_SCAN_INTERVAL:
                try:
                    scan_all_markets_pro(ex, top_n=3)
                    scan_count += 1
                    logger.info(f"Market scan #{scan_count} completed")
                except Exception as e:
                    logger.error(f"Scan error: {e}")
                last_scan = current_time
            
            # 3. Cleanup old data (once per hour)
            if current_time - last_cleanup > 3600:
                db.cleanup_old_data(days_old=3)
                last_cleanup = current_time
            
            # 4. Sleep
            time.sleep(CHECK_INTERVAL)
            
        except KeyboardInterrupt:
            logger.info("🛑 Bot stopped by user")
            telegram.send_message("🛑 Bot stopped by user")
            break
        except Exception as e:
            logger.error(f"Main loop error: {e}")
            time.sleep(30)

# ==================== START BOT ====================
if __name__ == '__main__':
    # Record start time for dashboard
    dashboard._start_time = datetime.now()
    
    # Start bot in separate thread
    bot_thread = Thread(target=main_loop, daemon=True)
    bot_thread.start()
    
    # Start Flask server
    logger.info(f"Starting web server on port {PORT}")
    app.run(host='0.0.0.0', port=PORT, debug=False, threaded=True)
else:
    # For Gunicorn on Render
    dashboard._start_time = datetime.now()
    bot_thread = Thread(target=main_loop, daemon=True)
    bot_thread.start()
