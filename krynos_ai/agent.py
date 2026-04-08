import os
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
import sqlite3
import requests
from datetime import datetime
from dotenv import load_dotenv
import krakenex
from groq import Groq
from .kraken_cli import cli as kraken_cli, setup_instructions as cli_setup_instructions

# ── Load environment variables ─────────────────────────────────────────────────
load_dotenv()

KRAKEN_API_KEY     = os.environ.get("KRAKEN_API_KEY")
KRAKEN_API_SECRET  = os.environ.get("KRAKEN_API_SECRET")
GROQ_API_KEY       = os.environ.get("GROQ_API_KEY")
PRISM_API_KEY      = os.environ.get("PRISM_API_KEY")

TRADING_PAIR       = "XBTUSD"
PAPER_TRADING      = True    # Set to False for live trades
LOOP_INTERVAL      = 60      # seconds between rounds
MAX_DAILY_LOSS     = 0.05    # 5%  — circuit breaker
MAX_RISK_PER_TRADE = 0.02    # 2%  — max portfolio risk per trade
MIN_CONFIDENCE     = 0.45    # minimum judge confidence to execute
MAX_RISK_SCORE     = 9       # skip if risk score at or above this
TRADING_FEE_PCT    = 0.0026  # 0.26% Kraken taker fee
STOP_LOSS_PCT      = 0.015   # 1.5% stop loss
TAKE_PROFIT_PCT    = 0.012   # 1.2% take profit

# ── Position State Machine ─────────────────────────────────────────────────────
class PositionTracker:
    def __init__(self):
        self.state = "FLAT"        # FLAT | LONG | SHORT
        self.entry_price = 0.0
        self.position_size = 0.0
        self.entry_time = None

    def can_trade(self, action: str) -> bool:
        if action == "HOLD":
            return True
        if self.state == "FLAT":
            return action == "BUY"
        if self.state == "LONG":
            return action == "SELL"
        return False

    def execute(self, action: str, price: float, size: float) -> dict:
        result = {"realized_pnl": 0.0, "fee": 0.0, "prev_state": self.state}
        trade_value = price * size
        result["fee"] = round(trade_value * TRADING_FEE_PCT, 4)

        if self.state == "FLAT":
            if action == "BUY":
                self.state = "LONG"
                self.entry_price = price
                self.position_size = size
                self.entry_time = datetime.now().isoformat()
            elif action == "SELL":
                self.state = "SHORT"
                self.entry_price = price
                self.position_size = size
                self.entry_time = datetime.now().isoformat()
        elif self.state == "LONG" and action == "SELL":
            raw_pnl = (price - self.entry_price) * self.position_size
            result["realized_pnl"] = round(raw_pnl - result["fee"], 4)
            self.state = "FLAT"
            self.entry_price = 0.0
            self.position_size = 0.0
            self.entry_time = None
        elif self.state == "SHORT" and action == "BUY":
            raw_pnl = (self.entry_price - price) * self.position_size
            result["realized_pnl"] = round(raw_pnl - result["fee"], 4)
            self.state = "FLAT"
            self.entry_price = 0.0
            self.position_size = 0.0
            self.entry_time = None
        return result

    def status_str(self) -> str:
        if self.state == "FLAT":
            return "FLAT (no position)"
        return f"{self.state} {self.position_size:.6f} BTC @ ${self.entry_price:,.2f}"

position = PositionTracker()

def restore_position_from_db():
    """Restore position state from DB on startup so restarts don't lose state."""
    try:
        conn = sqlite3.connect("krynos.db", timeout=5)
        c = conn.cursor()
        c.execute("""
            SELECT action, price, trade_size, timestamp
            FROM trades
            WHERE status IN ('executed', 'paper')
              AND action IN ('BUY', 'SELL')
            ORDER BY id DESC LIMIT 1
        """)
        row = c.fetchone()
        conn.close()
        if row and row[0] == "BUY":
            position.state = "LONG"
            position.entry_price = float(row[1])
            position.position_size = float(row[2])
            position.entry_time = row[3]
            print(f"   [RESTORE] Position restored: LONG {position.position_size:.6f} BTC @ ${position.entry_price:,.2f}")
        else:
            print(f"   [RESTORE] No open position — starting FLAT")
    except Exception as e:
        print(f"   [RESTORE] Could not restore position: {e}")

# ── Paper Portfolio Tracker ────────────────────────────────────────────────────
class PaperPortfolio:
    def __init__(self, starting_usd: float = 1000.0):
        self.usd = starting_usd
        self.btc = 0.0
        self.starting_usd = starting_usd
        self.total_fees = 0.0
        self.total_realized_pnl = 0.0
        self.trades_count = 0

    def buy(self, price: float, btc_amount: float) -> dict:
        cost = price * btc_amount
        fee = cost * TRADING_FEE_PCT
        total_cost = cost + fee
        if total_cost > self.usd:
            btc_amount = (self.usd / (1 + TRADING_FEE_PCT)) / price
            cost = price * btc_amount
            fee = cost * TRADING_FEE_PCT
            total_cost = cost + fee
        self.usd -= total_cost
        self.btc += btc_amount
        self.total_fees += fee
        self.trades_count += 1
        return {"cost": round(cost, 4), "fee": round(fee, 4), "btc_bought": round(btc_amount, 8)}

    def sell(self, price: float, btc_amount: float) -> dict:
        btc_amount = min(btc_amount, self.btc)
        revenue = price * btc_amount
        fee = revenue * TRADING_FEE_PCT
        net_revenue = revenue - fee
        self.usd += net_revenue
        self.btc -= btc_amount
        self.total_fees += fee
        self.trades_count += 1
        return {"revenue": round(revenue, 4), "fee": round(fee, 4), "btc_sold": round(btc_amount, 8)}

    def total_value(self, current_price: float) -> float:
        return self.usd + (self.btc * current_price)

    def unrealized_pnl(self, current_price: float) -> float:
        return round(self.total_value(current_price) - self.starting_usd, 4)

    def status_str(self, current_price: float) -> str:
        total = self.total_value(current_price)
        pnl = total - self.starting_usd
        return (f"${self.usd:,.2f} USD + {self.btc:.6f} BTC = "
                f"${total:,.2f} total | PnL: ${pnl:+,.2f} | Fees: ${self.total_fees:,.2f}")

paper_portfolio = PaperPortfolio(1000.0)

# ── Clients ────────────────────────────────────────────────────────────────────
k = krakenex.API()
k.key    = KRAKEN_API_KEY
k.secret = KRAKEN_API_SECRET

groq_client = Groq(api_key=GROQ_API_KEY)

# Kraken CLI detection (auto-detects native, WSL, or fallback)
print("[CLI] Detecting Kraken CLI...")
_cli_detected = kraken_cli.detect()
if _cli_detected:
    print(f"[CLI] OK - Kraken CLI available via {kraken_cli.mode}")
else:
    print("[CLI] WARNING - Kraken CLI not found -- using krakenex Python library as fallback")
    print("[CLI] For full hackathon compliance, install Kraken CLI:")
    print(cli_setup_instructions())

