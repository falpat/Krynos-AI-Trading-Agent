import streamlit as st
import sqlite3
import pandas as pd
import plotly.graph_objects as go
import plotly.express as px
from datetime import datetime, timedelta
import time
import random
import requests
import os
import re
import html as html_mod
import numpy as np
import krakenex
from dotenv import load_dotenv
from .kraken_cli import cli as kraken_cli

load_dotenv()
PRISM_API_KEY = os.environ.get("PRISM_API_KEY")

TRADING_FEE_PCT = 0.0026

# ── Manual trade helper ────────────────────────────────────────────────────────
def get_current_btc_price():
    """Fetch live BTC price — uses Kraken CLI if available, else krakenex."""
    result = kraken_cli.ticker("XBTUSD")
    if result.get("ok"):
        return {
            "last": result["last"],
            "ask": result["ask"],
            "bid": result["bid"],
            "source": result.get("source", "unknown"),
        }
    return {"last": 0, "ask": 0, "bid": 0, "error": result.get("error", "unknown")}

def get_position_from_db():
    """Get current position state from DB."""
    conn = get_db_connection()
    if not conn:
        return {"state": "UNKNOWN", "entry_price": 0, "size": 0}
    try:
        c = conn.cursor()
        c.execute("""
            SELECT action, price, trade_size
            FROM trades
            WHERE status IN ('executed', 'paper')
              AND action IN ('BUY', 'SELL')
            ORDER BY id DESC LIMIT 1
        """)
        row = c.fetchone()
        conn.close()
        if row and row["action"] == "BUY":
            return {"state": "LONG", "entry_price": row["price"], "size": row["trade_size"]}
        return {"state": "FLAT", "entry_price": 0, "size": 0}
    except Exception:
        return {"state": "UNKNOWN", "entry_price": 0, "size": 0}

def execute_manual_trade(action, price_data, trade_size_usd):
    """Execute a manual trade and log it to DB."""
    conn = get_db_connection()
    if not conn:
        return False, "Cannot connect to DB"
    try:
        fill_price = price_data["ask"] if action == "BUY" else price_data["bid"]
        if fill_price <= 0:
            return False, "Invalid price data"
        btc_amount = round(trade_size_usd / fill_price, 6)
        fee = round(fill_price * btc_amount * TRADING_FEE_PCT, 4)

        # Calculate PnL for SELL
        pnl = 0.0
        pos = get_position_from_db()
        if action == "SELL" and pos["state"] == "LONG":
            raw_pnl = (fill_price - pos["entry_price"]) * btc_amount
            pnl = round(raw_pnl - fee, 4)

        c = conn.cursor()
        c.execute("""
            INSERT INTO trades (
                timestamp, pair, action, winner, confidence,
                risk_score, reason, bull_argument, bear_argument,
                price, trade_size, paper, status, pnl,
                signal_score, signal_recommendation
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            datetime.now().isoformat(), "XBTUSD", action,
            "manual", 1.0, 0,
            f"Manual {action} from dashboard — ${trade_size_usd:.0f} USD",
            "Manual trade", "Manual trade",
            fill_price, btc_amount, 1, "paper", pnl,
            0, "MANUAL",
        ))
        conn.commit()
        conn.close()
        return True, f"{action} {btc_amount:.6f} BTC @ ${fill_price:,.2f} (fee: ${fee:.4f}, PnL: ${pnl:+.4f})"
    except Exception as e:
        return False, str(e)

# ── Page config ────────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Krynos AI — Trading Agent",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="collapsed"
)

# ── Dark trading terminal CSS ──────────────────────────────────────────────────
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@300;400;500;700&family=Syne:wght@400;600;700;800&display=swap');

:root {
    --bg-primary:    #080C14;
    --bg-secondary:  #0D1420;
    --bg-card:       #111827;
    --bg-hover:      #1a2235;
    --border:        #1e2d45;
    --border-bright: #2a3f5f;
    --green:         #00ff88;
    --green-dim:     #00cc6a;
    --green-glow:    rgba(0,255,136,0.15);
    --red:           #ff4466;
    --red-dim:       #cc2244;
    --red-glow:      rgba(255,68,102,0.15);
    --amber:         #ffaa00;
    --amber-glow:    rgba(255,170,0,0.15);
    --blue:          #4488ff;
    --blue-glow:     rgba(68,136,255,0.12);
    --text-primary:  #e8f0fe;
    --text-secondary:#8ba4c8;
    --text-dim:      #4a6080;
    --font-mono:     'JetBrains Mono', monospace;
    --font-display:  'Syne', sans-serif;
}

html, body, [class*="css"] {
    background-color: var(--bg-primary) !important;
    color: var(--text-primary) !important;
    font-family: var(--font-mono) !important;
}

.stApp { background: var(--bg-primary) !important; }

/* Hide streamlit chrome */
#MainMenu, footer, header { visibility: hidden; }
.block-container { padding: 1.5rem 2rem !important; max-width: 100% !important; }

/* Metric cards */
.metric-card {
    background: var(--bg-card);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 16px 20px;
    position: relative;
    overflow: hidden;
    transition: border-color 0.2s;
}
.metric-card::before {
    content: '';
    position: absolute;
    top: 0; left: 0; right: 0;
    height: 2px;
}
.metric-card.green::before { background: var(--green); box-shadow: 0 0 12px var(--green); }
.metric-card.red::before   { background: var(--red);   box-shadow: 0 0 12px var(--red); }
.metric-card.amber::before { background: var(--amber); box-shadow: 0 0 12px var(--amber); }
.metric-card.blue::before  { background: var(--blue);  box-shadow: 0 0 12px var(--blue); }

.metric-label {
    font-family: var(--font-mono);
    font-size: 10px;
    font-weight: 500;
    letter-spacing: 0.12em;
    text-transform: uppercase;
    color: var(--text-dim);
    margin-bottom: 6px;
}
.metric-value {
    font-family: var(--font-display);
    font-size: 28px;
    font-weight: 700;
    line-height: 1;
}
.metric-sub {
    font-size: 11px;
    color: var(--text-secondary);
    margin-top: 4px;
}
.green-text  { color: var(--green) !important; }
.red-text    { color: var(--red) !important; }
.amber-text  { color: var(--amber) !important; }
.blue-text   { color: var(--blue) !important; }

/* Section headers */
.section-header {
    font-family: var(--font-display);
    font-size: 11px;
    font-weight: 600;
    letter-spacing: 0.15em;
    text-transform: uppercase;
    color: var(--text-dim);
    border-bottom: 1px solid var(--border);
    padding-bottom: 8px;
    margin-bottom: 16px;
}

/* Debate cards */
.debate-card {
    background: var(--bg-card);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 16px;
    margin-bottom: 12px;
    font-size: 12px;
    line-height: 1.6;
}
.debate-card.buy   { border-left: 3px solid var(--green); }
.debate-card.sell  { border-left: 3px solid var(--red); }
.debate-card.hold  { border-left: 3px solid var(--amber); }

.debate-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 10px;
}
.debate-action {
    font-family: var(--font-display);
    font-size: 13px;
    font-weight: 700;
    letter-spacing: 0.05em;
}
.debate-meta {
    font-size: 10px;
    color: var(--text-dim);
    letter-spacing: 0.05em;
}

/* Pills */
.pill {
    display: inline-block;
    padding: 2px 10px;
    border-radius: 20px;
    font-size: 10px;
    font-weight: 600;
    letter-spacing: 0.08em;
    text-transform: uppercase;
}
.pill-green { background: var(--green-glow); color: var(--green); border: 1px solid rgba(0,255,136,0.3); }
.pill-red   { background: var(--red-glow);   color: var(--red);   border: 1px solid rgba(255,68,102,0.3); }
.pill-amber { background: var(--amber-glow); color: var(--amber); border: 1px solid rgba(255,170,0,0.3); }
.pill-blue  { background: var(--blue-glow);  color: var(--blue);  border: 1px solid rgba(68,136,255,0.3); }
.pill-gray  { background: rgba(138,164,200,0.1); color: var(--text-secondary); border: 1px solid rgba(138,164,200,0.2); }

/* Agent text */
.agent-label {
    font-size: 9px;
    letter-spacing: 0.15em;
    text-transform: uppercase;
    font-weight: 600;
    margin-bottom: 4px;
}
.bull-label { color: var(--green); }
.bear-label { color: var(--red); }
.agent-text {
    background: var(--bg-secondary);
    border-radius: 4px;
    padding: 8px 10px;
    font-size: 11px;
    color: var(--text-secondary);
    line-height: 1.5;
    border: 1px solid var(--border);
}

/* Table styling */
.trade-table {
    width: 100%;
    border-collapse: collapse;
    font-size: 11px;
}
.trade-table th {
    font-size: 9px;
    letter-spacing: 0.12em;
    text-transform: uppercase;
    color: var(--text-dim);
    padding: 8px 12px;
    border-bottom: 1px solid var(--border);
    text-align: left;
    font-weight: 500;
}
.trade-table td {
    padding: 9px 12px;
    border-bottom: 1px solid rgba(30,45,69,0.5);
    color: var(--text-secondary);
    font-family: var(--font-mono);
}
.trade-table tr:hover td { background: var(--bg-hover); }

/* Live indicator */
.live-dot {
    display: inline-block;
    width: 7px; height: 7px;
    background: var(--green);
    border-radius: 50%;
    margin-right: 6px;
    animation: pulse 2s infinite;
    box-shadow: 0 0 6px var(--green);
}
@keyframes pulse {
    0%, 100% { opacity: 1; transform: scale(1); }
    50%       { opacity: 0.5; transform: scale(0.85); }
}

/* Circuit breaker banner */
.circuit-banner {
    background: rgba(255,68,102,0.1);
    border: 1px solid rgba(255,68,102,0.4);
    border-radius: 8px;
    padding: 12px 20px;
    color: var(--red);
    font-size: 12px;
    font-weight: 600;
    letter-spacing: 0.05em;
    text-align: center;
    margin-bottom: 20px;
}

/* Risk meter */
.risk-bar-bg {
    background: var(--bg-secondary);
    border-radius: 4px;
    height: 6px;
    overflow: hidden;
    margin-top: 6px;
}
.risk-bar-fill {
    height: 100%;
    border-radius: 4px;
    transition: width 0.5s ease;
}

/* Top nav */
.topnav {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 0 0 20px 0;
    border-bottom: 1px solid var(--border);
    margin-bottom: 24px;
}
.logo {
    font-family: var(--font-display);
    font-size: 22px;
    font-weight: 800;
    letter-spacing: -0.02em;
}
.logo span { color: var(--green); }
.nav-status {
    font-size: 11px;
    color: var(--text-dim);
    letter-spacing: 0.05em;
}
</style>
""", unsafe_allow_html=True)

