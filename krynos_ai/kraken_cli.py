"""
kraken_cli.py — Kraken CLI wrapper for Krynos AI Trading Agent
=============================================================
Wraps the official Kraken CLI (https://github.com/krakenfx/kraken-cli)
for market data retrieval and trade execution.

Auto-detects CLI availability:
  1. Direct `kraken` command (Linux/macOS)
  2. Via WSL `wsl kraken` (Windows)
  3. Falls back to krakenex Python library for market data

Usage:
    from kraken_cli import cli
    cli.init_paper(balance=10000)
    ticker = cli.ticker("BTCUSD")
    cli.paper_buy("BTCUSD", 0.001)
    cli.paper_status()
"""

import json
import os
import subprocess
import sys
import time
import krakenex
from dotenv import load_dotenv

load_dotenv()


class KrakenCLI:
    """Wrapper around the Kraken CLI binary with krakenex fallback."""

    def __init__(self):
        self._cli_path = None   # "kraken" or "wsl"
        self._cli_args = []     # [] or ["kraken"] for wsl
        self._available = None  # True / False / None (not checked yet)
        self._paper_initialized = False

        # krakenex fallback for market data
        self._kapi = krakenex.API()
        self._kapi.key = os.environ.get("KRAKEN_API_KEY", "")
        self._kapi.secret = os.environ.get("KRAKEN_API_SECRET", "")

    # ── CLI Detection ─────────────────────────────────────────────────────
    def detect(self) -> bool:
        """Detect if Kraken CLI is available. Caches result."""
        if self._available is not None:
            return self._available

        # Try direct `kraken` command
        if self._try_cli(["kraken"]):
            self._cli_path = "kraken"
            self._cli_args = []
            self._available = True
            return True

        # Try kraken.exe (Windows without WSL)
        if self._try_cli(["kraken.exe"]):
            self._cli_path = "kraken.exe"
            self._cli_args = []
            self._available = True
            return True

        # Try via WSL
        if sys.platform == "win32" and self._try_cli(["wsl", "kraken"]):
            self._cli_path = "wsl"
            self._cli_args = ["kraken"]
            self._available = True
            return True

        self._available = False
        return False

    def _try_cli(self, base_cmd: list) -> bool:
        """Test if a kraken CLI command works."""
        try:
            result = subprocess.run(
                base_cmd + ["status", "-o", "json"],
                capture_output=True, text=True, timeout=15,
                env=self._get_env()
            )
            return result.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            return False

    def _get_env(self) -> dict:
        """Build environment with Kraken API keys."""
        env = os.environ.copy()
        if os.environ.get("KRAKEN_API_KEY"):
            env["KRAKEN_API_KEY"] = os.environ["KRAKEN_API_KEY"]
        if os.environ.get("KRAKEN_API_SECRET"):
            env["KRAKEN_API_SECRET"] = os.environ["KRAKEN_API_SECRET"]
        return env

    @property
    def is_available(self) -> bool:
        if self._available is None:
            self.detect()
        return self._available

    @property
    def mode(self) -> str:
        if not self.is_available:
            return "krakenex_fallback"
        if self._cli_path == "wsl":
            return "wsl"
        return "native"

    # ── Low-level CLI Execution ───────────────────────────────────────────
    def _run(self, args: list, timeout: int = 30) -> dict:
        """
        Run a kraken CLI command and return parsed JSON.
        Returns {"ok": True, "data": ...} or {"ok": False, "error": ...}
        """
        if not self.is_available:
            return {"ok": False, "error": "Kraken CLI not available"}

        if self._cli_path == "wsl":
            cmd = ["wsl"] + self._cli_args + args + ["-o", "json"]
        else:
            cmd = [self._cli_path] + args + ["-o", "json"]

        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True,
                timeout=timeout, env=self._get_env()
            )
            stdout = result.stdout.strip()
            if result.returncode == 0 and stdout:
                try:
                    data = json.loads(stdout)
                    return {"ok": True, "data": data}
                except json.JSONDecodeError:
                    return {"ok": True, "data": {"raw": stdout}}
            else:
                # Parse error envelope
                error_msg = stdout or result.stderr.strip()
                try:
                    err = json.loads(error_msg)
                    return {"ok": False, "error": err.get("message", str(err)),
                            "category": err.get("error", "unknown")}
                except (json.JSONDecodeError, ValueError):
                    return {"ok": False, "error": error_msg or f"exit code {result.returncode}"}
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "Command timed out"}
        except FileNotFoundError:
            self._available = False
            return {"ok": False, "error": "Kraken CLI binary not found"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ── Market Data ───────────────────────────────────────────────────────
    def ticker(self, pair: str) -> dict:
        """Get ticker data. Uses CLI if available, else krakenex."""
        if self.is_available:
            result = self._run(["ticker", pair])
            if result["ok"]:
                data = result["data"]
                # CLI returns {"BTCUSD": {"a": [...], "b": [...], "c": [...]}}
                key = pair if pair in data else list(data.keys())[0] if data else None
                if key:
                    t = data[key]
                    return {
                        "ok": True,
                        "source": "kraken_cli",
                        "last": float(t["c"][0]) if isinstance(t.get("c"), list) else float(t.get("last", 0)),
                        "ask": float(t["a"][0]) if isinstance(t.get("a"), list) else float(t.get("ask", 0)),
                        "bid": float(t["b"][0]) if isinstance(t.get("b"), list) else float(t.get("bid", 0)),
                        "high_24h": float(t["h"][1]) if isinstance(t.get("h"), list) else 0,
                        "low_24h": float(t["l"][1]) if isinstance(t.get("l"), list) else 0,
                        "volume_24h": float(t["v"][1]) if isinstance(t.get("v"), list) else 0,
                        "vwap_24h": float(t["p"][1]) if isinstance(t.get("p"), list) else 0,
                        "trades_24h": t["t"][1] if isinstance(t.get("t"), list) else 0,
                        "raw": t,
                    }

        # Fallback to krakenex
        return self._ticker_krakenex(pair)

    def _ticker_krakenex(self, pair: str) -> dict:
        """Fallback ticker via krakenex library."""
        try:
            result = self._kapi.query_public("Ticker", {"pair": pair})
            if "error" in result and result["error"]:
                return {"ok": False, "source": "krakenex", "error": str(result["error"])}
            data = result["result"][list(result["result"].keys())[0]]
            return {
                "ok": True,
                "source": "krakenex",
                "last": float(data["c"][0]),
                "ask": float(data["a"][0]),
                "bid": float(data["b"][0]),
                "high_24h": float(data["h"][1]),
                "low_24h": float(data["l"][1]),
                "volume_24h": float(data["v"][1]),
                "vwap_24h": float(data["p"][1]),
                "trades_24h": data["t"][1],
                "raw": data,
            }
        except Exception as e:
            return {"ok": False, "source": "krakenex", "error": str(e)}

    def ohlc(self, pair: str, interval: int = 60) -> dict:
        """Get OHLC candles. Uses CLI if available, else krakenex."""
        if self.is_available:
            result = self._run(["ohlc", pair, "--interval", str(interval)], timeout=15)
            if result["ok"]:
                data = result["data"]
                key = list(data.keys())[0] if data else None
                if key and key != "last":
                    return {"ok": True, "source": "kraken_cli", "candles": data[key]}

        # Fallback to krakenex
        try:
            result = self._kapi.query_public("OHLC", {"pair": pair, "interval": interval})
            key = [k for k in result["result"].keys() if k != "last"][0]
            return {"ok": True, "source": "krakenex", "candles": result["result"][key]}
        except Exception as e:
            return {"ok": False, "source": "krakenex", "error": str(e)}

    def orderbook(self, pair: str, count: int = 10) -> dict:
        """Get order book."""
        if self.is_available:
            result = self._run(["orderbook", pair, "--count", str(count)])
            if result["ok"]:
                return {"ok": True, "source": "kraken_cli", **result["data"]}

        try:
            result = self._kapi.query_public("Depth", {"pair": pair, "count": count})
            data = result["result"][list(result["result"].keys())[0]]
            return {"ok": True, "source": "krakenex", **data}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ── Account Data ──────────────────────────────────────────────────────
    def balance(self) -> dict:
        """Get account balance."""
        if self.is_available:
            result = self._run(["balance"])
            if result["ok"]:
                return {"ok": True, "source": "kraken_cli", "balances": result["data"]}

        try:
            result = self._kapi.query_private("Balance")
            if "error" in result and result["error"]:
                return {"ok": False, "error": str(result["error"])}
            return {"ok": True, "source": "krakenex", "balances": result["result"]}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def open_orders(self) -> dict:
        """Get open orders."""
        if self.is_available:
            result = self._run(["open-orders"])
            if result["ok"]:
                return {"ok": True, "source": "kraken_cli", **result["data"]}

        try:
            result = self._kapi.query_private("OpenOrders")
            return {"ok": True, "source": "krakenex", **result.get("result", {})}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def trades_history(self) -> dict:
        """Get trade history."""
        if self.is_available:
            result = self._run(["trades-history"])
            if result["ok"]:
                return {"ok": True, "source": "kraken_cli", **result["data"]}

        try:
            result = self._kapi.query_private("TradesHistory")
            return {"ok": True, "source": "krakenex", **result.get("result", {})}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ── Paper Trading ─────────────────────────────────────────────────────
    def init_paper(self, balance: int = 10000, currency: str = "USD") -> dict:
        """Initialize Kraken CLI paper trading account."""
        if not self.is_available:
            return {"ok": False, "error": "CLI not available — using internal paper trading"}
        result = self._run(["paper", "init", "--balance", str(balance), "--currency", currency])
        if result["ok"]:
            self._paper_initialized = True
        return result

    def paper_buy(self, pair: str, volume: float, order_type: str = "market",
                  price: float = None) -> dict:
        """Execute a paper BUY via Kraken CLI."""
        if not self.is_available:
            return {"ok": False, "error": "CLI not available"}
        args = ["paper", "buy", pair, f"{volume:.8f}"]
        if order_type == "limit" and price:
            args += ["--type", "limit", "--price", str(price)]
        return self._run(args)

    def paper_sell(self, pair: str, volume: float, order_type: str = "market",
                   price: float = None) -> dict:
        """Execute a paper SELL via Kraken CLI."""
        if not self.is_available:
            return {"ok": False, "error": "CLI not available"}
        args = ["paper", "sell", pair, f"{volume:.8f}"]
        if order_type == "limit" and price:
            args += ["--type", "limit", "--price", str(price)]
        return self._run(args)

    def paper_status(self) -> dict:
        """Get paper trading account status (value, PnL, trade count)."""
        if not self.is_available:
            return {"ok": False, "error": "CLI not available"}
        return self._run(["paper", "status"])

    def paper_balance(self) -> dict:
        """Get paper trading balances."""
        if not self.is_available:
            return {"ok": False, "error": "CLI not available"}
        return self._run(["paper", "balance"])

    def paper_history(self) -> dict:
        """Get paper trading filled trade history."""
        if not self.is_available:
            return {"ok": False, "error": "CLI not available"}
        return self._run(["paper", "history"])

    def paper_orders(self) -> dict:
        """Get paper trading open orders."""
        if not self.is_available:
            return {"ok": False, "error": "CLI not available"}
        return self._run(["paper", "orders"])

    def paper_reset(self, balance: int = 10000) -> dict:
        """Reset paper trading account."""
        if not self.is_available:
            return {"ok": False, "error": "CLI not available"}
        result = self._run(["paper", "reset", "--balance", str(balance)])
        if result["ok"]:
            self._paper_initialized = True
        return result

    # ── Live Trading ──────────────────────────────────────────────────────
    def order_buy(self, pair: str, volume: float, order_type: str = "market",
                  price: float = None) -> dict:
        """Place a real BUY order via Kraken CLI."""
        if not self.is_available:
            return {"ok": False, "error": "CLI not available — cannot place live orders"}
        args = ["order", "buy", pair, f"{volume:.8f}"]
        if order_type == "limit" and price:
            args += ["--type", "limit", "--price", str(price)]
        else:
            args += ["--type", "market"]
        return self._run(args)

    def order_sell(self, pair: str, volume: float, order_type: str = "market",
                   price: float = None) -> dict:
        """Place a real SELL order via Kraken CLI."""
        if not self.is_available:
            return {"ok": False, "error": "CLI not available — cannot place live orders"}
        args = ["order", "sell", pair, f"{volume:.8f}"]
        if order_type == "limit" and price:
            args += ["--type", "limit", "--price", str(price)]
        else:
            args += ["--type", "market"]
        return self._run(args)

    # ── Unified Trade Execution ───────────────────────────────────────────
    def execute_trade(self, action: str, pair: str, volume: float,
                      paper: bool = True) -> dict:
        """
        Execute a trade through Kraken CLI.
        Routes to paper or live based on `paper` flag.
        Falls back to internal tracking if CLI unavailable.
        """
        if action == "HOLD":
            return {"ok": True, "status": "skipped", "reason": "HOLD decision"}

        if not self.is_available:
            # Fallback: internal paper trade (no CLI)
            print(f"  [FALLBACK] Kraken CLI not available — using internal paper trading")
            return {
                "ok": True,
                "status": "paper_internal",
                "action": action,
                "amount": volume,
                "pair": pair,
                "source": "internal",
            }

        if paper:
            # Use Kraken CLI paper trading
            if not self._paper_initialized:
                init_result = self.init_paper()
                if not init_result["ok"]:
                    print(f"  [PAPER] Init failed: {init_result.get('error')}")

            if action == "BUY":
                result = self.paper_buy(pair, volume)
            else:
                result = self.paper_sell(pair, volume)

            if result["ok"]:
                return {
                    "ok": True,
                    "status": "paper_cli",
                    "action": action,
                    "amount": volume,
                    "pair": pair,
                    "source": "kraken_cli",
                    "cli_response": result["data"],
                }
            else:
                print(f"  [CLI PAPER] Error: {result.get('error')} — falling back to internal")
                return {
                    "ok": True,
                    "status": "paper_internal",
                    "action": action,
                    "amount": volume,
                    "pair": pair,
                    "source": "internal_fallback",
                    "cli_error": result.get("error"),
                }
        else:
            # Live trading via Kraken CLI
            if action == "BUY":
                result = self.order_buy(pair, volume)
            else:
                result = self.order_sell(pair, volume)

            if result["ok"]:
                return {
                    "ok": True,
                    "status": "executed",
                    "action": action,
                    "amount": volume,
                    "pair": pair,
                    "source": "kraken_cli",
                    "cli_response": result["data"],
                }
            else:
                return {
                    "ok": False,
                    "status": "error",
                    "error": result.get("error"),
                    "category": result.get("category"),
                }

    # ── System ────────────────────────────────────────────────────────────
    def status(self) -> dict:
        """Get Kraken system status."""
        if self.is_available:
            return self._run(["status"])
        try:
            result = self._kapi.query_public("SystemStatus")
            return {"ok": True, "source": "krakenex", "data": result.get("result", {})}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def info(self) -> dict:
        """Return connection info summary."""
        return {
            "cli_available": self.is_available,
            "mode": self.mode,
            "paper_initialized": self._paper_initialized,
            "has_api_key": bool(os.environ.get("KRAKEN_API_KEY")),
        }


# ── Module-level singleton ────────────────────────────────────────────────────
cli = KrakenCLI()


def setup_instructions() -> str:
    """Return setup instructions for the user."""
    if cli.is_available:
        return "Kraken CLI is ready!"

    if sys.platform == "win32":
        return """
╔══════════════════════════════════════════════════════════════╗
║  KRAKEN CLI SETUP (Windows)                                  ║
╠══════════════════════════════════════════════════════════════╣
║                                                              ║
║  1. Install WSL:                                             ║
║     > wsl --install                                          ║
║     (restart if prompted)                                    ║
║                                                              ║
║  2. Open Ubuntu/WSL terminal and install Kraken CLI:         ║
║     $ curl --proto '=https' --tlsv1.2 -LsSf \\               ║
║       https://github.com/krakenfx/kraken-cli/releases/       ║
║       latest/download/kraken-cli-installer.sh | sh           ║
║                                                              ║
║  3. Verify:                                                  ║
║     $ kraken status && kraken ticker BTCUSD                  ║
║                                                              ║
║  4. Set API keys in WSL:                                     ║
║     $ export KRAKEN_API_KEY="your-key"                       ║
║     $ export KRAKEN_API_SECRET="your-secret"                 ║
║                                                              ║
║  The agent will auto-detect CLI via `wsl kraken`.            ║
╚══════════════════════════════════════════════════════════════╝
"""
    else:
        return """
╔══════════════════════════════════════════════════════════════╗
║  KRAKEN CLI SETUP (Linux/macOS)                              ║
╠══════════════════════════════════════════════════════════════╣
║                                                              ║
║  1. Install:                                                 ║
║     $ curl --proto '=https' --tlsv1.2 -LsSf \\               ║
║       https://github.com/krakenfx/kraken-cli/releases/       ║
║       latest/download/kraken-cli-installer.sh | sh           ║
║                                                              ║
║  2. Verify:                                                  ║
║     $ kraken status && kraken ticker BTCUSD                  ║
║                                                              ║
║  3. Set API keys:                                            ║
║     $ export KRAKEN_API_KEY="your-key"                       ║
║     $ export KRAKEN_API_SECRET="your-secret"                 ║
╚══════════════════════════════════════════════════════════════╝
"""