# ── Database setup ─────────────────────────────────────────────────────────────
def get_db_conn():
    conn = sqlite3.connect("krynos.db", timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn

def init_db():
    conn = get_db_conn()
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp             TEXT,
            pair                  TEXT,
            action                TEXT,
            winner                TEXT,
            confidence            REAL,
            risk_score            INTEGER,
            reason                TEXT,
            bull_argument         TEXT,
            bear_argument         TEXT,
            price                 REAL,
            trade_size            REAL,
            paper                 INTEGER,
            status                TEXT,
            pnl                   REAL DEFAULT 0,
            signal_score          INTEGER DEFAULT 0,
            signal_recommendation TEXT DEFAULT 'HOLD',
            fear_greed_index      INTEGER DEFAULT 0,
            fear_greed_label      TEXT DEFAULT '',
            funding_rate          REAL DEFAULT 0,
            open_interest_usd     REAL DEFAULT 0,
            order_book_bias       REAL DEFAULT 0
        )
    """)
    # Migrate: add any missing columns to existing DB
    new_cols = [
        ("signal_score",          "INTEGER DEFAULT 0"),
        ("signal_recommendation", "TEXT DEFAULT 'HOLD'"),
        ("stop_loss_pct",         "REAL DEFAULT 0"),
        ("fear_greed_index",      "INTEGER DEFAULT 0"),
        ("fear_greed_label",      "TEXT DEFAULT ''"),
        ("funding_rate",          "REAL DEFAULT 0"),
        ("open_interest_usd",     "REAL DEFAULT 0"),
        ("order_book_bias",       "REAL DEFAULT 0"),
    ]
    for col, typedef in new_cols:
        try:
            c.execute(f"ALTER TABLE trades ADD COLUMN {col} {typedef}")
        except sqlite3.OperationalError:
            pass

    c.execute("""
        CREATE TABLE IF NOT EXISTS daily_summary (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            date           TEXT UNIQUE,
            total_trades   INTEGER DEFAULT 0,
            buys           INTEGER DEFAULT 0,
            sells          INTEGER DEFAULT 0,
            holds          INTEGER DEFAULT 0,
            total_pnl      REAL DEFAULT 0,
            circuit_break  INTEGER DEFAULT 0
        )
    """)
    conn.commit()
    conn.close()

def log_trade(data: dict):
    conn = get_db_conn()
    c = conn.cursor()
    c.execute("""
        INSERT INTO trades (
            timestamp, pair, action, winner, confidence,
            risk_score, reason, bull_argument, bear_argument,
            price, trade_size, paper, status, pnl,
            signal_score, signal_recommendation,
            fear_greed_index, fear_greed_label,
            funding_rate, open_interest_usd, order_book_bias
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        data["timestamp"], data["pair"], data["action"],
        data["winner"], data["confidence"], data["risk_score"],
        data["reason"], data["bull_argument"], data["bear_argument"],
        data["price"], data["trade_size"], int(data["paper"]),
        data["status"], data.get("pnl", 0.0),
        data.get("signal_score", 0), data.get("signal_recommendation", "HOLD"),
        data.get("fear_greed_index", 0), data.get("fear_greed_label", ""),
        data.get("funding_rate", 0.0), data.get("open_interest_usd", 0.0),
        data.get("order_book_bias", 0.0),
    ))
    today = datetime.now().strftime("%Y-%m-%d")
    c.execute("""
        INSERT INTO daily_summary (date, total_trades, buys, sells, holds)
        VALUES (?, 1,
            CASE WHEN ? = 'BUY'  THEN 1 ELSE 0 END,
            CASE WHEN ? = 'SELL' THEN 1 ELSE 0 END,
            CASE WHEN ? = 'HOLD' THEN 1 ELSE 0 END
        )
        ON CONFLICT(date) DO UPDATE SET
            total_trades = total_trades + 1,
            buys  = buys  + CASE WHEN ? = 'BUY'  THEN 1 ELSE 0 END,
            sells = sells + CASE WHEN ? = 'SELL' THEN 1 ELSE 0 END,
            holds = holds + CASE WHEN ? = 'HOLD' THEN 1 ELSE 0 END
    """, (today,
          data["action"], data["action"], data["action"],
          data["action"], data["action"], data["action"]))
    conn.commit()
    conn.close()