# ── DB helpers ─────────────────────────────────────────────────────────────────
def get_db_connection():
    try:
        conn = sqlite3.connect("krynos.db")
        conn.row_factory = sqlite3.Row
        return conn
    except Exception:
        return None

def get_trades_df():
    conn = get_db_connection()
    if not conn:
        return pd.DataFrame()
    try:
        df = pd.read_sql_query(
            "SELECT * FROM trades ORDER BY timestamp DESC LIMIT 200", conn)
        conn.close()
        if not df.empty:
            df["timestamp"] = pd.to_datetime(df["timestamp"])
        return df
    except Exception:
        return pd.DataFrame()

def get_daily_summary():
    conn = get_db_connection()
    if not conn:
        return pd.DataFrame()
    try:
        df = pd.read_sql_query(
            "SELECT * FROM daily_summary ORDER BY date DESC LIMIT 14", conn)
        conn.close()
        return df
    except Exception:
        return pd.DataFrame()

def get_stats(df):
    if df.empty:
        return {"total": 0, "buys": 0, "sells": 0, "holds": 0,
                "skipped": 0, "win_rate": 0, "avg_confidence": 0,
                "avg_risk": 0, "circuit_active": False, "profitable_trades": 0,
                "losing_trades": 0}
    executed = df[df["status"].isin(["executed", "paper"])]
    profitable = executed[executed["pnl"] > 0] if "pnl" in executed.columns else pd.DataFrame()
    losing = executed[executed["pnl"] < 0] if "pnl" in executed.columns else pd.DataFrame()
    total_executed = len(profitable) + len(losing)
    return {
        "total":             len(df),
        "buys":              len(df[df["action"] == "BUY"]),
        "sells":             len(df[df["action"] == "SELL"]),
        "holds":             len(df[df["action"] == "HOLD"]),
        "skipped":           len(df[df["status"].isin(["high_risk","low_confidence","draw"])]),
        "win_rate":          round(len(profitable) / max(total_executed, 1) * 100, 1),
        "profitable_trades": len(profitable),
        "losing_trades":     len(losing),
        "avg_confidence":    round(df["confidence"].mean() * 100, 1) if "confidence" in df else 0,
        "avg_risk":          round(df["risk_score"].mean(), 1) if "risk_score" in df else 0,
        "circuit_active":    False,
    }

# ── Demo data fallback ─────────────────────────────────────────────────────────
def generate_demo_data():
    rows = []
    base_price = 66500
    now = datetime.now()
    actions = ["BUY","SELL","HOLD","BUY","SELL","BUY","HOLD","SELL","BUY","BUY",
               "SELL","HOLD","BUY","SELL","BUY"]
    winners = ["bull","bear","draw","bull","bear","bull","draw","bear","bull","bull",
               "bear","draw","bull","bear","bull"]
    statuses = ["paper","paper","draw","paper","paper","paper","draw","high_risk",
                "paper","paper","low_confidence","draw","paper","paper","paper"]
    sig_scores = [25, -30, 5, 20, -20, 15, -5, -35, 30, 10,
                  -25, 0, 18, -22, 28]
    sig_recs   = ["BUY","SELL","HOLD","BUY","SELL","BUY","HOLD","SELL","BUY","HOLD",
                  "SELL","HOLD","BUY","SELL","BUY"]
    for i, (act, win, stat, ss, sr) in enumerate(zip(actions, winners, statuses, sig_scores, sig_recs)):
        rows.append({
            "id": i+1,
            "timestamp": now - timedelta(minutes=(len(actions)-i)*2),
            "pair": "XBTUSD",
            "action": act,
            "winner": win,
            "confidence": round(random.uniform(0.45, 0.92), 2),
            "risk_score": random.randint(2, 8),
            "reason": "Bear argument stronger — price below VWAP with declining volume" if win=="bear"
                      else "Bull case wins — price consolidating near support with uptick momentum"
                      if win=="bull" else "Draw — equally matched arguments, capital preserved",
            "bull_argument": "Current price at $66,507 is near 24h low of $66,200. VWAP at $67,150 signals mean reversion opportunity. Volume stabilizing with 50k trades. VERDICT: BUY",
            "bear_argument": "Price trading below 24h VWAP of $67,150 — bearish signal. Lower highs and lower lows in recent candles. Downward momentum persists. VERDICT: SELL",
            "price": base_price + random.uniform(-800, 800),
            "trade_size": 0.0013,
            "paper": 1,
            "status": stat,
            "pnl": round(random.uniform(-12, 28), 2),
            "signal_score": ss,
            "signal_recommendation": sr,
        })
    return pd.DataFrame(rows)

# ── Plotly theme ───────────────────────────────────────────────────────────────
PLOTLY_LAYOUT = dict(
    paper_bgcolor="rgba(0,0,0,0)",
    plot_bgcolor="rgba(13,20,32,0.8)",
    font=dict(family="JetBrains Mono", color="#8ba4c8", size=11),
    margin=dict(l=8, r=8, t=8, b=8),
    xaxis=dict(gridcolor="rgba(30,45,69,0.6)", linecolor="rgba(30,45,69,0.6)",
               tickfont=dict(size=10), showgrid=True),
    yaxis=dict(gridcolor="rgba(30,45,69,0.6)", linecolor="rgba(30,45,69,0.6)",
               tickfont=dict(size=10), showgrid=True),
)

