# Krynos AI — Autonomous BTC Trading Agent

> **LabLab.ai AI Trading Agents Hackathon · Kraken Challenge**
> March 30 – April 12, 2026

Krynos is an autonomous AI trading agent that uses **Kraken CLI** to retrieve market data and execute BTC/USD trades. It employs a multi-agent debate system (bull vs. bear) judged by a quantitative signal-scoring engine built on EMA crossovers, RSI, MACD, Bollinger Bands, and real-time market sentiment feeds.

## Features

- **Kraken CLI integration** — Native wrapper around the official [Kraken CLI](https://github.com/krakenfx/kraken-cli) for market data and trade execution, with `krakenex` fallback.
- **Bull/Bear debate system** — Two LLM agents (via Groq/Llama-3.3-70B) argue opposing trade theses; an impartial judge decides.
- **Quantitative signal scoring** — EMA 25/45 crossover strategy gated by RSI, MACD histogram, and Bollinger Band position (score range ±100).
- **Real-time sentiment feeds** — Crypto Fear & Greed Index, Binance funding rate, open interest, and order book bias.
- **PRISM API signals** — Cross-asset AI signals (BTC, ETH, SOL) with market consensus tracking.
- **Risk management** — Circuit breaker (5% daily loss limit), per-trade risk cap (2%), stop loss (1.5%), take profit (1.2%).
- **Paper & live trading** — Kraken CLI paper trading sandbox for testing; switch to live with one config flag.
- **Streamlit dashboard** — Real-time trading terminal UI with charts, trade history, debate logs, and portfolio metrics.

## Architecture

```
┌───────────────────────────────────────────────┐
│              Krynos AI Agent Loop              │
│                                                │
│  Market Data ──► Signal Score ──► Bull/Bear    │
│  (Kraken CLI)    (EMA/RSI/MACD)   Debate (LLM)│
│                                                │
│  Sentiment ──► Judge ──► Execute ──► Log       │
│  (Fear&Greed,   (LLM)   (Kraken    (SQLite)   │
│   Funding,               CLI)                  │
│   Order Book)                                  │
└───────────────────────────────────────────────┘
```

## Tech Stack

| Component | Technology |
|---|---|
| Execution layer | [Kraken CLI](https://github.com/krakenfx/kraken-cli) + krakenex |
| AI reasoning | Groq API (Llama-3.3-70B-Versatile) |
| Market signals | PRISM API (Strykr) |
| Sentiment data | Alternative.me Fear & Greed, Binance Futures API |
| Dashboard | Streamlit + Plotly |
| Database | SQLite (WAL mode) |
| Language | Python 3.10+ |

## Setup

### 1. Clone & install dependencies

```bash
git clone https://github.com/<your-username>/krynos-ai.git
cd krynos-ai
pip install -r requirements.txt
```

### 2. Install Kraken CLI

**Linux / macOS:**
```bash
curl --proto '=https' --tlsv1.2 -LsSf \
  https://github.com/krakenfx/kraken-cli/releases/latest/download/kraken-cli-installer.sh | sh
```

**Windows (via WSL):**
```powershell
wsl --install          # if not already installed
# Then inside WSL:
curl --proto '=https' --tlsv1.2 -LsSf \
  https://github.com/krakenfx/kraken-cli/releases/latest/download/kraken-cli-installer.sh | sh
```

**Windows (native build):**
If WSL installation fails or you prefer native Windows support, build from source:
```powershell
winget install Rustlang.Rustup
winget install Git.Git
git clone https://github.com/krakenfx/kraken-cli.git
cd kraken-cli
cargo build --release
# Binary: target\release\kraken.exe
# Add to PATH or use full path
```

Verify: `kraken status && kraken ticker BTCUSD`

### 3. Configure API keys

Copy the example env file and fill in your keys:

```bash
cp .env.example .env
```

Edit `.env` with your values (see below for Kraken API key setup).

### 4. Kraken API Key (Read-Only for Leaderboard)

1. Log in to [Kraken](https://www.kraken.com/) and go to **Settings → API**.
2. Create a new API key with **only these permissions**:
   - ✅ Query Funds
   - ✅ Query Open Orders & Trades
   - ✅ Query Closed Orders & Trades
   - ❌ Create & Modify Orders (disable for read-only leaderboard key)
   - ❌ Cancel/Close Orders
   - ❌ Withdraw Funds
3. Copy the key and secret into your `.env`.
4. This read-only key is shared with lablab.ai for leaderboard PnL verification — no execution or withdrawal access is granted.

### 5. Run the trading agent

```bash
python debate_agent.py
```

### 6. Run the dashboard (separate terminal)

```bash
streamlit run dashboard.py
```

## Configuration

Key parameters in `debate_agent.py`:

| Parameter | Default | Description |
|---|---|---|
| `PAPER_TRADING` | `True` | Set `False` for live trading |
| `LOOP_INTERVAL` | `60` | Seconds between trading rounds |
| `MAX_DAILY_LOSS` | `0.05` | 5% daily circuit breaker |
| `MAX_RISK_PER_TRADE` | `0.02` | 2% max portfolio risk per trade |
| `MIN_CONFIDENCE` | `0.45` | Minimum judge confidence to execute |
| `STOP_LOSS_PCT` | `0.015` | 1.5% stop loss |
| `TAKE_PROFIT_PCT` | `0.012` | 1.2% take profit |

## License

MIT