def get_daily_pnl() -> float:
    conn = get_db_conn()
    c = conn.cursor()
    today = datetime.now().strftime("%Y-%m-%d")
    c.execute("SELECT total_pnl FROM daily_summary WHERE date = ?", (today,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else 0.0

def get_all_trades(limit: int = 50) -> list:
    conn = get_db_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM trades ORDER BY timestamp DESC LIMIT ?", (limit,))
    rows = c.fetchall()
    conn.close()
    return rows

# ── Technical Indicators ────────────────────────────────────────────────────────
def compute_ema(closes: list, period: int) -> list:
    if len(closes) < period:
        return closes[:]
    ema = [sum(closes[:period]) / period]
    multiplier = 2 / (period + 1)
    for price in closes[period:]:
        ema.append((price - ema[-1]) * multiplier + ema[-1])
    return ema

def compute_rsi(closes: list, period: int = 14) -> float:
    if len(closes) < period + 1:
        return 50.0
    deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains = [d if d > 0 else 0 for d in deltas]
    losses = [-d if d < 0 else 0 for d in deltas]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)

def compute_macd(closes: list) -> dict:
    if len(closes) < 26:
        return {"macd_line": 0, "signal_line": 0, "histogram": 0}
    ema12 = compute_ema(closes, 12)
    ema26 = compute_ema(closes, 26)
    offset = 26 - 12
    macd_line = [ema12[offset + i] - ema26[i] for i in range(len(ema26))]
    signal = compute_ema(macd_line, 9) if len(macd_line) >= 9 else macd_line[:]
    histogram = macd_line[-1] - signal[-1] if signal else 0
    return {
        "macd_line":   round(macd_line[-1], 2) if macd_line else 0,
        "signal_line": round(signal[-1], 2) if signal else 0,
        "histogram":   round(histogram, 2),
    }

def compute_indicators(ohlc_data: list) -> dict:
    closes = [float(c[4]) for c in ohlc_data]
    ema_20 = compute_ema(closes, 25)   # EMA25 stored under ema_20 key
    ema_50 = compute_ema(closes, 45)   # EMA45 stored under ema_50 key

    bb_upper, bb_lower, bb_mid = None, None, None
    if len(closes) >= 20:
        window = closes[-20:]
        bb_mid = sum(window) / 20
        std = (sum((x - bb_mid)**2 for x in window) / 20) ** 0.5
        bb_upper = round(bb_mid + 2 * std, 2)
        bb_lower = round(bb_mid - 2 * std, 2)
        bb_mid = round(bb_mid, 2)

    pct_1h  = round((closes[-1] / closes[-2]  - 1) * 100, 3) if len(closes) >= 2  else 0
    pct_4h  = round((closes[-1] / closes[-5]  - 1) * 100, 3) if len(closes) >= 5  else 0
    pct_24h = round((closes[-1] / closes[-25] - 1) * 100, 3) if len(closes) >= 25 else 0

    recent_highs = [float(c[2]) for c in ohlc_data[-24:]]
    recent_lows  = [float(c[3]) for c in ohlc_data[-24:]]

    volumes = [float(c[6]) for c in ohlc_data]
    vol_ratio = None
    if len(volumes) >= 20 and volumes[-1] > 0:
        avg_vol = sum(volumes[-20:]) / 20
        vol_ratio = round(volumes[-1] / avg_vol, 2) if avg_vol > 0 else 1.0

    return {
        "rsi_14":      compute_rsi(closes, 14),
        "ema_20":      round(ema_20[-1], 2) if ema_20 else None,
        "ema_50":      round(ema_50[-1], 2) if ema_50 else None,
        "macd":        compute_macd(closes),
        "bb_upper":    bb_upper,
        "bb_lower":    bb_lower,
        "bb_mid":      bb_mid,
        "pct_1h":      pct_1h,
        "pct_4h":      pct_4h,
        "pct_24h":     pct_24h,
        "support":     round(min(recent_lows), 2)  if recent_lows  else None,
        "resistance":  round(max(recent_highs), 2) if recent_highs else None,
        "vol_ratio":   vol_ratio,
        "num_candles": len(closes),
    }

def detect_ema_crossover(ohlc_data_4h: list) -> dict:
    closes = [float(c[4]) for c in ohlc_data_4h]
    if len(closes) < 46:
        return {"type": "none", "ema25_prev": None, "ema45_prev": None}

    ema25_series = compute_ema(closes, 25)
    ema45_series = compute_ema(closes, 45)

    if len(ema25_series) < 2 or len(ema45_series) < 2:
        return {"type": "none"}

    ema25_now  = ema25_series[-1]
    ema25_prev = ema25_series[-2]
    ema45_now  = ema45_series[-1]
    ema45_prev = ema45_series[-2]

    golden = ema25_prev <= ema45_prev and ema25_now > ema45_now
    death  = ema25_prev >= ema45_prev and ema25_now < ema45_now

    return {
        "type":         "golden" if golden else "death" if death else "none",
        "ema25_now":    round(ema25_now, 2),
        "ema45_now":    round(ema45_now, 2),
        "ema25_prev":   round(ema25_prev, 2),
        "ema45_prev":   round(ema45_prev, 2),
        "aligned_bull": ema25_now > ema45_now,
        "aligned_bear": ema25_now < ema45_now,
    }

# ── Market Data Feeds ──────────────────────────────────────────────────────────

def get_fear_greed_index() -> dict:
    """Fetch the Crypto Fear & Greed Index from Alternative.me (0-100)."""
    try:
        r = requests.get(
            "https://api.alternative.me/fng/?limit=1&format=json",
            timeout=5
        )
        if r.status_code == 200:
            d = r.json()
            entry = d["data"][0]
            value = int(entry["value"])
            label = entry["value_classification"]

            if value <= 24:
                signal_pts = 10    # Extreme Fear → contrarian buy
            elif value <= 44:
                signal_pts = 5     # Fear → slight bullish lean
            elif value <= 55:
                signal_pts = 0     # Neutral
            elif value <= 74:
                signal_pts = -5    # Greed → slight bearish lean
            else:
                signal_pts = -10   # Extreme Greed → contrarian sell

            return {"value": value, "label": label, "signal_pts": signal_pts}
    except Exception as e:
        print(f"  [Fear&Greed] Failed: {e}")
    return {"value": 50, "label": "Neutral", "signal_pts": 0}


def get_binance_market_data() -> dict:
    """Fetch funding rate, open interest, and order book bias from Binance."""
    result = {
        "funding_rate": 0.0,
        "funding_signal_pts": 0,
        "open_interest_usd": 0.0,
        "oi_signal_pts": 0,
        "order_book_bias": 0.5,
        "ob_signal_pts": 0,
        "error": None,
    }

    try:
        # 1. Funding Rate
        r_fund = requests.get(
            "https://fapi.binance.com/fapi/v1/premiumIndex?symbol=BTCUSDT",
            timeout=5
        )
        if r_fund.status_code == 200:
            fund_data = r_fund.json()
            rate = float(fund_data.get("lastFundingRate", 0))
            result["funding_rate"] = rate

            # High positive funding = overleveraged longs → bearish signal
            # Negative funding = squeeze potential → bullish signal
            if rate > 0.001:        # > 0.1% per 8h = very high longs
                result["funding_signal_pts"] = -10
            elif rate > 0.0003:     # > 0.03%
                result["funding_signal_pts"] = -5
            elif rate < -0.0003:    # negative = shorts dominant
                result["funding_signal_pts"] = +5
            elif rate < -0.001:     # very negative
                result["funding_signal_pts"] = +10
            else:
                result["funding_signal_pts"] = 0

    except Exception as e:
        result["error"] = f"funding: {e}"

    try:
        # 2. Open Interest
        r_oi = requests.get(
            "https://fapi.binance.com/fapi/v1/openInterest?symbol=BTCUSDT",
            timeout=5
        )
        if r_oi.status_code == 200:
            oi_data = r_oi.json()
            oi = float(oi_data.get("openInterest", 0))
            # Convert BTC OI to USD using a rough price (will be overridden by real price later)
            result["open_interest_usd"] = oi  # store raw BTC OI; multiply by price in scoring
            # OI alone without trend is neutral — used in signal context only
            result["oi_signal_pts"] = 0

    except Exception as e:
        result["error"] = (result.get("error") or "") + f" oi: {e}"

    try:
        # 3. Order Book Depth (top 10 levels)
        r_ob = requests.get(
            "https://api.binance.com/api/v3/depth?symbol=BTCUSDT&limit=10",
            timeout=5
        )
        if r_ob.status_code == 200:
            ob_data = r_ob.json()
            bid_vol = sum(float(b[1]) for b in ob_data.get("bids", []))
            ask_vol = sum(float(a[1]) for a in ob_data.get("asks", []))
            total = bid_vol + ask_vol
            if total > 0:
                bias = round(bid_vol / total, 4)
                result["order_book_bias"] = bias

                if bias >= 0.65:
                    result["ob_signal_pts"] = +10   # strong buy pressure
                elif bias >= 0.55:
                    result["ob_signal_pts"] = +5    # mild buy pressure
                elif bias <= 0.35:
                    result["ob_signal_pts"] = -10   # strong sell pressure
                elif bias <= 0.45:
                    result["ob_signal_pts"] = -5    # mild sell pressure
                else:
                    result["ob_signal_pts"] = 0     # balanced

    except Exception as e:
        result["error"] = (result.get("error") or "") + f" ob: {e}"

    return result


def compute_signal_score(market_data: dict) -> dict:
    """
    EMA 25/45 Crossover strategy with RSI + MACD gates,
    enhanced with Fear & Greed, Funding Rate, and Order Book Bias.
    BUY >= +20, SELL <= -20, else HOLD.
    """
    ind   = market_data.get("indicators_4h") or market_data.get("indicators", {})
    price = market_data.get("last_price", 0)
    score = 0
    signals = []

    ema25 = ind.get("ema_20")
    ema45 = ind.get("ema_50")
    rsi   = ind.get("rsi_14", 50)
    macd  = ind.get("macd", {})
    hist  = macd.get("histogram", 0)
    prism = market_data.get("prism_data", {})

    # ── GATE 1: EMA 25/45 alignment — PRIMARY signal (weight: 50 pts) ────
    if ema25 and ema45:
        crossover  = market_data.get("crossover", {})
        cross_type = crossover.get("type", "none")

        if cross_type == "golden":
            score += 50
            signals.append("🟢 GOLDEN CROSS: EMA25 crossed above EMA45 — fresh BUY signal (+50)")
        elif cross_type == "death":
            score -= 50
            signals.append("🔴 DEATH CROSS: EMA25 crossed below EMA45 — fresh SELL signal (-50)")
        elif ema25 > ema45:
            score += 25
            signals.append(f"EMA25 ({ema25}) > EMA45 ({ema45}) — bullish trend continuation (+25)")
        elif ema25 < ema45:
            score -= 25
            signals.append(f"EMA25 ({ema25}) < EMA45 ({ema45}) — bearish trend continuation (-25)")
    else:
        signals.append("EMA25/45 unavailable — insufficient candle history")

    # ── GATE 2: RSI confirmation ──────────────────────────────────────────
    if rsi > 55:
        pts = 20 if rsi > 65 else 10
        score += pts
        signals.append(f"RSI {rsi} > 55 — momentum confirmed for BUY (+{pts})")
    elif rsi < 45:
        pts = 20 if rsi < 35 else 10
        score -= pts
        signals.append(f"RSI {rsi} < 45 — momentum confirmed for SELL (-{pts})")
    else:
        if score > 0:
            dampened = int(score * 0.5)
            signals.append(f"RSI {rsi} neutral (45-55) — BUY signal dampened {score} → {dampened}")
            score = dampened
        else:
            signals.append(f"RSI {rsi} neutral (45-55) — no confirmation")

    # ── GATE 3: MACD confirmation (weight: 20 pts) ────────────────────────
    if hist > 10:
        score += 20
        signals.append(f"MACD hist {hist} positive & strong — confirms BUY (+20)")
    elif hist > 0:
        score += 10
        signals.append(f"MACD hist {hist} positive — weak BUY confirmation (+10)")
    elif hist < -10:
        score -= 20
        signals.append(f"MACD hist {hist} negative & strong — confirms SELL (-20)")
    elif hist < 0:
        score -= 10
        signals.append(f"MACD hist {hist} negative — weak SELL confirmation (-10)")
    else:
        signals.append(f"MACD hist {hist} flat — no confirmation")

    # ── PRISM AI (external confirmation, +/- 10 pts) ──────────────────────
    prism_direction = prism.get("direction", "neutral")
    prism_strength  = prism.get("strength", "")
    prism_consensus = prism.get("market_consensus", "mixed")

    if prism_direction == "bullish":
        pts = 10 if prism_strength == "strong" else 5
        score += pts
        signals.append(f"PRISM AI: {prism.get('signal','?')} (+{pts})")
    elif prism_direction == "bearish":
        pts = 10 if prism_strength == "strong" else 5
        score -= pts
        signals.append(f"PRISM AI: {prism.get('signal','?')} (-{pts})")

    if prism_consensus == "bullish" and prism_direction == "bullish":
        score += 5
        signals.append("PRISM cross-crypto bullish consensus (+5)")
    elif prism_consensus == "bearish" and prism_direction == "bearish":
        score -= 5
        signals.append("PRISM cross-crypto bearish consensus (-5)")

    # ── NEW: Fear & Greed Index (+/- 10 pts) ─────────────────────────────
    fg = market_data.get("fear_greed", {})
    fg_pts = fg.get("signal_pts", 0)
    if fg_pts != 0:
        score += fg_pts
        direction = "contrarian BUY" if fg_pts > 0 else "contrarian SELL"
        signals.append(
            f"Fear&Greed {fg.get('value', '?')} ({fg.get('label', '?')}) "
            f"— {direction} ({fg_pts:+d})"
        )
    else:
        signals.append(f"Fear&Greed {fg.get('value', '?')} ({fg.get('label', '?')}) — neutral (0)")

    # ── NEW: Binance Funding Rate (+/- 10 pts) ────────────────────────────
    binance = market_data.get("binance_data", {})
    fund_pts = binance.get("funding_signal_pts", 0)
    fund_rate = binance.get("funding_rate", 0)
    if fund_pts != 0:
        score += fund_pts
        direction = "bullish (short squeeze)" if fund_pts > 0 else "bearish (overleveraged longs)"
        signals.append(
            f"Funding rate {fund_rate:.4%}/8h — {direction} ({fund_pts:+d})"
        )
    else:
        signals.append(f"Funding rate {fund_rate:.4%}/8h — neutral (0)")

    # ── NEW: Order Book Bias (+/- 10 pts) ────────────────────────────────
    ob_pts  = binance.get("ob_signal_pts", 0)
    ob_bias = binance.get("order_book_bias", 0.5)
    if ob_pts != 0:
        direction = "buy pressure" if ob_pts > 0 else "sell pressure"
        score += ob_pts
        signals.append(
            f"Order book bias {ob_bias:.1%} bids — {direction} ({ob_pts:+d})"
        )
    else:
        signals.append(f"Order book bias {ob_bias:.1%} — balanced (0)")

    # ── Volume sanity check ───────────────────────────────────────────────
    vol_ratio = ind.get("vol_ratio")
    if vol_ratio is not None and vol_ratio < 0.5 and abs(score) > 20:
        dampened = int(score * 0.7)
        signals.append(f"Volume {vol_ratio}x LOW — signal dampened {score} → {dampened}")
        score = dampened
    elif vol_ratio and vol_ratio > 2.0:
        signals.append(f"Volume {vol_ratio}x HIGH — confirming signal")

    # ── Clamp and convert ─────────────────────────────────────────────────
    score = max(-100, min(100, score))

    if score >= 20:
        recommendation = "BUY"
    elif score <= -20:
        recommendation = "SELL"
    else:
        recommendation = "HOLD"

    return {
        "score":          score,
        "recommendation": recommendation,
        "signals":        signals,
    }


def get_recent_decisions(limit: int = 5) -> list:
    conn = get_db_conn()
    c = conn.cursor()
    c.execute("""
        SELECT action, winner, confidence, risk_score, pnl
        FROM trades ORDER BY id DESC LIMIT ?
    """, (limit,))
    rows = c.fetchall()
    conn.close()
    return [{"action": r[0], "winner": r[1], "confidence": r[2],
             "risk_score": r[3], "pnl": r[4]} for r in rows]

# ── Step 1: Fetch market data from Kraken (CLI with krakenex fallback) ─────────
def get_market_data(pair: str) -> dict:
    # Ticker — try CLI first, fallback to krakenex
    ticker_result = kraken_cli.ticker(pair)
    if not ticker_result.get("ok"):
        raise Exception(f"Failed to get ticker: {ticker_result.get('error')}")
    data_source = ticker_result.get("source", "unknown")

    # OHLC — try CLI first, fallback to krakenex
    ohlc_result = kraken_cli.ohlc(pair, interval=60)
    if not ohlc_result.get("ok"):
        raise Exception(f"Failed to get OHLC: {ohlc_result.get('error')}")
    ohlc_all = ohlc_result["candles"]
    ohlc_recent = ohlc_all[-10:]

    last_candle_ts = int(ohlc_all[-1][0])
    now_ts = int(time.time())
    candle_age_min = (now_ts - last_candle_ts) / 60
    stale_warning = candle_age_min > 120
    if stale_warning:
        print(f"  ⚠️  STALE DATA WARNING: Last candle is {candle_age_min:.0f} min old")

    indicators = compute_indicators(ohlc_all)

    # 4h OHLC
    ohlc_4h_result = kraken_cli.ohlc(pair, interval=240)
    if ohlc_4h_result.get("ok"):
        ohlc_4h_all = ohlc_4h_result["candles"]
    else:
        ohlc_4h_all = ohlc_all  # fallback
    indicators_4h = compute_indicators(ohlc_4h_all)

    crossover = detect_ema_crossover(ohlc_4h_all)

    return {
        "pair":           pair,
        "last_price":     ticker_result["last"],
        "ask":            ticker_result["ask"],
        "bid":            ticker_result["bid"],
        "high_24h":       ticker_result["high_24h"],
        "low_24h":        ticker_result["low_24h"],
        "volume_24h":     ticker_result["volume_24h"],
        "vwap_24h":       ticker_result["vwap_24h"],
        "trades_24h":     ticker_result["trades_24h"],
        "ohlc_candles":   ohlc_recent,
        "indicators":     indicators,
        "indicators_4h":  indicators_4h,
        "crossover":      crossover,
        "stale_warning":  stale_warning,
        "candle_age_min": round(candle_age_min, 1),
        "data_source":    data_source,
    }

# ── Step 2: Fetch PRISM signals ────────────────────────────────────────────────
PRISM_BASE = "https://api.prismapi.ai"

def prism_resolve(symbol: str) -> dict:
    if not PRISM_API_KEY:
        return {}
    try:
        headers = {"X-API-Key": PRISM_API_KEY}
        r = requests.get(f"{PRISM_BASE}/resolve/{symbol}", headers=headers, timeout=5)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return {}

def prism_signals(symbol: str) -> dict:
    if not PRISM_API_KEY:
        return {}
    try:
        headers = {"X-API-Key": PRISM_API_KEY}
        r = requests.get(f"{PRISM_BASE}/signals/{symbol}", headers=headers, timeout=5)
        if r.status_code == 200:
            data = r.json()
            if "data" in data and data["data"]:
                return data["data"][0]
    except Exception:
        pass
    return {}

def get_prism_data() -> dict:
    if not PRISM_API_KEY:
        return {"signal": "N/A", "assets": {}}
    try:
        resolved = prism_resolve("BTC")
        venues = []
        if resolved.get("venues", {}).get("data"):
            venues = [{"name": v["name"], "type": v["type"]} for v in resolved["venues"]["data"][:5]]

        btc_sig = prism_signals("BTC")
        btc_data = {}
        if btc_sig:
            btc_data = {
                "signal":         btc_sig.get("overall_signal", "N/A"),
                "direction":      btc_sig.get("direction", "N/A"),
                "strength":       btc_sig.get("strength", "N/A"),
                "net_score":      btc_sig.get("net_score", 0),
                "bullish_score":  btc_sig.get("bullish_score", 0),
                "bearish_score":  btc_sig.get("bearish_score", 0),
                "prism_price":    btc_sig.get("current_price"),
                "prism_rsi":      btc_sig.get("indicators", {}).get("rsi"),
                "prism_macd_hist":btc_sig.get("indicators", {}).get("macd_histogram"),
                "prism_bb_upper": btc_sig.get("indicators", {}).get("bollinger_upper"),
                "prism_bb_lower": btc_sig.get("indicators", {}).get("bollinger_lower"),
                "active_signals": btc_sig.get("active_signals", []),
                "timestamp":      btc_sig.get("timestamp"),
            }
        else:
            btc_data = {"signal": "N/A", "direction": "N/A", "net_score": 0}

        correlated = {}
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = {executor.submit(prism_signals, sym): sym for sym in ["ETH", "SOL"]}
            for future in as_completed(futures):
                sym = futures[future]
                try:
                    sig = future.result()
                    if sig:
                        correlated[sym] = {
                            "signal":         sig.get("overall_signal", "N/A"),
                            "direction":      sig.get("direction", "N/A"),
                            "net_score":      sig.get("net_score", 0),
                            "price":          sig.get("current_price"),
                            "active_signals": [s.get("type") + ":" + s.get("signal", "") for s in sig.get("active_signals", [])],
                        }
                except Exception:
                    pass

        directions = [btc_data.get("direction", "neutral")]
        for sym_data in correlated.values():
            directions.append(sym_data.get("direction", "neutral"))
        bullish_count = sum(1 for d in directions if d == "bullish")
        bearish_count = sum(1 for d in directions if d == "bearish")
        market_consensus = "bullish" if bullish_count >= 2 else "bearish" if bearish_count >= 2 else "mixed"

        return {
            **btc_data,
            "resolved_symbol":  resolved.get("symbol", "BTC"),
            "resolved_name":    resolved.get("name", "Bitcoin"),
            "venues":           venues,
            "correlated_assets":correlated,
            "market_consensus": market_consensus,
        }
    except Exception as e:
        print(f"[PRISM] Error fetching signals: {e}")
        return {"signal": "N/A", "assets": {}}

# ── Step 2b: Fetch crypto news ─────────────────────────────────────────────────
def get_crypto_news(limit: int = 5) -> list:
    try:
        r = requests.get(
            "https://api.rss2json.com/v1/api.json?rss_url=https://cointelegraph.com/rss",
            timeout=5
        )
        if r.status_code == 200:
            items = r.json().get("items", [])
            return [
                {
                    "title":     item.get("title", ""),
                    "author":    item.get("author", ""),
                    "link":      item.get("link", ""),
                    "published": item.get("pubDate", ""),
                }
                for item in items[:limit]
            ]
    except Exception:
        pass
    return []

# ── Step 3: Portfolio value + position sizing ──────────────────────────────────
def get_portfolio_value(current_price: float = None) -> float:
    # Try Kraken CLI paper status first
    if PAPER_TRADING and kraken_cli.is_available:
        status = kraken_cli.paper_status()
        if status.get("ok") and isinstance(status.get("data"), dict):
            total = status["data"].get("current_value") or status["data"].get("total_value")
            if total:
                return float(total)

    if PAPER_TRADING:
        if current_price:
            return paper_portfolio.total_value(current_price)
        return paper_portfolio.usd + paper_portfolio.btc * 68000
    try:
        bal = kraken_cli.balance()
        if bal.get("ok") and bal.get("balances"):
            balances = bal["balances"]
            zusd = float(balances.get("ZUSD", balances.get("USD", 1000)))
            xxbt = float(balances.get("XXBT", balances.get("BTC", 0)))
            btc_price = current_price or get_market_data(TRADING_PAIR)["last_price"]
            return zusd + (xxbt * btc_price)
    except Exception:
        pass
    return 1000.0

def calculate_trade_size(price: float) -> float:
    portfolio = get_portfolio_value(price)
    max_usd   = portfolio * MAX_RISK_PER_TRADE
    size      = round(max_usd / price, 6)
    return max(size, 0.0001)

# ── Step 4: Run debate agent (bull or bear) ────────────────────────────────────
def run_agent(role: str, market_data: dict, prism_data: dict) -> str:
    ind    = market_data.get("indicators", {})
    signal = market_data.get("signal_score", {})
    fg     = market_data.get("fear_greed", {})
    binance = market_data.get("binance_data", {})

    ind_summary = f"""Technical Indicators:
- RSI(14): {ind.get('rsi_14', 'N/A')}
- EMA(25): {ind.get('ema_20', 'N/A')} | EMA(45): {ind.get('ema_50', 'N/A')}
- MACD Line: {ind.get('macd', {}).get('macd_line', 'N/A')} | Signal: {ind.get('macd', {}).get('signal_line', 'N/A')} | Histogram: {ind.get('macd', {}).get('histogram', 'N/A')}
- Bollinger Bands: Lower={ind.get('bb_lower', 'N/A')} | Mid={ind.get('bb_mid', 'N/A')} | Upper={ind.get('bb_upper', 'N/A')}
- Support: {ind.get('support', 'N/A')} | Resistance: {ind.get('resistance', 'N/A')}
- Price Change: 1h={ind.get('pct_1h', 'N/A')}% | 4h={ind.get('pct_4h', 'N/A')}% | 24h={ind.get('pct_24h', 'N/A')}%

Quantitative Signal Score: {signal.get('score', 0)}/100 ({signal.get('recommendation', 'N/A')})
Signal Breakdown: {'; '.join(signal.get('signals', []))}

Real Market Data:
- Fear & Greed Index: {fg.get('value', 'N/A')} ({fg.get('label', 'N/A')})
- BTC Funding Rate: {binance.get('funding_rate', 0):.4%}/8h ({'overleveraged longs → bearish' if binance.get('funding_rate', 0) > 0.001 else 'negative → squeeze potential' if binance.get('funding_rate', 0) < -0.0003 else 'neutral'})
- Order Book Bias: {binance.get('order_book_bias', 0.5):.1%} bids ({'buy pressure' if binance.get('order_book_bias', 0.5) > 0.55 else 'sell pressure' if binance.get('order_book_bias', 0.5) < 0.45 else 'balanced'})
- Open Interest: {binance.get('open_interest_usd', 0):,.0f} BTC"""

    system_prompts = {
        "bull": f"""You are an aggressive bull trader analyzing crypto markets.
Given market data, technical indicators, and a quantitative signal score, construct the strongest possible BUY argument.

KEY INDICATOR RULES:
- RSI below 30 = OVERSOLD — strong mean-reversion buy signal
- RSI 30-45 = approaching oversold — potential buy setup
- Price near Bollinger Band lower = oversold bounce expected
- Price near support level = likely bounce point
- MACD histogram turning positive = momentum shifting bullish
- Fear & Greed below 25 = Extreme Fear = contrarian BUY (market panic = opportunity)
- Negative funding rate = shorts are dominant = squeeze potential = bullish
- Order book bias >55% bids = real buy pressure

If the quantitative signal score is positive, emphasize why the numbers support BUY.
If the signal score is negative, argue why the indicators are about to reverse (mean reversion).
Be concise, data-driven, reference specific numbers.
End with: VERDICT: BUY""",

        "bear": f"""You are a cautious bear trader analyzing crypto markets.
Given market data, technical indicators, and a quantitative signal score, construct the strongest possible SELL argument.

KEY INDICATOR RULES:
- RSI above 70 = OVERBOUGHT — strong mean-reversion sell signal
- RSI 55-70 = approaching overbought — potential sell setup
- Price near Bollinger Band upper = overbought rejection expected
- Price near resistance level = likely rejection point
- MACD histogram turning negative = momentum shifting bearish
- Fear & Greed above 75 = Extreme Greed = contrarian SELL (market euphoria = top)
- High positive funding rate = overleveraged longs = bearish pressure
- Order book bias <45% bids = real sell pressure

If the quantitative signal score is negative, emphasize why the numbers support SELL.
If the signal score is positive, argue why the rally is unsustainable.
Be concise, data-driven, reference specific numbers.
End with: VERDICT: SELL""",
    }

    context = f"""Market Data:
Price: ${market_data['last_price']:,.2f} | Ask: ${market_data.get('ask',0):,.2f} | Bid: ${market_data.get('bid',0):,.2f}
24h High: ${market_data.get('high_24h',0):,.2f} | 24h Low: ${market_data.get('low_24h',0):,.2f}
24h Volume: {market_data.get('volume_24h','N/A')} | VWAP: ${market_data.get('vwap_24h',0):,.2f}

{ind_summary}

PRISM AI Signals:
{json.dumps({k: v for k, v in prism_data.items() if k not in ('venues', 'correlated_assets', 'active_signals')}, indent=2)}"""

    news = market_data.get("news", [])
    if news:
        headlines = "\n".join(f"- {n['title']}" for n in news[:5])
        context += f"\n\nLatest Crypto News:\n{headlines}"

    response = groq_client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[
            {"role": "system", "content": system_prompts[role]},
            {"role": "user",   "content": f"Analyze this data:\n{context}"}
        ],
        max_tokens=500
    )
    return response.choices[0].message.content