# ── Main dashboard ─────────────────────────────────────────────────────────────
def main():
    # Auto-refresh
    refresh = st.sidebar.slider("Auto-refresh (sec)", 10, 120, 30)

    # Load data
    df = get_trades_df()
    using_demo = df.empty
    if using_demo:
        df = generate_demo_data()
        st.markdown("""<div style='background:rgba(255,170,0,0.08);border:1px solid rgba(255,170,0,0.3);
        border-radius:6px;padding:8px 16px;font-size:11px;color:#ffaa00;margin-bottom:16px;'>
        ⚡ Demo mode — run <code>python debate_agent.py</code> to see live data</div>""",
        unsafe_allow_html=True)

    stats = get_stats(df)

    # ── Top nav ──────────────────────────────────────────────────────────────
    st.markdown(f"""
    <div class="topnav">
        <div class="logo"><span>Krynos</span>
            <span style="font-size:11px;font-weight:400;color:#4a6080;margin-left:12px;
            letter-spacing:0.1em;">AI TRADING AGENT</span>
        </div>
        <div style="display:flex;align-items:center;gap:20px;">
            <div class="nav-status">
                <span class="live-dot"></span>
                {'PAPER TRADING' if df['paper'].iloc[0] == 1 else 'LIVE TRADING'}
            </div>
            <div class="nav-status">BTC/USD · KRAKEN</div>
            <div class="nav-status">{datetime.now().strftime('%H:%M:%S')}</div>
        </div>
    </div>""", unsafe_allow_html=True)

    # ── Circuit breaker ───────────────────────────────────────────────────────
    if stats["circuit_active"]:
        st.markdown('<div class="circuit-banner">🚨 CIRCUIT BREAKER ACTIVE — Daily loss limit reached. Trading paused.</div>',
                    unsafe_allow_html=True)

    # ── Top metrics ───────────────────────────────────────────────────────────
    c1, c2, c3, c4, c5, c6 = st.columns(6)
    latest_price = f"${df['price'].iloc[0]:,.2f}" if not df.empty else "—"
    total_pnl    = df["pnl"].sum() if "pnl" in df.columns else 0
    pnl_color    = "green" if total_pnl >= 0 else "red"
    pnl_sign     = "+" if total_pnl >= 0 else ""

    with c1:
        st.markdown(f"""<div class="metric-card blue">
            <div class="metric-label">BTC Price</div>
            <div class="metric-value blue-text">{latest_price}</div>
            <div class="metric-sub">Last fetched</div>
        </div>""", unsafe_allow_html=True)

    with c2:
        st.markdown(f"""<div class="metric-card {pnl_color}">
            <div class="metric-label">Total PnL</div>
            <div class="metric-value {'green-text' if total_pnl >= 0 else 'red-text'}">{pnl_sign}${total_pnl:.2f}</div>
            <div class="metric-sub">All rounds</div>
        </div>""", unsafe_allow_html=True)

    with c3:
        st.markdown(f"""<div class="metric-card green">
            <div class="metric-label">Rounds Run</div>
            <div class="metric-value green-text">{stats['total']}</div>
            <div class="metric-sub">{stats['buys']} buy · {stats['sells']} sell · {stats['holds']} hold</div>
        </div>""", unsafe_allow_html=True)

    with c4:
        wr = stats['win_rate']
        wr_color = "green" if wr >= 55 else "amber" if wr >= 45 else "red"
        st.markdown(f"""<div class="metric-card {wr_color}">
            <div class="metric-label">Win Rate</div>
            <div class="metric-value {wr_color}-text">{wr}%</div>
            <div class="metric-sub">{stats.get('profitable_trades',0)}W / {stats.get('losing_trades',0)}L · {stats['skipped']} skipped</div>
        </div>""", unsafe_allow_html=True)

    with c5:
        st.markdown(f"""<div class="metric-card {'green' if stats['avg_confidence'] >= 60 else 'amber'}">
            <div class="metric-label">Avg Confidence</div>
            <div class="metric-value {'green-text' if stats['avg_confidence'] >= 60 else 'amber-text'}">{stats['avg_confidence']}%</div>
            <div class="metric-sub">Across all rounds</div>
        </div>""", unsafe_allow_html=True)

    with c6:
        risk_color = "red" if stats['avg_risk'] >= 7 else "amber" if stats['avg_risk'] >= 4 else "green"
        st.markdown(f"""<div class="metric-card {risk_color}">
            <div class="metric-label">Avg Risk Score</div>
            <div class="metric-value {risk_color}-text">{stats['avg_risk']}/10</div>
            <div class="metric-sub">Lower = safer</div>
        </div>""", unsafe_allow_html=True)

    st.markdown("<div style='height:20px'></div>", unsafe_allow_html=True)

    # ── Manual Trading Controls ──────────────────────────────────────────
    pos = get_position_from_db()
    pos_color = "#00ff88" if pos["state"] == "LONG" else "#8ba4c8"
    pos_text = f"LONG {pos['size']:.6f} BTC @ ${pos['entry_price']:,.2f}" if pos["state"] == "LONG" else "FLAT — no open position"

    mt1, mt2, mt3, mt4 = st.columns([3, 1, 1, 1])
    with mt1:
        st.markdown(f"""<div style="background:#0d1b2a;border:1px solid #1e2d3d;border-radius:8px;padding:12px 16px;display:flex;align-items:center;gap:12px;">
            <div style="font-size:10px;color:#4a6080;letter-spacing:0.1em;">MANUAL TRADING</div>
            <div style="font-size:13px;font-weight:600;color:{pos_color};">{pos_text}</div>
        </div>""", unsafe_allow_html=True)
    with mt2:
        trade_usd = st.number_input("USD Amount", min_value=5.0, max_value=10000.0,
                                     value=20.0, step=5.0, format="%.0f", label_visibility="collapsed")
    with mt3:
        buy_clicked = st.button("🟢 BUY", key="manual_buy", type="primary",
                                disabled=(pos["state"] == "LONG"))
    with mt4:
        sell_clicked = st.button("🔴 SELL", key="manual_sell",
                                 disabled=(pos["state"] != "LONG"))

    if buy_clicked or sell_clicked:
        action = "BUY" if buy_clicked else "SELL"
        price_data = get_current_btc_price()
        if price_data.get("error"):
            st.error(f"Price fetch failed: {price_data['error']}")
        elif price_data["last"] <= 0:
            st.error("Could not get live price")
        else:
            success, msg = execute_manual_trade(action, price_data, trade_usd)
            if success:
                st.success(msg)
                time.sleep(1)
                st.rerun()
            else:
                st.error(msg)

    st.markdown("<div style='height:8px'></div>", unsafe_allow_html=True)

    # ── Charts row ────────────────────────────────────────────────────────────
    col_left, col_right = st.columns([3, 2])

    with col_left:
        st.markdown('<div class="section-header">Price + Trade Signals</div>', unsafe_allow_html=True)
        fig = go.Figure()
        df_sorted = df.sort_values("timestamp")

        fig.add_trace(go.Scatter(
            x=df_sorted["timestamp"], y=df_sorted["price"],
            mode="lines", name="BTC/USD",
            line=dict(color="#4488ff", width=1.5),
            fill="tozeroy", fillcolor="rgba(68,136,255,0.05)"
        ))
        buys  = df_sorted[df_sorted["action"] == "BUY"]
        sells = df_sorted[df_sorted["action"] == "SELL"]
        holds = df_sorted[df_sorted["action"] == "HOLD"]

        fig.add_trace(go.Scatter(
            x=buys["timestamp"], y=buys["price"],
            mode="markers", name="BUY",
            marker=dict(color="#00ff88", size=10, symbol="triangle-up",
                        line=dict(color="#00ff88", width=1))
        ))
        fig.add_trace(go.Scatter(
            x=sells["timestamp"], y=sells["price"],
            mode="markers", name="SELL",
            marker=dict(color="#ff4466", size=10, symbol="triangle-down",
                        line=dict(color="#ff4466", width=1))
        ))
        fig.add_trace(go.Scatter(
            x=holds["timestamp"], y=holds["price"],
            mode="markers", name="HOLD",
            marker=dict(color="#ffaa00", size=7, symbol="diamond",
                        line=dict(color="#ffaa00", width=1))
        ))
        fig.update_layout(**PLOTLY_LAYOUT, height=280,
                          legend=dict(orientation="h", y=1.08, x=0))
        st.plotly_chart(fig, width="stretch", config={"displayModeBar": False})

    with col_right:
        st.markdown('<div class="section-header">Decision Breakdown</div>', unsafe_allow_html=True)
        labels = ["BUY", "SELL", "HOLD", "Skipped"]
        values = [stats["buys"], stats["sells"], stats["holds"], stats["skipped"]]
        colors = ["#00ff88", "#ff4466", "#ffaa00", "#4a6080"]
        fig2 = go.Figure(go.Pie(
            labels=labels, values=values,
            hole=0.65,
            marker=dict(colors=colors, line=dict(color="#080C14", width=2)),
            textfont=dict(family="JetBrains Mono", size=11),
            hovertemplate="<b>%{label}</b><br>%{value} rounds<br>%{percent}<extra></extra>"
        ))
        fig2.add_annotation(
            text=f"<b>{stats['total']}</b><br><span style='font-size:10px'>rounds</span>",
            x=0.5, y=0.5, showarrow=False,
            font=dict(color="#e8f0fe", size=18, family="Syne")
        )
        fig2.update_layout(**PLOTLY_LAYOUT, height=280,
                           showlegend=True,
                           legend=dict(orientation="v", x=1.05, y=0.5))
        st.plotly_chart(fig2, width="stretch", config={"displayModeBar": False})

    # ── Risk + Confidence over time ───────────────────────────────────────────
    col_r1, col_r2 = st.columns(2)

    with col_r1:
        st.markdown('<div class="section-header">Risk Score Over Time</div>', unsafe_allow_html=True)
        df_s = df.sort_values("timestamp")
        fig3 = go.Figure()
        fig3.add_hrect(y0=7, y1=10, fillcolor="rgba(255,68,102,0.06)",
                       line_width=0, annotation_text="HIGH RISK ZONE",
                       annotation_font=dict(color="#ff4466", size=9))
        fig3.add_trace(go.Scatter(
            x=df_s["timestamp"], y=df_s["risk_score"],
            mode="lines+markers", name="Risk Score",
            line=dict(color="#ffaa00", width=1.5),
            marker=dict(size=5, color=df_s["risk_score"].apply(
                lambda x: "#ff4466" if x >= 7 else "#ffaa00" if x >= 4 else "#00ff88"
            ))
        ))
        fig3.add_hline(y=7, line_dash="dot", line_color="rgba(255,68,102,0.4)",
                       annotation_text="Skip threshold",
                       annotation_font=dict(color="#ff4466", size=9))
        fig3.update_layout(**PLOTLY_LAYOUT, height=200)
        fig3.update_layout(yaxis=dict(**PLOTLY_LAYOUT["yaxis"], range=[0, 10]))
        st.plotly_chart(fig3, width="stretch", config={"displayModeBar": False})

    with col_r2:
        st.markdown('<div class="section-header">Confidence Over Time</div>', unsafe_allow_html=True)
        fig4 = go.Figure()
        fig4.add_trace(go.Scatter(
            x=df_s["timestamp"], y=df_s["confidence"] * 100,
            mode="lines", name="Confidence",
            line=dict(color="#00ff88", width=1.5),
            fill="tozeroy", fillcolor="rgba(0,255,136,0.04)"
        ))
        fig4.add_hline(y=50, line_dash="dot", line_color="rgba(255,170,0,0.4)",
                       annotation_text="Min threshold",
                       annotation_font=dict(color="#ffaa00", size=9))
        fig4.update_layout(**PLOTLY_LAYOUT, height=200)
        fig4.update_layout(yaxis=dict(**PLOTLY_LAYOUT["yaxis"], range=[0, 100],
                                      ticksuffix="%"))
        st.plotly_chart(fig4, width="stretch", config={"displayModeBar": False})

    # ── Live debate log ───────────────────────────────────────────────────────
    st.markdown('<div class="section-header" style="margin-top:8px">Live Debate Log</div>',
                unsafe_allow_html=True)

    debate_col, table_col = st.columns([2, 3])

    with debate_col:
        recent = df.head(4)
        all_debate_html = ""
        for _, row in recent.iterrows():
            action     = row["action"]
            card_class = action.lower()
            action_color = {"BUY": "green-text", "SELL": "red-text", "HOLD": "amber-text"}.get(action, "")
            winner_pill  = {
                "bull": '<span class="pill pill-green">BULL WON</span>',
                "bear": '<span class="pill pill-red">BEAR WON</span>',
                "draw": '<span class="pill pill-amber">DRAW</span>',
            }.get(row["winner"], "")
            status_pill = {
                "paper":           '<span class="pill pill-blue">PAPER</span>',
                "executed":        '<span class="pill pill-green">LIVE</span>',
                "high_risk":       '<span class="pill pill-red">SKIPPED · HIGH RISK</span>',
                "low_confidence":  '<span class="pill pill-amber">SKIPPED · LOW CONF</span>',
                "draw":            '<span class="pill pill-gray">SKIPPED · DRAW</span>',
            }.get(row["status"], '<span class="pill pill-gray">SKIPPED</span>')

            ts = row["timestamp"].strftime("%H:%M:%S") if hasattr(row["timestamp"], "strftime") else str(row["timestamp"])[:19]
            bull_text = str(row.get("bull_argument",""))[-200:].strip()
            bear_text = str(row.get("bear_argument",""))[-200:].strip()
            reason    = str(row.get("reason",""))[:140]
            conf      = int(row["confidence"] * 100) if row["confidence"] <= 1 else int(row["confidence"])
            risk      = int(row["risk_score"])
            sig_rec_card = row.get("signal_recommendation", "—")
            sig_sc_card  = row.get("signal_score", 0)
            sig_card_color = "#00ff88" if sig_rec_card == "BUY" else "#ff4466" if sig_rec_card == "SELL" else "#ffaa00"
            override_flag = ""
            if sig_rec_card in ("BUY", "SELL", "HOLD") and action in ("BUY", "SELL") and sig_rec_card != action:
                override_flag = '<span class="pill pill-amber" style="margin-left:6px;font-size:9px;">OVERRIDE</span>'

            debate_html = f'<div class="debate-card {card_class}">'
            debate_html += f'<div class="debate-header"><div class="debate-action {action_color}">{action} &nbsp; {winner_pill}{override_flag}</div>'
            debate_html += f'<div class="debate-meta"><span style="color:{sig_card_color};">Signal: {sig_rec_card} ({sig_sc_card:+d})</span> &nbsp; {ts} &nbsp; {status_pill}</div></div>'
            debate_html += f'<div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:10px;"><div><div class="agent-label bull-label">Bull argument</div><div class="agent-text">...{bull_text}</div></div><div><div class="agent-label bear-label">Bear argument</div><div class="agent-text">...{bear_text}</div></div></div>'
            debate_html += f'<div style="font-size:11px;color:#8ba4c8;margin-bottom:8px;"><span style="color:#4a6080;">Judge: </span>{reason}</div>'
            debate_html += f'<div style="display:flex;gap:16px;align-items:center;"><div style="flex:1;"><div style="font-size:9px;color:#4a6080;letter-spacing:0.1em;margin-bottom:3px;">CONFIDENCE</div><div class="risk-bar-bg"><div class="risk-bar-fill" style="width:{conf}%;background:{"#00ff88" if conf>=60 else "#ffaa00"};"></div></div><div style="font-size:10px;color:#8ba4c8;margin-top:2px;">{conf}%</div></div>'
            debate_html += f'<div style="flex:1;"><div style="font-size:9px;color:#4a6080;letter-spacing:0.1em;margin-bottom:3px;">RISK SCORE</div><div class="risk-bar-bg"><div class="risk-bar-fill" style="width:{risk*10}%;background:{"#ff4466" if risk>=7 else "#ffaa00" if risk>=4 else "#00ff88"};"></div></div><div style="font-size:10px;color:#8ba4c8;margin-top:2px;">{risk}/10</div></div></div></div>'
            all_debate_html += debate_html
        st.markdown(f'<div style="max-height:520px;overflow-y:auto;">{all_debate_html}</div>', unsafe_allow_html=True)

    with table_col:
        st.markdown('<div class="section-header">All Rounds</div>', unsafe_allow_html=True)

        rows_html = ""
        for _, row in df.head(20).iterrows():
            action = row["action"]
            a_color = {"BUY": "#00ff88", "SELL": "#ff4466", "HOLD": "#ffaa00"}.get(action, "#8ba4c8")
            w_pill  = {
                "bull": f'<span class="pill pill-green">bull</span>',
                "bear": f'<span class="pill pill-red">bear</span>',
                "draw": f'<span class="pill pill-amber">draw</span>',
            }.get(row["winner"], "—")
            stat = str(row["status"])
            s_color = {"paper": "#4488ff", "executed": "#00ff88",
                       "high_risk": "#ff4466", "low_confidence": "#ffaa00",
                       "draw": "#4a6080"}.get(stat, "#4a6080")
            ts = row["timestamp"].strftime("%m/%d %H:%M") if hasattr(row["timestamp"], "strftime") else str(row["timestamp"])[:16]
            price = f"${float(row['price']):,.0f}"
            conf  = f"{int(row['confidence']*100 if row['confidence']<=1 else row['confidence'])}%"
            risk  = f"{int(row['risk_score'])}/10"
            pnl   = row.get("pnl", 0)
            pnl_html = f'<span style="color:{"#00ff88" if pnl>=0 else "#ff4466"}">{"+" if pnl>=0 else ""}{pnl:.2f}</span>'
            sig_rec = row.get("signal_recommendation", "—")
            sig_sc  = row.get("signal_score", 0)
            sig_color = "#00ff88" if sig_rec == "BUY" else "#ff4466" if sig_rec == "SELL" else "#ffaa00"
            # Highlight overrides where judge disagrees with signal
            override = ""
            if sig_rec in ("BUY", "SELL", "HOLD") and action in ("BUY", "SELL") and sig_rec != action:
                override = ' style="background:rgba(255,170,0,0.1);"'

            rows_html += f'<tr{override}><td style="color:#4a6080;">{ts}</td><td style="color:{a_color};font-weight:600;">{action}</td><td><span style="color:{sig_color};font-weight:500;">{sig_rec}</span><span style="color:#4a6080;font-size:9px;"> {sig_sc:+d}</span></td><td>{w_pill}</td><td>{price}</td><td>{conf}</td><td>{risk}</td><td>{pnl_html}</td><td style="color:{s_color};font-size:10px;">{stat}</td></tr>'

        st.markdown(f'<div style="background:#0d1b2a;border:1px solid #1e2d3d;border-radius:8px;overflow:hidden;max-height:520px;overflow-y:auto;"><table class="trade-table"><thead><tr><th>Time</th><th>Action</th><th>Signal</th><th>Winner</th><th>Price</th><th>Confidence</th><th>Risk</th><th>PnL</th><th>Status</th></tr></thead><tbody>{rows_html}</tbody></table></div>', unsafe_allow_html=True)

    # ── PRISM Market Intelligence Panel ───────────────────────────────────────
    st.markdown('<div class="section-header" style="margin-top:16px">PRISM Market Intelligence</div>',
                unsafe_allow_html=True)

    prism_data = {}
    if PRISM_API_KEY:
        try:
            headers = {"X-API-Key": PRISM_API_KEY}
            # BTC signals
            sig_r = requests.get("https://api.prismapi.ai/signals/BTC", headers=headers, timeout=5)
            if sig_r.status_code == 200:
                sig_json = sig_r.json()
                if "data" in sig_json and sig_json["data"]:
                    prism_data["BTC"] = sig_json["data"][0]
            # ETH + SOL for cross-crypto
            for sym in ["ETH", "SOL"]:
                try:
                    sr = requests.get(f"https://api.prismapi.ai/signals/{sym}", headers=headers, timeout=5)
                    if sr.status_code == 200:
                        sj = sr.json()
                        if "data" in sj and sj["data"]:
                            prism_data[sym] = sj["data"][0]
                except Exception:
                    pass
            # Resolve BTC for venue info
            try:
                rv = requests.get("https://api.prismapi.ai/resolve/BTC", headers=headers, timeout=5)
                if rv.status_code == 200:
                    prism_data["_resolve"] = rv.json()
            except Exception:
                pass
        except Exception:
            pass

    if prism_data and "BTC" in prism_data:
        btc = prism_data["BTC"]
        sig_label = btc.get("overall_signal", "N/A").replace("_", " ").upper()
        direction = btc.get("direction", "neutral")
        net_score = btc.get("net_score", 0)
        sig_color = "#00ff88" if direction == "bullish" else "#ff4466" if direction == "bearish" else "#ffaa00"
        prism_px = btc.get("current_price", 0)
        prism_rsi = btc.get("indicators", {}).get("rsi", "N/A")
        prism_macd_h = btc.get("indicators", {}).get("macd_histogram", "N/A")
        active_sigs = btc.get("active_signals", [])

        # Cross-crypto data
        directions = [direction]
        for sym in ["ETH", "SOL"]:
            if sym in prism_data:
                directions.append(prism_data[sym].get("direction", "neutral"))
        bull_ct = sum(1 for d in directions if d == "bullish")
        bear_ct = sum(1 for d in directions if d == "bearish")
        consensus = "BULLISH" if bull_ct >= 2 else "BEARISH" if bear_ct >= 2 else "MIXED"
        consensus_color = "#00ff88" if consensus == "BULLISH" else "#ff4466" if consensus == "BEARISH" else "#ffaa00"

        prism_col1, prism_col2, prism_col3 = st.columns([2, 2, 3])

        with prism_col1:
            active_html = "".join(
                f'<div style="color:#4488ff;font-size:10px;">⚡ {s.get("type","")}: {s.get("signal","")}</div>'
                for s in active_sigs
            )
            st.markdown(f'<div style="background:#0d1b2a;border:1px solid #1e2d3d;border-radius:8px;padding:16px;"><div style="font-size:10px;color:#4a6080;letter-spacing:0.1em;margin-bottom:8px;">BTC PRISM SIGNAL</div><div style="font-size:20px;font-weight:700;color:{sig_color};margin-bottom:4px;">{sig_label}</div><div style="display:grid;grid-template-columns:1fr 1fr;gap:4px;font-size:11px;margin-bottom:8px;"><div style="color:#8ba4c8;">Net Score</div><div style="color:{sig_color};font-weight:600;">{net_score}</div><div style="color:#8ba4c8;">PRISM Price</div><div style="color:#e0e6ed;">${prism_px:,.2f}</div><div style="color:#8ba4c8;">RSI</div><div style="color:#e0e6ed;">{prism_rsi}</div><div style="color:#8ba4c8;">MACD Hist</div><div style="color:#e0e6ed;">{prism_macd_h}</div></div>{active_html}</div>', unsafe_allow_html=True)

        with prism_col2:
            crypto_rows = ""
            for sym in ["ETH", "SOL"]:
                if sym in prism_data:
                    sd = prism_data[sym]
                    d = sd.get("direction", "?")
                    s = sd.get("overall_signal", "?").replace("_", " ")
                    px_val = sd.get("current_price", 0)
                    ns = sd.get("net_score", 0)
                    dc = "#00ff88" if d == "bullish" else "#ff4466" if d == "bearish" else "#ffaa00"
                    asigs = ", ".join(a.get("type", "") for a in sd.get("active_signals", []))
                    crypto_rows += f'<div style="display:grid;grid-template-columns:50px 1fr 60px;gap:4px;font-size:11px;padding:4px 0;border-bottom:1px solid #1e2d3d;"><div style="font-weight:600;">{sym}</div><div style="color:{dc};">{s} <span style="color:#4a6080;font-size:9px;">{asigs}</span></div><div style="color:#8ba4c8;text-align:right;">${px_val:,.2f}</div></div>'
            st.markdown(f'<div style="background:#0d1b2a;border:1px solid #1e2d3d;border-radius:8px;padding:16px;"><div style="font-size:10px;color:#4a6080;letter-spacing:0.1em;margin-bottom:8px;">CROSS-CRYPTO SIGNALS</div>{crypto_rows}<div style="margin-top:10px;font-size:12px;"><span style="color:#8ba4c8;">Market Consensus:</span> <span style="color:{consensus_color};font-weight:700;font-size:14px;margin-left:4px;">{consensus}</span> <span style="color:#4a6080;font-size:10px;margin-left:8px;">({bull_ct}↑ {bear_ct}↓ of 3)</span></div></div>', unsafe_allow_html=True)

        with prism_col3:
            # Venue info from resolve
            venue_html = ""
            resolve = prism_data.get("_resolve", {})
            venues = resolve.get("venues", {}).get("data", [])
            if venues:
                venue_rows = ""
                for v in venues[:6]:
                    vtype = v.get("type", "").replace("cex_", "CEX ").replace("dex_", "DEX ").replace("_", " ").title()
                    comm = v.get("commission") or "—"
                    lev = v.get("leverage") or "—"
                    venue_rows += f'<div style="display:grid;grid-template-columns:1fr 80px 60px 50px;gap:2px;font-size:10px;padding:3px 0;border-bottom:1px solid #1e2d3d;"><div style="font-weight:500;">{v.get("name","?")}</div><div style="color:#4a6080;">{vtype}</div><div style="color:#8ba4c8;">{comm}</div><div style="color:#4488ff;">{lev}</div></div>'
                venue_html = f'<div style="font-size:10px;color:#4a6080;letter-spacing:0.1em;margin-bottom:6px;">RESOLVED VENUES (via PRISM)</div><div style="display:grid;grid-template-columns:1fr 80px 60px 50px;gap:2px;font-size:9px;padding:2px 0;color:#4a6080;"><div>Exchange</div><div>Type</div><div>Fee</div><div>Leverage</div></div>{venue_rows}'

            resolved_sym = resolve.get("symbol", "BTC")
            resolved_name = resolve.get("name", "Bitcoin")
            ts = btc.get("timestamp", "?")
            if isinstance(ts, str) and len(ts) > 19:
                ts = ts[:19]
            st.markdown(f'<div style="background:#0d1b2a;border:1px solid #1e2d3d;border-radius:8px;padding:16px;"><div style="font-size:10px;color:#4a6080;letter-spacing:0.1em;margin-bottom:8px;">PRISM ASSET RESOLUTION</div><div style="font-size:11px;margin-bottom:8px;"><span style="color:#8ba4c8;">Resolved:</span> <span style="font-weight:600;color:#e0e6ed;">{resolved_sym}</span> <span style="color:#4a6080;">({resolved_name} · crypto)</span> <span style="color:#4488ff;font-size:9px;margin-left:8px;">BTC = XBT = bitcoin → {resolved_sym}</span></div>{venue_html}<div style="margin-top:6px;font-size:9px;color:#4a6080;">Last signal: {ts}</div></div>', unsafe_allow_html=True)
    else:
        st.markdown('<div style="background:#0d1b2a;border:1px solid #1e2d3d;border-radius:8px;padding:24px;text-align:center;"><div style="color:#4a6080;font-size:12px;">PRISM API not configured — set PRISM_API_KEY in .env</div></div>', unsafe_allow_html=True)

    # ── Live Crypto News Headlines ────────────────────────────────────────────
    st.markdown('<div class="section-header" style="margin-top:16px">Live Crypto News</div>',
                unsafe_allow_html=True)

    news_items = []
    try:
        news_r = requests.get(
            "https://api.rss2json.com/v1/api.json?rss_url=https://cointelegraph.com/rss",
            timeout=5
        )
        if news_r.status_code == 200:
            news_items = news_r.json().get("items", [])[:8]
    except Exception:
        pass

    if news_items:
        news_html = ""
        for item in news_items:
            # Strip HTML tags and unescape entities from RSS fields
            raw_title = item.get("title", "")
            title = html_mod.unescape(re.sub(r'<[^>]+>', '', raw_title)).strip()
            raw_author = item.get("author", "CoinTelegraph")
            author = html_mod.unescape(re.sub(r'<[^>]+>', '', raw_author)).strip() or "CoinTelegraph"
            link = item.get("link", "#")
            pub = item.get("pubDate", "")
            # Extract just the time portion
            pub_short = pub[11:16] if len(pub) > 16 else pub[:10]

            # Simple sentiment coloring based on keywords
            title_lower = title.lower()
            if any(w in title_lower for w in ["surge", "rally", "bull", "soar", "gain", "rise", "up", "high", "record", "adoption"]):
                dot_color = "#00ff88"
            elif any(w in title_lower for w in ["crash", "bear", "drop", "fall", "loss", "down", "low", "fear", "sell", "hack", "fraud"]):
                dot_color = "#ff4466"
            else:
                dot_color = "#4488ff"

            news_html += f'<div style="display:grid;grid-template-columns:8px 1fr 60px;gap:10px;padding:8px 0;border-bottom:1px solid #1e2d3d;align-items:start;"><div style="width:6px;height:6px;border-radius:50%;background:{dot_color};margin-top:5px;"></div><div><a href="{link}" target="_blank" style="color:#e0e6ed;text-decoration:none;font-size:12px;font-weight:500;line-height:1.4;">{title}</a><div style="color:#4a6080;font-size:9px;margin-top:2px;">{author}</div></div><div style="color:#4a6080;font-size:10px;text-align:right;white-space:nowrap;">{pub_short}</div></div>'

        st.markdown(f'<div style="background:#0d1b2a;border:1px solid #1e2d3d;border-radius:8px;padding:16px;max-height:400px;overflow-y:auto;"><div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;"><div style="font-size:10px;color:#4a6080;letter-spacing:0.1em;">COINTELEGRAPH · LIVE FEED</div><div style="font-size:9px;color:#4a6080;">🟢 bullish  🔴 bearish  🔵 neutral</div></div>{news_html}</div>', unsafe_allow_html=True)
    else:
        st.markdown('<div style="background:#0d1b2a;border:1px solid #1e2d3d;border-radius:8px;padding:24px;text-align:center;"><div style="color:#4a6080;font-size:12px;">Unable to fetch news — check internet connection</div></div>', unsafe_allow_html=True)

    # ── Performance Metrics (Sharpe, Drawdown, Win Rate) ──────────────────────
    executed_trades = df[df["action"].isin(["BUY", "SELL"])].copy()
    if len(executed_trades) >= 3 and "pnl" in executed_trades.columns:
        st.markdown('<div class="section-header" style="margin-top:16px">Performance Metrics</div>',
                    unsafe_allow_html=True)

        pnl_series = executed_trades["pnl"].fillna(0).astype(float)
        returns = pnl_series.tolist()

        # Win rate
        wins = sum(1 for r in returns if r > 0)
        losses_count = sum(1 for r in returns if r < 0)
        total_trades = len(returns)
        win_rate = (wins / total_trades * 100) if total_trades > 0 else 0

        # Average win / average loss
        avg_win = sum(r for r in returns if r > 0) / wins if wins > 0 else 0
        avg_loss = sum(r for r in returns if r < 0) / losses_count if losses_count > 0 else 0
        profit_factor = abs(sum(r for r in returns if r > 0) / sum(r for r in returns if r < 0)) if sum(r for r in returns if r < 0) != 0 else float('inf')

        # Sharpe ratio (annualized, assuming 1h intervals = 8760 periods/year)
        returns_arr = np.array(returns)
        mean_ret = np.mean(returns_arr)
        std_ret = np.std(returns_arr)
        sharpe = (mean_ret / std_ret) * np.sqrt(8760) if std_ret > 0 else 0
        sharpe = round(sharpe, 2)

        # Max drawdown
        cumulative = np.cumsum(returns_arr)
        running_max = np.maximum.accumulate(cumulative)
        drawdowns = cumulative - running_max
        max_dd = round(float(np.min(drawdowns)), 4)
        max_dd_pct = round(max_dd / 1000 * 100, 2) if max_dd != 0 else 0  # % of starting $1000

        # Sharpe color
        sharpe_color = "#00ff88" if sharpe > 1.0 else "#ffaa00" if sharpe > 0 else "#ff4466"
        dd_color = "#00ff88" if max_dd > -0.5 else "#ffaa00" if max_dd > -2 else "#ff4466"
        wr_color = "#00ff88" if win_rate > 55 else "#ffaa00" if win_rate > 45 else "#ff4466"

        pm1, pm2, pm3, pm4 = st.columns(4)
        with pm1:
            st.markdown(f'<div style="background:#0d1b2a;border:1px solid #1e2d3d;border-radius:8px;padding:16px;text-align:center;"><div style="font-size:10px;color:#4a6080;letter-spacing:0.1em;margin-bottom:6px;">SHARPE RATIO</div><div style="font-size:24px;font-weight:700;color:{sharpe_color};">{sharpe}</div><div style="font-size:9px;color:#4a6080;margin-top:4px;">Annualized · {"Good" if sharpe > 1 else "Acceptable" if sharpe > 0 else "Negative"}</div></div>', unsafe_allow_html=True)
        with pm2:
            st.markdown(f'<div style="background:#0d1b2a;border:1px solid #1e2d3d;border-radius:8px;padding:16px;text-align:center;"><div style="font-size:10px;color:#4a6080;letter-spacing:0.1em;margin-bottom:6px;">MAX DRAWDOWN</div><div style="font-size:24px;font-weight:700;color:{dd_color};">${max_dd:+.2f}</div><div style="font-size:9px;color:#4a6080;margin-top:4px;">{max_dd_pct:+.2f}% of portfolio</div></div>', unsafe_allow_html=True)
        with pm3:
            st.markdown(f'<div style="background:#0d1b2a;border:1px solid #1e2d3d;border-radius:8px;padding:16px;text-align:center;"><div style="font-size:10px;color:#4a6080;letter-spacing:0.1em;margin-bottom:6px;">WIN RATE</div><div style="font-size:24px;font-weight:700;color:{wr_color};">{win_rate:.0f}%</div><div style="font-size:9px;color:#4a6080;margin-top:4px;">{wins}W / {losses_count}L of {total_trades}</div></div>', unsafe_allow_html=True)
        with pm4:
            pf_display = f"{profit_factor:.2f}" if profit_factor != float('inf') else "∞"
            pf_color = "#00ff88" if profit_factor > 1.5 else "#ffaa00" if profit_factor > 1 else "#ff4466"
            st.markdown(f'<div style="background:#0d1b2a;border:1px solid #1e2d3d;border-radius:8px;padding:16px;text-align:center;"><div style="font-size:10px;color:#4a6080;letter-spacing:0.1em;margin-bottom:6px;">PROFIT FACTOR</div><div style="font-size:24px;font-weight:700;color:{pf_color};">{pf_display}</div><div style="font-size:9px;color:#4a6080;margin-top:4px;">Avg Win ${avg_win:+.4f} · Avg Loss ${avg_loss:+.4f}</div></div>', unsafe_allow_html=True)

    # ── Signal vs Judge Bias Analysis ─────────────────────────────────────────
    has_signal = "signal_recommendation" in df.columns and df["signal_recommendation"].notna().any()
    executed = df[df["action"].isin(["BUY", "SELL"])]
    if has_signal and len(executed) >= 5:
        st.markdown('<div class="section-header" style="margin-top:16px">Signal vs Judge Bias Analysis</div>',
                    unsafe_allow_html=True)

        sig = executed["signal_recommendation"].fillna("HOLD")
        act = executed["action"]

        aligned_buy   = ((sig == "BUY")  & (act == "BUY")).sum()
        aligned_sell  = ((sig == "SELL") & (act == "SELL")).sum()
        override_sell = ((sig == "BUY")  & (act == "SELL")).sum()
        override_buy  = ((sig == "SELL") & (act == "BUY")).sum()
        aggr_sell     = ((sig == "HOLD") & (act == "SELL")).sum()
        aggr_buy      = ((sig == "HOLD") & (act == "BUY")).sum()

        total_exec = len(executed)
        total_sells = (act == "SELL").sum()
        total_buys  = (act == "BUY").sum()
        total_overrides = override_sell + override_buy + aggr_sell + aggr_buy
        override_pct = total_overrides / max(total_exec, 1) * 100

        # PnL by category
        def cat_pnl(mask):
            subset = executed[mask]
            return subset["pnl"].sum() if "pnl" in subset.columns and len(subset) > 0 else 0

        pnl_aligned_buy   = cat_pnl((sig == "BUY")  & (act == "BUY"))
        pnl_aligned_sell  = cat_pnl((sig == "SELL") & (act == "SELL"))
        pnl_override_sell = cat_pnl((sig == "BUY")  & (act == "SELL"))
        pnl_override_buy  = cat_pnl((sig == "SELL") & (act == "BUY"))
        pnl_aggr_sell     = cat_pnl((sig == "HOLD") & (act == "SELL"))
        pnl_aggr_buy      = cat_pnl((sig == "HOLD") & (act == "BUY"))

        # Determine diagnosis
        if total_sells > total_buys * 1.5:
            legit_sells = aligned_sell
            biased_sells = override_sell + aggr_sell
            if biased_sells > legit_sells:
                diag_icon = "\U0001f534"  # red circle
                diag_text = f"BIAS PROBLEM: {biased_sells} sells are overrides vs {legit_sells} legitimate. Judge has a SELL bias."
                diag_color = "#ff4466"
            else:
                diag_icon = "\U0001f7e1"  # yellow circle
                diag_text = f"MARKET PROBLEM: {legit_sells} sells are signal-aligned. Indicators are genuinely bearish."
                diag_color = "#ffaa00"
        elif total_buys > total_sells * 1.5:
            legit_buys = aligned_buy
            biased_buys = override_buy + aggr_buy
            if biased_buys > legit_buys:
                diag_icon = "\U0001f534"
                diag_text = f"BIAS PROBLEM: {biased_buys} buys are overrides vs {legit_buys} legitimate. Judge has a BUY bias."
                diag_color = "#ff4466"
            else:
                diag_icon = "\U0001f7e2"  # green circle
                diag_text = f"MARKET ALIGNED: {legit_buys} buys are signal-aligned. Legit bull market."
                diag_color = "#00ff88"
        else:
            diag_icon = "\U0001f7e2"
            diag_text = f"BALANCED: {total_buys}B / {total_sells}S ratio is healthy."
            diag_color = "#00ff88"
            if override_pct > 40:
                diag_icon = "\U0001f7e1"
                diag_text += f" But override rate is {override_pct:.0f}% — judge often disagrees."
                diag_color = "#ffaa00"

        bias_col1, bias_col2, bias_col3 = st.columns([2, 2, 3])

        with bias_col1:
            st.markdown(f'<div style="background:#0d1b2a;border:1px solid #1e2d3d;border-radius:8px;padding:16px;"><div style="font-size:10px;color:#4a6080;letter-spacing:0.1em;margin-bottom:12px;">SIGNAL ALIGNMENT</div><div style="display:grid;grid-template-columns:1fr 1fr;gap:6px;font-size:11px;"><div style="color:#8ba4c8;">Aligned BUY</div><div style="color:#00ff88;font-weight:600;">{aligned_buy} <span style="color:#4a6080;font-weight:400;">(${pnl_aligned_buy:+.2f})</span></div><div style="color:#8ba4c8;">Aligned SELL</div><div style="color:#ff4466;font-weight:600;">{aligned_sell} <span style="color:#4a6080;font-weight:400;">(${pnl_aligned_sell:+.2f})</span></div></div></div>', unsafe_allow_html=True)

        with bias_col2:
            st.markdown(f'<div style="background:#0d1b2a;border:1px solid #1e2d3d;border-radius:8px;padding:16px;"><div style="font-size:10px;color:#4a6080;letter-spacing:0.1em;margin-bottom:12px;">OVERRIDES & AGGRESSIVE</div><div style="display:grid;grid-template-columns:1fr 1fr;gap:6px;font-size:11px;"><div style="color:#ffaa00;">Sig BUY → Judge SELL</div><div style="color:#ff4466;font-weight:600;">{override_sell} <span style="color:#4a6080;font-weight:400;">(${pnl_override_sell:+.2f})</span></div><div style="color:#ffaa00;">Sig SELL → Judge BUY</div><div style="color:#00ff88;font-weight:600;">{override_buy} <span style="color:#4a6080;font-weight:400;">(${pnl_override_buy:+.2f})</span></div><div style="color:#ffaa00;">Sig HOLD → Judge SELL</div><div style="color:#ff4466;font-weight:600;">{aggr_sell} <span style="color:#4a6080;font-weight:400;">(${pnl_aggr_sell:+.2f})</span></div><div style="color:#ffaa00;">Sig HOLD → Judge BUY</div><div style="color:#00ff88;font-weight:600;">{aggr_buy} <span style="color:#4a6080;font-weight:400;">(${pnl_aggr_buy:+.2f})</span></div></div></div>', unsafe_allow_html=True)

        with bias_col3:
            or_color = '#ff4466' if override_pct > 40 else '#ffaa00' if override_pct > 20 else '#00ff88'
            st.markdown(f'<div style="background:#0d1b2a;border:1px solid #1e2d3d;border-radius:8px;padding:16px;"><div style="font-size:10px;color:#4a6080;letter-spacing:0.1em;margin-bottom:12px;">DIAGNOSIS</div><div style="font-size:13px;font-weight:600;color:{diag_color};margin-bottom:8px;">{diag_icon} {diag_text}</div><div style="display:grid;grid-template-columns:1fr 1fr;gap:4px;font-size:10px;color:#8ba4c8;"><div>Override rate:</div><div style="color:{or_color};">{override_pct:.0f}% ({total_overrides}/{total_exec})</div><div>Total trades:</div><div>{total_buys} BUY / {total_sells} SELL</div></div></div>', unsafe_allow_html=True)
    # ── Portfolio Simulator ─────────────────────────────────────────────────
    st.markdown('<div class="section-header" style="margin-top:16px">Portfolio Simulator</div>',
                unsafe_allow_html=True)
    st.markdown('<div style="font-size:11px;color:#8ba4c8;margin-bottom:12px;">'
                'Replay recorded trades with your own starting capital and trade size to see projected performance.</div>',
                unsafe_allow_html=True)

    sim_col1, sim_col2, sim_col3 = st.columns([1, 1, 3])
    with sim_col1:
        sim_capital = st.number_input("Starting Capital (USD)", min_value=100.0, max_value=1_000_000.0,
                                      value=1000.0, step=100.0, format="%.0f")
    with sim_col2:
        sim_trade_pct = st.slider("Trade Size (% of portfolio)", min_value=1, max_value=20, value=2,
                                  help="Percentage of portfolio value risked per trade")

    # Run simulation on executed trades
    sim_trades = df[df["status"].isin(["executed", "paper"])].sort_values("timestamp").copy()
    if len(sim_trades) >= 1:
        sim_usd = float(sim_capital)
        sim_btc = 0.0
        sim_entry_price = 0.0
        sim_fee_pct = 0.0026
        sim_history = []
        sim_total_fees = 0.0
        sim_wins = 0
        sim_losses = 0

        for _, row in sim_trades.iterrows():
            action = row["action"]
            price = float(row["price"])
            portfolio_val = sim_usd + sim_btc * price

            if action == "BUY" and sim_btc == 0:
                trade_usd = portfolio_val * (sim_trade_pct / 100.0)
                trade_usd = min(trade_usd, sim_usd / (1 + sim_fee_pct))
                btc_amount = trade_usd / price
                fee = trade_usd * sim_fee_pct
                sim_usd -= (trade_usd + fee)
                sim_btc += btc_amount
                sim_entry_price = price
                sim_total_fees += fee
            elif action == "SELL" and sim_btc > 0:
                revenue = sim_btc * price
                fee = revenue * sim_fee_pct
                sim_usd += (revenue - fee)
                trade_pnl = (price - sim_entry_price) * sim_btc - fee
                if trade_pnl > 0:
                    sim_wins += 1
                elif trade_pnl < 0:
                    sim_losses += 1
                sim_btc = 0.0
                sim_total_fees += fee
                sim_entry_price = 0.0

            total_val = sim_usd + sim_btc * price
            sim_history.append({
                "timestamp": row["timestamp"],
                "action": action,
                "price": price,
                "portfolio_value": round(total_val, 2),
            })

        sim_df = pd.DataFrame(sim_history)
        final_val = sim_df["portfolio_value"].iloc[-1] if len(sim_df) > 0 else sim_capital
        sim_pnl = final_val - sim_capital
        sim_pnl_pct = (sim_pnl / sim_capital) * 100
        sim_wr = (sim_wins / max(sim_wins + sim_losses, 1)) * 100

        # Buy & hold comparison
        first_price = sim_trades["price"].iloc[0]
        last_price = sim_trades["price"].iloc[-1]
        bh_return_pct = ((last_price - first_price) / first_price) * 100
        bh_final = sim_capital * (1 + bh_return_pct / 100)
        beats_bh = sim_pnl_pct > bh_return_pct

        with sim_col3:
            pnl_c = "#00ff88" if sim_pnl >= 0 else "#ff4466"
            bh_c = "#00ff88" if bh_return_pct >= 0 else "#ff4466"
            vs_c = "#00ff88" if beats_bh else "#ff4466"
            vs_text = "BEATS BUY & HOLD" if beats_bh else "UNDERPERFORMS BUY & HOLD"
            st.markdown(f'''<div style="background:#0d1b2a;border:1px solid #1e2d3d;border-radius:8px;padding:16px;">
                <div style="display:flex;gap:24px;align-items:center;flex-wrap:wrap;">
                    <div><div style="font-size:9px;color:#4a6080;letter-spacing:0.1em;">STARTING</div>
                        <div style="font-size:18px;font-weight:700;color:#4488ff;">${sim_capital:,.0f}</div></div>
                    <div style="font-size:18px;color:#4a6080;">→</div>
                    <div><div style="font-size:9px;color:#4a6080;letter-spacing:0.1em;">FINAL VALUE</div>
                        <div style="font-size:18px;font-weight:700;color:{pnl_c};">${final_val:,.2f}</div></div>
                    <div><div style="font-size:9px;color:#4a6080;letter-spacing:0.1em;">PnL</div>
                        <div style="font-size:18px;font-weight:700;color:{pnl_c};">{"+" if sim_pnl>=0 else ""}{sim_pnl_pct:.2f}%</div></div>
                    <div><div style="font-size:9px;color:#4a6080;letter-spacing:0.1em;">BUY & HOLD</div>
                        <div style="font-size:14px;font-weight:600;color:{bh_c};">{"+" if bh_return_pct>=0 else ""}{bh_return_pct:.2f}%</div></div>
                    <div><div style="font-size:9px;color:#4a6080;letter-spacing:0.1em;">VS HODL</div>
                        <div style="font-size:12px;font-weight:700;color:{vs_c};">{vs_text}</div></div>
                    <div><div style="font-size:9px;color:#4a6080;letter-spacing:0.1em;">WIN RATE</div>
                        <div style="font-size:14px;font-weight:600;color:{"#00ff88" if sim_wr>=55 else "#ffaa00"};">{sim_wr:.0f}%</div></div>
                    <div><div style="font-size:9px;color:#4a6080;letter-spacing:0.1em;">FEES PAID</div>
                        <div style="font-size:14px;font-weight:600;color:#ff4466;">${sim_total_fees:,.2f}</div></div>
                </div></div>''', unsafe_allow_html=True)

        # Portfolio value chart
        if len(sim_df) > 1:
            fig_sim = go.Figure()
            fig_sim.add_trace(go.Scatter(
                x=sim_df["timestamp"], y=sim_df["portfolio_value"],
                mode="lines", name="Agent Strategy",
                line=dict(color="#00ff88", width=2),
                fill="tozeroy", fillcolor="rgba(0,255,136,0.04)"
            ))
            # Buy & hold line
            bh_values = [sim_capital * (1 + ((p - first_price) / first_price))
                         for p in sim_df["price"]]
            fig_sim.add_trace(go.Scatter(
                x=sim_df["timestamp"], y=bh_values,
                mode="lines", name="Buy & Hold",
                line=dict(color="#4488ff", width=1.5, dash="dot")
            ))
            fig_sim.add_hline(y=sim_capital, line_dash="dash",
                              line_color="rgba(138,164,200,0.3)",
                              annotation_text=f"Start ${sim_capital:,.0f}",
                              annotation_font=dict(color="#4a6080", size=9))
            fig_sim.update_layout(**PLOTLY_LAYOUT, height=220,
                                  legend=dict(orientation="h", y=1.1, x=0))
            st.plotly_chart(fig_sim, width="stretch", config={"displayModeBar": False})
    else:
        st.markdown('<div style="background:#0d1b2a;border:1px solid #1e2d3d;border-radius:8px;padding:24px;text-align:center;"><div style="color:#4a6080;font-size:12px;">No executed trades yet — run the agent to generate data for simulation</div></div>', unsafe_allow_html=True)

    st.markdown(f'<div style="margin-top:24px;padding-top:16px;border-top:1px solid #1e2d3d;display:flex;justify-content:space-between;align-items:center;font-size:10px;color:#4a6080;letter-spacing:0.08em;"><div>KRYNOS AI · TRADING AGENT · LABLAB.AI HACKATHON 2026</div><div>AUTO-REFRESH {refresh}s · {"DEMO DATA" if using_demo else "LIVE DATA FROM krynos.db"}</div></div>', unsafe_allow_html=True)

    # Auto-refresh
    time.sleep(refresh)
    st.rerun()

if __name__ == "__main__":
    main()