# ── Step 5: Judge decides ──────────────────────────────────────────────────────
def run_judge(bull_argument: str, bear_argument: str, market_data: dict = None) -> dict:
    ind_ctx     = ""
    history_ctx = ""

    if market_data:
        ind    = market_data.get("indicators", {})
        signal = market_data.get("signal_score", {})
        score  = signal.get("score", 0)
        rec    = signal.get("recommendation", "HOLD")
        fg     = market_data.get("fear_greed", {})
        binance = market_data.get("binance_data", {})

        ind_ctx = f"""
QUANTITATIVE SIGNAL SCORE: {score}/100 → {rec}
This score is computed from RSI, MACD, EMA, Bollinger Bands, Fear&Greed, Funding Rate, and Order Book.
- Score >= +20: BUY signal
- Score <= -20: SELL signal
- Between -20 and +20: HOLD

Signal breakdown:
{chr(10).join('  • ' + s for s in signal.get('signals', []))}

Raw indicators:
- RSI(14): {ind.get('rsi_14', 'N/A')}
- MACD Histogram: {ind.get('macd', {}).get('histogram', 'N/A')}
- Price vs EMA(25): {'above' if market_data.get('last_price', 0) > (ind.get('ema_20') or 0) else 'below'}
- Price vs EMA(45): {'above' if market_data.get('last_price', 0) > (ind.get('ema_50') or 0) else 'below'}
- Fear & Greed: {fg.get('value', 'N/A')} ({fg.get('label', 'N/A')})
- Funding Rate: {binance.get('funding_rate', 0):.4%}/8h
- Order Book Bias: {binance.get('order_book_bias', 0.5):.1%} bids"""

    recent = get_recent_decisions(5)
    if recent:
        actions = [r["action"] for r in recent]
        consecutive = 1
        if len(actions) >= 2:
            for a in actions[1:]:
                if a == actions[0]:
                    consecutive += 1
                else:
                    break
        total_pnl = sum(r.get("pnl") or 0 for r in recent)
        history_ctx = f"""
RECENT TRADE HISTORY (last {len(recent)} trades):
Actions: {' → '.join(actions)}
Consecutive {actions[0]}s: {consecutive}
Recent PnL: ${total_pnl:+.4f}
{'⚠️ WARNING: ' + str(consecutive) + ' consecutive ' + actions[0] + 's. Avoid bias — check if indicators still support this direction.' if consecutive >= 3 else ''}"""

    judge_system = f"""You are a decisive quantitative trading judge using the EMA 25/45 crossover strategy enhanced with real market data.

STRATEGY RULES (non-negotiable):
- BUY only when: EMA25 > EMA45 AND RSI > 55 AND MACD histogram positive
- SELL when: EMA25 < EMA45 OR stop loss triggered OR MACD turns negative after profit
- Real market signals (Fear&Greed, Funding Rate, Order Book) provide additional confirmation

The QUANTITATIVE SIGNAL SCORE encodes these rules. Trust it above narrative arguments.

You MUST respond with ONLY valid JSON:
{{
  "action": "BUY" or "SELL" or "HOLD",
  "winner": "bull" or "bear" or "draw",
  "confidence": 0.0 to 1.0,
  "risk_score": 1 to 10,
  "reason": "one sentence explanation"
}}

DECISION PROCESS (follow strictly):
1. Signal score >= +20: BUY confirmed — act on it
2. Signal score <= -20: SELL confirmed — act on it
3. Signal score -20 to +20: HOLD — do NOT force a trade
   ⚠️ CRITICAL: If signal says HOLD, your action MUST be HOLD.

4. Confidence calibration:
   - Score >= ±35 with aligned real-market data: confidence 0.75-0.90
   - Score ±20-35: confidence 0.55-0.75
   - HOLD: set winner to "draw", confidence can be any value

5. Risk score:
   - 1-3: Clear trend, low volatility
   - 4-6: Normal (DEFAULT — use this unless extreme conditions)
   - 7-8: High volatility
   - 9-10: Extreme event only

6. POSITION AWARENESS:
   Current position: {position.status_str()}
   {'You are LONG — only SELL or HOLD is valid.' if position.state == 'LONG' else 'You are FLAT — only BUY is valid to open. SELL signals = HOLD and wait.'}
{ind_ctx}
{history_ctx}"""

    SAFE_HOLD = {"action": "HOLD", "winner": "draw", "confidence": 0.5,
                 "risk_score": 5, "reason": "Judge failed to produce valid JSON — defaulting to HOLD"}

    for attempt in range(3):
        try:
            response = groq_client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[
                    {"role": "system", "content": judge_system},
                    {"role": "user",   "content": f"BULL ARGUMENT:\n{bull_argument}\n\nBEAR ARGUMENT:\n{bear_argument}\n\nPick a winner and decide. Remember: HOLD = no profit. Be decisive."}
                ],
                max_tokens=300,
                temperature=0.3 + (attempt * 0.1)
            )
            raw = response.choices[0].message.content.strip()
            raw = raw.replace("```json", "").replace("```", "").strip()
            if "{" in raw:
                raw = raw[raw.index("{"):raw.rindex("}") + 1]
            result = json.loads(raw)
            if result.get("action") not in ("BUY", "SELL", "HOLD"):
                print(f"  [Judge] Invalid action '{result.get('action')}', retrying...")
                continue
            result.setdefault("winner", "draw")
            result.setdefault("confidence", 0.55)
            result.setdefault("risk_score", 5)
            result.setdefault("reason", "No reason provided")
            return result
        except (json.JSONDecodeError, ValueError) as e:
            print(f"  [Judge] JSON parse failed (attempt {attempt + 1}/3): {e}")
            if attempt < 2:
                continue
    print("  [Judge] All attempts failed — safe HOLD")
    return SAFE_HOLD

# ── Step 6: Execute via Kraken CLI ─────────────────────────────────────────────
def execute_via_kraken_cli(action: str, pair: str, amount: float, paper: bool = True) -> dict:
    """Execute trade through Kraken CLI (paper or live).
    Uses the kraken_cli wrapper which auto-detects CLI availability."""
    if action == "HOLD":
        return {"status": "skipped", "reason": "HOLD decision"}

    result = kraken_cli.execute_trade(
        action=action,
        pair=pair,
        volume=amount,
        paper=paper
    )

    cli_status = result.get("status", "unknown")
    source = result.get("source", "unknown")

    if cli_status == "paper_cli":
        print(f"\n  [KRAKEN CLI] Paper {action} {amount:.8f} {pair} — executed via CLI")
        cli_resp = result.get("cli_response", {})
        if cli_resp:
            print(f"    CLI response: {json.dumps(cli_resp, indent=2)[:200]}")
    elif cli_status == "paper_internal":
        print(f"\n  [INTERNAL] Paper {action} {amount:.8f} {pair} — CLI unavailable, internal tracking")
    elif cli_status == "executed":
        print(f"\n  [KRAKEN CLI] LIVE {action} {amount:.8f} {pair} — ORDER PLACED")
        cli_resp = result.get("cli_response", {})
        if cli_resp:
            print(f"    CLI response: {json.dumps(cli_resp, indent=2)[:300]}")
    elif cli_status == "error":
        print(f"\n  [KRAKEN CLI] Error: {result.get('error')}")

    return {"status": cli_status, "source": source, **result}

# ── Step 7: Circuit breaker ────────────────────────────────────────────────────
def check_circuit_breaker(current_price: float = None) -> bool:
    daily_pnl = get_daily_pnl()
    portfolio  = get_portfolio_value(current_price)
    loss_pct   = abs(daily_pnl) / portfolio if daily_pnl < 0 and portfolio > 0 else 0
    if loss_pct >= MAX_DAILY_LOSS:
        print(f"\n🚨 CIRCUIT BREAKER — Daily loss {loss_pct:.1%} exceeds {MAX_DAILY_LOSS:.0%} limit")
        print("   Trading paused for today.")
        conn  = get_db_conn()
        c     = conn.cursor()
        today = datetime.now().strftime("%Y-%m-%d")
        c.execute("""
            INSERT INTO daily_summary (date, circuit_break)
            VALUES (?, 1)
            ON CONFLICT(date) DO UPDATE SET circuit_break = 1
        """, (today,))
        conn.commit()
        conn.close()
        return True
    return False

# ── PnL tracking ───────────────────────────────────────────────────────────────
def update_previous_pnl(current_price: float):
    """Update unrealized PnL on the last BUY trade while position is still open.
    SELL trades already have realized PnL set at execution time — never overwrite."""
    conn = get_db_conn()
    c    = conn.cursor()
    c.execute("""
        SELECT id, action, price, trade_size, status
        FROM trades ORDER BY id DESC LIMIT 1
    """)
    row = c.fetchone()
    if row:
        trade_id, action, entry_price, trade_size, status = row
        # Only update unrealized PnL on open BUY trades (position still LONG)
        if status in ("paper", "executed") and action == "BUY" and position.state == "LONG":
            pnl = round((current_price - entry_price) * trade_size, 4)
            c.execute("UPDATE trades SET pnl = ? WHERE id = ?", (pnl, trade_id))
            today = datetime.now().strftime("%Y-%m-%d")
            c.execute("""
                INSERT INTO daily_summary (date, total_pnl)
                VALUES (?, ?)
                ON CONFLICT(date) DO UPDATE SET total_pnl = total_pnl + ?
            """, (today, pnl, pnl))
            conn.commit()
            print(f"\n[PnL] Open position (#{trade_id}) BUY @ ${entry_price:,.2f} → now ${current_price:,.2f} = ${pnl:+.4f} (unrealized)")
        elif action == "SELL" and status in ("paper", "executed"):
            print(f"\n[PnL] Last trade (#{trade_id}) was SELL — realized PnL already recorded")
    conn.close()

# ── Main debate round ──────────────────────────────────────────────────────────
def run_debate():
    print("=" * 60)
    print(f"  KRYNOS AI — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    if check_circuit_breaker():
        return None

    # 1. Market data from Kraken
    print("\n[1/7] Fetching market data from Kraken...")
    market_data = get_market_data(TRADING_PAIR)
    price       = market_data["last_price"]
    indicators  = market_data.get("indicators", {})
    data_source = market_data.get("data_source", "unknown")
    print(f"      BTC Last Price: ${price:,.2f} (via {data_source})")
    print(f"      RSI(14): {indicators.get('rsi_14', 'N/A')}")
    print(f"      EMA(25): {indicators.get('ema_20', 'N/A')} | EMA(45): {indicators.get('ema_50', 'N/A')}")
    macd = indicators.get('macd', {})
    print(f"      MACD: {macd.get('macd_line', 'N/A')} | Signal: {macd.get('signal_line', 'N/A')} | Hist: {macd.get('histogram', 'N/A')}")
    ind_4h    = market_data.get("indicators_4h", {})
    crossover = market_data.get("crossover", {})
    if ind_4h:
        print(f"      4h EMA(25): {ind_4h.get('ema_20', 'N/A')} | 4h EMA(45): {ind_4h.get('ema_50', 'N/A')}")
    if crossover:
        cross_type = crossover.get("type", "none")
        marker  = "🟢 GOLDEN CROSS!" if cross_type == "golden" else "🔴 DEATH CROSS!" if cross_type == "death" else "no cross"
        aligned = "EMA25 > EMA45 (BULLISH)" if crossover.get("aligned_bull") else "EMA25 < EMA45 (BEARISH)"
        print(f"      Crossover: {marker} | Alignment: {aligned}")

    # 2. PnL update
    print("\n[2/7] Evaluating previous trade PnL...")
    update_previous_pnl(price)
    print(f"      Position: {position.status_str()}")

    # 3. NEW: Fear & Greed Index
    print("\n[3/7] Fetching Fear & Greed Index...")
    fear_greed = get_fear_greed_index()
    market_data["fear_greed"] = fear_greed
    fg_bar = "🟢" if fear_greed["value"] <= 25 else "🔴" if fear_greed["value"] >= 75 else "🟡"
    print(f"      {fg_bar} Fear & Greed: {fear_greed['value']}/100 ({fear_greed['label']}) → signal {fear_greed['signal_pts']:+d} pts")

    # 4. NEW: Binance Funding Rate + Open Interest + Order Book
    print("\n[4/7] Fetching Binance market data (funding rate, OI, order book)...")
    binance_data = get_binance_market_data()
    market_data["binance_data"] = binance_data
    fund_rate = binance_data["funding_rate"]
    ob_bias   = binance_data["order_book_bias"]
    oi        = binance_data["open_interest_usd"]
    print(f"      Funding Rate: {fund_rate:.4%}/8h → signal {binance_data['funding_signal_pts']:+d} pts")
    print(f"      Open Interest: {oi:,.0f} BTC")
    print(f"      Order Book Bias: {ob_bias:.1%} bids → signal {binance_data['ob_signal_pts']:+d} pts")
    if binance_data.get("error"):
        print(f"      ⚠️  Partial error: {binance_data['error']}")

    # 5. PRISM signals
    print("\n[5/7] Fetching PRISM signals...")
    prism_data = get_prism_data()
    market_data["prism_data"] = prism_data
    prism_sig       = prism_data.get("signal", "N/A")
    prism_consensus = prism_data.get("market_consensus", "N/A")
    print(f"      BTC Signal: {prism_sig} | Direction: {prism_data.get('direction', 'N/A')} | Strength: {prism_data.get('strength', 'N/A')} | Net: {prism_data.get('net_score', 'N/A')}")
    for asig in prism_data.get("active_signals", []):
        print(f"        ⚡ {asig.get('type', '?')}: {asig.get('signal', '?')} ({asig.get('value', '')})")
    correlated = prism_data.get("correlated_assets", {})
    if correlated:
        parts = [f"{sym}: {d.get('signal','?')} (net={d.get('net_score',0)})" for sym, d in correlated.items()]
        print(f"      Cross-crypto: {' | '.join(parts)}")
        print(f"      Market consensus: {prism_consensus}")
    prism_px = prism_data.get("prism_price")
    if prism_px:
        diff_pct = abs(price - prism_px) / price * 100
        print(f"      PRISM price: ${prism_px:,.2f} (Kraken: ${price:,.2f}, diff: {diff_pct:.3f}%)")

    # 5b. News
    print("\n[5b/7] Fetching crypto news...")
    news = get_crypto_news(5)
    market_data["news"] = news
    if news:
        for n in news[:3]:
            print(f"      📰 {n['title'][:75]}")
    else:
        print("      No news available")

    # 6. Compute signal score (now includes Fear&Greed + Funding + OB)
    signal_score = compute_signal_score(market_data)
    market_data["signal_score"] = signal_score
    score_val   = signal_score["score"]
    score_color = "🟢" if score_val >= 20 else "🔴" if score_val <= -20 else "🟡"
    print(f"\n      {score_color} Signal Score: {score_val:+d}/100 → {signal_score['recommendation']}")
    for s in signal_score["signals"]:
        print(f"        • {s}")

    # 7. Bull/Bear debate
    print("\n[6/7] Bull & Bear agents arguing...")
    bull_arg = run_agent("bull", market_data, prism_data)
    print(f"\n  BULL:\n{bull_arg}")

    bear_arg = run_agent("bear", market_data, prism_data)
    print(f"\n  BEAR:\n{bear_arg}")

    # Judge
    print("\n[7/7] Judge evaluating...")
    decision = run_judge(bull_arg, bear_arg, market_data)
    print(f"\n  JUDGE DECISION:")
    print(f"    Action:     {decision['action']}")
    print(f"    Winner:     {decision['winner']}")
    print(f"    Confidence: {decision['confidence']:.0%}")
    print(f"    Risk Score: {decision['risk_score']}/10")
    print(f"    Reason:     {decision['reason']}")

    status = "pending"

    # ── Block BUY when signal is actively SELL ───────────────────────────────
    if decision["action"] == "BUY" and signal_score["recommendation"] == "SELL":
        print(f"\n[OVERRIDE BLOCKED] Agent wanted BUY but signal says SELL "
              f"(score={score_val:+d}). Forcing HOLD.")
        decision["action"] = "HOLD"
        decision["winner"] = "draw"
        decision["reason"] = f"HOLD enforced: signal score {score_val:+d} is bearish — cannot BUY"
        status = "signal_override"

    # ── Stop loss & take profit ──────────────────────────────────────────
    if position.state == "LONG" and position.entry_price > 0:
        loss_pct = (position.entry_price - price) / position.entry_price
        gain_pct = (price - position.entry_price) / position.entry_price
        if loss_pct >= STOP_LOSS_PCT:
            print(f"\n[STOP LOSS] Price dropped {loss_pct:.2%} from entry ${position.entry_price:,.2f} "
                  f"(now ${price:,.2f}) — forcing SELL")
            decision["action"] = "SELL"
            decision["winner"] = "bear"
            decision["reason"] = f"Stop loss triggered: {loss_pct:.2%} drop from entry"
            status = "stop_loss"
        elif gain_pct >= TAKE_PROFIT_PCT:
            print(f"\n[TAKE PROFIT] Price rose {gain_pct:.2%} from entry ${position.entry_price:,.2f} "
                  f"(now ${price:,.2f}) — locking in profit")
            decision["action"] = "SELL"
            decision["winner"] = "bull"
            decision["reason"] = f"Take profit: {gain_pct:.2%} gain from entry ${position.entry_price:,.2f}"
            status = "take_profit"

    # ── Position-aware action translation ──────────────────────────────────
    # When LONG: BUY signal means "keep holding", not "buy more"
    # When FLAT: SELL signal means "stay out", not "short"
    if position.state == "LONG" and decision["action"] == "BUY" and status == "pending":
        print(f"\n[POSITION] Already LONG — BUY signal converted to HOLD (maintaining position)")
        decision["action"] = "HOLD"
        decision["reason"] = f"HOLD: already LONG, signal still bullish ({score_val:+d}) — holding position"
    elif position.state == "FLAT" and decision["action"] == "SELL" and status == "pending":
        print(f"\n[POSITION] FLAT — SELL signal converted to HOLD (no position to sell)")
        decision["action"] = "HOLD"
        decision["reason"] = f"HOLD: no open position, signal bearish ({score_val:+d}) — waiting"

    trade_size = calculate_trade_size(price)
    round_pnl = 0.0  # realized PnL for this round (set on SELL)
    if status == "pending":
        status = "executed"

    # Gate checks
    if decision["winner"] == "draw" and status not in ("signal_override",):
        print("\n[SKIP] Draw — no trade (risk protection)")
        status = "draw"
    elif decision["confidence"] < MIN_CONFIDENCE and status != "stop_loss":
        print(f"\n[SKIP] Confidence {decision['confidence']:.0%} below {MIN_CONFIDENCE:.0%} threshold")
        status = "low_confidence"
    elif decision["risk_score"] >= MAX_RISK_SCORE and status != "stop_loss":
        print(f"\n[SKIP] Risk score {decision['risk_score']}/10 too high")
        status = "high_risk"
    elif not position.can_trade(decision["action"]):
        print(f"\n[SKIP] Position is {position.state} — cannot {decision['action']}")
        status = "position_blocked"
    else:
        fill_price = market_data["ask"] if decision["action"] == "BUY" else market_data["bid"]
        pos_result = position.execute(decision["action"], fill_price, trade_size)

        if decision["action"] == "BUY":
            portfolio_result = paper_portfolio.buy(fill_price, trade_size)
            print(f"\n[TRADE] BUY {trade_size} BTC @ ${fill_price:,.2f} | Cost: ${portfolio_result['cost']:.2f} | Fee: ${portfolio_result['fee']:.4f}")
        else:
            portfolio_result = paper_portfolio.sell(fill_price, trade_size)
            round_pnl = pos_result["realized_pnl"]  # capture realized PnL
            print(f"\n[TRADE] SELL {portfolio_result['btc_sold']:.6f} BTC @ ${fill_price:,.2f} | Revenue: ${portfolio_result['revenue']:.2f} | Fee: ${portfolio_result['fee']:.4f}")

        print(f"  Position: {pos_result['prev_state']} → {position.state}")
        if pos_result["realized_pnl"] != 0:
            print(f"  Realized PnL: ${pos_result['realized_pnl']:+.4f} (after fees)")
        print(f"  Portfolio: {paper_portfolio.status_str(fill_price)}")

        result = execute_via_kraken_cli(
            action=decision["action"],
            pair=TRADING_PAIR,
            amount=trade_size,
            paper=PAPER_TRADING
        )
        print(f"  Result: {result}")

    # Log to DB
    log_trade({
        "timestamp":             datetime.now().isoformat(),
        "pair":                  TRADING_PAIR,
        "action":                decision["action"],
        "winner":                decision["winner"],
        "confidence":            decision["confidence"],
        "risk_score":            decision["risk_score"],
        "reason":                decision["reason"],
        "bull_argument":         bull_arg,
        "bear_argument":         bear_arg,
        "price":                 price,
        "trade_size":            trade_size,
        "paper":                 PAPER_TRADING,
        "status":                status,
        "pnl":                   round_pnl,
        "signal_score":          signal_score["score"],
        "signal_recommendation": signal_score["recommendation"],
        "fear_greed_index":      fear_greed.get("value", 0),
        "fear_greed_label":      fear_greed.get("label", ""),
        "funding_rate":          binance_data.get("funding_rate", 0.0),
        "open_interest_usd":     binance_data.get("open_interest_usd", 0.0),
        "order_book_bias":       binance_data.get("order_book_bias", 0.5),
    })

    print(f"\n[DB] Round logged → krynos.db")
    print(f"[Portfolio] {paper_portfolio.status_str(price)}")
    print("=" * 60)
    return decision

# ── Continuous loop ────────────────────────────────────────────────────────────
def run_loop():F
    print("\n🚀 Krynos AI Starting...")
    print(f"   Mode:      {'PAPER TRADING' if PAPER_TRADING else '⚠️  LIVE TRADING'}")
    print(f"   Pair:      {TRADING_PAIR}")
    print(f"   Interval:  {LOOP_INTERVAL}s")
    print(f"   Max Risk:  {MAX_RISK_PER_TRADE:.0%} per trade")
    print(f"   Stop Loss: {STOP_LOSS_PCT:.1%} | Take Profit: {TAKE_PROFIT_PCT:.1%} | Circuit: {MAX_DAILY_LOSS:.0%} daily")

    # Kraken CLI status
    cli_info = kraken_cli.info()
    print(f"   Kraken CLI: {'OK via ' + kraken_cli.mode if cli_info['cli_available'] else 'NOT FOUND (using krakenex fallback)'}")
    if cli_info["cli_available"] and PAPER_TRADING:
        print("   Initializing Kraken CLI paper trading...")
        paper_init = kraken_cli.init_paper(balance=10000)
        if paper_init["ok"]:
            print("   Kraken CLI paper account ready ($10,000)")
        else:
            print(f"   Paper init issue: {paper_init.get('error', 'unknown')} -- will use internal tracking")
    print()

    init_db()
    restore_position_from_db()
    round_num = 0

    while True:
        round_num += 1
        print(f"\n{'─'*20} Round {round_num} {'─'*20}")
        try:
            run_debate()
        except Exception as e:
            print(f"\n[ERROR] Round {round_num} failed: {e}")
            print("  Defaulting to HOLD — waiting for next round")
        print(f"\n⏳ Next round in {LOOP_INTERVAL}s...")
        time.sleep(LOOP_INTERVAL)

if __name__ == "__main__":
    run_loop()